# ============================================================
#  Cryo Compiler - x86-64 Assembly Code Generator  (v0.4)
#  .cryo  ->  .s  (GAS / Intel syntax)
#
#  Objective: extract the most from the machine by generating native code
#  directly, without passing through C. Supports two ABIs:
#
#    sysv  (System V AMD64 — Linux, macOS, WSL):
#        gcc -no-pie program.s cryo_runtime.c -lm -o program
#    win64 (Microsoft x64 — Windows/MinGW):
#        gcc program.s cryo_runtime.c -o program
#
#  Supported subset: integers (int) and booleans (bool),
#  functions/recursion, if/else, while, for, switch, break/continue,
#  arithmetic/bitwise/logical operators, assert and print.
#  High-level resources (number/double, dynamic strings,
#  arrays, structs, enums, try/catch) remain in the C backend.
#
#  Register strategy (common to both ABIs):
#    rax  = accumulator / expression result
#    r10  = right operand (RHS) — volatile and NOT-argument in
#           both ABIs, avoiding conflict with rcx (arg in Win64)
#    r15  = saves rsp around calls (non-volatile, preserved)
# ============================================================
from ast_nodes import *
from typing import List, Dict, Optional


class CodeGenAsmError(Exception):
    pass


# Integer argument registers by ABI
ARG_REGS_SYSV  = ['rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9']
ARG_REGS_WIN64 = ['rcx', 'rdx', 'r8', 'r9']

# Comparison operator -> 'set' instruction
_SETCC = {
    '==': 'sete', '!=': 'setne',
    '<':  'setl', '>':  'setg',
    '<=': 'setle', '>=': 'setge',
}


