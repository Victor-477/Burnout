# ============================================================
#  Cryo Compiler — C++ Code Generator
#  .cryo  ->  .cpp  (C++14, buildable with g++/clang++/MSVC)
#
#  Sibling of codegen_csharp.py rather than of codegen_c.py, and
#  deliberately so: C++ has containers, so this backend does not need
#  the hand-written value model cryo_runtime.c exists for, and the
#  shape that worked for C# carries over almost line for line.
#
#  Three things C++ forces that C# did not:
#
#    * REFERENCE SEMANTICS have to be built. PYRO_RUNTIME.md §2 makes
#      arrays, maps and structs shared objects; a bare std::vector
#      copies on assignment, so `int[] b = a; b.push(1);` would leave
#      `a` alone here and change it everywhere else. Every container
#      is a shared_ptr, which is why the emitted code is full of them.
#    * NULLABLE is shared_ptr, not std::optional — the oldest
#      toolchain this targets is C++14, and it matches how the go
#      backend represents `T?` anyway.
#    * DECLARATION ORDER matters. C++ needs a declaration before a
#      use, so every function is forward-declared in a first pass.
#
#  Unsupported constructs are REFUSED here rather than emitted for
#  the C++ compiler to complain about, because its diagnostics for
#  generated template code are famously unreadable and would be
#  reporting on code the programmer never wrote.
# ============================================================
from ast_nodes import *
from foreign import collect_imports, resolve_library_lang
from typing import List, Dict, Set


class CodeGenCppError(Exception):
    pass


CPP_TYPE: Dict[str, str] = {
    'int':    'int64_t',
    'number': 'double',
    'string': 'std::string',
    'bool':   'bool',
    'void':   'void',
}

_CPP_LANGS = ('c++', 'cpp', 'cxx')

# Registered per generate(). A Cryo enum is an INTEGER value (12.9), and a
# struct name has to become a shared handle rather than a bare value, so
# cpp_type needs to know both — and threading an environment through its forty
# call sites would cost more than a module global the compiler resets.
_ENUM_NAMES: Set[str] = set()
_STRUCT_NAMES: Set[str] = set()


def is_map(t: str) -> bool:
    return bool(t) and t.startswith('map<') and t.endswith('>')


def map_kv(t: str):
    """('string', 'int') for map<string,int>.

    Split on the FIRST top-level comma: a nested map<...> value would otherwise
    be cut in half.
    """
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


def elem_type(t: str) -> str:
    if not t:
        return 'unknown'
    if t.endswith('[]'):
        return t[:-2]
    if is_map(t):
        return map_kv(t)[1]
    return 'unknown'


def _is_null(node, t: str) -> bool:
    """Is this operand the null literal? (ISSUES/18)"""
    return t == 'null' or (isinstance(node, Literal) and node.kind == 'null')


def cpp_type(t: str) -> str:
    if not t:
        return 'auto'
    if t in _ENUM_NAMES:
        return 'int64_t'
    if t in _STRUCT_NAMES:
        # A handle, not a value: two Cryo names for one struct see each other's
        # writes, and a by-value struct would silently stop doing that.
        return f"std::shared_ptr<{t}>"
    if t.startswith('(') and t.endswith(')'):
        return cpp_type(t[1:-1])
    if t.startswith('fn(') and '->' in t:
        raise CodeGenCppError(
            f"function type '{t}' (first-class functions) is not yet supported "
            f"in the C++ backend; use --backend go, node or pyro.")
    if t.startswith('future<'):
        raise CodeGenCppError(
            "concurrency (spawn/await) is not supported in the C++ backend; "
            "use --backend go or pyro.")
    if t == 'any':
        raise CodeGenCppError(
            "the dynamic type 'any' has no C++ representation. It also comes "
            "from constructs that infer it — a comprehension, or a "
            "`for (x in xs)` without a declared type. Give the variable an "
            "explicit type, or use --backend go, node or pyro.")
    if is_optional(t):
        return f"std::shared_ptr<{cpp_type(opt_base(t))}>"
    if is_map(t):
        k, v = map_kv(t)
        return f"cryo::Map<{cpp_type(k)}, {cpp_type(v)}>"
    if t.endswith('[]'):
        return f"cryo::Arr<{cpp_type(t[:-2])}>"
    return CPP_TYPE.get(t, t)


_CPP_ESCAPES = {'\\': '\\\\', '"': '\\"', '\n': '\\n', '\t': '\\t',
                '\r': '\\r', '\0': '\\0'}


