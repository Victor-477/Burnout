# ============================================================
#  Cryo Compiler - C Code Generator  (v0.3)
#  .cryo  ->  .pyro  (Native C, compilable with gcc/clang)
# ============================================================
from ast_nodes import *
from foreign import collect_imports, resolve_library_lang
from typing import List, Dict, Optional, Set


class CodeGenError(Exception):
    pass

# ── Cryo -> C type mapping ───────────────────────────

C_TYPE: Dict[str, str] = {
    'int':    'int64_t',
    'number': 'double',
    'string': 'char*',
    'bool':   'bool',
    'void':   'void',
    'null':   'void*',
}

# 11.27 — T? is a POINTER, the same representation the go backend uses.
# `string?` needs no wrapper: char* is already nullable.
_OPT_C = {'int': 'int64_t*', 'number': 'double*', 'bool': 'bool*',
          'string': 'char*'}
_OPT_WRAP = {'int': 'cryo_opt_i', 'number': 'cryo_opt_f', 'bool': 'cryo_opt_b'}
_OPT_UNWRAP = {'int': 'cryo_unwrap_i', 'number': 'cryo_unwrap_f',
               'bool': 'cryo_unwrap_b', 'string': 'cryo_unwrap_s'}


# 11.27 — maps. Keys and values travel through the runtime as uint64_t, so
# each end needs one conversion, picked by the declared type.
_U64_TO   = {'int': 'cryo_u64_i', 'number': 'cryo_u64_f',
             'string': 'cryo_u64_s', 'bool': 'cryo_u64_i'}
_U64_FROM = {'int': 'cryo_of_u64_i', 'number': 'cryo_of_u64_f',
             'string': 'cryo_of_u64_s', 'bool': 'cryo_of_u64_b'}
_VAL_KIND = {'int': 0, 'number': 1, 'string': 2, 'bool': 3}


def is_map(t: str) -> bool:
    return bool(t) and t.startswith('map<') and t.endswith('>')


def map_kv(t: str):
    """('string', 'int') for map<string,int>. Split on the FIRST comma only —
    a nested map<...> value would otherwise be cut in half."""
    if not is_map(t):
        return ('unknown', 'unknown')
    inner = t[4:-1]
    depth = 0
    for i, ch in enumerate(inner):
        if ch == '<':
            depth += 1
        elif ch == '>':
            depth -= 1
        elif ch == ',' and depth == 0:
            return (inner[:i].strip(), inner[i + 1:].strip())
    return (inner.strip(), 'unknown')


def is_optional(t: str) -> bool:
    return bool(t) and t.endswith('?')


def opt_base(t: str) -> str:
    return t[:-1] if is_optional(t) else t


def c_type(t: str) -> str:
    if t and t.endswith('?'):
        base = t[:-1]
        if base in _OPT_C:
            return _OPT_C[base]
        raise CodeGenError(
            f"optional type '{t}' is not supported in the C backend — only "
            f"int?, number?, bool? and string? are; use --backend go, node or "
            f"pyro.")
    if t and t.startswith('map<'):
        k, v = map_kv(t)
        if k in _U64_TO and v in _U64_TO:
            return 'CryoMap*'
        raise CodeGenError(
            f"map '{t}' is not supported in the C backend — keys and values "
            f"must be int, number, string or bool; use --backend go, node or "
            f"pyro.")
    # function types have no C spelling here: unknown types pass through
    # verbatim, so without this guard `fn(int)->int` leaked into the output and
    # produced invalid C that only failed later, inside gcc.
    if t and t.startswith('fn('):
        raise CodeGenError(
            f"function type '{t}' (first-class functions) is not supported in the C "
            f"backend; use --backend go, node or pyro.")
    # Same leak, same fix. C has no dynamic type, and `any` fell through the
    # pass-through below into the output verbatim, so gcc — not the compiler —
    # reported `unknown type name 'any'` against generated code the programmer
    # never wrote. A list comprehension reaches here without the word `any`
    # appearing in the source at all: its synthetic helper is typed `any`, so
    # the message says where it comes from rather than naming a type the
    # reader cannot find.
    if t == 'any':
        raise CodeGenError(
            "the dynamic type 'any' has no C representation. It also comes from "
            "constructs that infer it — a comprehension, or a `for (x in xs)` "
            "without a declared type. Give the variable an explicit type, or use "
            "--backend go, node or pyro.")
    if t and t.endswith('[]'):
        return 'CryoArray*'
    return C_TYPE.get(t, t)          # struct types pass directly

def elem_type(arr_t: str) -> str:
    return arr_t[:-2] if arr_t.endswith('[]') else 'unknown'


_C_ESCAPES = {'\\': '\\\\', '"': '\\"', '\n': '\\n', '\t': '\\t',
              '\r': '\\r', '\x00': '\\0'}


