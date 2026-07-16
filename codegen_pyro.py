# ============================================================
#  Burnout — Gerador de bytecode Pyro  (.cryo -> .pyro)
#
#  O .pyro é a LINGUAGEM-ALVO PRÓPRIA do sistema: um bytecode
#  binário com um conjunto de instruções (ISA) inventado aqui —
#  não é x86, nem Go, nem C. É executado pela VM Pyro (em Go).
#
#  Formato do arquivo .pyro (little-endian):
#    magic    4  "PYRO"
#    version  1  0x01
#    flags    1  bit0 = code section codificada (XOR rolling)
#    nconsts  u16   depois: [ tag(1) + payload ] * nconsts
#       tag 1 int64  (8)   | tag 2 float64 (8)
#       tag 3 string (u16 len + bytes utf8) | tag 4 bool (1)
#    nfuncs   u16   depois: [ nameidx u16, entry u32, nparams u8, nlocals u16 ] * nfuncs
#    entryfn  u16   (índice da função 'main')
#    codelen  u32   depois: code bytes (possivelmente codificados)
# ============================================================
import struct
from ast_nodes import *
from typing import Dict, List, Optional


class CodeGenPyroError(Exception):
    pass


# ── Opcodes da ISA Pyro ─────────────────────────────────────
OP_HALT  = 0x00
OP_CONST = 0x01   # u16 idx  -> empilha consts[idx]
OP_TRUE  = 0x02
OP_FALSE = 0x03
OP_NULL  = 0x04
OP_POP   = 0x05
OP_LOAD  = 0x06   # u16 slot -> empilha local[slot]
OP_STORE = 0x07   # u16 slot -> local[slot] = pop (mantém no topo? não: consome)
OP_ADD   = 0x10
OP_SUB   = 0x11
OP_MUL   = 0x12
OP_DIV   = 0x13
OP_MOD   = 0x14
OP_NEG   = 0x15
OP_BAND  = 0x16
OP_BOR   = 0x17
OP_BXOR  = 0x18
OP_SHL   = 0x19
OP_SHR   = 0x1A
OP_BNOT  = 0x1B
OP_EQ    = 0x20
OP_NE    = 0x21
OP_LT    = 0x22
OP_GT    = 0x23
OP_LE    = 0x24
OP_GE    = 0x25
OP_NOT   = 0x26
OP_JMP   = 0x30   # i16 rel
OP_JMPF  = 0x31   # i16 rel (pop; salta se falso)
OP_JMPT  = 0x32   # i16 rel (pop; salta se verdadeiro)
OP_CALL  = 0x40   # u16 funcidx, u8 argc
OP_RET   = 0x41
OP_PRINT = 0x50   # pop e imprime (com \n) conforme o tipo em runtime
OP_PRINTLN = 0x52 # imprime \n
OP_ASSERT = 0x51  # pop msg(const via topo?) ... usamos: pop cond; se falso aborta

# tamanho do operando por opcode (bytes após o opcode)
_OPERAND = {
    OP_CONST: 2, OP_LOAD: 2, OP_STORE: 2,
    OP_JMP: 2, OP_JMPF: 2, OP_JMPT: 2,
    OP_CALL: 3,
}

def _isize(op: int) -> int:
    return 1 + _OPERAND.get(op, 0)

# tags de constante
TAG_INT = 1
TAG_FLT = 2
TAG_STR = 3
TAG_BOOL = 4

_MAGIC = b"PYRO"
_VERSION = 1


class _Label:
    __slots__ = ('id',)
    def __init__(self, i): self.id = i


class _Func:
    def __init__(self, name, nparams):
        self.name = name
        self.nparams = nparams
        self.code: List = []          # [(op, arg)] ; op == -1 => LABEL(arg)
        self.locals: Dict[str, int] = {}
        self.entry = 0
        self.index = 0


