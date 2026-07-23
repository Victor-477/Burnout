# ============================================================
#  Cryo Compiler — WebAssembly backend (Phase 9.6, "full-stack" client)
#
#  Compiles a Cryo program to a **WebAssembly binary module** (.wasm) — the
#  client/front-end target that runs in the browser (or any WASM runtime such
#  as Node's WebAssembly). Emits the binary format directly (no wat2wasm).
#
#  Minimal numeric subset (proof of concept): user functions, `int`/`bool`
#  (lowered to i64), arithmetic `+ - * / %`, comparisons, `&&`/`||` (bitwise
#  on 0/1), unary `-`, `if/else`, `while`, calls/recursion, `return`, and
#  `print(x)` (imported host function `env.log`). Top-level statements become
#  an exported `main`. Strings/arrays/maps/structs/enums/floats are rejected
#  with a clear message (future iterations add a linear-memory runtime + DOM).
# ============================================================
from ast_nodes import (
    Program, FunctionDecl, VarDecl, Assignment, CompoundAssignment, Increment,
    Return, If, While, For, ForEach, Break, Continue,
    BinaryExpr, UnaryExpr, CallExpr, Identifier, Literal,
)


class CodeGenWasmError(Exception):
    pass


# ── value types / opcodes ────────────────────────────────────
I32, I64 = 0x7F, 0x7E
_ARITH = {'+': 0x7C, '-': 0x7D, '*': 0x7E, '/': 0x7F, '%': 0x81,
          '&': 0x83, '|': 0x84, '^': 0x85, '<<': 0x86, '>>': 0x87}
_CMP = {'==': 0x51, '!=': 0x52, '<': 0x53, '>': 0x55, '<=': 0x57, '>=': 0x59}


def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if (n == 0 and not (b & 0x40)) or (n == -1 and (b & 0x40)):
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


def _vec(items) -> bytes:
    return _uleb(len(items)) + b"".join(items)


def _name(s: str) -> bytes:
    b = s.encode("utf-8")
    return _uleb(len(b)) + b


def _section(sid: int, content: bytes) -> bytes:
    return bytes([sid]) + _uleb(len(content)) + content