def _c_string(v: str) -> str:
    """A Cryo string as a C string literal.

    The value was interpolated verbatim before, so a backslash in the program's
    data became an ESCAPE in the generated C: `print("a\\\\b")` emitted
    `"a\\b"`, which C reads as a backspace. A quote or a newline in the value
    did worse and produced code that would not compile — the C compiler
    reporting on a string the programmer never wrote.

    Anything outside printable ASCII is left alone: the runtime is UTF-8 and
    the generated file is compiled with -finput-charset=UTF-8, so the bytes
    pass through correctly as themselves.
    """
    out = []
    for ch in v:
        if ch in _C_ESCAPES:
            out.append(_C_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append('\\%03o' % ord(ch))
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


# ── Type Inference ──────────────────────────────────────

class TypeEnv:
    def __init__(self):
        self._scopes: List[Dict[str, str]] = [{}]
        self._fns:    Dict[str, str] = {}
        self._structs:Dict[str, Dict[str, str]] = {}
        self._enums:  Set[str] = set()
        self._enum_members: Dict[str, str] = {}   # 12.9: 'A' -> 'E_A'

    def push(self): self._scopes.append({})
    def pop(self):  self._scopes.pop()

    def set(self, name: str, typ: str): self._scopes[-1][name] = typ

    def get(self, name: str) -> str:
        for s in reversed(self._scopes):
            if name in s:
                return s[name]
        return 'unknown'

    def reg_fn(self, name: str, ret: str): self._fns[name] = ret
    def fn_ret(self, name: str) -> str:    return self._fns.get(name, 'unknown')

    def reg_struct(self, name: str, fields: Dict[str, str]):
        self._structs[name] = fields

    def struct_field(self, struct: str, field: str) -> str:
        return self._structs.get(struct, {}).get(field, 'unknown')

    def reg_enum(self, name: str): self._enums.add(name)
    def is_enum(self, name: str) -> bool: return name in self._enums

    # 12.9 — bare member name -> the constant C actually declares.
    # `typedef enum { E_A, E_B } E;` declares E_A, but `E e = A;` emitted a
    # bare `A`, so the C did not compile. Registered for both spellings so a
    # qualified `E.A` resolves through the same table.
    def reg_enum_member(self, member: str, qualified: str):
        self._enum_members[member] = qualified

    def enum_member(self, name: str):
        return self._enum_members.get(name)

    def infer(self, node) -> str:
        if node is None: return 'unknown'
        if isinstance(node, Literal):
            return {'int': 'int', 'float': 'number',
                    'string': 'string', 'bool': 'bool',
                    'null': 'null'}.get(node.kind, 'unknown')
        if isinstance(node, Identifier):
            return self.get(node.name)
        if isinstance(node, BinaryExpr):
            if node.op in ('==', '!=', '<', '>', '<=', '>=', '&&', '||'):
                return 'bool'
            # 11.27 — `a ?? b` yields a PRESENT value, so its type is the
            # unwrapped one. Reporting 'int?' here made print() try to render
            # an optional and refuse.
            if node.op == '??':
                lt = self.infer(node.left)
                return lt[:-1] if lt.endswith('?') else lt
            lt = self.infer(node.left)
            rt = self.infer(node.right)
            if lt == 'string' or rt == 'string': return 'string'
            if lt == 'number' or rt == 'number': return 'number'
            return lt if lt != 'unknown' else rt
        if isinstance(node, UnaryExpr):
            return 'bool' if node.op == '!' else self.infer(node.operand)
        # 11.27 — `x!` yields the value, so the type is the unwrapped one.
        if isinstance(node, UnwrapExpr):
            inner = getattr(node, 'operand', None) or getattr(node, 'expr', None)
            t = self.infer(inner)
            return t[:-1] if t.endswith('?') else t
        if isinstance(node, TernaryExpr):
            t = self.infer(node.then_value)
            return t if t != 'unknown' else self.infer(node.else_value)
        if isinstance(node, CallExpr):
            if node.callee in ('to_string', 'input', 'upper', 'lower', 'trim', 'substr',
                               'concat', 'repeat', 'pad_start', 'pad_end', 'replace', 'join',
                               'read_file', 'env', 'exec'):
                return 'string'
            if node.callee in ('to_int', 'len', 'sign', 'gcd', 'find',
                               'count', 'index_of', 'file_size'):
                return 'int'
            if node.callee in ('file_exists', 'is_dir', 'make_dir', 'delete_file', 'write_file'):
                return 'bool'
            # 11.27 — the map builtins. `keys` yields an array of the KEY type,
            # which is the only way `string[] k = keys(m); print(k);` can pick
            # the right renderer.
            if node.callee == 'has':
                return 'bool'
            if node.callee == 'keys' and node.args:
                mt = self.infer(node.args[0])
                return (map_kv(mt)[0] + '[]') if is_map(mt) else 'unknown'
            if node.callee in ('list_dir', 'split'):
                return 'string[]'
            # these return a NEW array of the same type as their first argument
            if node.callee in ('sort', 'reverse', 'slice', 'concat') and node.args:
                return self.infer(node.args[0])
            # sum() yields the element type of its array
            if node.callee == 'sum' and node.args:
                return elem_type(self.infer(node.args[0]))
            if node.callee in ('to_number', 'sqrt', 'pow', 'hypot', 'floor', 'ceil', 'round'):
                return 'number'
            if node.callee in ('starts_with', 'ends_with', 'contains'):
                return 'bool'
            # abs/min/max preserve the argument's type. `abs` must NOT be typed
            # `int` unconditionally: _call() already picks cryo_abs_f for a
            # `number`, so claiming `int` here made print() emit
            # cryo_print_i64() on a double and truncate (abs(-2.5) -> 2).
            if node.callee in ('abs', 'min', 'max'):
                return self.infer(node.args[0]) if node.args else 'number'
            # clamp(x, lo, hi) is int only when ALL THREE are int — mixing in a
            # float makes the result a float, matching the Pyro runtime.
            if node.callee == 'clamp':
                ts = [self.infer(a) for a in node.args]
                return 'int' if ts and all(t == 'int' for t in ts) else 'number'
            return self.fn_ret(node.callee)
        if isinstance(node, StructInit):
            return node.struct_name
        if isinstance(node, ArrayLiteral):
            return 'array'
        if isinstance(node, FieldAccess):
            ot = self.infer(node.obj)
            if node.field == 'length': return 'int'
            return self.struct_field(ot, node.field)
        if isinstance(node, MethodCallExpr):
            if node.method in ('upper', 'lower', 'substr'): return 'string'
            if node.method in ('length', 'size'): return 'int'
            if node.method == 'contains': return 'bool'
        if isinstance(node, IndexAccess):
            at = self.infer(node.obj)
            if at.startswith('map<'):
                return map_kv(at)[1]
            return elem_type(at)
        return 'unknown'


# ── CodeGen C ────────────────────────────────────────────────

_PUSH_FN = {
    'int': 'cryo_push_i64', 'number': 'cryo_push_f64',
    'string': 'cryo_push_str', 'bool': 'cryo_push_bool',
}

# 11.27 — the assignment counterparts, for `a[i] = v`.
_SET_FN = {
    'int': 'cryo_set_i64', 'number': 'cryo_set_f64',
    'string': 'cryo_set_str', 'bool': 'cryo_set_bool',
}
_GET_FN = {
    'int': 'cryo_get_i64', 'number': 'cryo_get_f64',
    'string': 'cryo_get_str', 'bool': 'cryo_get_bool',
}


class CodeGenC:
    def __init__(self, safe: bool = True):
        self.te = TypeEnv()
        self._indent = 0
        self._type_decls:   List[str] = []
        self._fwd_decls:    List[str] = []
        self._global_decls: List[str] = []
        self._fn_defs:      List[str] = []
        self._main_stmts:   List[str] = []
        self._cur:          List[str] = self._main_stmts
        # ── security ──
        self._safe_default = safe          # global --safe mode
        self._safe_stack:  List[bool] = [] # override by safe/unsafe blocks
        self._loop_depth = 0               # validates break/continue
        self._fe = 0                       # for-each index counter

    @property
    def _safe(self) -> bool:
        return self._safe_stack[-1] if self._safe_stack else self._safe_default

    # ── emission ──────────────────────────────────────────────

    def _pad(self) -> str:
        return '    ' * self._indent

    def _emit(self, line: str = ''):
        self._cur.append(self._pad() + line if line else '')

    # ── main entry ────────────────────────────────────

    def generate(self, program: Program) -> str:
        self._pre_scan(program.statements)
        self._imported_langs = collect_imports(program)

        for node in program.statements:
            if isinstance(node, (StructDecl, EnumDecl)):
                self._cur = self._type_decls
                self._indent = 0
                self._gen(node)
            elif isinstance(node, FunctionDecl):
                self._cur = self._fn_defs
                self._indent = 0
                self._gen(node)
            elif isinstance(node, ConstDecl):
                self._cur = self._global_decls
                self._indent = 0
                self._gen(node)
            elif isinstance(node, (Import, Library)):
                self._cur = self._global_decls
                self._indent = 0
                self._gen(node)
            elif isinstance(node, VarDecl):
                self._module_var(node)
            else:
                self._cur = self._main_stmts
                self._indent = 1
                self._gen(node)

        return self._assemble()

    def _pre_scan(self, stmts: List[Node]):
        """Primeiro passo: registrar tipos e gerar prototipos."""
        for n in stmts:
            if isinstance(n, StructDecl):
                self.te.reg_struct(n.name, {f.name: f.field_type for f in n.fields})
            elif isinstance(n, EnumDecl):
                self.te.reg_enum(n.name)
                for m in n.members:
                    self.te.reg_enum_member(m.name, f"{n.name}_{m.name}")
                    self.te.reg_enum_member(f"{n.name}_{m.name}", f"{n.name}_{m.name}")
            elif isinstance(n, FunctionDecl):
                ret = n.return_type or 'void'
                self.te.reg_fn(n.name, ret)
                params_c = ', '.join(
                    f"{c_type(pt)} {pn}" for pt, pn in n.params
                ) or 'void'
                self._fwd_decls.append(f"{c_type(ret)} {n.name}({params_c});")
            elif isinstance(n, ConstDecl):
                self.te.set(n.name, n.var_type)
            elif isinstance(n, VarDecl):
                # 11.1 module state. Registered in the FIRST pass because a
                # function declared above the variable may still refer to it —
                # declaration order constrains initialisation, not visibility.
                self.te.set(n.name, n.var_type)

    def _assemble(self) -> str:
        lines = [
            "/* ================================================",
            " * [PYRO] Compiled from Cryo -> Native C  (v0.3)",
            " * Compile: gcc -O2 file.pyro cryo_runtime.c -lm -o program",
            " * ================================================ */",
            "",
            '#include "cryo_runtime.h"',
            "",
        ]
        if self._type_decls:
            lines += ["/* -- Tipos -- */", ""] + self._type_decls + [""]
        if self._fwd_decls:
            lines += ["/* -- Prototipos -- */", ""] + self._fwd_decls + [""]
        if self._global_decls:
            lines += ["/* -- Globais -- */", ""] + self._global_decls + [""]
        if self._fn_defs:
            lines += ["/* -- Funcoes -- */", ""] + self._fn_defs + [""]
        lines += [
            "/* -- Entrada principal -- */",
            "int main(void) {",
        ] + self._main_stmts + [
            "    return 0;",
            "}",
            "",
        ]
        return '\n'.join(lines)

    # ── statements ──────────────────────────────────────────

    def _gen(self, node: Node):
        if   isinstance(node, StructDecl):         self._struct(node)
        elif isinstance(node, EnumDecl):            self._enum(node)
        elif isinstance(node, FunctionDecl):        self._fn(node)
        elif isinstance(node, VarDecl):             self._var(node)
        elif isinstance(node, ConstDecl):           self._const(node)
        elif isinstance(node, Assignment):          self._assign(node)
        elif isinstance(node, CompoundAssignment):  self._compound(node)
        elif isinstance(node, Increment):           self._incr(node)
        elif isinstance(node, Return):              self._return(node)
        elif isinstance(node, If):                  self._if(node)
        elif isinstance(node, While):               self._while(node)
        elif isinstance(node, DoWhile):             self._do_while(node)
        elif isinstance(node, For):                 self._for(node)
        elif isinstance(node, ForEach):             self._foreach(node)
        elif isinstance(node, TryCatch):            self._try(node)
        elif isinstance(node, Break):               self._break(node)
        elif isinstance(node, Continue):            self._continue(node)
        elif isinstance(node, Switch):              self._switch(node)
        elif isinstance(node, Assert):              self._assert(node)
        elif isinstance(node, SafetyBlock):         self._safety(node)
        elif isinstance(node, Block):               self._block(node)
        elif isinstance(node, Import):              self._import(node)
        elif isinstance(node, Library):             self._library(node)
        elif isinstance(node, ForeignBlock):        self._foreign(node)
        elif isinstance(node, IndexAssignment):
            self._index_assign(node)
        elif isinstance(node, (MapLiteral, CastExpr, UnwrapExpr, MatchStatement)):
            raise CodeGenError(
                f"'{type(node).__name__}' (map/JSON/optional/match) is not yet "
                f"supported in C backend; use --backend go.")
        elif isinstance(node, SkillDecl):
            raise CodeGenError(
                "'skill' declaration is part of the Pyro layer and only exists in the "
                "backend Go; use --backend go.")
        elif isinstance(node, (CallExpr, MethodCallExpr)):
            self._emit(self._expr(node) + ';')
        else:
            self._emit(f"/* UNSUPPORTED: {type(node).__name__} */")

    def _struct(self, n: StructDecl):
        self._emit(f"typedef struct {{")
        for f in n.fields:
            self._emit(f"    {c_type(f.field_type)} {f.name};")
        self._emit(f"}} {n.name};")
        self._emit()

    def _enum(self, n: EnumDecl):
        has_data = any(len(m.fields) > 0 for m in n.members)
        if has_data:
            raise CodeGenError(
                "Enums with data (Algebraic Data Types) are not supported "
                "in C backend; use --backend go or node.")
        members = ', '.join(f"{n.name}_{m.name}" for m in n.members)
        self._emit(f"typedef enum {{ {members} }} {n.name};")
        self._emit()

    def _fn(self, n: FunctionDecl):
        ret = c_type(n.return_type or 'void')
        params = ', '.join(
            f"{c_type(pt)} {pn}" for pt, pn in n.params
        ) or 'void'
        self._emit(f"{ret} {n.name}({params}) {{")
        self._indent = 1
        self.te.push()
        for pt, pn in n.params:
            self.te.set(pn, pt)
        for s in n.body:
            self._gen(s)
        self.te.pop()
        self._indent = 0
        self._emit("}")
        self._emit()

    def _var(self, n: VarDecl):
        self.te.set(n.name, n.var_type)
        t = c_type(n.var_type)
        if isinstance(n.value, ArrayLiteral):
            et = elem_type(n.var_type)
            self._emit(f"{t} {n.name} = cryo_array_new();")
            for elem in n.value.elements:
                fn = _PUSH_FN.get(et, 'cryo_array_push')
                self._emit(f"{fn}({n.name}, {self._expr(elem)});")
        elif is_map(n.var_type) and (n.value is None
                                     or isinstance(n.value, MapLiteral)):
            # 11.27 — a map literal is built with statements, like an array
            # literal: C has no expression that constructs one.
            kt, vt = map_kv(n.var_type)
            self._emit(f"{t} {n.name} = cryo_map_new({1 if kt == 'string' else 0});")
            for k, v in (getattr(n.value, 'pairs', None) or []):
                self._emit(f"cryo_map_set({n.name}, {_U64_TO[kt]}({self._expr(k)}), "
                           f"{_U64_TO[vt]}({self._expr(v)}));")
        elif n.value is not None:
            self._emit(f"{t} {n.name} = {self._opt_value(n.value, n.var_type)};")
        else:
            # An optional with no initialiser is null, not uninitialised: a
            # dangling pointer here would be read as "some value".
            init = ' = NULL' if is_optional(n.var_type) else ''
            self._emit(f"{t} {n.name}{init};")

    def _module_var(self, n: VarDecl):
        """A top-level `var` is MODULE STATE (11.1), not a local of main.

        It used to be emitted inside main() like any other statement, so a
        function that referred to it produced C that would not compile —
        "'counter' undeclared". The declaration goes to file scope and the
        INITIALISER stays in main, in source order, because an initialiser may
        call a function or build an array and neither can run before main.

        Found by 12.4's generated capability matrix, which is the point of
        generating it: the hand-written table had claimed this worked.
        """
        self.te.set(n.name, n.var_type)
        t = c_type(n.var_type)
        self._global_decls.append(f"static {t} {n.name};")

        if n.value is None:
            return
        prev_cur, prev_indent = self._cur, self._indent
        self._cur, self._indent = self._main_stmts, 1
        if isinstance(n.value, ArrayLiteral):
            et = elem_type(n.var_type)
            self._emit(f"{n.name} = cryo_array_new();")
            for elem in n.value.elements:
                fn = _PUSH_FN.get(et, 'cryo_array_push')
                self._emit(f"{fn}({n.name}, {self._expr(elem)});")
        elif is_map(n.var_type):
            kt, vt = map_kv(n.var_type)
            self._emit(f"{n.name} = cryo_map_new({1 if kt == 'string' else 0});")
            for k, v in (getattr(n.value, 'pairs', None) or []):
                self._emit(f"cryo_map_set({n.name}, {_U64_TO[kt]}({self._expr(k)}), "
                           f"{_U64_TO[vt]}({self._expr(v)}));")
        else:
            self._emit(f"{n.name} = {self._opt_value(n.value, n.var_type)};")
        self._cur, self._indent = prev_cur, prev_indent

    def _const(self, n: ConstDecl):
        self.te.set(n.name, n.var_type)
        t = c_type(n.var_type)
        self._emit(f"static const {t} {n.name} = {self._expr(n.value)};")

    def _index_assign(self, n: IndexAssignment):
        """`m[k] = v` for a map, `a[i] = v` for an array (11.27)."""
        ot = self.te.infer(n.obj)
        obj = self._expr(n.obj)
        if is_map(ot):
            kt, vt = map_kv(ot)
            self._emit(f"cryo_map_set({obj}, {_U64_TO[kt]}({self._expr(n.index)}), "
                       f"{_U64_TO[vt]}({self._expr(n.value)}));")
            return
        et = elem_type(ot)
        fn = _SET_FN.get(et, 'cryo_array_set')
        self._emit(f"{fn}({obj}, {self._expr(n.index)}, {self._expr(n.value)});")

    def _assign(self, n: Assignment):
        self._emit(f"{n.name} = {self._expr(n.value)};")

    def _compound(self, n: CompoundAssignment):
        self._emit(f"{n.name} {n.op} {self._expr(n.value)};")

    def _incr(self, n: Increment):
        self._emit(f"{n.name}{n.op};")

    def _return(self, n: Return):
        if n.value is None:
            self._emit("return;")
        else:
            self._emit(f"return {self._expr(n.value)};")

    def _if(self, n: If):
        self._emit(f"if ({self._expr(n.condition)}) {{")
        self._indent += 1
        self.te.push()
        for s in n.then_body: self._gen(s)
        self.te.pop()
        self._indent -= 1
        if n.else_body:
            # elif chaining
            if len(n.else_body) == 1 and isinstance(n.else_body[0], If):
                inner = n.else_body[0]
                self._emit(f"}} else if ({self._expr(inner.condition)}) {{")
                self._indent += 1
                self.te.push()
                for s in inner.then_body: self._gen(s)
                self.te.pop()
                self._indent -= 1
                if inner.else_body:
                    self._emit("} else {")
                    self._indent += 1
                    self.te.push()
                    for s in inner.else_body: self._gen(s)
                    self.te.pop()
                    self._indent -= 1
                self._emit("}")
            else:
                self._emit("} else {")
                self._indent += 1
                self.te.push()
                for s in n.else_body: self._gen(s)
                self.te.pop()
                self._indent -= 1
                self._emit("}")
        else:
            self._emit("}")

    def _while(self, n: While):
        self._emit(f"while ({self._expr(n.condition)}) {{")
        self._indent += 1
        self.te.push()
        self._loop_depth += 1
        for s in n.body: self._gen(s)
        self._loop_depth -= 1
        self.te.pop()
        self._indent -= 1
        self._emit("}")

    def _for(self, n: For):
        init_s = self._for_part(n.init)   if n.init      else ''
        cond_s = self._expr(n.condition)  if n.condition  else '1'
        upd_s  = self._for_part(n.update) if n.update     else ''
        self._emit(f"for ({init_s}; {cond_s}; {upd_s}) {{")
        self._indent += 1
        self.te.push()
        self._loop_depth += 1
        for s in n.body: self._gen(s)
        self._loop_depth -= 1
        self.te.pop()
        self._indent -= 1
        self._emit("}")

    def _do_while(self, n: DoWhile):
        self._emit("do {")
        self._indent += 1
        self.te.push()
        self._loop_depth += 1
        for s in n.body: self._gen(s)
        self._loop_depth -= 1
        self.te.pop()
        self._indent -= 1
        self._emit(f"}} while ({self._expr(n.condition)});")

    def _foreach(self, n: ForEach):
        idx = f"_fe{self._fe}"; self._fe += 1
        arr = self._expr(n.iterable)
        et  = n.var_type
        get = _GET_FN.get(et, 'cryo_array_get')
        self._emit(f"for (int64_t {idx} = 0; {idx} < ({arr})->length; {idx}++) {{")
        self._indent += 1
        self.te.push()
        self.te.set(n.var_name, et)
        self._emit(f"{c_type(et)} {n.var_name} = {get}({arr}, {idx});")
        self._loop_depth += 1
        for s in n.body: self._gen(s)
        self._loop_depth -= 1
        self.te.pop()
        self._indent -= 1
        self._emit("}")

    def _break(self, n: Break):
        if self._loop_depth == 0:
            raise CodeGenError("'break' out of a loop")
        self._emit("break;")

    def _continue(self, n: Continue):
        if self._loop_depth == 0:
            raise CodeGenError("'continue' out of a loop")
        self._emit("continue;")

    def _switch(self, n: Switch):
        sub_t = self.te.infer(n.subject)
        # switch on string does not exist in C: unfold into chained if/else.
        if sub_t == 'string':
            self._switch_as_if(n)
            return
        self._emit(f"switch ({self._expr(n.subject)}) {{")
        self._indent += 1
        # each case inside a switch breaks by default (no implicit fall-through)
        self._loop_depth += 1  # allows 'break' inside case
        for case in n.cases:
            for v in case.values:
                self._emit(f"case {self._expr(v)}:")
            self._indent += 1
            self.te.push()
            for s in case.body: self._gen(s)
            self.te.pop()
            if not self._terminates(case.body):
                self._emit("break;")
            self._indent -= 1
        if n.default_body is not None:
            self._emit("default:")
            self._indent += 1
            self.te.push()
            for s in n.default_body: self._gen(s)
            self.te.pop()
            if not self._terminates(n.default_body):
                self._emit("break;")
            self._indent -= 1
        self._loop_depth -= 1
        self._indent -= 1
        self._emit("}")

    @staticmethod
    def _terminates(body: List[Node]) -> bool:
        """True if the block ends with return/break/continue (no fall-through)."""
        return bool(body) and isinstance(body[-1], (Return, Break, Continue))

    def _switch_as_if(self, n: Switch):
        subj = self._expr(n.subject)
        first = True
        for case in n.cases:
            conds = ' || '.join(f"cryo_str_eq({subj}, {self._expr(v)})"
                                for v in case.values)
            kw = 'if' if first else '} else if'
            self._emit(f"{kw} ({conds}) {{")
            self._indent += 1
            self.te.push()
            for s in case.body: self._gen(s)
            self.te.pop()
            self._indent -= 1
            first = False
        if n.default_body is not None:
            self._emit("} else {" if not first else "if (1) {")
            self._indent += 1
            self.te.push()
            for s in n.default_body: self._gen(s)
            self.te.pop()
            self._indent -= 1
        if not first or n.default_body is not None:
            self._emit("}")

    def _assert(self, n: Assert):
        cond = self._expr(n.condition)
        if n.message is not None:
            msg = self._expr(n.message)
        else:
            msg = f'"assert failed (line {n.line})"'
        # 12.12 — lazy, for the same reason as go: C evaluates arguments
        # eagerly, so the message was built even when the assertion held.
        # cryo_assert still does the failing — it longjmps through CRYO_TRY so
        # an assert stays catchable (12.1) — it is just no longer reached.
        self._emit(f"if (!({cond})) {{ cryo_assert(false, {msg}); }}")

    def _block(self, n: Block):
        """A plain lexical scope: `{ ... }` and a new name scope.

        Block was routed to _safety, which reads `n.safe` — an attribute a
        plain Block does not have, so `for (int i, string s in enumerate(a))`
        on --backend c aborted with a Python AttributeError instead of
        compiling. Every front-end desugaring that introduces a scope emits one
        of these: enumerate, pairs, the 11.5 map form, comprehensions and
        11.17's stream loop, so the C backend could not compile any of them.

        A Block carries NO safety meaning, so it must not touch _safe_stack —
        doing that would silently change whether the code inside it is
        instrumented.
        """
        self._emit("{")
        self._indent += 1
        self.te.push()
        for s in n.body:
            self._gen(s)
        self.te.pop()
        self._indent -= 1
        self._emit("}")

    def _safety(self, n: SafetyBlock):
        tag = 'safe' if n.safe else 'unsafe'
        self._emit(f"{{  /* [CRYO] bloco {tag} */")
        self._indent += 1
        self._safe_stack.append(n.safe)
        self.te.push()
        for s in n.body: self._gen(s)
        self.te.pop()
        self._safe_stack.pop()
        self._indent -= 1
        self._emit("}")

    def _for_part(self, node: Node) -> str:
        if isinstance(node, VarDecl):
            self.te.set(node.name, node.var_type)
            val = self._expr(node.value) if node.value else '0'
            return f"{c_type(node.var_type)} {node.name} = {val}"
        if isinstance(node, Assignment):
            return f"{node.name} = {self._expr(node.value)}"
        if isinstance(node, CompoundAssignment):
            return f"{node.name} {node.op} {self._expr(node.value)}"
        if isinstance(node, Increment):
            return f"{node.name}{node.op}"
        return self._expr(node)

    def _try(self, n: TryCatch):
        # CRYO_TRY macro opens:  if (!setjmp(...)) { active=true;
        self._emit("CRYO_TRY")
        self._indent += 1
        self.te.push()
        for s in n.try_body: self._gen(s)
        self.te.pop()
        self._indent -= 1
        if n.catch_body is not None:
            var = n.catch_name or '_cryo_err'
            self._emit(f"CRYO_CATCH({var})")
            self._indent += 1
            self.te.push()
            self.te.set(var, 'string')
            for s in n.catch_body: self._gen(s)
            self.te.pop()
            self._indent -= 1
        self._emit("CRYO_END_CATCH")
        # Finally: always emitted after try/catch block
        if n.finally_body:
            self._emit("CRYO_FINALLY {")
            self._indent += 1
            self.te.push()
            for s in n.finally_body: self._gen(s)
            self.te.pop()
            self._indent -= 1
            self._emit("}")

    def _import(self, n: Import):
        self._emit(f"/* [CRYO] import >{n.lang}< */")

    def _library(self, n: Library):
        # only includes the library if it belongs to the C language
        lang = resolve_library_lang(n, getattr(self, "_imported_langs", set()))
        if lang == 'c':
            lib = n.name.lower()
            self._emit(f'#include <{lib}.h>  /* [CRYO] library >c {n.name}< */')
        else:
            self._emit(f'/* [CRYO] library >{n.name}< (language {lang or "?"}) '
                       f'ignored in C backend */')

    def _foreign(self, n: ForeignBlock):
        if n.lang.lower() == 'c':
            self._emit("/* -- [C block] -- */")
            for line in n.code.strip().split('\n'):
                self._emit(line.rstrip())
            self._emit("/* -- [/C block] -- */")
        else:
            self._emit(f"/* [CRYO] >{n.lang}< block ignored in C backend */")

    # ── expressions ──────────────────────────────────────────

    def _expr(self, node: Node) -> str:
        if isinstance(node, UnwrapExpr):
            return self._unwrap(node)
        if isinstance(node, (SpawnExpr, AwaitExpr)):
            # Named separately from the rest: the suggestion below used to list
            # every other backend, and for concurrency two of the three refuse
            # it as well — a message that sends the reader somewhere it also
            # does not work is worse than no suggestion.
            raise CodeGenError(
                f"concurrency (spawn/await) is not supported in the C backend; "
                f"use --backend go or pyro.")
        if isinstance(node, (MapLiteral, CastExpr, TryExpr)):
            raise CodeGenError(
                f"'{type(node).__name__}' (map/JSON/'?' propagation) "
                f"is not yet supported in the C backend; use --backend go, node "
                f"or pyro.")
        if isinstance(node, Literal):
            if node.kind == 'null':   return 'NULL'
            if node.kind == 'bool':   return 'true' if node.value else 'false'
            if node.kind == 'string': return _c_string(node.value)
            if node.kind == 'int':    return str(node.value)
            if node.kind == 'float':  return repr(float(node.value))
            return str(node.value)

        if isinstance(node, Identifier):
            # 12.9 — a bare enum member resolves to the declared constant, but
            # a variable of the same name still shadows it (TypeEnv.get returns
            # 'unknown' only for names nothing has declared).
            if self.te.get(node.name) == 'unknown':
                q = self.te.enum_member(node.name)
                if q:
                    return q
            return node.name

        if isinstance(node, BinaryExpr):
            return self._binary(node)

        if isinstance(node, UnaryExpr):
            op = '!' if node.op == '!' else node.op
            return f"({op}{self._expr(node.operand)})"

        if isinstance(node, TernaryExpr):
            return (f"({self._expr(node.condition)} ? "
                    f"{self._expr(node.then_value)} : "
                    f"{self._expr(node.else_value)})")

        if isinstance(node, CallExpr):
            return self._call(node)

        if isinstance(node, MethodCallExpr):
            return self._method(node)

        if isinstance(node, FieldAccess):
            # 12.9 — `E.A` is a qualified enum member, not a field read. A
            # variable of the same name still wins, so a struct called `E` with
            # a field `A` keeps reading its field.
            if (isinstance(node.obj, Identifier)
                    and self.te.is_enum(node.obj.name)
                    and self.te.get(node.obj.name) == 'unknown'):
                q = self.te.enum_member(f"{node.obj.name}_{node.field}")
                if q:
                    return q
            obj = self._expr(node.obj)
            ot  = self.te.infer(node.obj)
            if node.field == 'length':
                # array length
                return f"(({obj})->length)"
            # struct field: pointer or value?
            return f"{obj}.{node.field}"

        if isinstance(node, IndexAccess):
            obj = self._expr(node.obj)
            idx = self._expr(node.index)
            at  = self.te.infer(node.obj)
            if is_map(at):
                kt, vt = map_kv(at)
                return (f"{_U64_FROM[vt]}(cryo_map_get({obj}, "
                        f"{_U64_TO[kt]}({idx})))")
            et  = elem_type(at)
            fn  = _GET_FN.get(et, 'cryo_array_get')
            return f"{fn}({obj}, {idx})"

        if isinstance(node, ArrayLiteral):
            return "/* inline-array */"

        if isinstance(node, StructInit):
            # C99 compound literal: (TypeName){ .field = val, ... }
            fields = ', '.join(f".{k} = {self._expr(v)}" for k, v in node.fields)
            return f"({node.struct_name}){{{fields}}}"

        return f"/* UNKNOWN_EXPR({type(node).__name__}) */"

    def _binary(self, node: BinaryExpr) -> str:
        lt = self.te.infer(node.left)
        rt = self.te.infer(node.right)
        l  = self._expr(node.left)
        r  = self._expr(node.right)

        if node.op == '&&': return f"({l} && {r})"
        if node.op == '||': return f"({l} || {r})"
        if node.op == '??':
            # A scalar optional is a POINTER, so the present branch has to
            # dereference it; `string?` is already a char* and must not be.
            # Emitted via a statement expression so `l` is evaluated once —
            # the obvious `(l != NULL ? *l : r)` evaluates it twice, which
            # doubles any side effect in it.
            base = opt_base(lt)
            if is_optional(lt) and base in _OPT_WRAP:
                ct = c_type(base)
                return (f"({{ {c_type(lt)} __o = ({l}); "
                        f"__o != NULL ? *__o : ({ct})({r}); }})")
            return f"(({l}) != NULL ? ({l}) : ({r}))"

        # String concatenation
        if node.op == '+' and (lt == 'string' or rt == 'string'):
            ls = l if lt == 'string' else self._to_str(l, lt)
            rs = r if rt == 'string' else self._to_str(r, rt)
            return f"cryo_str_concat({ls}, {rs})"

        # String comparison
        if node.op == '==' and (lt == 'string' or rt == 'string'):
            return f"cryo_str_eq({l}, {r})"
        if node.op == '!=' and (lt == 'string' or rt == 'string'):
            return f"(!cryo_str_eq({l}, {r}))"

        # ── Bitwise operators (integers only) ──
        if node.op in ('&', '|', '^', '<<', '>>'):
            return f"({l} {node.op} {r})"

        # ── Integer security instrumentation ──
        both_int = (lt == 'int' and rt == 'int')
        if self._safe and both_int:
            if node.op == '+': return f"cryo_add_ovf({l}, {r})"
            if node.op == '-': return f"cryo_sub_ovf({l}, {r})"
            if node.op == '*': return f"cryo_mul_ovf({l}, {r})"
            if node.op == '/': return f"cryo_idiv_chk({l}, {r})"
            if node.op == '%': return f"cryo_imod_chk({l}, {r})"
        # Division/modulo by zero: always protected on integers
        elif both_int and node.op in ('/', '%'):
            fn = 'cryo_idiv_chk' if node.op == '/' else 'cryo_imod_chk'
            return f"{fn}({l}, {r})"

        return f"({l} {node.op} {r})"

    def _call(self, node: CallExpr) -> str:
        callee = node.callee
        args   = node.args

        # resources only for Go/Node/Pyro backends
        if callee.startswith('pyro_') or callee in (
                'skills', 'skill_get', 'skill_has', 'skills_json', 'json_encode',
                'http_get', 'http_post', 'sleep',
                'schema_of', 'llm', 'tools', 'tool_get', 'tools_json', 'agent',
                'clamp', 'sign', 'gcd', 'hypot', 'starts_with', 'ends_with', 'repeat',
                'pad_start', 'pad_end'):
            raise CodeGenError(
                f"'{callee}()' is not supported in the C backend; "
                f"use --backend go, node or pyro.")
        # 11.27 — the map builtins. Each needs the KEY type to convert the
        # key into the uint64_t the runtime stores, and the generator is the
        # only place that knows it.
        if callee in ('remove', 'has', 'keys') and args:
            mt = self.te.infer(args[0])
            if not is_map(mt):
                raise CodeGenError(
                    f"'{callee}()' expects a map; this is '{mt}'.")
            kt, _vt = map_kv(mt)
            m = self._expr(args[0])
            if callee == 'keys':
                return f"cryo_map_keys({m})"
            k = f"{_U64_TO[kt]}({self._expr(args[1])})"
            if callee == 'has':
                return f"cryo_map_has({m}, {k})"
            return f"cryo_map_remove({m}, {k})"

        # ── built-ins ──
        if callee == 'print':
            return self._gen_print(args)
        if callee == 'sqrt':
            return f"cryo_sqrt({self._expr(args[0])})"
        if callee == 'pow':
            return f"cryo_pow({self._expr(args[0])}, {self._expr(args[1])})"
        if callee in ('abs', 'fabs'):
            t = self.te.infer(args[0])
            fn = 'cryo_abs_f' if t == 'number' else 'cryo_abs_i'
            return f"{fn}({self._expr(args[0])})"
        if callee in ('min', 'max') and len(args) == 2:
            t = self.te.infer(args[0])
            suf = 'i' if t == 'int' else 'f'
            return f"cryo_{callee}_{suf}({self._expr(args[0])}, {self._expr(args[1])})"
        # ── Phase 10.4 stdlib math (ISSUES/09) ──
        if callee == 'clamp' and len(args) == 3:
            # keeps the argument's type: int only when ALL three are int, so the
            # result matches what the Pyro VM computes for the same call
            ts = [self.te.infer(a) for a in args]
            suf = 'i' if all(t == 'int' for t in ts) else 'f'
            a = ', '.join(self._expr(x) for x in args)
            return f"cryo_clamp_{suf}({a})"
        if callee == 'sign' and len(args) == 1:
            suf = 'i' if self.te.infer(args[0]) == 'int' else 'f'
            return f"cryo_sign_{suf}({self._expr(args[0])})"
        if callee == 'gcd' and len(args) == 2:
            return f"cryo_gcd({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'hypot' and len(args) == 2:
            return f"cryo_hypot({self._expr(args[0])}, {self._expr(args[1])})"
        # ── Phase 10.4 strings (ISSUES/09) ──
        if callee == 'upper' and len(args) == 1:
            return f"cryo_str_upper({self._expr(args[0])})"
        if callee == 'lower' and len(args) == 1:
            return f"cryo_str_lower({self._expr(args[0])})"
        if callee == 'trim' and len(args) == 1:
            return f"cryo_str_trim({self._expr(args[0])})"
        if callee == 'contains' and len(args) == 2:
            return f"cryo_str_contains({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'find' and len(args) == 2:
            return f"cryo_str_find({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'starts_with' and len(args) == 2:
            return f"cryo_str_starts_with({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'ends_with' and len(args) == 2:
            return f"cryo_str_ends_with({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'repeat' and len(args) == 2:
            return f"cryo_str_repeat({self._expr(args[0])}, {self._expr(args[1])})"
        if callee in ('pad_start', 'pad_end') and len(args) == 3:
            fn = 'cryo_str_pad_start' if callee == 'pad_start' else 'cryo_str_pad_end'
            return (f"{fn}({self._expr(args[0])}, {self._expr(args[1])}, "
                    f"{self._expr(args[2])})")
        # ── Phase 10.2 collection ops (ISSUES/09) ──
        # CryoArray is untyped, so equality/ordering/sum need the ELEMENT type.
        if callee in ('sort', 'reverse', 'slice', 'index_of', 'concat', 'count', 'sum'):
            def _elem_suffix(arr_node):
                et = elem_type(self.te.infer(arr_node))
                if et == 'string': return 's'
                if et == 'number': return 'f'
                if et == 'int':    return 'i'
                raise CodeGenError(
                    f"'{callee}()' needs a typed array in the C backend "
                    f"(element type is '{et}'); use --backend go, node or pyro.")
            if callee == 'reverse' and len(args) == 1:
                return f"cryo_array_reverse({self._expr(args[0])})"
            if callee == 'concat' and len(args) == 2:
                return f"cryo_array_concat({self._expr(args[0])}, {self._expr(args[1])})"
            if callee == 'slice' and len(args) == 3:
                # slice() is polymorphic over array|string (10.9); C is not, so
                # dispatch on the inferred operand type. Both helpers already
                # clamp out-of-range bounds the same way the VM does.
                fn = ('cryo_str_slice' if self.te.infer(args[0]) == 'string'
                      else 'cryo_array_slice')
                return (f"{fn}({self._expr(args[0])}, "
                        f"{self._expr(args[1])}, {self._expr(args[2])})")
            if callee == 'sort' and len(args) == 1:
                return f"cryo_sort_{_elem_suffix(args[0])}({self._expr(args[0])})"
            if callee == 'sum' and len(args) == 1:
                suf = _elem_suffix(args[0])
                if suf == 's':
                    raise CodeGenError("'sum()' needs a numeric array")
                return f"cryo_sum_{suf}({self._expr(args[0])})"
            if callee in ('count', 'index_of') and len(args) == 2:
                fn = 'cryo_count' if callee == 'count' else 'cryo_index_of'
                return (f"{fn}_{_elem_suffix(args[0])}({self._expr(args[0])}, "
                        f"{self._expr(args[1])})")
        if callee == 'replace' and len(args) == 3:
            a = ', '.join(self._expr(x) for x in args)
            return f"cryo_str_replace({a})"
        if callee == 'split' and len(args) == 2:
            return f"cryo_str_split({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'join' and len(args) == 2:
            return f"cryo_str_join({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'substr' and len(args) == 3:
            # Cryo substr(s, start, n) takes a LENGTH; cryo_str_slice takes an
            # END offset — pass start+n, and the helper clamps.
            s, st, n = (self._expr(a) for a in args)
            return f"cryo_str_slice({s}, {st}, ({st}) + ({n}))"
        if callee == 'floor':
            return f"cryo_floor({self._expr(args[0])})"
        if callee == 'ceil':
            return f"cryo_ceil({self._expr(args[0])})"
        if callee == 'round':
            return f"cryo_round({self._expr(args[0])})"
        if callee == 'to_string':
            a = args[0]
            return self._to_str(self._expr(a), self.te.infer(a))
        # to_int/to_number are typed conversions here, not one runtime call.
        # cryo_to_num takes int64_t and cryo_to_int takes double, so passing an
        # argument of the other kind used to convert SILENTLY through the
        # parameter type: to_number(3.14159) truncated to 3.0, losing the
        # fraction with nothing to indicate it. A string argument was worse —
        # the pointer itself was read as a number — and the C runtime has no
        # parse helper to call instead, so that one is refused rather than
        # emitted wrong. (go emits float64()/int64() and never had this.)
        if callee in ('to_int', 'to_number'):
            at = self.te.infer(args[0])
            inner = self._expr(args[0])
            if at == 'string':
                raise CodeGenError(
                    f"'{callee}()' on a string is not supported in the C "
                    f"backend; use --backend go, node or pyro.")
            if callee == 'to_int':
                return inner if at == 'int' else f"cryo_to_int({inner})"
            return f"((double)({inner}))" if at == 'number' else f"cryo_to_num({inner})"
        if callee == 'len':
            a = args[0]
            t = self.te.infer(a)
            if t == 'string':
                return f"cryo_str_len({self._expr(a)})"
            if is_map(t):
                return f"cryo_map_len({self._expr(a)})"
            return f"(({self._expr(a)})->length)"
        if callee == 'input':
            prompt = self._expr(args[0]) if args else '""'
            return f"cryo_input({prompt})"
        if callee == 'throw':
            return f"CRYO_THROW({self._expr(args[0])})"
        # ── Filesystem & process natives (Roadmap 11.7) ──
        if callee == 'file_exists' and len(args) == 1:
            return f"cryo_file_exists({self._expr(args[0])})"
        if callee == 'is_dir' and len(args) == 1:
            return f"cryo_is_dir({self._expr(args[0])})"
        if callee == 'list_dir' and len(args) == 1:
            return f"cryo_list_dir({self._expr(args[0])})"
        if callee == 'make_dir' and len(args) == 1:
            return f"cryo_make_dir({self._expr(args[0])})"
        if callee == 'delete_file' and len(args) == 1:
            return f"cryo_delete_file({self._expr(args[0])})"
        if callee == 'file_size' and len(args) == 1:
            return f"cryo_file_size({self._expr(args[0])})"
        if callee == 'write_file' and len(args) == 2:
            return f"cryo_write_file({self._expr(args[0])}, {self._expr(args[1])})"
        if callee == 'read_file' and len(args) == 1:
            return f"cryo_read_file({self._expr(args[0])})"
        if callee == 'env' and len(args) == 1:
            return f"cryo_env({self._expr(args[0])})"
        if callee == 'exec' and len(args) == 1:
            return f"cryo_exec({self._expr(args[0])})"

        # User-defined function
        args_str = ', '.join(self._expr(a) for a in args)
        return f"{callee}({args_str})"

    def _gen_print(self, args: List[Node]) -> str:
        if not args:
            return 'cryo_print_newline()'
        arg = args[0]
        typ = self.te.infer(arg)
        e   = self._expr(arg)
        if typ == 'string': return f'cryo_print_str({e})'
        if typ == 'int':    return f'cryo_print_i64({e})'
        if typ == 'number': return f'cryo_print_f64({e})'
        if typ == 'bool':   return f'cryo_print_bool({e})'
        # fallback: converts to string
        return f'cryo_print_str({self._to_str(e, typ)})'

    def _method(self, node: MethodCallExpr) -> str:
        obj  = self._expr(node.obj)
        at   = self.te.infer(node.obj)
        et   = elem_type(at)
        args = [self._expr(a) for a in node.args]
        m    = node.method

        if m == 'push':
            fn = _PUSH_FN.get(et, 'cryo_array_push')
            return f"{fn}({obj}, {args[0] if args else '0'})"
        if m in ('length', 'size'):
            return f"(({obj})->length)"
        if m == 'pop_last':
            fn = _GET_FN.get(et, 'cryo_array_get')
            return f"{fn}({obj}, ({obj})->length - 1)"
        if m == 'slice':
            s, e = (args + ['0', '0'])[:2]
            return f"cryo_array_slice({obj}, {s}, {e})"
        if m == 'upper':
            return f"cryo_str_upper({obj})"
        if m == 'lower':
            return f"cryo_str_lower({obj})"
        if m == 'contains':
            empty = '""'
            arg0 = args[0] if args else empty
            return f"cryo_str_contains({obj}, {arg0})"

        args_str = ', '.join(args)
        return f"{obj}.{m}({args_str})"

    _ARR_TO_STR = {'int': 'cryo_arr_to_str_i', 'number': 'cryo_arr_to_str_f',
                   'string': 'cryo_arr_to_str_s', 'bool': 'cryo_arr_to_str_b'}

    def _opt_value(self, value: Node, declared: str) -> str:
        """A value on its way into a slot of type `declared` (11.27).

        A plain `3` going into an `int?` has to be put somewhere with an
        address. `null` and something already optional pass straight through —
        wrapping those would box a pointer inside another pointer.
        """
        src = self._expr(value)
        if not is_optional(declared):
            return src
        if isinstance(value, Literal) and value.kind == 'null':
            return 'NULL'
        if is_optional(self.te.infer(value)):
            return src
        wrap = _OPT_WRAP.get(opt_base(declared))
        return f"{wrap}({src})" if wrap else src

    def _unwrap(self, node) -> str:
        """`x!` — the value, or abort if it is null."""
        inner = getattr(node, 'operand', None) or getattr(node, 'expr', None)
        t = self.te.infer(inner)
        fn = _OPT_UNWRAP.get(opt_base(t))
        if not fn:
            raise CodeGenError(
                f"'!' (unwrap) needs an optional; this is '{t}'. Only int?, "
                f"number?, bool? and string? are supported in the C backend.")
        return f"{fn}({self._expr(inner)})"

    def _to_str(self, expr: str, typ: str) -> str:
        if typ == 'string': return expr
        if typ == 'int':    return f"cryo_i64_to_str({expr})"
        if typ == 'number': return f"cryo_f64_to_str({expr})"
        if typ == 'bool':   return f"cryo_bool_to_str({expr})"
        # 11.27 — printing an array. CryoArray holds raw uint64_t and does not
        # know what is in it, so the ELEMENT TYPE picks the function; the
        # generator is the only place that knows it.
        if is_map(typ):
            _kt, vt = map_kv(typ)
            if vt in _VAL_KIND:
                return f"cryo_map_to_str({expr}, {_VAL_KIND[vt]})"
            raise CodeGenError(
                f"printing a map with '{vt}' values is not supported in the C "
                f"backend; use --backend go, node or pyro.")
        if typ and typ.endswith('[]'):
            el = typ[:-2]
            fn = self._ARR_TO_STR.get(el)
            if fn:
                return f"{fn}({expr})"
            if el.endswith('[]'):
                raise CodeGenError(
                    f"printing a nested array ('{typ}') is not supported in the C "
                    f"backend; print the inner arrays, or use --backend go, node "
                    f"or pyro.")
            raise CodeGenError(
                f"printing an array of '{el}' is not supported in the C backend; "
                f"use --backend go, node or pyro.")
        raise CodeGenError(f"cannot convert type '{typ}' to string in C backend")
