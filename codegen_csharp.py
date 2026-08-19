# ============================================================
#  Cryo Compiler — C# Code Generator
#  .cryo  ->  .cs  (compiled and run with the .NET SDK)
#
#  Modelled on codegen_c.py, which is the closest sibling: both
#  targets are statically typed and both have to pick a concrete
#  type for every slot. The difference is the standard library —
#  .NET already has string, List<T> and Dictionary<K,V>, so this
#  backend needs no hand-written runtime the way the C one needs
#  cryo_runtime.c. What it DOES need is rendering that matches the
#  VM byte for byte, because invariant 1 is checked by comparing
#  program output, not by compiling.
#
#  The two places that costs something:
#    * a `number` prints as "2", not "2.0" — .NET's default for a
#      double is "2" but for 2.5 it is culture-dependent, so every
#      conversion goes through InvariantCulture.
#    * a map renders sorted by the key's own TEXT ("1, 10, 2"),
#      which is what the VM does and looks wrong until you know.
#
#  Unsupported constructs are REFUSED here rather than emitted for
#  the C# compiler to complain about: a message naming a backend
#  that works beats one naming a type the programmer never wrote.
# ============================================================
from ast_nodes import *
from foreign import collect_imports, resolve_library_lang
from typing import List, Dict, Set


class CodeGenCSharpError(Exception):
    pass


# ── Cryo -> C# type mapping ─────────────────────────────────

CS_TYPE: Dict[str, str] = {
    'int':    'long',
    'number': 'double',
    'string': 'string',
    'bool':   'bool',
    'void':   'void',
    'null':   'object',
    'any':    'object',
}

# `string` is already a reference type, so `string?` needs no wrapper; the
# value types become Nullable<T>.
_OPT_CS = {'int': 'long?', 'number': 'double?', 'bool': 'bool?',
           'string': 'string'}

_CS_LANGS = ('c#', 'cs', 'csharp', 'dotnet')


def is_map(t: str) -> bool:
    return bool(t) and t.startswith('map<') and t.endswith('>')