class CodeGenAsm:
    def __init__(self, safe: bool = True, abi: str = 'sysv'):
        if abi not in ('sysv', 'win64'):
            raise CodeGenAsmError(f"Unknown ABI: {abi!r}")
        self._abi = abi
        self._arg_regs = ARG_REGS_WIN64 if abi == 'win64' else ARG_REGS_SYSV
        # Win64 requires 32 bytes of shadow space before each call
        self._shadow = 32 if abi == 'win64' else 0
        # Offset (from rbp) of the 1st argument passed on the stack, seen by the
        # callee. Win64: shadow(32) + retaddr/rbp(16) = 48. SysV: 16.
        self._stack_arg_base = 48 if abi == 'win64' else 16
        # Read-only data section (COFF uses .rdata; ELF uses .rodata)
        self._rodata_section = '.rdata,"dr"' if abi == 'win64' else '.rodata'

        self._safe_default = safe
        self._safe_stack: List[bool] = []
        self._text:   List[str] = []
        self._rodata: List[str] = []
        self._str_labels: Dict[str, str] = {}
        self._n_str = 0
        self._n_lbl = 0

        # per-function state
        self._locals: Dict[str, int] = {}
        self._vartypes: Dict[str, str] = {}
        self._cur_fn = ''
        self._loop_stack: List[tuple] = []   # (break_label, continue_label)

        # tipos de retorno das functions (para inferencia de print)
        self._fn_ret: Dict[str, str] = {}

        # registered structs: name -> list of (field_name, field_type)
        self._structs: Dict[str, List[tuple]] = {}
        # sizes per local variable (structs occupy several slots)
        self._var_sizes: Dict[str, int] = {}
        self._cur_fn_ret = 'void'

    @property
    def _safe(self) -> bool:
        return self._safe_stack[-1] if self._safe_stack else self._safe_default

    # ── util ────────────────────────────────────────────────

    def _emit(self, line: str = ''):
        self._text.append(('    ' + line) if line and not line.endswith(':')
                          and not line.startswith('.') else line)

    def _label(self, hint: str = 'L') -> str:
        self._n_lbl += 1
        return f".{hint}{self._n_lbl}"

    def _str_label(self, s: str) -> str:
        if s in self._str_labels:
            return self._str_labels[s]
        lbl = f".LCstr{self._n_str}"
        self._n_str += 1
        self._str_labels[s] = lbl
        esc = (s.replace('\\', '\\\\').replace('"', '\\"')
                .replace('\n', '\\n').replace('\t', '\\t'))
        self._rodata.append(f'{lbl}:')
        self._rodata.append(f'    .string "{esc}"')
        return lbl

    # ── main entry ───────────────────────────────────

    def generate(self, program: Program) -> str:
        # 1st pass: registers structs (layout) and return types
        for n in program.statements:
            if isinstance(n, StructDecl):
                self._reg_struct(n)
        for n in program.statements:
            if isinstance(n, FunctionDecl):
                self._fn_ret[n.name] = n.return_type or 'void'

        top_level = []
        for n in program.statements:
            if isinstance(n, FunctionDecl):
                self._gen_function(n)
            elif isinstance(n, (Import, Library, StructDecl)):
                pass  # structs are only layout; nothing to emit
            elif isinstance(n, (EnumDecl, ForeignBlock)):
                raise CodeGenAsmError(
                    f"'{type(n).__name__}' is not supported in the assembly backend; "
                    f"use --backend c para enums/foreign blocks."
                )
            else:
                top_level.append(n)

        # synthetic 'main' from top-level statements
        self._gen_function(FunctionDecl('main', [], 'int', top_level),
                           synthetic_main=True)

        return self._assemble()

    def _assemble(self) -> str:
        if self._abi == 'win64':
            montar = "gcc file.s cryo_runtime.c -o program   (MinGW)"
            abi_nome = "Microsoft x64 (Win64)"
        else:
            montar = "gcc -no-pie file.s cryo_runtime.c -lm -o program"
            abi_nome = "System V AMD64"
        out = [
            "# ================================================",
            f"# [CRYO] Compiled from Cryo -> x86-64 (ABI {abi_nome})",
            f"# Montar: {montar}",
            "# ================================================",
            "    .intel_syntax noprefix",
            "    .text",
            "    .globl main",
            "",
        ]
        out += self._text
        if self._rodata:
            out += ["", f"    .section {self._rodata_section}"] + self._rodata
        out.append("")
        return '\n'.join(out)

    # ── structs: layout and return classification ─────────

    def _reg_struct(self, n: StructDecl):
        for f in n.fields:
            if f.field_type not in ('int', 'bool'):
                raise CodeGenAsmError(
                    f"struct '{n.name}', field '{f.name}': the assembly backend "
                    f"so aceita campos int/bool (8 bytes). Use --backend c.")
        self._structs[n.name] = [(f.name, f.field_type) for f in n.fields]

    def _is_struct(self, typ: str) -> bool:
        return typ in self._structs

    def _type_size(self, typ: str) -> int:
        if self._is_struct(typ):
            return len(self._structs[typ]) * 8
        return 8   # int/bool/pointer

    def _field_index(self, struct_type: str, field: str) -> int:
        for i, (fname, _) in enumerate(self._structs[struct_type]):
            if fname == field:
                return i
        raise CodeGenAsmError(
            f"field '{field}' non-existent in struct '{struct_type}'.")

    def _field_addr(self, var_name: str, field: str) -> str:
        typ = self._vartypes.get(var_name)
        if not self._is_struct(typ):
            raise CodeGenAsmError(
                f"'{var_name}' is not a struct; invalid field access '{field}'.")
        idx = self._field_index(typ, field)
        lo  = self._locals[var_name]          # field 0 at the lowest address
        disp = lo - idx * 8
        return f"[rbp-{disp}]"

    def _struct_return_kind(self, struct_type: str) -> str:
        """How a struct is returned: 'rax', 'rax_rdx' or 'memory'."""
        size = self._type_size(struct_type)
        if self._abi == 'sysv':
            if size <= 8:  return 'rax'
            if size <= 16: return 'rax_rdx'
            return 'memory'
        # win64: returns in RAX only if size is 1/2/4/8 bytes
        if size in (1, 2, 4, 8): return 'rax'
        return 'memory'

    def _struct_regs(self, struct_type: str) -> List[str]:
        kind = self._struct_return_kind(struct_type)
        if kind == 'rax':     return ['rax']
        if kind == 'rax_rdx': return ['rax', 'rdx']
        raise CodeGenAsmError(
            f"struct '{struct_type}' ({self._type_size(struct_type)} bytes) e "
            f"too large for register return in ABI {self._abi}; "
            f"the assembly backend does not yet implement pointer return "
            f"(sret). Use --backend c or reduce the struct.")

    # ── functions ─────────────────────────────────────────────

    def _collect_locals(self, stmts, names, types):
        """Discovers all local variables of a function (flat namespace)."""
        for n in stmts:
            if isinstance(n, VarDecl):
                if n.name not in names:
                    names.append(n.name)
                    types[n.name] = n.var_type
            elif isinstance(n, ConstDecl):
                if n.name not in names:
                    names.append(n.name)
                    types[n.name] = n.var_type
            elif isinstance(n, If):
                self._collect_locals(n.then_body, names, types)
                if n.else_body: self._collect_locals(n.else_body, names, types)
            elif isinstance(n, (While,)):
                self._collect_locals(n.body, names, types)
            elif isinstance(n, For):
                if n.init: self._collect_locals([n.init], names, types)
                self._collect_locals(n.body, names, types)
            elif isinstance(n, Switch):
                for c in n.cases: self._collect_locals(c.body, names, types)
                if n.default_body: self._collect_locals(n.default_body, names, types)
            elif isinstance(n, SafetyBlock):
                self._collect_locals(n.body, names, types)

    def _gen_function(self, fn: FunctionDecl, synthetic_main=False):
        self._cur_fn = fn.name
        self._cur_fn_ret = fn.return_type or 'void'
        self._locals = {}
        self._vartypes = {}
        self._var_sizes = {}
        self._loop_stack = []

        names: List[str] = []
        types: Dict[str, str] = {}
        # parameters first (structs by value as parameter not supported)
        for ptype, pname in fn.params:
            if self._is_struct(ptype):
                raise CodeGenAsmError(
                    f"funcao '{fn.name}': struct '{ptype}' como parametro por "
                    f"value is not supported in assembly backend. Use --backend c.")
            names.append(pname)
            types[pname] = ptype
        self._collect_locals(fn.body, names, types)
        self._vartypes = types

        # allocates by size (structs occupy several slots); frame aligned to 16.
        # cursor = distance (downwards) from rbp to the lowest byte of the local.
        cursor = 0
        for name in names:
            size = self._type_size(types[name])
            cursor += size
            self._locals[name] = cursor      # lowest byte in [rbp-cursor]
            self._var_sizes[name] = size
        frame = ((cursor + 15) // 16) * 16
        if frame == 0:
            frame = 16  # maintains alignment even without locals

        self._text.append(f"{fn.name}:")
        self._emit("push rbp")
        self._emit("mov rbp, rsp")
        self._emit(f"sub rsp, {frame}")

        # saves parameters in local slots.
        #   i < reg_n  -> came in a register
        #   i >= reg_n -> came on the caller's stack ([rbp+base+k*8])
        reg_n = len(self._arg_regs)
        for i, (ptype, pname) in enumerate(fn.params):
            off = self._locals[pname]
            if i < reg_n:
                self._emit(f"mov [rbp-{off}], {self._arg_regs[i]}")
            else:
                src = self._stack_arg_base + (i - reg_n) * 8
                self._emit(f"mov rax, [rbp+{src}]")
                self._emit(f"mov [rbp-{off}], rax")

        for s in fn.body:
            self._gen(s)

        # epilogue (default return 0)
        self._text.append(f".Lret_{fn.name}:")
        if synthetic_main:
            self._emit("xor eax, eax")   # main returns 0
        self._emit("leave")
        self._emit("ret")
        self._text.append("")

    # ── statements ──────────────────────────────────────────

    def _gen(self, node: Node):
        if   isinstance(node, VarDecl):            self._var(node)
        elif isinstance(node, ConstDecl):          self._const(node)
        elif isinstance(node, Assignment):         self._assign(node)
        elif isinstance(node, CompoundAssignment): self._compound(node)
        elif isinstance(node, Increment):          self._incr(node)
        elif isinstance(node, Return):             self._return(node)
        elif isinstance(node, If):                 self._if(node)
        elif isinstance(node, While):              self._while(node)
        elif isinstance(node, For):                self._for(node)
        elif isinstance(node, Switch):             self._switch(node)
        elif isinstance(node, Break):              self._break()
        elif isinstance(node, Continue):           self._continue()
        elif isinstance(node, Assert):             self._assert(node)
        elif isinstance(node, SafetyBlock):        self._safety(node)
        elif isinstance(node, CallExpr):
            self._eval_call(node); # discarded result
        else:
            raise CodeGenAsmError(
                f"'{type(node).__name__}' not supported in assembly backend "
                f"(use --backend c).")

    def _slot(self, name: str) -> str:
        if name not in self._locals:
            raise CodeGenAsmError(
                f"variable '{name}' unknown in assembly backend "
                f"(o subconjunto nativo cobre apenas int/bool locais).")
        return f"[rbp-{self._locals[name]}]"

    def _var(self, n: VarDecl):
        if self._is_struct(n.var_type):
            if n.value is None:
                for fname, _ in self._structs[n.var_type]:
                    self._emit(f"mov qword ptr {self._field_addr(n.name, fname)}, 0")
            else:
                self._store_struct(n.name, n.value)
            return
        self._check_int_type(n.var_type, n.name)
        if n.value is not None:
            self._eval(n.value)
            self._emit(f"mov {self._slot(n.name)}, rax")
        else:
            self._emit(f"mov qword ptr {self._slot(n.name)}, 0")

    def _const(self, n: ConstDecl):
        self._check_int_type(n.var_type, n.name)
        self._eval(n.value)
        self._emit(f"mov {self._slot(n.name)}, rax")

    def _assign(self, n: Assignment):
        if self._is_struct(self._vartypes.get(n.name)):
            self._store_struct(n.name, n.value)
            return
        self._eval(n.value)
        self._emit(f"mov {self._slot(n.name)}, rax")

    def _store_struct(self, name: str, value: Node):
        """Materializes 'value' into local struct 'name' (by fields)."""
        typ = self._vartypes[name]
        fields = self._structs[typ]
        if isinstance(value, StructInit):
            init_map = {k: v for k, v in value.fields}
            for fname, _ in fields:
                if fname in init_map:
                    self._eval(init_map[fname])
                else:
                    self._emit("xor eax, eax")          # omitted field = 0
                self._emit(f"mov {self._field_addr(name, fname)}, rax")
        elif isinstance(value, Identifier):
            src = value.name
            if self._vartypes.get(src) != typ:
                raise CodeGenAsmError(
                    f"incompatible struct copy: '{src}' -> '{name}'.")
            for fname, _ in fields:
                self._emit(f"mov rax, {self._field_addr(src, fname)}")
                self._emit(f"mov {self._field_addr(name, fname)}, rax")
        elif isinstance(value, CallExpr):
            regs = self._struct_regs(typ)               # validates register-return
            self._eval_call(value)                       # result in RAX/RDX
            for i, reg in enumerate(regs):
                fname = fields[i][0]
                self._emit(f"mov {self._field_addr(name, fname)}, {reg}")
        else:
            raise CodeGenAsmError(
                f"invalid value for struct '{name}': accepted 'new Struct{{...}}', "
                f"another struct variable, or function call.")

    def _compound(self, n: CompoundAssignment):
        base_op = n.op[:-1]                   # '+=' -> '+', '<<=' -> '<<'
        expr = BinaryExpr(base_op, Identifier(n.name), n.value)
        self._eval(expr)
        self._emit(f"mov {self._slot(n.name)}, rax")

    def _incr(self, n: Increment):
        instr = 'add' if n.op == '++' else 'sub'
        self._emit(f"{instr} qword ptr {self._slot(n.name)}, 1")

    def _return(self, n: Return):
        if self._is_struct(self._cur_fn_ret):
            if n.value is None:
                raise CodeGenAsmError(
                    f"function '{self._cur_fn}' returns struct but 'return' is empty.")
            self._return_struct(n.value)
            self._emit(f"jmp .Lret_{self._cur_fn}")
            return
        if n.value is not None:
            self._eval(n.value)
        else:
            self._emit("xor eax, eax")
        self._emit(f"jmp .Lret_{self._cur_fn}")

    def _return_struct(self, value: Node):
        """Puts the return struct in the ABI registers (RAX[/RDX])."""
        typ = self._cur_fn_ret
        regs = self._struct_regs(typ)       # validatestes register-return; otherwise error
        fields = self._structs[typ]

        if isinstance(value, Identifier):
            for i, reg in enumerate(regs):
                fname = fields[i][0]
                self._emit(f"mov {reg}, {self._field_addr(value.name, fname)}")
        elif isinstance(value, StructInit):
            init_map = {k: v for k, v in value.fields}
            def field_val(i):
                fname = fields[i][0]
                return init_map.get(fname, Literal('int', 0))
            if len(regs) == 1:
                self._eval(field_val(0))                 # -> rax
            else:  # rax + rdx: evaluates field0 -> stack, field1 -> rdx
                self._eval(field_val(0))
                self._emit("push rax")
                self._eval(field_val(1))
                self._emit("mov rdx, rax")
                self._emit("pop rax")
        elif isinstance(value, CallExpr):
            self._struct_regs(typ)                       # validates
            self._eval_call(value)                        # result already in RAX[/RDX]
        else:
            raise CodeGenAsmError(
                "struct return only accepts 'new Struct{...}', struct variable "
                "or function call in the assembly backend.")

    def _if(self, n: If):
        l_else = self._label('Lelse')
        l_end  = self._label('Lend')
        self._eval(n.condition)
        self._emit("cmp rax, 0")
        self._emit(f"je {l_else}")
        for s in n.then_body: self._gen(s)
        self._emit(f"jmp {l_end}")
        self._text.append(f"{l_else}:")
        if n.else_body:
            for s in n.else_body: self._gen(s)
        self._text.append(f"{l_end}:")

    def _while(self, n: While):
        l_cond = self._label('Lwcond')
        l_end  = self._label('Lwend')
        self._text.append(f"{l_cond}:")
        self._eval(n.condition)
        self._emit("cmp rax, 0")
        self._emit(f"je {l_end}")
        self._loop_stack.append((l_end, l_cond))
        for s in n.body: self._gen(s)
        self._loop_stack.pop()
        self._emit(f"jmp {l_cond}")
        self._text.append(f"{l_end}:")

    def _for(self, n: For):
        l_cond = self._label('Lfcond')
        l_cont = self._label('Lfcont')
        l_end  = self._label('Lfend')
        if n.init: self._gen(n.init)
        self._text.append(f"{l_cond}:")
        if n.condition:
            self._eval(n.condition)
            self._emit("cmp rax, 0")
            self._emit(f"je {l_end}")
        self._loop_stack.append((l_end, l_cont))
        for s in n.body: self._gen(s)
        self._loop_stack.pop()
        self._text.append(f"{l_cont}:")
        if n.update: self._gen(n.update)
        self._emit(f"jmp {l_cond}")
        self._text.append(f"{l_end}:")

    def _switch(self, n: Switch):
        # unfolded into comparisons (integer subset)
        l_end = self._label('Lsend')
        self._loop_stack.append((l_end, l_end))  # break -> end of switch
        case_labels = [self._label('Lcase') for _ in n.cases]
        l_default = self._label('Ldefault')
        self._eval(n.subject)
        self._emit("mov rdx, rax")            # rdx = switch value
        for lbl, case in zip(case_labels, n.cases):
            for v in case.values:
                self._eval(v)
                self._emit("cmp rdx, rax")
                self._emit(f"je {lbl}")
        self._emit(f"jmp {l_default}")
        for lbl, case in zip(case_labels, n.cases):
            self._text.append(f"{lbl}:")
            for s in case.body: self._gen(s)
            self._emit(f"jmp {l_end}")
        self._text.append(f"{l_default}:")
        if n.default_body:
            for s in n.default_body: self._gen(s)
        self._text.append(f"{l_end}:")
        self._loop_stack.pop()

    def _break(self):
        if not self._loop_stack:
            raise CodeGenAsmError("'break' out of a loop/switch")
        self._emit(f"jmp {self._loop_stack[-1][0]}")

    def _continue(self):
        if not self._loop_stack:
            raise CodeGenAsmError("'continue' out of a loop")
        self._emit(f"jmp {self._loop_stack[-1][1]}")

    def _assert(self, n: Assert):
        self._eval(n.condition)
        self._emit(f"mov {self._arg_regs[0]}, rax")
        if n.message is not None and isinstance(n.message, Literal) \
                and n.message.kind == 'string':
            lbl = self._str_label(n.message.value)
        else:
            lbl = self._str_label(f"assert failed (line {n.line})")
        self._emit(f"lea {self._arg_regs[1]}, [rip+{lbl}]")
        self._call_aligned("cryo_assert")

    def _safety(self, n: SafetyBlock):
        self._safe_stack.append(n.safe)
        for s in n.body: self._gen(s)
        self._safe_stack.pop()

    # ── expressions (result in rax) ───────────────────────

    def _eval(self, node: Node):
        if isinstance(node, Literal):
            if node.kind == 'int':
                self._emit(f"mov rax, {int(node.value)}")
            elif node.kind == 'bool':
                self._emit(f"mov rax, {1 if node.value else 0}")
            elif node.kind == 'null':
                self._emit("xor eax, eax")
            else:
                raise CodeGenAsmError(
                    f"literal '{node.kind}' not supported in assembly backend "
                    f"(apenas int/bool; use --backend c).")
            return

        if isinstance(node, Identifier):
            if self._is_struct(self._vartypes.get(node.name)):
                raise CodeGenAsmError(
                    f"struct '{node.name}' used as scalar value; access a "
                    f"field (e.g., {node.name}.field) in the assembly backend.")
            self._emit(f"mov rax, {self._slot(node.name)}")
            return

        if isinstance(node, UnaryExpr):
            self._eval(node.operand)
            if node.op == '-':   self._emit("neg rax")
            elif node.op == '~': self._emit("not rax")
            elif node.op == '!':
                self._emit("cmp rax, 0")
                self._emit("sete al")
                self._emit("movzx rax, al")
            else:
                raise CodeGenAsmError(f"unary '{node.op}' not supported")
            return

        if isinstance(node, BinaryExpr):
            self._binary(node)
            return

        if isinstance(node, CallExpr):
            self._eval_call(node)
            return

        if isinstance(node, FieldAccess):
            obj = node.obj
            if isinstance(obj, Identifier) and \
                    self._is_struct(self._vartypes.get(obj.name)):
                self._emit(f"mov rax, {self._field_addr(obj.name, node.field)}")
                return
            raise CodeGenAsmError(
                "field access is only supported on a local struct variable in backend "
                "assembly (use --backend c).")

        if isinstance(node, StructInit):
            raise CodeGenAsmError(
                "'new Struct{...}' can only be assigned to a struct variable or "
                "returned; cannot be used as a subexpression in assembly backend.")

        raise CodeGenAsmError(
            f"expression '{type(node).__name__}' not supported in backend "
            f"assembly (use --backend c).")

    def _binary(self, node: BinaryExpr):
        # logical short-circuit
        if node.op in ('&&', '||'):
            self._logical(node)
            return

        # evaluates left -> stack, right -> r10, left -> rax
        # (r10 is volatile and NOT-argument in both ABIs; rcx is argument in Win64)
        self._eval(node.left)
        self._emit("push rax")
        self._eval(node.right)
        self._emit("mov r10, rax")
        self._emit("pop rax")
        op = node.op

        if op == '+':
            if self._safe: self._call_binop_safe('cryo_add_ovf')
            else:          self._emit("add rax, r10")
        elif op == '-':
            if self._safe: self._call_binop_safe('cryo_sub_ovf')
            else:          self._emit("sub rax, r10")
        elif op == '*':
            if self._safe: self._call_binop_safe('cryo_mul_ovf')
            else:          self._emit("imul rax, r10")
        elif op == '/':
            self._call_binop_safe('cryo_idiv_chk')   # always protected
        elif op == '%':
            self._call_binop_safe('cryo_imod_chk')   # always protected
        elif op == '&':  self._emit("and rax, r10")
        elif op == '|':  self._emit("or rax, r10")
        elif op == '^':  self._emit("xor rax, r10")
        elif op == '<<':
            self._emit("mov rcx, r10")   # shift count must be in cl
            self._emit("sal rax, cl")
        elif op == '>>':
            self._emit("mov rcx, r10")
            self._emit("sar rax, cl")
        elif op in _SETCC:
            self._emit("cmp rax, r10")
            self._emit(f"{_SETCC[op]} al")
            self._emit("movzx rax, al")
        elif op == '??':
            # integers are never null: result = left
            pass
        else:
            raise CodeGenAsmError(f"operator '{op}' not supported")

    def _logical(self, node: BinaryExpr):
        l_short = self._label('Lshort')
        l_end   = self._label('Llend')
        self._eval(node.left)
        self._emit("cmp rax, 0")
        if node.op == '&&':
            self._emit(f"je {l_short}")       # false -> short-circuit 0
            self._eval(node.right)
            self._emit("cmp rax, 0")
            self._emit(f"je {l_short}")
            self._emit("mov rax, 1")
            self._emit(f"jmp {l_end}")
            self._text.append(f"{l_short}:")
            self._emit("xor eax, eax")
        else:  # ||
            self._emit(f"jne {l_short}")      # true -> short-circuit 1
            self._eval(node.right)
            self._emit("cmp rax, 0")
            self._emit(f"jne {l_short}")
            self._emit("xor eax, eax")
            self._emit(f"jmp {l_end}")
            self._text.append(f"{l_short}:")
            self._emit("mov rax, 1")
        self._text.append(f"{l_end}:")

    def _call_binop_safe(self, fn: str):
        # rax = lhs, r10 = rhs  ->  calls fn(arg0=lhs, arg1=rhs)
        a0, a1 = self._arg_regs[0], self._arg_regs[1]
        self._emit(f"mov {a0}, rax")
        self._emit(f"mov {a1}, r10")
        self._call_aligned(fn)

    def _eval_call(self, node: CallExpr):
        if node.callee == 'print':
            self._gen_print(node.args)
            self._emit("xor eax, eax")
            return
        if node.callee == 'abs' and len(node.args) == 1:
            self._eval(node.args[0])
            self._emit(f"mov {self._arg_regs[0]}, rax")
            self._call_aligned("cryo_abs_i")
            return

        # user-defined function
        reg_n = len(self._arg_regs)
        if len(node.args) <= reg_n:
            # simple path: everything in registers
            for a in node.args:
                self._eval(a)
                self._emit("push rax")
            for i in reversed(range(len(node.args))):
                self._emit(f"pop {self._arg_regs[i]}")
            self._call_aligned(node.callee)
        else:
            self._call_with_stack_args(node.callee, node.args)

    def _gen_print(self, args: List[Node]):
        if not args:
            self._emit("call cryo_print_newline")
            return
        arg = args[0]
        a0 = self._arg_regs[0]
        if isinstance(arg, Literal) and arg.kind == 'string':
            lbl = self._str_label(arg.value)
            self._emit(f"lea {a0}, [rip+{lbl}]")
            self._call_aligned("cryo_print_str")
            return
        t = self._infer(arg)
        self._eval(arg)
        self._emit(f"mov {a0}, rax")
        if t == 'bool':
            self._call_aligned("cryo_print_bool")
        else:
            self._call_aligned("cryo_print_i64")

    def _call_aligned(self, target: str):
        """Calls 'target' with the stack aligned to 16 bytes.

        Preserves rsp via r15 (non-volatile, saved/restored locally), which
        that works regardless of the parity of temporaries already
        pushed. On Win64 it also reserves 32 bytes of shadow space
        (required by the ABI for the callee to spill the 4 args into register).
        The argument registers must already be loaded.
        """
        self._emit("push r15")
        self._emit("mov r15, rsp")
        self._emit("and rsp, -16")
        if self._shadow:
            self._emit(f"sub rsp, {self._shadow}")   # shadow space (Win64)
        self._emit(f"call {target}")
        self._emit("mov rsp, r15")                    # frees shadow + restores
        self._emit("pop r15")

    def _call_with_stack_args(self, target: str, args: List[Node]):
        """Calls 'target' with more arguments than registers.

        The first reg_n arguments go in a register; the rest is
        placed in the stack's output area (above the shadow space in
        Win64), in the order expected by callee. Works for both ABIs.

        Layout in the output area (from rsp, at call time):
            Win64: [shadow 32B][arg_reg_n][arg_reg_n+1]...
            SysV : [arg_reg_n][arg_reg_n+1]...
        """
        n = len(args)
        reg_n = len(self._arg_regs)
        n_stack = n - reg_n
        shadow = self._shadow
        reserve = shadow + n_stack * 8
        reserve_padded = (reserve + 15) & ~15   # keeps rsp aligned to 16

        # 1) evaluates all args -> temporaries on stack (left to right)
        for a in args:
            self._eval(a)
            self._emit("push rax")
        # temporaries: arg[n-1] in [rsp], arg[k] in [rsp + (n-1-k)*8]

        # 2) anchors the base of the temporaries (r11 is volatile, used only before call)
        self._emit("mov r11, rsp")
        # 3) saves rsp, aligns and reserves the output area
        self._emit("push r15")
        self._emit("mov r15, rsp")
        self._emit("and rsp, -16")
        self._emit(f"sub rsp, {reserve_padded}")

        # 4) marshaling: registers from temporaries
        for i in range(reg_n):
            off = (n - 1 - i) * 8
            self._emit(f"mov {self._arg_regs[i]}, [r11+{off}]")
        # 5) marshaling: stack args to output area
        for j in range(n_stack):
            src_idx = reg_n + j
            off = (n - 1 - src_idx) * 8
            self._emit(f"mov rax, [r11+{off}]")
            self._emit(f"mov [rsp+{shadow + j * 8}], rax")

        self._emit(f"call {target}")
        # 6) undoes output area, restores r15 and discards temporaries
        self._emit("mov rsp, r15")
        self._emit("pop r15")
        self._emit(f"add rsp, {n * 8}")

    # ── minimal type inference ───────────────────────────

    def _infer(self, node: Node) -> str:
        if isinstance(node, Literal):
            return {'int': 'int', 'bool': 'bool', 'float': 'number',
                    'string': 'string', 'null': 'null'}.get(node.kind, 'int')
        if isinstance(node, Identifier):
            return self._vartypes.get(node.name, 'int')
        if isinstance(node, BinaryExpr):
            if node.op in ('==', '!=', '<', '>', '<=', '>=', '&&', '||'):
                return 'bool'
            return 'int'
        if isinstance(node, UnaryExpr):
            return 'bool' if node.op == '!' else 'int'
        if isinstance(node, CallExpr):
            return self._fn_ret.get(node.callee, 'int')
        if isinstance(node, FieldAccess):
            obj = node.obj
            if isinstance(obj, Identifier):
                st = self._vartypes.get(obj.name)
                if self._is_struct(st):
                    idx = self._field_index(st, node.field)
                    return self._structs[st][idx][1]
            return 'int'
        return 'int'

    def _check_int_type(self, t: str, name: str):
        if self._is_struct(t):
            raise CodeGenAsmError(
                f"variable '{name}': struct '{t}' in scalar context.")
        if t not in ('int', 'bool'):
            raise CodeGenAsmError(
                f"variable '{name}': type '{t}' not supported in backend "
                f"assembly (apenas int/bool). Use --backend c.")