class CodeGenPyro:
    def __init__(self, safe: bool = True, encode: bool = True):
        self.safe = safe
        self.encode = encode
        self._consts: List = []            # [(tag, value)]
        self._const_idx: Dict = {}
        self._funcs: List[_Func] = []
        self._fnindex: Dict[str, int] = {}
        self._cur: Optional[_Func] = None
        self._nlabels = 0
        self._loop_stack: List = []        # (break_label, continue_label)

    # ── constantes ──────────────────────────────────────────

    def _const(self, tag: int, value) -> int:
        key = (tag, value)
        if key in self._const_idx:
            return self._const_idx[key]
        idx = len(self._consts)
        self._consts.append((tag, value))
        self._const_idx[key] = idx
        return idx

    # ── emissao ─────────────────────────────────────────────

    def _emit(self, op: int, arg=None):
        self._cur.code.append((op, arg))

    def _label(self) -> _Label:
        self._nlabels += 1
        return _Label(self._nlabels)

    def _place(self, lbl: _Label):
        self._cur.code.append((-1, lbl))

    def _slot(self, name: str) -> int:
        f = self._cur
        if name not in f.locals:
            f.locals[name] = len(f.locals)
        return f.locals[name]

    def _get_slot(self, name: str) -> int:
        if name not in self._cur.locals:
            raise CodeGenPyroError(f"variável '{name}' não declarada")
        return self._cur.locals[name]

    # ── entrada principal ───────────────────────────────────

    def generate(self, program: Program) -> bytes:
        # coleta funções (índices) — inclui 'main' sintético
        top = []
        user_fns = []
        for n in program.statements:
            if isinstance(n, FunctionDecl):
                user_fns.append(n)
            elif isinstance(n, (Import, Library)):
                pass
            elif isinstance(n, (StructDecl, EnumDecl, SkillDecl, ForeignBlock)):
                raise CodeGenPyroError(
                    f"'{type(n).__name__}' ainda não é suportado no backend pyro "
                    f"(bytecode v1: int/number/bool/string, funções e controle de fluxo).")
            else:
                top.append(n)

        names = [f.name for f in user_fns] + ['main']
        for i, nm in enumerate(names):
            self._fnindex[nm] = i

        for fn in user_fns:
            self._compile_fn(fn.name, fn.params, fn.body)
        self._compile_fn('main', [], top, synthetic=True)

        return self._assemble()

    def _compile_fn(self, name, params, body, synthetic=False):
        f = _Func(name, len(params))
        f.index = self._fnindex[name]
        self._cur = f
        self._loop_stack = []
        for _pt, pn in params:
            self._slot(pn)
        for s in body:
            self._stmt(s)
        # retorno implícito (0 para main; null caso contrário)
        self._emit(OP_NULL)
        self._emit(OP_RET)
        self._funcs.append(f)
        self._cur = None

    # ── statements ──────────────────────────────────────────

    def _stmt(self, n: Node):
        if   isinstance(n, VarDecl):            self._var(n)
        elif isinstance(n, Assignment):         self._assign(n)
        elif isinstance(n, CompoundAssignment): self._compound(n)
        elif isinstance(n, Increment):          self._incr(n)
        elif isinstance(n, Return):             self._return(n)
        elif isinstance(n, If):                 self._if(n)
        elif isinstance(n, While):              self._while(n)
        elif isinstance(n, DoWhile):            self._do_while(n)
        elif isinstance(n, For):                self._for(n)
        elif isinstance(n, Switch):             self._switch(n)
        elif isinstance(n, Break):              self._break()
        elif isinstance(n, Continue):           self._continue()
        elif isinstance(n, Assert):             self._assert(n)
        elif isinstance(n, SafetyBlock):
            for s in n.body: self._stmt(s)
        elif isinstance(n, (CallExpr, MethodCallExpr)):
            self._expr(n); self._emit(OP_POP)   # descarta resultado
        else:
            raise CodeGenPyroError(
                f"'{type(n).__name__}' não suportado no backend pyro (use --backend go).")

    def _var(self, n: VarDecl):
        slot = self._slot(n.name)
        if n.value is not None:
            self._expr(n.value)
        else:
            self._emit(OP_NULL)
        self._emit(OP_STORE, slot)

    def _assign(self, n: Assignment):
        self._expr(n.value)
        self._emit(OP_STORE, self._get_slot(n.name))

    def _compound(self, n: CompoundAssignment):
        base = n.op[:-1]                       # '+=' -> '+'
        self._expr(BinaryExpr(base, Identifier(n.name), n.value))
        self._emit(OP_STORE, self._get_slot(n.name))

    def _incr(self, n: Increment):
        op = '+' if n.op == '++' else '-'
        self._expr(BinaryExpr(op, Identifier(n.name), Literal('int', 1)))
        self._emit(OP_STORE, self._get_slot(n.name))

    def _return(self, n: Return):
        if n.value is not None:
            self._expr(n.value)
        else:
            self._emit(OP_NULL)
        self._emit(OP_RET)

    def _if(self, n: If):
        l_else = self._label()
        l_end = self._label()
        self._expr(n.condition)
        self._emit(OP_JMPF, l_else)
        for s in n.then_body: self._stmt(s)
        self._emit(OP_JMP, l_end)
        self._place(l_else)
        if n.else_body:
            for s in n.else_body: self._stmt(s)
        self._place(l_end)

    def _while(self, n: While):
        l_cond = self._label()
        l_end = self._label()
        self._place(l_cond)
        self._expr(n.condition)
        self._emit(OP_JMPF, l_end)
        self._loop_stack.append((l_end, l_cond))
        for s in n.body: self._stmt(s)
        self._loop_stack.pop()
        self._emit(OP_JMP, l_cond)
        self._place(l_end)

    def _do_while(self, n: DoWhile):
        l_top = self._label()
        l_cond = self._label()
        l_end = self._label()
        self._place(l_top)
        self._loop_stack.append((l_end, l_cond))
        for s in n.body: self._stmt(s)
        self._loop_stack.pop()
        self._place(l_cond)
        self._expr(n.condition)
        self._emit(OP_JMPT, l_top)
        self._place(l_end)

    def _for(self, n: For):
        l_cond = self._label()
        l_cont = self._label()
        l_end = self._label()
        if n.init: self._stmt(n.init)
        self._place(l_cond)
        if n.condition:
            self._expr(n.condition)
            self._emit(OP_JMPF, l_end)
        self._loop_stack.append((l_end, l_cont))
        for s in n.body: self._stmt(s)
        self._loop_stack.pop()
        self._place(l_cont)
        if n.update: self._stmt(n.update)
        self._emit(OP_JMP, l_cond)
        self._place(l_end)

    def _switch(self, n: Switch):
        l_end = self._label()
        self._loop_stack.append((l_end, l_end))
        subj_slot = self._slot(f"__sw{self._nlabels}")
        self._expr(n.subject)
        self._emit(OP_STORE, subj_slot)
        case_labels = [self._label() for _ in n.cases]
        l_default = self._label()
        for lbl, case in zip(case_labels, n.cases):
            for v in case.values:
                self._emit(OP_LOAD, subj_slot)
                self._expr(v)
                self._emit(OP_EQ)
                self._emit(OP_JMPT, lbl)
        self._emit(OP_JMP, l_default)
        for lbl, case in zip(case_labels, n.cases):
            self._place(lbl)
            for s in case.body: self._stmt(s)
            self._emit(OP_JMP, l_end)
        self._place(l_default)
        if n.default_body:
            for s in n.default_body: self._stmt(s)
        self._place(l_end)
        self._loop_stack.pop()

    def _break(self):
        if not self._loop_stack:
            raise CodeGenPyroError("'break' fora de laço/switch")
        self._emit(OP_JMP, self._loop_stack[-1][0])

    def _continue(self):
        if not self._loop_stack:
            raise CodeGenPyroError("'continue' fora de laço")
        self._emit(OP_JMP, self._loop_stack[-1][1])

    def _assert(self, n: Assert):
        if n.message is not None and isinstance(n.message, Literal) \
                and n.message.kind == 'string':
            msg = n.message.value
        else:
            msg = f"assert falhou (linha {n.line})"
        self._emit(OP_CONST, self._const(TAG_STR, msg))   # msg no topo
        self._expr(n.condition)                            # cond acima da msg
        self._emit(OP_ASSERT)

    # ── expressoes ──────────────────────────────────────────

    def _expr(self, n: Node):
        if isinstance(n, Literal):
            self._literal(n); return
        if isinstance(n, Identifier):
            self._emit(OP_LOAD, self._get_slot(n.name)); return
        if isinstance(n, UnaryExpr):
            self._expr(n.operand)
            if   n.op == '-': self._emit(OP_NEG)
            elif n.op == '!': self._emit(OP_NOT)
            elif n.op == '~': self._emit(OP_BNOT)
            else: raise CodeGenPyroError(f"unário '{n.op}' não suportado")
            return
        if isinstance(n, BinaryExpr):
            self._binary(n); return
        if isinstance(n, TernaryExpr):
            l_else = self._label(); l_end = self._label()
            self._expr(n.condition); self._emit(OP_JMPF, l_else)
            self._expr(n.then_value); self._emit(OP_JMP, l_end)
            self._place(l_else); self._expr(n.else_value)
            self._place(l_end)
            return
        if isinstance(n, CallExpr):
            self._call(n); return
        raise CodeGenPyroError(
            f"expressão '{type(n).__name__}' não suportada no backend pyro.")

    def _literal(self, n: Literal):
        if n.kind == 'int':    self._emit(OP_CONST, self._const(TAG_INT, int(n.value)))
        elif n.kind == 'float': self._emit(OP_CONST, self._const(TAG_FLT, float(n.value)))
        elif n.kind == 'string': self._emit(OP_CONST, self._const(TAG_STR, n.value))
        elif n.kind == 'bool':  self._emit(OP_TRUE if n.value else OP_FALSE)
        elif n.kind == 'null':  self._emit(OP_NULL)
        else: raise CodeGenPyroError(f"literal '{n.kind}' não suportado")

    _BINOP = {
        '+': OP_ADD, '-': OP_SUB, '*': OP_MUL, '/': OP_DIV, '%': OP_MOD,
        '&': OP_BAND, '|': OP_BOR, '^': OP_BXOR, '<<': OP_SHL, '>>': OP_SHR,
        '==': OP_EQ, '!=': OP_NE, '<': OP_LT, '>': OP_GT, '<=': OP_LE, '>=': OP_GE,
    }

    def _binary(self, n: BinaryExpr):
        if n.op == '&&':
            l_false = self._label(); l_end = self._label()
            self._expr(n.left);  self._emit(OP_JMPF, l_false)
            self._expr(n.right); self._emit(OP_JMPF, l_false)
            self._emit(OP_TRUE); self._emit(OP_JMP, l_end)
            self._place(l_false); self._emit(OP_FALSE)
            self._place(l_end); return
        if n.op == '||':
            l_true = self._label(); l_end = self._label()
            self._expr(n.left);  self._emit(OP_JMPT, l_true)
            self._expr(n.right); self._emit(OP_JMPT, l_true)
            self._emit(OP_FALSE); self._emit(OP_JMP, l_end)
            self._place(l_true); self._emit(OP_TRUE)
            self._place(l_end); return
        if n.op == '??':
            # inteiros/valores nunca nulos aqui: resultado = esquerda
            self._expr(n.left); return
        op = self._BINOP.get(n.op)
        if op is None:
            raise CodeGenPyroError(f"operador '{n.op}' não suportado no backend pyro")
        self._expr(n.left)
        self._expr(n.right)
        self._emit(op)

    def _call(self, n: CallExpr):
        if n.callee == 'print':
            if not n.args:
                self._emit(OP_PRINTLN)
                self._emit(OP_NULL)     # valor de expressão (descartado no stmt)
                return
            self._expr(n.args[0])
            self._emit(OP_PRINT)
            self._emit(OP_NULL)
            return
        # função do usuário
        fi = self._fnindex.get(n.callee)
        if fi is None:
            raise CodeGenPyroError(
                f"função '{n.callee}' desconhecida no backend pyro "
                f"(builtins: print; structs/arrays/etc. ainda não).")
        for a in n.args:
            self._expr(a)
        self._emit(OP_CALL, (fi, len(n.args)))

    # ── montagem final (.pyro) ──────────────────────────────

    def _assemble(self) -> bytes:
        # registra os nomes das funções no pool ANTES de serializar as consts
        for f in self._funcs:
            self._const(TAG_STR, f.name)
        # layout: offsets de instruções e labels
        label_off: Dict[int, int] = {}
        flat = []      # (op, arg, offset)
        offset = 0
        for f in self._funcs:
            f.entry = offset
            for (op, arg) in f.code:
                if op == -1:                    # LABEL
                    label_off[arg.id] = offset
                else:
                    flat.append((op, arg, offset))
                    offset += _isize(op)
        code_len = offset

        code = bytearray()
        for (op, arg, off) in flat:
            code.append(op)
            if op in (OP_CONST, OP_LOAD, OP_STORE):
                code += struct.pack('<H', arg)
            elif op in (OP_JMP, OP_JMPF, OP_JMPT):
                rel = label_off[arg.id] - (off + 3)
                code += struct.pack('<h', rel)
            elif op == OP_CALL:
                fi, argc = arg
                code += struct.pack('<H', fi)
                code.append(argc & 0xFF)
        assert len(code) == code_len

        flags = 0
        if self.encode:
            flags |= 0x01
            code = self._xor_encode(code)

        out = bytearray()
        out += _MAGIC
        out.append(_VERSION)
        out.append(flags)
        # constantes
        out += struct.pack('<H', len(self._consts))
        for tag, val in self._consts:
            out.append(tag)
            if tag == TAG_INT:   out += struct.pack('<q', int(val))
            elif tag == TAG_FLT: out += struct.pack('<d', float(val))
            elif tag == TAG_BOOL: out.append(1 if val else 0)
            elif tag == TAG_STR:
                b = val.encode('utf-8')
                out += struct.pack('<H', len(b)); out += b
        # funcoes
        out += struct.pack('<H', len(self._funcs))
        for f in self._funcs:
            nameidx = self._const_idx[(TAG_STR, f.name)]
            out += struct.pack('<H', nameidx)
            out += struct.pack('<I', f.entry)
            out.append(f.nparams & 0xFF)
            out += struct.pack('<H', len(f.locals))
        # entry
        out += struct.pack('<H', self._fnindex['main'])
        # code
        out += struct.pack('<I', code_len)
        out += code
        return bytes(out)

    @staticmethod
    def _xor_encode(code: bytes) -> bytes:
        # Camada de ofuscação leve (NÃO é criptografia forte): XOR rolling
        # com chave derivada de uma semente fixa. A VM aplica o inverso.
        out = bytearray(len(code))
        k = 0x5A
        for i, b in enumerate(code):
            out[i] = b ^ k
            k = (k * 31 + 7 + b) & 0xFF
        return bytes(out)