def map_kv(t: str):
    """('string', 'int') for map<string,int>.

    Split on the FIRST top-level comma: a nested map<...> value would
    otherwise be cut in half.
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


# 12.9 — a payload-less enum member is an INTEGER value, so `E e = A;
# print(e)` is "0" on every backend. A C# `enum` would render as "A", so the
# type maps to long and the members become constants. Registered per
# generate(); the compiler is single-threaded, and the alternative is threading
# the environment through cs_type's forty call sites.
_ENUM_NAMES: Set[str] = set()


def cs_type(t: str) -> str:
    if not t:
        return 'object'
    if t in _ENUM_NAMES:
        return 'long'
    if t.startswith('(') and t.endswith(')'):
        return cs_type(t[1:-1])
    if t.startswith('fn(') and '->' in t:
        raise CodeGenCSharpError(
            f"function type '{t}' (first-class functions) is not yet supported "
            f"in the C# backend; use --backend go, node or pyro.")
    if t.startswith('future<'):
        raise CodeGenCSharpError(
            "concurrency (spawn/await) is not supported in the C# backend; "
            "use --backend go or pyro.")
    if is_optional(t):
        base = t[:-1]
        if base in _OPT_CS:
            return _OPT_CS[base]
        # A struct is already a reference type here, so T? is just T.
        return cs_type(base)
    if is_map(t):
        k, v = map_kv(t)
        return f"Dictionary<{cs_type(k)}, {cs_type(v)}>"
    if t.endswith('[]'):
        return f"List<{cs_type(t[:-2])}>"
    return CS_TYPE.get(t, t)      # struct / enum names pass through


_CS_ESCAPES = {'\\': '\\\\', '"': '\\"', '\n': '\\n', '\t': '\\t',
               '\r': '\\r', '\0': '\\0'}


def cs_string(v: str) -> str:
    """A Cryo string as a C# string literal.

    Interpolating the value verbatim would turn a backslash in the program's
    DATA into an escape in the generated C#, and a quote or newline would
    produce code that does not compile — the C# compiler reporting on a string
    the programmer never wrote. Non-ASCII passes through as itself: the file is
    written as UTF-8 and the SDK reads it as UTF-8.
    """
    out = []
    for ch in v:
        if ch in _CS_ESCAPES:
            out.append(_CS_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


# C# keywords a Cryo identifier could legitimately collide with. Prefixed with
# `@` rather than renamed, so the name in the generated code still reads as the
# one the programmer wrote.
_CS_KEYWORDS = {
    'abstract', 'as', 'base', 'bool', 'break', 'byte', 'case', 'catch', 'char',
    'checked', 'class', 'const', 'continue', 'decimal', 'default', 'delegate',
    'do', 'double', 'else', 'enum', 'event', 'explicit', 'extern', 'false',
    'finally', 'fixed', 'float', 'for', 'foreach', 'goto', 'if', 'implicit',
    'in', 'int', 'interface', 'internal', 'is', 'lock', 'long', 'namespace',
    'new', 'null', 'object', 'operator', 'out', 'override', 'params',
    'private', 'protected', 'public', 'readonly', 'ref', 'return', 'sbyte',
    'sealed', 'short', 'sizeof', 'stackalloc', 'static', 'string', 'struct',
    'switch', 'this', 'throw', 'true', 'try', 'typeof', 'uint', 'ulong',
    'unchecked', 'unsafe', 'ushort', 'using', 'virtual', 'void', 'volatile',
    'while',
}


def csid(name: str) -> str:
    return '@' + name if name in _CS_KEYWORDS else name


# ── type inference ──────────────────────────────────────────
#
# Deliberately the same shape as codegen_c's TypeEnv. Sharing one would be
# better, but the two disagree on what they can represent (this one has maps of
# any key type, that one does not), and a common version that served both would
# have to be told which backend is asking.

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
                     'repeat', 'pad_start', 'pad_end', 'replace', 'join',
                     'read_file', 'env'):
                return 'string'
            if c in ('to_int', 'len', 'sign', 'gcd', 'find', 'count',
                     'index_of'):
                return 'int'
            if c in ('has', 'starts_with', 'ends_with', 'contains',
                     'file_exists'):
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


# ── the emitted runtime ─────────────────────────────────────
#
# Read from Burnout/runtime/cryo_runtime.cs and embedded in the generated
# file, so a compiled program is a single self-contained .cs. The C backend
# ships its runtime as a second file to hand to gcc; here a single file is
# what `dotnet run` wants, and the runtime is small enough that inlining it
# costs nothing.

def _runtime_text() -> str:
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'runtime', 'cryo_runtime.cs')
    with open(path, encoding='utf-8') as f:
        return f.read()


class CodeGenCSharp:
    def __init__(self, safe: bool = True):
        self.te = TypeEnv()
        self._indent = 0
        self._types: List[str] = []
        self._fields: List[str] = []
        self._fns: List[str] = []
        self._main: List[str] = []
        self._usings: Set[str] = set()
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
        raise CodeGenCSharpError(msg)

    def _pad(self) -> str:
        return '    ' * self._indent

    def _emit(self, line: str = ''):
        self._cur.append(self._pad() + line if line else '')

    def _next_tmp(self) -> str:
        self._tmp += 1
        return f"__cs{self._tmp}"

    # ── entry ────────────────────────────────────────────────

    def generate(self, program: Program) -> str:
        _ENUM_NAMES.clear()
        self._imported_langs = collect_imports(program)
        self._pre_scan(program.statements)

        for node in program.statements:
            if isinstance(node, (StructDecl, EnumDecl)):
                self._cur, self._indent = self._types, 0
                self._gen(node)
            elif isinstance(node, FunctionDecl):
                self._cur, self._indent = self._fns, 1
                self._gen(node)
            elif isinstance(node, (ConstDecl, VarDecl)):
                # Module state is a static field, so a function declared above
                # the variable can still refer to it (11.1). The INITIALISER
                # stays in Main, in source order, because it may call a
                # function or read another module variable.
                self._module_var(node)
            elif isinstance(node, (Import, Library)):
                self._cur, self._indent = self._main, 2
                self._gen(node)
            else:
                self._cur, self._indent = self._main, 2
                self._gen(node)

        return self._assemble()

    def _pre_scan(self, stmts: List[Node]):
        for n in stmts:
            if isinstance(n, StructDecl):
                self.te.reg_struct(n.name, {f.name: f.field_type for f in n.fields})
            elif isinstance(n, EnumDecl):
                self.te.reg_enum(n.name)
                _ENUM_NAMES.add(n.name)
                for m in n.members:
                    if not m.fields:
                        self._plain_enum_member[m.name] = f"{n.name}.{m.name}"
                        self._plain_enum_member[f"{n.name}_{m.name}"] = f"{n.name}.{m.name}"
                    self.te.reg_enum_member(m.name, f"{n.name}.{m.name}")
                    self.te.reg_enum_member(f"{n.name}_{m.name}", f"{n.name}.{m.name}")
            elif isinstance(n, FunctionDecl):
                self.te.reg_fn(n.name, n.return_type or 'void')
            elif isinstance(n, (ConstDecl, VarDecl)):
                self.te.set(n.name, n.var_type)

    def _assemble(self) -> str:
        usings = sorted({'System', 'System.Collections.Generic',
                         'System.Globalization', 'System.Text'} | self._usings)
        lines = [
            "// ============================================================",
            "//  Generated from Cryo by Burnout — C# backend",
            "//  Build & run:  dotnet run     (or: csc program.cs)",
            "// ============================================================",
        ]
        lines += [f"using {u};" for u in usings]
        lines.append("")
        lines.append(_runtime_text().rstrip())
        lines.append("")
        if self._types:
            lines += ["// ── declared types ──", ""] + self._types + [""]
        lines += ["public static class Program {"]
        if self._fields:
            lines += ["    // ── module state ──"] + self._fields + [""]
        if self._fns:
            lines += self._fns + [""]
        lines += ["    public static void Main() {"] + self._main + ["    }", "}", ""]
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
            self._err("'skill' (the LLM layer) is not supported in the C# "
                      "backend; use --backend go.")
        elif isinstance(node, MatchStatement):
            self._err("'match' on a data-carrying enum is not yet supported in "
                      "the C# backend; use --backend go, node or pyro.")
        elif isinstance(node, (CallExpr, MethodCallExpr, CallValueExpr)):
            self._emit(self._expr(node) + ';')
        else:
            self._err(f"'{type(node).__name__}' is not supported in the C# "
                      f"backend; use --backend go, node or pyro.")

    def _struct(self, n: StructDecl):
        # A class rather than a C# struct: Cryo structs are reference values
        # (two names for one object see each other's writes), and a C# struct
        # would copy on every assignment and silently diverge from the VM.
        self._emit(f"public class {n.name} {{")
        for f in n.fields:
            self._emit(f"    public {cs_type(f.field_type)} {csid(f.name)};")
        self._emit("}")
        self._emit()

    def _enum(self, n: EnumDecl):
        if any(m.fields for m in n.members):
            self._err(
                "enums with data (algebraic data types) are not yet supported "
                "in the C# backend; use --backend go, node or pyro.")
        # Constants in a holder class, not a C# `enum`: the member is an
        # integer VALUE in Cryo (12.9), and a C# enum would render as its name.
        self._emit(f"public static class {n.name} {{")
        for i, m in enumerate(n.members):
            self._emit(f"    public const long {m.name} = {i};")
        self._emit("}")
        self._emit()

    def _fn(self, n: FunctionDecl):
        if n.type_params:
            self._err(f"generic function '{n.name}' reached the C# backend "
                      f"un-monomorphised; this is a compiler bug.")
        self.te.push()
        for pt, pn in n.params:
            self.te.set(pn, pt)
        params = ', '.join(f"{cs_type(pt)} {csid(pn)}" for pt, pn in n.params)
        ret = cs_type(n.return_type or 'void')
        self._emit(f"public static {ret} {csid(n.name)}({params}) {{")
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
        self._fields.append(f"    static {cs_type(t)} {csid(n.name)};")
        if n.value is not None:
            self._cur, self._indent = self._main, 2
            self._emit(f"{csid(n.name)} = {self._expr_typed(n.value, t)};")

    def _var(self, n: VarDecl):
        t = n.var_type or self.te.infer(n.value)
        self.te.set(n.name, t)
        if n.value is None:
            self._emit(f"{cs_type(t)} {csid(n.name)};")
        else:
            self._emit(f"{cs_type(t)} {csid(n.name)} = {self._expr_typed(n.value, t)};")

    def _const(self, n: ConstDecl):
        t = n.var_type or self.te.infer(n.value)
        self.te.set(n.name, t)
        self._fields.append(
            f"    static readonly {cs_type(t)} {csid(n.name)} = "
            f"{self._expr_typed(n.value, t)};")

    def _assign(self, n: Assignment):
        t = self.te.get(n.name)
        self._emit(f"{csid(n.name)} = {self._expr_typed(n.value, t)};")

    def _compound(self, n: CompoundAssignment):
        # `/=` and `%=` on ints have to go through the checked helpers, or a
        # zero divisor throws a .NET exception instead of the VM's abort.
        t = self.te.get(n.name)
        if t == 'int' and n.op in ('/=', '%='):
            fn = 'Cryo.IDiv' if n.op == '/=' else 'Cryo.IMod'
            self._emit(f"{csid(n.name)} = {fn}({csid(n.name)}, {self._expr(n.value)});")
            return
        self._emit(f"{csid(n.name)} {n.op} {self._expr(n.value)};")

    def _incr(self, n: Increment):
        self._emit(f"{csid(n.name)}{n.op};")

    def _return(self, n: Return):
        if n.value is None:
            self._emit("return;")
        else:
            self._emit(f"return {self._expr(n.value)};")

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
            return (f"{cs_type(t)} {csid(node.name)} = "
                    f"{self._expr_typed(node.value, t)}")
        if isinstance(node, Assignment):
            return f"{csid(node.name)} = {self._expr(node.value)}"
        if isinstance(node, CompoundAssignment):
            return f"{csid(node.name)} {node.op} {self._expr(node.value)}"
        if isinstance(node, Increment):
            return f"{csid(node.name)}{node.op}"
        return self._expr(node)

    def _foreach(self, n: ForEach):
        it = self.te.infer(n.iterable)
        vt = n.var_type or (elem_type(it) if it != 'string' else 'string')
        self.te.push()
        self.te.set(n.var_name, vt)
        if it == 'string':
            # A Cryo string iterates by CHARACTER as a one-character string;
            # C# would hand back a `char`, which renders as the character but
            # is a different type everywhere else.
            tmp = self._next_tmp()
            self._emit(f"foreach (var {tmp} in {self._expr(n.iterable)}) {{")
            self._indent += 1
            self._emit(f"string {csid(n.var_name)} = {tmp}.ToString();")
        else:
            self._emit(f"foreach ({cs_type(vt)} {csid(n.var_name)} in "
                       f"{self._expr(n.iterable)}) {{")
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
        subj = self._expr(n.subject)
        st = self.te.infer(n.subject)
        tmp = self._next_tmp()
        self._emit(f"{cs_type(st if st != 'unknown' else 'int')} {tmp} = {subj};")
        first = True
        for case in n.cases:
            # Cryo has no fall-through, so each arm is an if/else — a C#
            # `switch` would need a `break` per arm and reject a non-constant
            # label, which an enum member reached through a variable is.
            cond = ' || '.join(f"{tmp} == {self._expr(v)}" for v in case.values)
            self._emit(f"{'if' if first else '} else if'} ({cond}) {{"
                       if first else f"}} else if ({cond}) {{")
            first = False
            self._indent += 1
            self.te.push()
            for s in case.body:
                self._gen(s)
            self.te.pop()
            self._indent -= 1
        if n.default_body:
            if first:
                self._emit("{")
            else:
                self._emit("} else {")
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
        # 12.12 — the message is evaluated ONLY on the failing path, and the
        # value a catch binds is the message string, not an exception object.
        msg = (self._to_str(n.message) if getattr(n, 'message', None) is not None
               else cs_string(f"assert failed (line {getattr(n, 'line', 0)})"))
        self._emit(f"if (!({self._truthy(n.condition)}))")
        self._emit(f"    throw new CryoThrow(\"[Cryo Assert] \" + ({msg}));")

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
        self._emit(f"}} catch (CryoThrow {tmp}) {{")
        self._indent += 1
        self.te.push()
        if n.catch_name:
            self.te.set(n.catch_name, 'string')
            self._emit(f"string {csid(n.catch_name)} = {tmp}.Value;")
        for s in n.catch_body:
            self._gen(s)
        self.te.pop()
        self._indent -= 1
        if getattr(n, 'finally_body', None):
            self._emit("} finally {")
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
            self._emit(f"{obj}[{idx}] = {val};")
        else:
            self._emit(f"Cryo.SetAt({obj}, {idx}, {val});")

    def _import(self, n: Import):
        self._emit(f"// [Cryo] import >{n.lang}<")

    def _library(self, n: Library):
        lang = resolve_library_lang(n, self._imported_langs)
        if lang in _CS_LANGS:
            # `library >C# System.Text.Json<` -> `using System.Text.Json;`
            self._usings.add(n.name)
            self._emit(f"// [Cryo] library >{n.lang or 'C#'} {n.name}< -> using")
        else:
            self._emit(f"// [Cryo] library >{n.name}< (language {lang or '?'}) "
                       f"ignored in the C# backend")

    def _foreign(self, n: ForeignBlock):
        lang = (n.lang or '').strip().lower()
        if lang in _CS_LANGS:
            self._emit(f"// -- [{n.lang} block] --")
            for line in n.code.strip().split('\n'):
                self._emit(line.strip())
            self._emit(f"// -- [/{n.lang} block] --")
        elif lang in ('html', 'css'):
            self._err(
                f"'>{n.lang}(' blocks build a page and the C# backend does not "
                f"render one. Use --backend frontend (or --backend auto).")
        else:
            self._emit(f"// [Cryo] >{n.lang}< block omitted in the C# backend "
                       f"(use >C#( ... ))")

    # ── expressions ──────────────────────────────────────────

    def _truthy(self, node: Node) -> str:
        """A condition as a C# bool.

        Cryo's truthiness is wider than C#'s: 0, "" and null are falsy. A
        condition that is already a comparison needs none of that, so the
        wrapper is only applied where the type says it could matter.
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
            return f"(!string.IsNullOrEmpty({e}))"
        if is_optional(t) or t in ('null',):
            return f"(({e}) != null)"
        return e

    def _to_str(self, node: Node) -> str:
        t = self.te.infer(node)
        e = self._expr(node)
        if t == 'string':
            return e
        return f"Cryo.Str({e})"

    def _expr_typed(self, node: Node, target: str) -> str:
        """An expression with the target type in hand.

        Two things need it. A bare `null` has no type of its own, and an empty
        `[]` or `{}` has no element type — both would otherwise reach C# as
        something it cannot infer, and `var` is not available in a field
        declaration.
        """
        if isinstance(node, Literal) and node.kind == 'null':
            if target and is_optional(target) and opt_base(target) == 'string':
                return "(string)null"
            return "null"
        if isinstance(node, ArrayLiteral) and not node.elements and target:
            return f"new {cs_type(target)}()"
        if isinstance(node, MapLiteral) and not node.pairs and target:
            return f"new {cs_type(target)}()"
        if isinstance(node, ArrayLiteral) and target and target.endswith('[]'):
            et = target[:-2]
            items = ', '.join(self._expr_typed(x, et) for x in node.elements)
            return f"new {cs_type(target)} {{ {items} }}"
        if isinstance(node, MapLiteral) and target and is_map(target):
            kt, vt = map_kv(target)
            parts = ', '.join(
                f"{{ {self._expr_typed(k, kt)}, {self._expr_typed(v, vt)} }}"
                for k, v in node.pairs)
            return f"new {cs_type(target)} {{ {parts} }}"
        # An int literal landing in a `number` slot has to become a double, or
        # C# picks integer division for `1 / 2` inside it.
        if target == 'number' and self.te.infer(node) == 'int':
            return f"(double)({self._expr(node)})"
        return self._expr(node)

    def _expr(self, node: Node) -> str:
        if isinstance(node, Literal):
            if node.kind == 'null':
                return "null"
            if node.kind == 'bool':
                return 'true' if node.value else 'false'
            if node.kind == 'string':
                return cs_string(node.value)
            if node.kind == 'int':
                return f"{node.value}L"
            if node.kind == 'float':
                # An integral double still has to be spelled with a decimal
                # point, or C# types the literal as an int and `2 / 4` in a
                # number context truncates.
                v = repr(float(node.value))
                return v if ('.' in v or 'e' in v or 'E' in v) else v + '.0'
            return str(node.value)

        if isinstance(node, Identifier):
            m = self._plain_enum_member.get(node.name)
            if m and self.te.get(node.name) == 'unknown':
                return m
            return csid(node.name)

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
            t = self.te.infer(inner)
            base = opt_base(t)
            if base == 'string' or not is_optional(t):
                return f"Cryo.UnwrapS({self._expr(inner)})"
            return f"Cryo.Unwrap({self._expr(inner)})"

        if isinstance(node, FieldAccess):
            if node.field == 'length':
                return f"Cryo.Str({self._expr(node.obj)}).Length"
            # `Enum.MEMBER` resolves to the C# member; a struct field does not.
            ot = self.te.infer(node.obj)
            if isinstance(node.obj, Identifier) and self.te.is_enum(node.obj.name) \
                    and not self.te.is_struct(ot):
                return f"{node.obj.name}.{node.field}"
            return f"{self._expr(node.obj)}.{csid(node.field)}"

        if isinstance(node, IndexAccess):
            ot = self.te.infer(node.obj)
            obj, idx = self._expr(node.obj), self._expr(node.index)
            if is_map(ot):
                return f"Cryo.Get({obj}, {idx})"
            if ot == 'string':
                return f"Cryo.CharAt({obj}, {idx})"
            return f"Cryo.At({obj}, {idx})"

        if isinstance(node, ArrayLiteral):
            if not node.elements:
                return "new List<object>()"
            et = self.te.infer(node.elements[0])
            items = ', '.join(self._expr(x) for x in node.elements)
            return f"new List<{cs_type(et)}> {{ {items} }}"

        if isinstance(node, MapLiteral):
            if not node.pairs:
                return "new Dictionary<string, object>()"
            kt = self.te.infer(node.pairs[0][0])
            vt = self.te.infer(node.pairs[0][1])
            parts = ', '.join(f"{{ {self._expr(k)}, {self._expr(v)} }}"
                              for k, v in node.pairs)
            return (f"new Dictionary<{cs_type(kt)}, {cs_type(vt)}> "
                    f"{{ {parts} }}")

        if isinstance(node, StructInit):
            fields = ', '.join(f"{csid(k)} = {self._expr(v)}"
                               for k, v in node.fields)
            return f"new {node.struct_name} {{ {fields} }}"

        if isinstance(node, CallExpr):
            return self._call(node)

        if isinstance(node, MethodCallExpr):
            return self._method(node)

        if isinstance(node, (SpawnExpr, AwaitExpr)):
            self._err("concurrency (spawn/await) is not supported in the C# "
                      "backend; use --backend go or pyro.")

        if isinstance(node, (Lambda, CallValueExpr)):
            self._err("first-class functions are not yet supported in the C# "
                      "backend; use --backend go, node or pyro.")

        if isinstance(node, CastExpr):
            self._err("'as T' (JSON casting) is not yet supported in the C# "
                      "backend; use --backend go, node or pyro.")

        if isinstance(node, TryExpr):
            self._err("propagation '?' is not yet supported in the C# backend; "
                      "use --backend go, node or pyro.")

        self._err(f"'{type(node).__name__}' is not supported in the C# backend; "
                  f"use --backend go, node or pyro.")

    def _binary(self, node: BinaryExpr) -> str:
        op = node.op
        lt, rt = self.te.infer(node.left), self.te.infer(node.right)

        if op == '&&':
            return f"({self._truthy(node.left)} && {self._truthy(node.right)})"
        if op == '||':
            return f"({self._truthy(node.left)} || {self._truthy(node.right)})"

        if op == '??':
            # C#'s own `??` has the same meaning, and it evaluates its left
            # side once — which is what 11.27 needed a statement expression for
            # in C.
            return f"({self._expr(node.left)} ?? {self._expr(node.right)})"

        l, r = self._expr(node.left), self._expr(node.right)

        # String concatenation: the non-string side is rendered, so `"n: " + 2`
        # gives "n: 2" and not whatever C#'s ToString does with a double.
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

        if op in ('/', '%') and lt == 'int' and rt == 'int':
            fn = 'Cryo.IDiv' if op == '/' else 'Cryo.IMod'
            return f"{fn}({l}, {r})"

        # int/number mixing: C# promotes, but an int literal on both sides of
        # `/` would do integer division inside a number context.
        if op in ('+', '-', '*', '/', '%') and {lt, rt} == {'int', 'number'}:
            if lt == 'int':
                l = f"(double)({l})"
            if rt == 'int':
                r = f"(double)({r})"
            return f"({l} {op} {r})"

        # Containers compare by identity, and a struct does too — Cryo's rule
        # (PYRO_RUNTIME.md), and C#'s default for a class, so nothing to add.
        if op in ('==', '!=') and lt == 'string' and rt == 'string':
            eq = "string.Equals(" + l + ", " + r + ", StringComparison.Ordinal)"
            return eq if op == '==' else f"(!{eq})"

        return f"({l} {op} {r})"

    def _method(self, node: MethodCallExpr) -> str:
        obj = self._expr(node.obj)
        if node.method == 'push':
            return f"{obj}.Add({self._expr(node.args[0])})"
        ot = self.te.infer(node.obj)
        self._err(f"method '.{node.method}()' on a value of type '{ot}' is not "
                  f"supported in the C# backend; use --backend go, node or pyro.")

    # ── builtin calls ────────────────────────────────────────
    #
    # Mapped one at a time rather than through a table, because most need the
    # ARGUMENT'S TYPE to pick the right form: `sum` over ints and over numbers
    # are different methods, `slice` is polymorphic over array and string, and
    # `abs` must not return a long for a double. codegen_c learned the same
    # lesson the hard way (abs typed 'int' unconditionally truncated -2.5).

    def _call(self, node: CallExpr) -> str:
        c = node.callee
        a = node.args
        E = lambda i: self._expr(a[i])
        T = lambda i: self.te.infer(a[i])

        if c == 'print':
            if not a:
                return "Console.WriteLine()"
            return f"Cryo.Print({E(0)})"
        if c == 'throw' and len(a) == 1:
            # Modelled as an exception so try/catch works; the VALUE a catch
            # binds is the message string (12.12), not the exception object.
            return f"throw new CryoThrow({self._to_str(a[0])})"
        if c == 'to_string' and len(a) == 1:
            return self._to_str(a[0])
        if c == 'to_int' and len(a) == 1:
            t = T(0)
            if t == 'string':
                return f"Cryo.ToInt({E(0)})"
            return E(0) if t == 'int' else f"((long)({E(0)}))"
        if c == 'to_number' and len(a) == 1:
            t = T(0)
            if t == 'string':
                return f"Cryo.ToNum({E(0)})"
            return E(0) if t == 'number' else f"((double)({E(0)}))"

        if c == 'len' and len(a) == 1:
            t = T(0)
            if t == 'string':
                return f"((long){E(0)}.Length)"
            if is_map(t):
                return f"((long){E(0)}.Count)"
            return f"((long){E(0)}.Count)"

        # ── math ──
        if c == 'abs' and len(a) == 1:
            return (f"System.Math.Abs({E(0)})")
        if c in ('min', 'max') and len(a) == 2:
            fn = 'System.Math.Min' if c == 'min' else 'System.Math.Max'
            if {T(0), T(1)} == {'int', 'number'}:
                return f"{fn}((double)({E(0)}), (double)({E(1)}))"
            return f"{fn}({E(0)}, {E(1)})"
        if c == 'sqrt' and len(a) == 1:
            return f"System.Math.Sqrt({E(0)})"
        if c == 'pow' and len(a) == 2:
            return f"System.Math.Pow({E(0)}, {E(1)})"
        if c == 'hypot' and len(a) == 2:
            return f"System.Math.Sqrt(({E(0)})*({E(0)}) + ({E(1)})*({E(1)}))"
        if c in ('floor', 'ceil', 'round') and len(a) == 1:
            fn = {'floor': 'Floor', 'ceil': 'Ceiling', 'round': 'Round'}[c]
            if c == 'round':
                # Away-from-zero, not .NET's banker's rounding: round(2.5) is
                # 3 on every other backend and would be 2 by default here.
                return (f"System.Math.Round((double)({E(0)}), "
                        f"MidpointRounding.AwayFromZero)")
            return f"System.Math.{fn}((double)({E(0)}))"
        if c == 'sign' and len(a) == 1:
            return f"Cryo.SignI({E(0)})"
        if c == 'gcd' and len(a) == 2:
            return f"Cryo.Gcd({E(0)}, {E(1)})"
        if c == 'clamp' and len(a) == 3:
            allint = all(T(i) == 'int' for i in range(3))
            fn = 'Cryo.ClampI' if allint else 'Cryo.ClampF'
            if allint:
                return f"{fn}({E(0)}, {E(1)}, {E(2)})"
            return (f"{fn}((double)({E(0)}), (double)({E(1)}), "
                    f"(double)({E(2)}))")

        # ── strings ──
        if c == 'upper' and len(a) == 1:
            return f"{E(0)}.ToUpperInvariant()"
        if c == 'lower' and len(a) == 1:
            return f"{E(0)}.ToLowerInvariant()"
        if c == 'trim' and len(a) == 1:
            return f"{E(0)}.Trim()"
        if c == 'contains' and len(a) == 2:
            if T(0) == 'string':
                return f"{E(0)}.Contains({E(1)})"
            return f"(Cryo.IndexOf({E(0)}, {E(1)}) >= 0)"
        if c == 'find' and len(a) == 2:
            return f"Cryo.Find({E(0)}, {E(1)})"
        if c == 'starts_with' and len(a) == 2:
            return f"{E(0)}.StartsWith({E(1)}, StringComparison.Ordinal)"
        if c == 'ends_with' and len(a) == 2:
            return f"{E(0)}.EndsWith({E(1)}, StringComparison.Ordinal)"
        if c == 'repeat' and len(a) == 2:
            return f"Cryo.Repeat({E(0)}, {E(1)})"
        if c == 'pad_start' and len(a) == 3:
            return f"Cryo.PadStart({E(0)}, {E(1)}, {E(2)})"
        if c == 'pad_end' and len(a) == 3:
            return f"Cryo.PadEnd({E(0)}, {E(1)}, {E(2)})"
        if c == 'replace' and len(a) == 3:
            return f"Cryo.ReplaceAll({E(0)}, {E(1)}, {E(2)})"
        if c == 'split' and len(a) == 2:
            return f"Cryo.Split({E(0)}, {E(1)})"
        if c == 'join' and len(a) == 2:
            return f"Cryo.Join({E(0)}, {E(1)})"
        if c == 'substr' and len(a) == 3:
            return f"Cryo.Substr({E(0)}, {E(1)}, {E(2)})"

        # ── collections ──
        if c == 'sort' and len(a) == 1:
            return f"Cryo.Sorted({E(0)})"
        if c == 'reverse' and len(a) == 1:
            return f"Cryo.Reversed({E(0)})"
        if c == 'slice' and len(a) == 3:
            # Polymorphic over array and string (10.9), and C# is not, so the
            # inferred operand type picks the helper.
            fn = 'Cryo.SliceStr' if T(0) == 'string' else 'Cryo.Slice'
            return f"{fn}({E(0)}, {E(1)}, {E(2)})"
        if c == 'index_of' and len(a) == 2:
            return f"Cryo.IndexOf({E(0)}, {E(1)})"
        if c == 'count' and len(a) == 2:
            return f"Cryo.CountOf({E(0)}, {E(1)})"
        if c == 'concat' and len(a) == 2:
            return f"Cryo.Concat({E(0)}, {E(1)})"
        if c == 'sum' and len(a) == 1:
            et = elem_type(T(0))
            if et == 'number':
                return f"Cryo.SumF({E(0)})"
            if et == 'int':
                return f"Cryo.SumI({E(0)})"
            self._err(f"'sum()' needs a numeric array in the C# backend "
                      f"(element type is '{et}').")

        # ── maps ──
        if c == 'has' and len(a) == 2:
            return f"Cryo.Has({E(0)}, {E(1)})"
        if c == 'keys' and len(a) == 1:
            return f"Cryo.Keys({E(0)})"
        if c == 'remove' and len(a) == 2:
            return f"Cryo.Remove({E(0)}, {E(1)})"

        # ── I/O the C# backend can honestly do ──
        if c == 'input' and not a:
            return "(Console.ReadLine() ?? \"\")"

        if c in ('llm', 'agent', 'llm_call', 'llm_try', 'agent_call',
                 'agent_try', 'llm_stream', 'skills', 'skill_get'):
            self._err(f"'{c}()' is part of the LLM layer and is not available "
                      f"in the C# backend; use --backend go.")
        if c in ('http_get', 'http_post', 'http_serve', 'http_listen',
                 'http_accept', 'http_respond', 'spawn', 'sleep'):
            self._err(f"'{c}()' is not yet available in the C# backend; "
                      f"use --backend go or pyro.")

        # a user function
        args = ', '.join(self._expr(x) for x in a)
        return f"{csid(c)}({args})"