def cpp_string(v: str) -> str:
    """A Cryo string as a C++ string literal.

    Interpolating the value verbatim would turn a backslash in the program's
    DATA into an escape in the generated C++, and a quote or newline would
    produce code that does not compile — the compiler reporting on a string the
    programmer never wrote. Bytes outside printable ASCII pass through as
    themselves; the file is written and read as UTF-8.
    """
    out = []
    for ch in v:
        if ch in _CPP_ESCAPES:
            out.append(_CPP_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append('\\%03o' % ord(ch))
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


_CPP_KEYWORDS = {
    'alignas', 'alignof', 'and', 'asm', 'auto', 'bool', 'break', 'case',
    'catch', 'char', 'class', 'const', 'constexpr', 'continue', 'decltype',
    'default', 'delete', 'do', 'double', 'else', 'enum', 'explicit', 'export',
    'extern', 'false', 'float', 'for', 'friend', 'goto', 'if', 'inline', 'int',
    'long', 'mutable', 'namespace', 'new', 'noexcept', 'not', 'nullptr',
    'operator', 'or', 'private', 'protected', 'public', 'register',
    'reinterpret_cast', 'return', 'short', 'signed', 'sizeof', 'static',
    'static_cast', 'struct', 'switch', 'template', 'this', 'throw', 'true',
    'try', 'typedef', 'typeid', 'typename', 'union', 'unsigned', 'using',
    'virtual', 'void', 'volatile', 'while', 'xor',
}


def cppid(name: str) -> str:
    # Suffixed rather than renamed, so the name still reads as the one the
    # programmer wrote.
    return name + '_' if name in _CPP_KEYWORDS else name


class TypeEnv:
    def __init__(self):
        self._scopes: List[Dict[str, str]] = [{}]
        self._fns: Dict[str, str] = {}
        self._structs: Dict[str, Dict[str, str]] = {}
        self._enums: Set[str] = set()
        self._enum_members: Dict[str, str] = {}

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

    def is_struct(self, name: str) -> bool: return name in self._structs

    def struct_field(self, struct: str, field: str) -> str:
        return self._structs.get(struct, {}).get(field, 'unknown')

    def reg_enum(self, name: str): self._enums.add(name)
    def is_enum(self, name: str) -> bool: return name in self._enums

    def reg_enum_member(self, member: str, qualified: str):
        self._enum_members[member] = qualified

    def enum_member(self, name: str):
        return self._enum_members.get(name)

    def infer(self, node) -> str:
        if node is None:
            return 'unknown'
        if isinstance(node, Literal):
            return {'int': 'int', 'float': 'number', 'string': 'string',
                    'bool': 'bool', 'null': 'null'}.get(node.kind, 'unknown')
        if isinstance(node, Identifier):
            t = self.get(node.name)
            if t == 'unknown' and self.enum_member(node.name):
                return 'int'
            return t
        if isinstance(node, BinaryExpr):
            if node.op in ('==', '!=', '<', '>', '<=', '>=', '&&', '||'):
                return 'bool'
            if node.op == '??':
                lt = self.infer(node.left)
                return lt[:-1] if lt.endswith('?') else lt
            lt, rt = self.infer(node.left), self.infer(node.right)
            if lt == 'string' or rt == 'string':
                return 'string'
            if lt == 'number' or rt == 'number':
                return 'number'
            return lt if lt != 'unknown' else rt
        if isinstance(node, UnaryExpr):
            return 'bool' if node.op == '!' else self.infer(node.operand)
        if isinstance(node, UnwrapExpr):
            inner = getattr(node, 'operand', None) or getattr(node, 'expr', None)
            t = self.infer(inner)
            return t[:-1] if t.endswith('?') else t
        if isinstance(node, TernaryExpr):
            t = self.infer(node.then_value)
            return t if t not in ('unknown', 'null') else self.infer(node.else_value)
        if isinstance(node, CallExpr):
            c = node.callee
            if c in ('to_string', 'input', 'upper', 'lower', 'trim', 'substr',
                     'repeat', 'pad_start', 'pad_end', 'replace', 'join'):
                return 'string'
            if c in ('to_int', 'len', 'sign', 'gcd', 'find', 'count',
                     'index_of'):
                return 'int'
            if c in ('has', 'starts_with', 'ends_with', 'contains'):
                return 'bool'
            if c == 'keys' and node.args:
                mt = self.infer(node.args[0])
                return (map_kv(mt)[0] + '[]') if is_map(mt) else 'unknown'
            if c in ('split', 'lines', 'chars'):
                return 'string[]'
            if c in ('sort', 'reverse', 'slice', 'concat') and node.args:
                return self.infer(node.args[0])
            if c == 'sum' and node.args:
                return elem_type(self.infer(node.args[0]))
            if c in ('to_number', 'sqrt', 'pow', 'hypot', 'floor', 'ceil',
                     'round'):
                return 'number'
            if c in ('abs', 'min', 'max'):
                return self.infer(node.args[0]) if node.args else 'number'
            if c == 'clamp':
                ts = [self.infer(a) for a in node.args]
                return 'int' if ts and all(t == 'int' for t in ts) else 'number'
            return self.fn_ret(c)
        if isinstance(node, StructInit):
            return node.struct_name
        if isinstance(node, ArrayLiteral):
            if node.elements:
                et = self.infer(node.elements[0])
                if et not in ('unknown', 'null'):
                    return et + '[]'
            return 'array'
        if isinstance(node, MapLiteral):
            return 'map'
        if isinstance(node, FieldAccess):
            if node.field == 'length':
                return 'int'
            return self.struct_field(self.infer(node.obj), node.field)
        if isinstance(node, IndexAccess):
            at = self.infer(node.obj)
            if is_map(at):
                return map_kv(at)[1]
            if at == 'string':
                return 'string'
            return elem_type(at)
        return 'unknown'


class CodeGenCpp:
    def __init__(self, safe: bool = True):
        self.te = TypeEnv()
        self._indent = 0
        self._types: List[str] = []
        self._fwd: List[str] = []
        self._globals: List[str] = []
        self._fns: List[str] = []
        self._main: List[str] = []
        self._includes: Set[str] = set()
        self._cur: List[str] = self._main
        self._safe_default = safe
        self._safe_stack: List[bool] = []
        self._loop_depth = 0
        self._tmp = 0
        self._imported_langs: Set[str] = set()
        self._plain_enum_member: Dict[str, str] = {}

    @property
    def _safe(self) -> bool:
        return self._safe_stack[-1] if self._safe_stack else self._safe_default

    def _err(self, msg: str):
        raise CodeGenCppError(msg)

    def _pad(self) -> str:
        return '    ' * self._indent

    def _emit(self, line: str = ''):
        self._cur.append(self._pad() + line if line else '')

    def _next_tmp(self) -> str:
        self._tmp += 1
        return f"__cx{self._tmp}"

    # ── entry ────────────────────────────────────────────────

    def generate(self, program: Program) -> str:
        _ENUM_NAMES.clear()
        _STRUCT_NAMES.clear()
        self._imported_langs = collect_imports(program)
        self._pre_scan(program.statements)

        for node in program.statements:
            if isinstance(node, (StructDecl, EnumDecl)):
                self._cur, self._indent = self._types, 0
                self._gen(node)
            elif isinstance(node, FunctionDecl):
                self._cur, self._indent = self._fns, 0
                self._gen(node)
            elif isinstance(node, (ConstDecl, VarDecl)):
                self._module_var(node)
            else:
                self._cur, self._indent = self._main, 1
                self._gen(node)

        return self._assemble()

    def _pre_scan(self, stmts: List[Node]):
        """Register types and forward-declare every function.

        C++ needs a declaration before a use, and Cryo does not — a function
        may call one declared below it. Without this pass the generated file
        compiles only when the program happens to be in dependency order.
        """
        for n in stmts:
            if isinstance(n, StructDecl):
                self.te.reg_struct(n.name, {f.name: f.field_type for f in n.fields})
                _STRUCT_NAMES.add(n.name)
            elif isinstance(n, EnumDecl):
                self.te.reg_enum(n.name)
                _ENUM_NAMES.add(n.name)
                for m in n.members:
                    if not m.fields:
                        self._plain_enum_member[m.name] = f"{n.name}_{m.name}"
                        self._plain_enum_member[f"{n.name}_{m.name}"] = f"{n.name}_{m.name}"
                    self.te.reg_enum_member(m.name, f"{n.name}_{m.name}")
                    self.te.reg_enum_member(f"{n.name}_{m.name}", f"{n.name}_{m.name}")
            elif isinstance(n, (ConstDecl, VarDecl)):
                self.te.set(n.name, n.var_type)
        # Functions after the types, so a signature naming a struct resolves.
        for n in stmts:
            if isinstance(n, FunctionDecl):
                self.te.reg_fn(n.name, n.return_type or 'void')
                params = ', '.join(f"{cpp_type(pt)} {cppid(pn)}"
                                   for pt, pn in n.params)
                self._fwd.append(
                    f"{cpp_type(n.return_type or 'void')} {cppid(n.name)}({params});")

    def _assemble(self) -> str:
        lines = [
            "// ============================================================",
            "//  Generated from Cryo by Burnout — C++ backend (C++14)",
            "//  Build: g++ -std=c++14 -O2 -I <burnout>/runtime prog.cpp -o prog",
            "// ============================================================",
            '#include "cryo_runtime.hpp"',
        ]
        lines += [f"#include <{h}>" for h in sorted(self._includes)]
        lines.append("")
        if self._types:
            lines += ["// ── declared types ──", ""] + self._types + [""]
        if self._fwd:
            lines += ["// ── forward declarations ──", ""] + self._fwd + [""]
        if self._globals:
            lines += ["// ── module state ──", ""] + self._globals + [""]
        if self._fns:
            lines += self._fns + [""]
        lines += ["int main() {"] + self._main + ["    return 0;", "}", ""]
        return '\n'.join(lines)

    # ── statements ───────────────────────────────────────────

    def _gen(self, node: Node):
        if   isinstance(node, StructDecl):          self._struct(node)
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
        elif isinstance(node, IndexAssignment):     self._index_assign(node)
        elif isinstance(node, SkillDecl):
            self._err("'skill' (the LLM layer) is not supported in the C++ "
                      "backend; use --backend go.")
        elif isinstance(node, MatchStatement):
            self._err("'match' on a data-carrying enum is not yet supported in "
                      "the C++ backend; use --backend go, node or pyro.")
        elif isinstance(node, (CallExpr, MethodCallExpr, CallValueExpr)):
            self._emit(self._expr(node) + ';')
        else:
            self._err(f"'{type(node).__name__}' is not supported in the C++ "
                      f"backend; use --backend go, node or pyro.")

    def _struct(self, n: StructDecl):
        self._emit(f"struct {n.name} {{")
        for f in n.fields:
            self._emit(f"    {cpp_type(f.field_type)} {cppid(f.name)};")
        self._emit("};")
        # Its own str(), because a struct renders as a MAP of its field names
        # (PYRO_RUNTIME §3.1) and the generic shared_ptr overload cannot know
        # them. More specialised than the template, so it wins overload
        # resolution.
        self._emit(f"namespace cryo {{ inline std::string str(const {n.name}& v) {{")
        parts = ' + ", " + '.join(
            f'std::string({cpp_string(f.name)}) + ": " + cryo::str(v.{cppid(f.name)})'
            for f in n.fields)
        self._emit(f'    return std::string("{{") + {parts or "std::string()"} + "}}";')
        self._emit("} }")
        self._emit()

    def _enum(self, n: EnumDecl):
        if any(m.fields for m in n.members):
            self._err(
                "enums with data (algebraic data types) are not yet supported "
                "in the C++ backend; use --backend go, node or pyro.")
        # Constants, not an `enum class`: the member is an integer VALUE in
        # Cryo (12.9), so `print(e)` is "0" and not "A".
        for i, m in enumerate(n.members):
            self._emit(f"static const int64_t {n.name}_{m.name} = {i};")
        self._emit()

    def _fn(self, n: FunctionDecl):
        if n.type_params:
            self._err(f"generic function '{n.name}' reached the C++ backend "
                      f"un-monomorphised; this is a compiler bug.")
        self.te.push()
        for pt, pn in n.params:
            self.te.set(pn, pt)
        params = ', '.join(f"{cpp_type(pt)} {cppid(pn)}" for pt, pn in n.params)
        ret = cpp_type(n.return_type or 'void')
        self._emit(f"{ret} {cppid(n.name)}({params}) {{")
        self._indent += 1
        for s in n.body:
            self._gen(s)
        self._indent -= 1
        self._emit("}")
        self._emit()
        self.te.pop()

    def _module_var(self, n: VarDecl):
        t = n.var_type or self.te.infer(n.value)
        self.te.set(n.name, t)
        self._globals.append(f"static {cpp_type(t)} {cppid(n.name)};")
        if n.value is not None:
            # The initialiser runs in main, in source order: it may call a
            # function or read another module variable, and a static
            # initialiser's order across translation units is not something to
            # depend on even in one file.
            self._cur, self._indent = self._main, 1
            self._emit(f"{cppid(n.name)} = {self._expr_typed(n.value, t)};")

    def _var(self, n: VarDecl):
        t = n.var_type or self.te.infer(n.value)
        self.te.set(n.name, t)
        if n.value is None:
            self._emit(f"{cpp_type(t)} {cppid(n.name)} = {self._zero(t)};")
        else:
            self._emit(f"{cpp_type(t)} {cppid(n.name)} = "
                       f"{self._expr_typed(n.value, t)};")

    def _zero(self, t: str) -> str:
        if t == 'int':
            return '0'
        if t == 'number':
            return '0.0'
        if t == 'bool':
            return 'false'
        if t == 'string':
            return 'std::string()'
        return f"{cpp_type(t)}()"

    def _const(self, n: ConstDecl):
        t = n.var_type or self.te.infer(n.value)
        self.te.set(n.name, t)
        self._globals.append(
            f"static const {cpp_type(t)} {cppid(n.name)} = "
            f"{self._expr_typed(n.value, t)};")

    def _assign(self, n: Assignment):
        t = self.te.get(n.name)
        self._emit(f"{cppid(n.name)} = {self._expr_typed(n.value, t)};")

    def _compound(self, n: CompoundAssignment):
        t = self.te.get(n.name)
        if t == 'int' and n.op in ('/=', '%='):
            fn = 'cryo::idiv' if n.op == '/=' else 'cryo::imod'
            self._emit(f"{cppid(n.name)} = {fn}({cppid(n.name)}, "
                       f"{self._expr(n.value)});")
            return
        self._emit(f"{cppid(n.name)} {n.op} {self._expr(n.value)};")

    def _incr(self, n: Increment):
        self._emit(f"{cppid(n.name)}{n.op};")

    def _return(self, n: Return):
        self._emit("return;" if n.value is None
                   else f"return {self._expr(n.value)};")

    def _if(self, n: If):
        self._emit(f"if ({self._truthy(n.condition)}) {{")
        self._indent += 1
        self.te.push()
        for s in n.then_body:
            self._gen(s)
        self.te.pop()
        self._indent -= 1
        if n.else_body:
            self._emit("} else {")
            self._indent += 1
            self.te.push()
            for s in n.else_body:
                self._gen(s)
            self.te.pop()
            self._indent -= 1
        self._emit("}")

    def _while(self, n: While):
        self._emit(f"while ({self._truthy(n.condition)}) {{")
        self._indent += 1
        self._loop_depth += 1
        self.te.push()
        for s in n.body:
            self._gen(s)
        self.te.pop()
        self._loop_depth -= 1
        self._indent -= 1
        self._emit("}")

    def _do_while(self, n: DoWhile):
        self._emit("do {")
        self._indent += 1
        self._loop_depth += 1
        self.te.push()
        for s in n.body:
            self._gen(s)
        self.te.pop()
        self._loop_depth -= 1
        self._indent -= 1
        self._emit(f"}} while ({self._truthy(n.condition)});")

    def _for(self, n: For):
        self.te.push()
        init = self._for_part(n.init) if n.init is not None else ''
        cond = self._truthy(n.condition) if n.condition is not None else ''
        upd = self._for_part(n.update) if n.update is not None else ''
        self._emit(f"for ({init}; {cond}; {upd}) {{")
        self._indent += 1
        self._loop_depth += 1
        for s in n.body:
            self._gen(s)
        self._loop_depth -= 1
        self._indent -= 1
        self._emit("}")
        self.te.pop()

    def _for_part(self, node: Node) -> str:
        if isinstance(node, VarDecl):
            t = node.var_type or self.te.infer(node.value)
            self.te.set(node.name, t)
            return (f"{cpp_type(t)} {cppid(node.name)} = "
                    f"{self._expr_typed(node.value, t)}")
        if isinstance(node, Assignment):
            return f"{cppid(node.name)} = {self._expr(node.value)}"
        if isinstance(node, CompoundAssignment):
            return f"{cppid(node.name)} {node.op} {self._expr(node.value)}"
        if isinstance(node, Increment):
            return f"{cppid(node.name)}{node.op}"
        return self._expr(node)

    def _foreach(self, n: ForEach):
        it = self.te.infer(n.iterable)
        vt = n.var_type or (elem_type(it) if it != 'string' else 'string')
        self.te.push()
        self.te.set(n.var_name, vt)
        tmp = self._next_tmp()
        if it == 'string':
            # A Cryo string iterates by CHARACTER as a one-character string;
            # C++ would hand back a char, which prints as a number through
            # ostream in some contexts and is a different type everywhere.
            self._emit(f"for (char {tmp} : {self._expr(n.iterable)}) {{")
            self._indent += 1
            self._emit(f"std::string {cppid(n.var_name)} = std::string(1, {tmp});")
        else:
            # By const reference: the container is shared, and copying every
            # element would be both slower and a different object.
            self._emit(f"for (const {cpp_type(vt)}& {cppid(n.var_name)} : "
                       f"*({self._expr(n.iterable)})) {{")
            self._indent += 1
        self._loop_depth += 1
        for s in n.body:
            self._gen(s)
        self._loop_depth -= 1
        self._indent -= 1
        self._emit("}")
        self.te.pop()

    def _break(self, n: Break):
        if self._loop_depth == 0:
            self._err("'break' outside a loop")
        self._emit("break;")

    def _continue(self, n: Continue):
        if self._loop_depth == 0:
            self._err("'continue' outside a loop")
        self._emit("continue;")

    def _switch(self, n: Switch):
        # An if/else chain, not a C++ `switch`: Cryo has no fall-through, and a
        # case label must be a compile-time constant in C++ while a Cryo case
        # value can be a variable.
        subj = self._expr(n.subject)
        st = self.te.infer(n.subject)
        tmp = self._next_tmp()
        self._emit(f"{cpp_type(st if st != 'unknown' else 'int')} {tmp} = {subj};")
        first = True
        for case in n.cases:
            cond = ' || '.join(f"{tmp} == {self._expr(v)}" for v in case.values)
            self._emit(f"if ({cond}) {{" if first else f"}} else if ({cond}) {{")
            first = False
            self._indent += 1
            self.te.push()
            for s in case.body:
                self._gen(s)
            self.te.pop()
            self._indent -= 1
        if n.default_body:
            self._emit("{" if first else "} else {")
            self._indent += 1
            self.te.push()
            for s in n.default_body:
                self._gen(s)
            self.te.pop()
            self._indent -= 1
            self._emit("}")
        elif not first:
            self._emit("}")

    def _assert(self, n: Assert):
        # 12.12 — the message is evaluated ONLY on the failing path, and what a
        # catch binds is the message string.
        msg = (self._to_str(n.message) if getattr(n, 'message', None) is not None
               else cpp_string(f"assert failed (line {getattr(n, 'line', 0)})"))
        self._emit(f"if (!({self._truthy(n.condition)}))")
        self._emit(f'    throw cryo::Thrown(std::string("[Cryo Assert] ") + ({msg}));')

    def _safety(self, n: SafetyBlock):
        self._safe_stack.append(bool(getattr(n, 'safe', True)))
        self.te.push()
        for s in n.body:
            self._gen(s)
        self.te.pop()
        self._safe_stack.pop()

    def _block(self, n: Block):
        self._emit("{")
        self._indent += 1
        self.te.push()
        for s in n.body:
            self._gen(s)
        self.te.pop()
        self._indent -= 1
        self._emit("}")

    def _try(self, n: TryCatch):
        self._emit("try {")
        self._indent += 1
        self.te.push()
        for s in n.try_body:
            self._gen(s)
        self.te.pop()
        self._indent -= 1
        tmp = self._next_tmp()
        self._emit(f"}} catch (const cryo::Thrown& {tmp}) {{")
        self._indent += 1
        self.te.push()
        if getattr(n, 'catch_name', None):
            self.te.set(n.catch_name, 'string')
            self._emit(f"std::string {cppid(n.catch_name)} = {tmp}.value;")
        for s in (n.catch_body or []):
            self._gen(s)
        self.te.pop()
        self._indent -= 1
        self._emit("}")
        if getattr(n, 'finally_body', None):
            # C++ has no `finally`. The body is emitted after the try/catch,
            # which is the same thing here because a Cryo catch does not
            # rethrow and there is no early return out of a try in this subset.
            self._emit("{")
            self._indent += 1
            self.te.push()
            for s in n.finally_body:
                self._gen(s)
            self.te.pop()
            self._indent -= 1
            self._emit("}")

    def _index_assign(self, n: IndexAssignment):
        ot = self.te.infer(n.obj)
        obj, idx, val = self._expr(n.obj), self._expr(n.index), self._expr(n.value)
        if is_map(ot):
            self._emit(f"cryo::map_set({obj}, {idx}, {val});")
        else:
            self._emit(f"cryo::set_at({obj}, {idx}, {val});")

    def _import(self, n: Import):
        self._emit(f"// [Cryo] import >{n.lang}<")

    def _library(self, n: Library):
        lang = resolve_library_lang(n, self._imported_langs)
        if lang in _CPP_LANGS:
            # `library >C++ vector<` -> `#include <vector>`
            self._includes.add(n.name)
            self._emit(f"// [Cryo] library >{n.lang or 'C++'} {n.name}< -> #include")
        else:
            self._emit(f"// [Cryo] library >{n.name}< (language {lang or '?'}) "
                       f"ignored in the C++ backend")

    def _foreign(self, n: ForeignBlock):
        lang = (n.lang or '').strip().lower()
        if lang in _CPP_LANGS:
            self._emit(f"// -- [{n.lang} block] --")
            for line in n.code.strip().split('\n'):
                self._emit(line.strip())
            self._emit(f"// -- [/{n.lang} block] --")
        elif lang in ('html', 'css'):
            self._err(
                f"'>{n.lang}(' blocks build a page and the C++ backend does not "
                f"render one. Use --backend frontend (or --backend auto).")
        else:
            self._emit(f"// [Cryo] >{n.lang}< block omitted in the C++ backend "
                       f"(use >C++( ... ))")

    # ── expressions ──────────────────────────────────────────

    def _truthy(self, node: Node) -> str:
        """A condition as a C++ bool.

        Cryo's truthiness is wider than C++'s for strings and containers: "" is
        falsy, and a shared_ptr is truthy only when it holds something.
        """
        t = self.te.infer(node)
        e = self._expr(node)
        if t == 'bool':
            return e
        if t == 'int':
            return f"(({e}) != 0)"
        if t == 'number':
            return f"(({e}) != 0.0)"
        if t == 'string':
            return f"(!({e}).empty())"
        if is_optional(t) or t == 'null':
            return f"((bool)({e}))"
        return e

    def _to_str(self, node: Node) -> str:
        t = self.te.infer(node)
        e = self._expr(node)
        return e if t == 'string' else f"cryo::str({e})"

    def _expr_typed(self, node: Node, target: str) -> str:
        """An expression with the target type in hand.

        A bare `null` and an empty `[]` / `{}` have no type of their own, and
        C++ will not deduce one from the assignment target the way a dynamic
        backend does.
        """
        if isinstance(node, Literal) and node.kind == 'null':
            return f"{cpp_type(target)}()" if target else "nullptr"
        if isinstance(node, ArrayLiteral) and not node.elements:
            et = elem_type(target) if target else 'int'
            return f"cryo::arr<{cpp_type(et)}>()"
        if isinstance(node, MapLiteral) and not node.pairs:
            k, v = map_kv(target) if is_map(target) else ('string', 'int')
            return f"cryo::mapof<{cpp_type(k)}, {cpp_type(v)}>()"
        if isinstance(node, ArrayLiteral) and target and target.endswith('[]'):
            et = target[:-2]
            items = ', '.join(self._expr_typed(x, et) for x in node.elements)
            return (f"cryo::arr<{cpp_type(et)}>(std::vector<{cpp_type(et)}>"
                    f"{{ {items} }})")
        if isinstance(node, MapLiteral) and target and is_map(target):
            kt, vt = map_kv(target)
            parts = ', '.join(
                f"{{ {self._expr_typed(k, kt)}, {self._expr_typed(v, vt)} }}"
                for k, v in node.pairs)
            return (f"cryo::mapof<{cpp_type(kt)}, {cpp_type(vt)}>("
                    f"std::map<{cpp_type(kt)}, {cpp_type(vt)}>{{ {parts} }})")
        # An optional slot taking a present value has to be wrapped.
        if target and is_optional(target) and not _is_null(node, self.te.infer(node)):
            it = self.te.infer(node)
            if not is_optional(it):
                return f"cryo::opt<{cpp_type(opt_base(target))}>({self._expr(node)})"
        if target == 'number' and self.te.infer(node) == 'int':
            return f"(double)({self._expr(node)})"
        return self._expr(node)

    def _expr(self, node: Node) -> str:
        if isinstance(node, Literal):
            if node.kind == 'null':
                return "nullptr"
            if node.kind == 'bool':
                return 'true' if node.value else 'false'
            if node.kind == 'string':
                return f"std::string({cpp_string(node.value)})"
            if node.kind == 'int':
                # INT64_C-style suffix: a bare literal is `int` in C++ and
                # would overflow silently in 64-bit arithmetic.
                return f"(int64_t){node.value}"
            if node.kind == 'float':
                v = repr(float(node.value))
                return v if ('.' in v or 'e' in v or 'E' in v) else v + '.0'
            return str(node.value)

        if isinstance(node, Identifier):
            m = self._plain_enum_member.get(node.name)
            if m and self.te.get(node.name) == 'unknown':
                return m
            return cppid(node.name)

        if isinstance(node, BinaryExpr):
            return self._binary(node)

        if isinstance(node, UnaryExpr):
            if node.op == '!':
                return f"(!{self._truthy(node.operand)})"
            return f"({node.op}{self._expr(node.operand)})"

        if isinstance(node, TernaryExpr):
            return (f"({self._truthy(node.condition)} ? "
                    f"{self._expr(node.then_value)} : "
                    f"{self._expr(node.else_value)})")

        if isinstance(node, UnwrapExpr):
            inner = getattr(node, 'operand', None) or getattr(node, 'expr', None)
            return f"cryo::unwrap({self._expr(inner)})"

        if isinstance(node, FieldAccess):
            if node.field == 'length':
                return f"cryo::len({self._expr(node.obj)})"
            if isinstance(node.obj, Identifier) and self.te.is_enum(node.obj.name):
                return f"{node.obj.name}_{node.field}"
            # A struct is a handle, so field access goes through ->
            return f"{self._expr(node.obj)}->{cppid(node.field)}"

        if isinstance(node, IndexAccess):
            ot = self.te.infer(node.obj)
            obj, idx = self._expr(node.obj), self._expr(node.index)
            if is_map(ot):
                return f"cryo::map_get({obj}, {idx})"
            if ot == 'string':
                return f"cryo::char_at({obj}, {idx})"
            return f"cryo::at({obj}, {idx})"

        if isinstance(node, ArrayLiteral):
            if not node.elements:
                return "cryo::arr<int64_t>()"
            et = self.te.infer(node.elements[0])
            items = ', '.join(self._expr_typed(x, et) for x in node.elements)
            return (f"cryo::arr<{cpp_type(et)}>(std::vector<{cpp_type(et)}>"
                    f"{{ {items} }})")

        if isinstance(node, MapLiteral):
            if not node.pairs:
                return "cryo::mapof<std::string, int64_t>()"
            kt = self.te.infer(node.pairs[0][0])
            vt = self.te.infer(node.pairs[0][1])
            parts = ', '.join(f"{{ {self._expr(k)}, {self._expr(v)} }}"
                              for k, v in node.pairs)
            return (f"cryo::mapof<{cpp_type(kt)}, {cpp_type(vt)}>("
                    f"std::map<{cpp_type(kt)}, {cpp_type(vt)}>{{ {parts} }})")

        if isinstance(node, StructInit):
            # Field order follows the DECLARATION, not the initialiser, because
            # a brace-init list in C++ is positional.
            decl = self.te._structs.get(node.struct_name, {})
            given = dict(node.fields)
            vals = []
            for fname, ftype in decl.items():
                v = given.get(fname)
                vals.append(self._expr_typed(v, ftype) if v is not None
                            else self._zero(ftype))
            inner = ', '.join(vals)
            return (f"std::make_shared<{node.struct_name}>("
                    f"{node.struct_name}{{ {inner} }})")

        if isinstance(node, CallExpr):
            return self._call(node)

        if isinstance(node, MethodCallExpr):
            return self._method(node)

        if isinstance(node, (SpawnExpr, AwaitExpr)):
            self._err("concurrency (spawn/await) is not supported in the C++ "
                      "backend; use --backend go or pyro.")
        if isinstance(node, (Lambda, CallValueExpr)):
            self._err("first-class functions are not yet supported in the C++ "
                      "backend; use --backend go, node or pyro.")
        if isinstance(node, CastExpr):
            self._err("'as T' (JSON casting) is not yet supported in the C++ "
                      "backend; use --backend go, node or pyro.")
        if isinstance(node, TryExpr):
            self._err("propagation '?' is not yet supported in the C++ "
                      "backend; use --backend go, node or pyro.")

        self._err(f"'{type(node).__name__}' is not supported in the C++ "
                  f"backend; use --backend go, node or pyro.")

    def _binary(self, node: BinaryExpr) -> str:
        op = node.op
        lt, rt = self.te.infer(node.left), self.te.infer(node.right)

        if op == '&&':
            return f"({self._truthy(node.left)} && {self._truthy(node.right)})"
        if op == '||':
            return f"({self._truthy(node.left)} || {self._truthy(node.right)})"
        if op == '??':
            # Evaluates its left side ONCE, which a plain conditional would not
            # (11.27 hit exactly this in C).
            return f"cryo::or_else({self._expr(node.left)}, {self._expr(node.right)})"

        l, r = self._expr(node.left), self._expr(node.right)

        if op == '+' and (lt == 'string' or rt == 'string'):
            ls = l if lt == 'string' else self._to_str(node.left)
            rs = r if rt == 'string' else self._to_str(node.right)
            return f"({ls} + {rs})"

        # ISSUES/18 — a value with no null to be, compared against null.
        if op in ('==', '!=') and (_is_null(node.left, lt) != _is_null(node.right, rt)):
            other = rt if _is_null(node.left, lt) else lt
            if other not in ('unknown', 'any', 'null', 'string') \
                    and not is_optional(other) and not other.endswith('[]') \
                    and not is_map(other) and not self.te.is_struct(other):
                return 'false' if op == '==' else 'true'
        # A container or optional against null is a pointer test.
        if op in ('==', '!=') and (_is_null(node.left, lt) or _is_null(node.right, rt)):
            live = r if _is_null(node.left, lt) else l
            return f"(({live}) {op} nullptr)"

        if op in ('/', '%') and lt == 'int' and rt == 'int':
            fn = 'cryo::idiv' if op == '/' else 'cryo::imod'
            return f"{fn}({l}, {r})"
        # C++ has no % for doubles.
        if op == '%' and (lt == 'number' or rt == 'number'):
            return f"std::fmod((double)({l}), (double)({r}))"

        if op in ('+', '-', '*', '/') and {lt, rt} == {'int', 'number'}:
            if lt == 'int':
                l = f"(double)({l})"
            if rt == 'int':
                r = f"(double)({r})"
            return f"({l} {op} {r})"

        return f"({l} {op} {r})"

    def _method(self, node: MethodCallExpr) -> str:
        obj = self._expr(node.obj)
        if node.method == 'push':
            return f"cryo::push({obj}, {self._expr(node.args[0])})"
        ot = self.te.infer(node.obj)
        self._err(f"method '.{node.method}()' on a value of type '{ot}' is not "
                  f"supported in the C++ backend; use --backend go, node or pyro.")

    # ── builtin calls ────────────────────────────────────────

    def _call(self, node: CallExpr) -> str:
        c = node.callee
        a = node.args
        E = lambda i: self._expr(a[i])
        T = lambda i: self.te.infer(a[i])

        if c == 'print':
            return "std::cout << std::endl" if not a else f"cryo::print({E(0)})"
        if c == 'throw' and len(a) == 1:
            return f"throw cryo::Thrown({self._to_str(a[0])})"
        if c == 'to_string' and len(a) == 1:
            return self._to_str(a[0])
        if c == 'to_int' and len(a) == 1:
            t = T(0)
            if t == 'string':
                return f"cryo::to_int({E(0)})"
            return E(0) if t == 'int' else f"((int64_t)({E(0)}))"
        if c == 'to_number' and len(a) == 1:
            t = T(0)
            if t == 'string':
                return f"cryo::to_num({E(0)})"
            return E(0) if t == 'number' else f"((double)({E(0)}))"
        if c == 'len' and len(a) == 1:
            return f"cryo::len({E(0)})"

        # ── math ──
        if c == 'abs' and len(a) == 1:
            return (f"std::llabs({E(0)})" if T(0) == 'int'
                    else f"std::fabs({E(0)})")
        if c in ('min', 'max') and len(a) == 2:
            fn = 'std::min' if c == 'min' else 'std::max'
            if {T(0), T(1)} == {'int', 'number'}:
                return f"{fn}((double)({E(0)}), (double)({E(1)}))"
            return f"{fn}({E(0)}, {E(1)})"
        if c == 'sqrt' and len(a) == 1:
            return f"std::sqrt((double)({E(0)}))"
        if c == 'pow' and len(a) == 2:
            return f"std::pow((double)({E(0)}), (double)({E(1)}))"
        if c == 'hypot' and len(a) == 2:
            return f"std::sqrt((double)({E(0)})*(double)({E(0)}) + (double)({E(1)})*(double)({E(1)}))"
        if c == 'floor' and len(a) == 1:
            return f"std::floor((double)({E(0)}))"
        if c == 'ceil' and len(a) == 1:
            return f"std::ceil((double)({E(0)}))"
        if c == 'round' and len(a) == 1:
            return f"cryo::round_half_up((double)({E(0)}))"
        if c == 'sign' and len(a) == 1:
            return f"cryo::sign_i({E(0)})"
        if c == 'gcd' and len(a) == 2:
            return f"cryo::gcd({E(0)}, {E(1)})"
        if c == 'clamp' and len(a) == 3:
            allint = all(T(i) == 'int' for i in range(3))
            if allint:
                return f"cryo::clamp_i({E(0)}, {E(1)}, {E(2)})"
            return (f"cryo::clamp_f((double)({E(0)}), (double)({E(1)}), "
                    f"(double)({E(2)}))")

        # ── strings ──
        _S1 = {'upper': 'upper', 'lower': 'lower', 'trim': 'trim'}
        if c in _S1 and len(a) == 1:
            return f"cryo::{_S1[c]}({E(0)})"
        _S2 = {'find': 'find', 'starts_with': 'starts_with',
               'ends_with': 'ends_with', 'repeat': 'repeat', 'split': 'split',
               'join': 'join'}
        if c in _S2 and len(a) == 2:
            return f"cryo::{_S2[c]}({E(0)}, {E(1)})"
        if c == 'contains' and len(a) == 2:
            if T(0) == 'string':
                return f"cryo::contains({E(0)}, {E(1)})"
            return f"(cryo::index_of({E(0)}, {E(1)}) >= 0)"
        if c in ('pad_start', 'pad_end') and len(a) == 3:
            return f"cryo::{c}({E(0)}, {E(1)}, {E(2)})"
        if c == 'replace' and len(a) == 3:
            return f"cryo::replace_all({E(0)}, {E(1)}, {E(2)})"
        if c == 'substr' and len(a) == 3:
            return f"cryo::substr({E(0)}, {E(1)}, {E(2)})"

        # ── collections ──
        if c == 'sort' and len(a) == 1:
            return f"cryo::sorted({E(0)})"
        if c == 'reverse' and len(a) == 1:
            return f"cryo::reversed({E(0)})"
        if c == 'slice' and len(a) == 3:
            fn = 'cryo::slice_str' if T(0) == 'string' else 'cryo::slice'
            return f"{fn}({E(0)}, {E(1)}, {E(2)})"
        if c == 'index_of' and len(a) == 2:
            return f"cryo::index_of({E(0)}, {E(1)})"
        if c == 'count' and len(a) == 2:
            return f"cryo::count_of({E(0)}, {E(1)})"
        if c == 'concat' and len(a) == 2:
            return f"cryo::concat({E(0)}, {E(1)})"
        if c == 'sum' and len(a) == 1:
            et = elem_type(T(0))
            if et not in ('int', 'number'):
                self._err(f"'sum()' needs a numeric array in the C++ backend "
                          f"(element type is '{et}').")
            return f"cryo::sum({E(0)})"

        # ── maps ──
        if c == 'has' and len(a) == 2:
            return f"cryo::has({E(0)}, {E(1)})"
        if c == 'keys' and len(a) == 1:
            return f"cryo::keys({E(0)})"
        if c == 'remove' and len(a) == 2:
            return f"cryo::remove({E(0)}, {E(1)})"

        if c in ('llm', 'agent', 'llm_call', 'llm_try', 'agent_call',
                 'agent_try', 'llm_stream', 'skills', 'skill_get'):
            self._err(f"'{c}()' is part of the LLM layer and is not available "
                      f"in the C++ backend; use --backend go.")
        if c in ('http_get', 'http_post', 'http_serve', 'http_listen',
                 'http_accept', 'http_respond', 'sleep', 'input'):
            self._err(f"'{c}()' is not yet available in the C++ backend; "
                      f"use --backend go or pyro.")

        args = ', '.join(self._expr(x) for x in a)
        return f"{cppid(c)}({args})"