class CodeGenWasm:
    def __init__(self, safe: bool = True):
        self.safe = safe

    # ── public entry ─────────────────────────────────────────
    def generate(self, program: Program) -> bytes:
        user = [s for s in program.statements if isinstance(s, FunctionDecl)]
        top = [s for s in program.statements
               if not isinstance(s, (FunctionDecl,))]
        for s in top:
            if not isinstance(s, (VarDecl, Assignment, CompoundAssignment,
                                  Increment, Return, If, While, For, CallExpr)):
                raise CodeGenWasmError(
                    f"'{type(s).__name__}' not supported by the wasm backend "
                    f"(numeric subset only; use --backend go/node for the rest)")

        # synthetic 'main' from the top-level statements (void, no params)
        main = FunctionDecl("main", [], "void", top)
        fns = user + [main]

        # function index map (import log = 0; defined funcs start at 1)
        self._fnidx = {"__log": 0}
        for i, f in enumerate(fns):
            self._fnidx[f.name] = 1 + i

        # ── sections ──
        # type 0 = log:(i64)->() ; then one type per function
        types = [self._functype([I64], [])]
        ftypeidx = []
        for f in fns:
            res = [] if (f.return_type in (None, "void")) else [I64]
            types.append(self._functype([I64] * len(f.params), res))
            ftypeidx.append(len(types) - 1)

        type_sec = _section(1, _vec(types))
        import_sec = _section(2, _vec([
            _name("env") + _name("log") + bytes([0x00]) + _uleb(0)  # func, type 0
        ]))
        func_sec = _section(3, _vec([_uleb(t) for t in ftypeidx]))
        # export every function by name (so a host can call any of them)
        export_sec = _section(7, _vec([
            _name(f.name) + bytes([0x00]) + _uleb(self._fnidx[f.name]) for f in fns
        ]))
        code_sec = _section(10, _vec([self._function_body(f) for f in fns]))

        return (b"\x00asm\x01\x00\x00\x00"
                + type_sec + import_sec + func_sec + export_sec + code_sec)

    # ── helpers ──────────────────────────────────────────────
    @staticmethod
    def _functype(params, results) -> bytes:
        return bytes([0x60]) + _vec([bytes([p]) for p in params]) + _vec([bytes([r]) for r in results])

    def _collect_locals(self, f: FunctionDecl):
        names = [pn for (_pt, pn) in f.params]

        def walk(stmts):
            for s in stmts:
                if isinstance(s, VarDecl) and s.name not in names:
                    names.append(s.name)
                elif isinstance(s, If):
                    walk(s.then_body)
                    if s.else_body:
                        walk(s.else_body)
                elif isinstance(s, While):
                    walk(s.body)
                elif isinstance(s, For):
                    if isinstance(s.init, VarDecl) and s.init.name not in names:
                        names.append(s.init.name)
                    walk(s.body)
        walk(f.body)
        return names

    def _function_body(self, f: FunctionDecl) -> bytes:
        self._locals = self._collect_locals(f)
        self._returns = f.return_type not in (None, "void")
        self._blocks = []        # stack of enclosing block/loop/if constructs
        self._loops = []         # stack of (break_target_pos, continue_target_pos)
        self.body = bytearray()
        for s in f.body:
            self._stmt(s)
        if self._returns:
            self.body += bytes([0x42]) + _sleb(0)   # default i64 0 (fall-through guard)
        self.body += bytes([0x0B])                   # end

        nextra = len(self._locals) - len(f.params)
        locals_vec = _vec([_uleb(nextra) + bytes([I64])]) if nextra > 0 else _vec([])
        code = locals_vec + bytes(self.body)
        return _uleb(len(code)) + code

    def _slot(self, name: str) -> int:
        if name not in self._locals:
            raise CodeGenWasmError(f"variable '{name}' not declared (wasm backend)")
        return self._locals.index(name)

    # ── statements ───────────────────────────────────────────
    def _stmt(self, n):
        if isinstance(n, VarDecl):
            if n.var_type not in ("int", "bool"):
                raise CodeGenWasmError(
                    f"type '{n.var_type}' not supported by the wasm backend (int/bool only)")
            if n.value is not None:
                self._expr(n.value)
            else:
                self.body += bytes([0x42]) + _sleb(0)
            self.body += bytes([0x21]) + _uleb(self._slot(n.name))   # local.set
        elif isinstance(n, Assignment):
            self._expr(n.value)
            self.body += bytes([0x21]) + _uleb(self._slot(n.name))
        elif isinstance(n, CompoundAssignment):
            self.body += bytes([0x20]) + _uleb(self._slot(n.name))   # local.get
            self._expr(n.value)
            self.body += bytes([_ARITH[n.op[:-1]]])
            self.body += bytes([0x21]) + _uleb(self._slot(n.name))
        elif isinstance(n, Increment):
            self.body += bytes([0x20]) + _uleb(self._slot(n.name))
            self.body += bytes([0x42]) + _sleb(1)
            self.body += bytes([0x7C if n.op == '++' else 0x7D])     # add/sub
            self.body += bytes([0x21]) + _uleb(self._slot(n.name))
        elif isinstance(n, Return):
            if n.value is not None:
                self._expr(n.value)
            elif self._returns:
                self.body += bytes([0x42]) + _sleb(0)
            self.body += bytes([0x0F])                               # return
        elif isinstance(n, If):
            self._cond(n.condition)
            self.body += bytes([0x04, 0x40])                         # if (void)
            self._blocks.append('if')
            for s in n.then_body:
                self._stmt(s)
            if n.else_body:
                self.body += bytes([0x05])                           # else
                for s in n.else_body:
                    self._stmt(s)
            self.body += bytes([0x0B])                               # end
            self._blocks.pop()
        elif isinstance(n, While):
            # block { loop { <cond> i32.eqz br_if(block) ; <body> ; br(loop) } }
            self.body += bytes([0x02, 0x40]); self._blocks.append('block'); bpos = len(self._blocks) - 1
            self.body += bytes([0x03, 0x40]); self._blocks.append('loop'); lpos = len(self._blocks) - 1
            self._loops.append((bpos, lpos))            # break->block(exit), continue->loop(re-test)
            self._cond(n.condition)
            self.body += bytes([0x45])                  # i32.eqz
            self.body += bytes([0x0D]) + _uleb(len(self._blocks) - 1 - bpos)
            for s in n.body:
                self._stmt(s)
            self.body += bytes([0x0C]) + _uleb(len(self._blocks) - 1 - lpos)
            self._loops.pop()
            self.body += bytes([0x0B]); self._blocks.pop()          # end loop
            self.body += bytes([0x0B]); self._blocks.pop()          # end block
        elif isinstance(n, For):
            # init ; block { loop { cond? br_if(block); block{ body } update ; br(loop) } }
            if n.init is not None:
                self._stmt(n.init)
            self.body += bytes([0x02, 0x40]); self._blocks.append('block'); bpos = len(self._blocks) - 1
            self.body += bytes([0x03, 0x40]); self._blocks.append('loop'); lpos = len(self._blocks) - 1
            if n.condition is not None:
                self._cond(n.condition)
                self.body += bytes([0x45])
                self.body += bytes([0x0D]) + _uleb(len(self._blocks) - 1 - bpos)
            self.body += bytes([0x02, 0x40]); self._blocks.append('cblock'); cpos = len(self._blocks) - 1
            self._loops.append((bpos, cpos))            # continue -> inner block end (runs update next)
            for s in n.body:
                self._stmt(s)
            self._loops.pop()
            self.body += bytes([0x0B]); self._blocks.pop()          # end inner block (continue target)
            if n.update is not None:
                self._stmt(n.update)
            self.body += bytes([0x0C]) + _uleb(len(self._blocks) - 1 - lpos)
            self.body += bytes([0x0B]); self._blocks.pop()          # end loop
            self.body += bytes([0x0B]); self._blocks.pop()          # end block
        elif isinstance(n, ForEach):
            raise CodeGenWasmError("for-each (arrays) not supported by the wasm backend (numeric subset)")
        elif isinstance(n, Break):
            if not self._loops:
                raise CodeGenWasmError("'break' outside a loop (wasm backend)")
            bpos, _cpos = self._loops[-1]
            self.body += bytes([0x0C]) + _uleb(len(self._blocks) - 1 - bpos)
        elif isinstance(n, Continue):
            if not self._loops:
                raise CodeGenWasmError("'continue' outside a loop (wasm backend)")
            _bpos, cpos = self._loops[-1]
            self.body += bytes([0x0C]) + _uleb(len(self._blocks) - 1 - cpos)
        elif isinstance(n, CallExpr):
            self._call(n)
            if n.callee != "print":
                self.body += bytes([0x1A])                           # drop non-void result
        else:
            raise CodeGenWasmError(f"'{type(n).__name__}' not supported by the wasm backend")

    # ── expressions (leave one i64 on the stack) ─────────────
    def _expr(self, n):
        if isinstance(n, Literal):
            if n.kind == "int":
                self.body += bytes([0x42]) + _sleb(int(n.value))
            elif n.kind == "bool":
                self.body += bytes([0x42]) + _sleb(1 if n.value else 0)
            else:
                raise CodeGenWasmError(
                    f"literal '{n.kind}' not supported by the wasm backend (int/bool only)")
        elif isinstance(n, Identifier):
            self.body += bytes([0x20]) + _uleb(self._slot(n.name))
        elif isinstance(n, UnaryExpr):
            if n.op != '-':
                raise CodeGenWasmError(f"unary '{n.op}' not supported by the wasm backend")
            self.body += bytes([0x42]) + _sleb(0)
            self._expr(n.operand)
            self.body += bytes([0x7D])                               # i64.sub -> negate
        elif isinstance(n, BinaryExpr):
            self._expr(n.left)
            self._expr(n.right)
            if n.op in _ARITH:
                self.body += bytes([_ARITH[n.op]])
            elif n.op in _CMP:
                self.body += bytes([_CMP[n.op]])                     # -> i32
                self.body += bytes([0xAD])                           # i64.extend_i32_u
            elif n.op == '&&':
                self.body += bytes([0x83])                           # i64.and (operands 0/1)
            elif n.op == '||':
                self.body += bytes([0x84])                           # i64.or
            else:
                raise CodeGenWasmError(f"operator '{n.op}' not supported by the wasm backend")
        elif isinstance(n, CallExpr):
            self._call(n)
        else:
            raise CodeGenWasmError(f"'{type(n).__name__}' not supported by the wasm backend")

    def _cond(self, n):
        """Emit condition -> i32 (nonzero = true)."""
        self._expr(n)
        self.body += bytes([0x42]) + _sleb(0)
        self.body += bytes([0x52])                                   # i64.ne -> i32

    def _call(self, n: CallExpr):
        if n.callee == "print":
            if len(n.args) != 1:
                raise CodeGenWasmError("print() takes 1 argument in the wasm backend")
            self._expr(n.args[0])
            self.body += bytes([0x10]) + _uleb(0)                    # call env.log
            return
        if n.callee not in self._fnidx:
            raise CodeGenWasmError(
                f"function '{n.callee}' unknown in the wasm backend "
                f"(numeric subset: user functions + print)")
        for a in n.args:
            self._expr(a)
        self.body += bytes([0x10]) + _uleb(self._fnidx[n.callee])
