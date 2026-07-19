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
OP_ASSERT = 0x51  # pop cond, pop msg; se cond falso aborta
# ── containers: arrays, maps e structs (struct = map de chaves string) ──
OP_NEWARR = 0x60  # u16 count -> array a partir dos 'count' valores do topo
OP_NEWMAP = 0x61  # u16 count -> map a partir de 'count' pares (chave,valor)
OP_INDEX  = 0x62  # pop key, pop cont -> empilha cont[key]
OP_SETIDX = 0x63  # pop val, pop key, pop cont -> cont[key] = val
OP_LEN    = 0x64  # pop cont -> empilha tamanho (string/array/map)
OP_APPEND = 0x65  # pop val, pop arr -> arr.push(val); empilha novo tamanho
OP_HAS    = 0x66  # pop key, pop map -> empilha bool (existe)
OP_KEYS   = 0x67  # pop map -> empilha array de chaves
OP_NATIVE = 0x70  # u8 id, u8 argc -> chama builtin nativo da VM (tabela NATIVES)
OP_TRYPUSH = 0x71 # i16 rel (catch), u16 slot (var do catch; 0xFFFF = nenhuma)
OP_TRYPOP  = 0x72 # remove o handler de exceção do topo (try concluído)
OP_THROW   = 0x73 # pop valor -> desenrola até o handler mais próximo
OP_COALESCE = 0x74 # pop b, a -> a se a != null, senão b  (operador ??)
OP_UNWRAP  = 0x75 # pop a -> a se a != null, senão aborta  (unwrap x!)

# tamanho do operando por opcode (bytes após o opcode)
#  v2: saltos usam i32 (antes i16) -> sem limite de ±32KB por função.
_OPERAND = {
    OP_CONST: 2, OP_LOAD: 2, OP_STORE: 2,
    OP_JMP: 4, OP_JMPF: 4, OP_JMPT: 4,
    OP_CALL: 3, OP_NEWARR: 2, OP_NEWMAP: 2,
    OP_NATIVE: 2, OP_TRYPUSH: 6,   # i32 rel + u16 slot
}

_NO_SLOT = 0xFFFF   # TRYPUSH sem variável de catch

# builtins nativos da VM: nome -> (id, argc). A VM espelha esta tabela.
NATIVES = {
    'sqrt':      (0, 1),  'pow':      (1, 2),  'abs':    (2, 1),
    'min':       (3, 2),  'max':      (4, 2),  'floor':  (5, 1),
    'ceil':      (6, 1),  'round':    (7, 1),
    'to_string': (8, 1),  'to_int':   (9, 1),  'to_number': (10, 1),
    'remove':    (11, 2),
    # ── strings ──
    'upper':     (12, 1), 'lower':    (13, 1), 'trim':   (14, 1),
    'contains':  (15, 2), 'find':     (16, 2), 'replace': (17, 3),
    'substr':    (18, 3), 'split':    (19, 2), 'join':   (20, 2),
    # ── E/S e JSON (Fase 5) ──
    'input':     (21, 1),
    'json_encode': (22, 1), 'json_decode': (23, 1),
    # ── rede / tempo (Fase 5) ──
    'http_get':  (24, 1), 'http_post': (25, 2), 'sleep': (26, 1),
}

def _isize(op: int) -> int:
    return 1 + _OPERAND.get(op, 0)

# tags de constante
TAG_INT = 1
TAG_FLT = 2
TAG_STR = 3
TAG_BOOL = 4

_MAGIC = b"PYRO"
_VERSION = 2         # v2: saltos i32 + seção opcional de depuração (pc->linha)
_FLAG_ENCODED = 0x01
_FLAG_DEBUG   = 0x02


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


_FOLD_BIN = frozenset({
    OP_ADD, OP_SUB, OP_MUL, OP_DIV, OP_BAND, OP_BOR, OP_BXOR, OP_SHL, OP_SHR,
    OP_EQ, OP_NE, OP_LT, OP_GT, OP_LE, OP_GE,
})
_PUSH_PURE = frozenset({OP_CONST, OP_TRUE, OP_FALSE, OP_NULL, OP_LOAD})
_I64_MIN, _I64_MAX = -(1 << 63), (1 << 63) - 1


class CodeGenPyro:
    def __init__(self, safe: bool = True, encode: bool = True,
                 optimize: bool = True):
        self.safe = safe
        self.encode = encode
        self.optimize = optimize
        self._consts: List = []            # [(tag, value)]
        self._const_idx: Dict = {}
        self._funcs: List[_Func] = []
        self._fnindex: Dict[str, int] = {}
        self._cur: Optional[_Func] = None
        self._nlabels = 0
        self._loop_stack: List = []        # (break_label, continue_label)
        self._structs: Dict[str, List[str]] = {}
        self._enum_consts: Dict[str, int] = {}   # 'Nivel_ALTO' -> 2
        self._global_consts: Dict[str, Literal] = {}  # const global -> literal inlined
        self._ntmp = 0
        self._cur_line = 0            # última linha marcada (evita marcadores repetidos)

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
            elif isinstance(n, StructDecl):
                # struct = map de chaves string; registramos os campos
                self._structs[n.name] = [f.name for f in n.fields]
            elif isinstance(n, EnumDecl):
                # enum = constantes inteiras em tempo de compilação
                for i, m in enumerate(n.members):
                    self._enum_consts[f"{n.name}_{m}"] = i
            elif isinstance(n, ConstDecl) and isinstance(n.value, Literal):
                # const global com valor literal: inlined em todo uso
                # (visível dentro de funções, sem precisar de globais na VM)
                self._global_consts[n.name] = n.value
            elif isinstance(n, (Import, Library)):
                pass
            elif isinstance(n, (SkillDecl, ForeignBlock)):
                raise CodeGenPyroError(
                    f"'{type(n).__name__}' ainda não é suportado no backend pyro "
                    f"(bytecode: escalares, arrays, maps, structs, enums, funções e fluxo).")
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
        self._cur_line = 0
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

    def _mark_line(self, line):
        # marcador de linha (pseudo-instrução de tamanho zero, como um label).
        # A montagem transforma em entradas pc->linha da seção de depuração.
        if line and line != self._cur_line:
            self._cur_line = line
            self._cur.code.append((-2, line))

    def _stmt(self, n: Node):
        self._mark_line(getattr(n, 'line', 0))
        if   isinstance(n, ConstDecl):
            # const não-literal (ou local): tratada como variável imutável
            slot = self._slot(n.name)
            self._expr(n.value)
            self._emit(OP_STORE, slot)
        elif isinstance(n, VarDecl):            self._var(n)
        elif isinstance(n, Assignment):         self._assign(n)
        elif isinstance(n, IndexAssignment):    self._index_assign(n)
        elif isinstance(n, CompoundAssignment): self._compound(n)
        elif isinstance(n, Increment):          self._incr(n)
        elif isinstance(n, Return):             self._return(n)
        elif isinstance(n, If):                 self._if(n)
        elif isinstance(n, While):              self._while(n)
        elif isinstance(n, DoWhile):            self._do_while(n)
        elif isinstance(n, For):                self._for(n)
        elif isinstance(n, ForEach):            self._foreach(n)
        elif isinstance(n, Switch):             self._switch(n)
        elif isinstance(n, Break):              self._break()
        elif isinstance(n, Continue):           self._continue()
        elif isinstance(n, Assert):             self._assert(n)
        elif isinstance(n, TryCatch):           self._try(n)
        elif isinstance(n, SafetyBlock):
            for s in n.body: self._stmt(s)
        elif isinstance(n, CallExpr) and n.callee == 'throw':
            arg = n.args[0] if n.args else Literal('null', None)
            self._expr(arg); self._emit(OP_THROW)
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

    def _index_assign(self, n: IndexAssignment):
        # cont[key] = val
        self._expr(n.obj)
        self._expr(n.index)
        self._expr(n.value)
        self._emit(OP_SETIDX)

    def _foreach(self, n: ForEach):
        # for (T x in iter): itera por índice (arrays; maps via keys(m))
        #   __it = iter; __i = 0
        #   while (__i < len(__it)) { x = __it[__i]; <body>; __i++ }
        self._ntmp += 1
        it = self._slot(f"__it{self._ntmp}")
        ix = self._slot(f"__i{self._ntmp}")
        xs = self._slot(n.var_name)
        self._expr(n.iterable); self._emit(OP_STORE, it)
        self._emit(OP_CONST, self._const(TAG_INT, 0)); self._emit(OP_STORE, ix)
        l_cond = self._label(); l_cont = self._label(); l_end = self._label()
        self._place(l_cond)
        self._emit(OP_LOAD, ix)
        self._emit(OP_LOAD, it); self._emit(OP_LEN)
        self._emit(OP_LT)
        self._emit(OP_JMPF, l_end)
        # x = __it[__i]
        self._emit(OP_LOAD, it); self._emit(OP_LOAD, ix); self._emit(OP_INDEX)
        self._emit(OP_STORE, xs)
        self._loop_stack.append((l_end, l_cont))
        for s in n.body: self._stmt(s)
        self._loop_stack.pop()
        self._place(l_cont)
        # __i++
        self._emit(OP_LOAD, ix)
        self._emit(OP_CONST, self._const(TAG_INT, 1)); self._emit(OP_ADD)
        self._emit(OP_STORE, ix)
        self._emit(OP_JMP, l_cond)
        self._place(l_end)

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

    def _try(self, n: TryCatch):
        # TRYPUSH -> catch; <try>; TRYPOP; JMP finally; catch:; <catch>; finally:
        l_catch = self._label()
        l_finally = self._label()
        slot = self._slot(n.catch_name) if n.catch_name else _NO_SLOT
        self._emit(OP_TRYPUSH, (l_catch, slot))
        for s in n.try_body:
            self._stmt(s)
        self._emit(OP_TRYPOP)
        self._emit(OP_JMP, l_finally)
        self._place(l_catch)             # o valor lançado já foi guardado no slot
        if n.catch_body:
            for s in n.catch_body:
                self._stmt(s)
        self._place(l_finally)
        if n.finally_body:
            for s in n.finally_body:
                self._stmt(s)

    # ── expressoes ──────────────────────────────────────────

    def _expr(self, n: Node):
        if isinstance(n, Literal):
            self._literal(n); return
        if isinstance(n, Identifier):
            if n.name in self._enum_consts:      # membro de enum -> const int
                self._emit(OP_CONST, self._const(TAG_INT, self._enum_consts[n.name]))
                return
            # const global inlined (visível em qualquer função), exceto se
            # houver uma variável local com o mesmo nome (sombra)
            if n.name in self._global_consts and n.name not in self._cur.locals:
                self._literal(self._global_consts[n.name])
                return
            self._emit(OP_LOAD, self._get_slot(n.name)); return
        if isinstance(n, UnaryExpr):
            self._expr(n.operand)
            if   n.op == '-': self._emit(OP_NEG)
            elif n.op == '!': self._emit(OP_NOT)
            elif n.op == '~': self._emit(OP_BNOT)
            else: raise CodeGenPyroError(f"unário '{n.op}' não suportado")
            return
        if isinstance(n, UnwrapExpr):
            # x! : aborta se nulo, senão devolve o valor (COALESCE c/ THROW)
            self._expr(n.operand)
            self._emit(OP_UNWRAP); return
        if isinstance(n, CastExpr):
            # tipos são dinâmicos na VM: 'expr as T' é identidade.
            # json_decode(s) as T -> decodifica p/ valor dinâmico (structs
            # são maps, então acesso a campo funciona por indexação).
            self._expr(n.expr); return
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
        if isinstance(n, ArrayLiteral):
            for e in n.elements: self._expr(e)
            self._emit(OP_NEWARR, len(n.elements)); return
        if isinstance(n, MapLiteral):
            for k, v in n.pairs:
                self._expr(k); self._expr(v)
            self._emit(OP_NEWMAP, len(n.pairs)); return
        if isinstance(n, StructInit):
            # struct = map de chaves string (nome do campo)
            for fname, val in n.fields:
                self._emit(OP_CONST, self._const(TAG_STR, fname))
                self._expr(val)
            self._emit(OP_NEWMAP, len(n.fields)); return
        if isinstance(n, IndexAccess):
            self._expr(n.obj); self._expr(n.index); self._emit(OP_INDEX); return
        if isinstance(n, FieldAccess):
            if n.field == 'length':
                self._expr(n.obj); self._emit(OP_LEN); return
            self._expr(n.obj)
            self._emit(OP_CONST, self._const(TAG_STR, n.field))
            self._emit(OP_INDEX); return
        if isinstance(n, MethodCallExpr):
            self._method(n); return
        raise CodeGenPyroError(
            f"expressão '{type(n).__name__}' não suportada no backend pyro.")

    def _method(self, n: MethodCallExpr):
        m = n.method
        if m == 'push':
            self._expr(n.obj)
            self._expr(n.args[0] if n.args else Literal('null', None))
            self._emit(OP_APPEND); return       # empilha novo tamanho
        if m in ('length', 'size'):
            self._expr(n.obj); self._emit(OP_LEN); return
        raise CodeGenPyroError(
            f"método '.{m}()' não suportado no backend pyro.")

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
            # a ?? b : a se a != null, senão b (avaliação simples de ambos)
            self._expr(n.left)
            self._expr(n.right)
            self._emit(OP_COALESCE); return
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
        if n.callee == 'len' and len(n.args) == 1:
            self._expr(n.args[0]); self._emit(OP_LEN); return
        if n.callee == 'has' and len(n.args) == 2:
            self._expr(n.args[0]); self._expr(n.args[1]); self._emit(OP_HAS); return
        if n.callee == 'keys' and len(n.args) == 1:
            self._expr(n.args[0]); self._emit(OP_KEYS); return
        # builtins nativos da VM (matemática, conversões, strings, remove)
        nat = NATIVES.get(n.callee)
        if nat is not None:
            nid, argc = nat
            if len(n.args) != argc:
                raise CodeGenPyroError(
                    f"'{n.callee}' espera {argc} argumento(s), recebeu {len(n.args)}")
            for a in n.args:
                self._expr(a)
            self._emit(OP_NATIVE, (nid, argc))
            return
        # função do usuário
        fi = self._fnindex.get(n.callee)
        if fi is None:
            raise CodeGenPyroError(
                f"função '{n.callee}' desconhecida no backend pyro "
                f"(builtins: print, len, has, keys, {', '.join(sorted(NATIVES))}).")
        for a in n.args:
            self._expr(a)
        self._emit(OP_CALL, (fi, len(n.args)))

    # ── otimizador de bytecode ──────────────────────────────
    #  Peephole sobre a lista (op, arg) de cada função. Como os
    #  saltos referenciam LABELS (resolvidos só em _assemble),
    #  remover/dobrar instruções é seguro — os rótulos ficam
    #  intactos e reancoram nos offsets certos.

    def _optimize_all(self):
        for f in self._funcs:
            code = f.code
            for _ in range(8):                 # itera até estabilizar
                code, c1 = self._fold_pass(code)
                code, c2 = self._dce_pass(code)
                code, c3 = self._peephole_pass(code)
                if not (c1 or c2 or c3):
                    break
            f.code = code
        self._prune_consts()

    def _prune_consts(self):
        # remove do pool as constantes que ninguém referencia mais
        # (ex.: operandos intermediários que o folding tornou mortos)
        used = set()
        for f in self._funcs:
            for (op, arg) in f.code:
                if op == OP_CONST:
                    used.add(arg)
        remap, new_consts = {}, []
        for old, c in enumerate(self._consts):
            if old in used:
                remap[old] = len(new_consts)
                new_consts.append(c)
        for f in self._funcs:
            f.code = [(op, remap[arg]) if op == OP_CONST else (op, arg)
                      for (op, arg) in f.code]
        self._consts = new_consts
        self._const_idx = {c: i for i, c in enumerate(new_consts)}

    def _fold_pass(self, code):
        out, i, changed = [], 0, False
        n = len(code)
        while i < n:
            # unário: CONST a ; NEG|BNOT
            if (i + 1 < n and code[i][0] == OP_CONST
                    and code[i + 1][0] in (OP_NEG, OP_BNOT)):
                r = self._fold_unary(code[i + 1][0], self._consts[code[i][1]])
                if r is not None:
                    out.append(r); i += 2; changed = True; continue
            # binário: CONST a ; CONST b ; <op>
            if (i + 2 < n and code[i][0] == OP_CONST and code[i + 1][0] == OP_CONST
                    and code[i + 2][0] in _FOLD_BIN):
                r = self._fold_binary(code[i + 2][0],
                                      self._consts[code[i][1]],
                                      self._consts[code[i + 1][1]])
                if r is not None:
                    out.append(r); i += 3; changed = True; continue
            out.append(code[i]); i += 1
        return out, changed

    def _fold_unary(self, opc, ca):
        t, v = ca
        if opc == OP_NEG and t in (TAG_INT, TAG_FLT):
            if t == TAG_INT:
                r = -v
                return (OP_CONST, self._const(TAG_INT, r)) if _I64_MIN <= r <= _I64_MAX else None
            return (OP_CONST, self._const(TAG_FLT, -v))
        if opc == OP_BNOT and t == TAG_INT:
            return (OP_CONST, self._const(TAG_INT, ~v))
        return None

    def _fold_binary(self, opc, ca, cb):
        ta, a = ca
        tb, b = cb
        numeric = ta in (TAG_INT, TAG_FLT) and tb in (TAG_INT, TAG_FLT)
        both_int = ta == TAG_INT and tb == TAG_INT
        both_str = ta == TAG_STR and tb == TAG_STR
        # comparações -> bool
        if opc in (OP_EQ, OP_NE):
            if not (numeric or both_str):
                return None
            r = (a == b) if opc == OP_EQ else (a != b)
            return (OP_TRUE if r else OP_FALSE, None)
        if opc in (OP_LT, OP_GT, OP_LE, OP_GE):
            if not (numeric or both_str):
                return None
            r = {OP_LT: a < b, OP_GT: a > b, OP_LE: a <= b, OP_GE: a >= b}[opc]
            return (OP_TRUE if r else OP_FALSE, None)
        # concatenação de literais string
        if both_str and opc == OP_ADD:
            return (OP_CONST, self._const(TAG_STR, a + b))
        if not numeric:
            return None
        # aritmética / bitwise
        if opc == OP_ADD:   r = a + b
        elif opc == OP_SUB: r = a - b
        elif opc == OP_MUL: r = a * b
        elif opc == OP_DIV:
            if both_int or b == 0:      # int/int e div-por-zero: deixa p/ runtime
                return None
            r = a / b
        elif opc in (OP_BAND, OP_BOR, OP_BXOR, OP_SHL, OP_SHR):
            if not both_int:
                return None
            if opc in (OP_SHL, OP_SHR) and not (0 <= b <= 63):
                return None
            r = {OP_BAND: a & b, OP_BOR: a | b, OP_BXOR: a ^ b,
                 OP_SHL: a << b, OP_SHR: a >> b}[opc]
        else:
            return None
        if both_int:
            if not (_I64_MIN <= r <= _I64_MAX):   # estouraria int64: runtime resolve
                return None
            return (OP_CONST, self._const(TAG_INT, int(r)))
        return (OP_CONST, self._const(TAG_FLT, float(r)))

    def _dce_pass(self, code):
        out, reachable, changed = [], True, False
        for entry in code:
            op = entry[0]
            if op == -1:                    # LABEL: religa o fluxo
                reachable = True
                out.append(entry); continue
            if reachable:
                out.append(entry)
                if op in (OP_JMP, OP_RET, OP_HALT):
                    reachable = False
            else:
                changed = True              # instrução morta, descartada
        return out, changed

    def _peephole_pass(self, code):
        out, i, changed = [], 0, False
        n = len(code)
        while i < n:
            op = code[i][0]
            # push puro seguido de POP: some com os dois
            if (i + 1 < n and op in _PUSH_PURE and code[i + 1][0] == OP_POP):
                i += 2; changed = True; continue
            # JMP para o rótulo imediatamente à frente (só labels entre eles)
            if op == OP_JMP:
                tgt = code[i][1]
                j = i + 1
                hit = False
                while j < n and code[j][0] in (-1, -2):
                    if code[j][0] == -1 and code[j][1] is tgt:
                        hit = True; break
                    j += 1
                if hit:
                    i += 1; changed = True; continue
            out.append(code[i]); i += 1
        return out, changed

    # ── montagem final (.pyro) ──────────────────────────────

    def _assemble(self) -> bytes:
        if self.optimize:
            self._optimize_all()
        # registra os nomes das funções no pool ANTES de serializar as consts
        for f in self._funcs:
            self._const(TAG_STR, f.name)
        # layout: offsets de instruções, labels e marcadores de linha
        label_off: Dict[int, int] = {}
        debug = []     # (offset, line) — seção de depuração
        flat = []      # (op, arg, offset)
        offset = 0
        for f in self._funcs:
            f.entry = offset
            for (op, arg) in f.code:
                if op == -1:                    # LABEL
                    label_off[arg.id] = offset
                elif op == -2:                  # marcador de linha (0 bytes)
                    if not debug or debug[-1][0] != offset:
                        debug.append((offset, arg))
                else:
                    flat.append((op, arg, offset))
                    offset += _isize(op)
        code_len = offset

        code = bytearray()
        for (op, arg, off) in flat:
            code.append(op)
            if op in (OP_CONST, OP_LOAD, OP_STORE, OP_NEWARR, OP_NEWMAP):
                code += struct.pack('<H', arg)
            elif op in (OP_JMP, OP_JMPF, OP_JMPT):
                rel = label_off[arg.id] - (off + 5)   # 1 opcode + 4 (i32)
                code += struct.pack('<i', rel)
            elif op == OP_CALL:
                fi, argc = arg
                code += struct.pack('<H', fi)
                code.append(argc & 0xFF)
            elif op == OP_NATIVE:
                nid, argc = arg
                code.append(nid & 0xFF)
                code.append(argc & 0xFF)
            elif op == OP_TRYPUSH:
                lbl, slot = arg
                rel = label_off[lbl.id] - (off + 7)   # 1 opcode + 6 (i32 + u16)
                code += struct.pack('<i', rel)
                code += struct.pack('<H', slot)
        assert len(code) == code_len

        flags = 0
        if self.encode:
            flags |= _FLAG_ENCODED
            code = self._xor_encode(code)
        if debug:
            flags |= _FLAG_DEBUG

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
        # seção de depuração (pc -> linha), se houver
        if debug:
            out += struct.pack('<I', len(debug))
            for pc, line in debug:
                out += struct.pack('<I', pc)
                out += struct.pack('<I', line)
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
