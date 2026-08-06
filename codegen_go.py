# ============================================================
#  Cryo Compiler - Go Code Generator  (v0.5)
#  .cryo  ->  .go  (Native Go, compilable with `go build`)
#
#  Go becomes the base compilation language for Cryo: it is
#  high level, multiplatform, with a single `go build`, and covers
#  naturally structs, arrays, strings, enums and exceptions.
#  (The assembly backend remains available for future use.)
# ============================================================
from ast_nodes import *
from foreign import collect_imports, resolve_library_lang
import json
from typing import List, Dict, Set, Optional


class CodeGenGoError(Exception):
    pass


# ── Cryo -> Go type mapping ──────────────────────────

GO_TYPE: Dict[str, str] = {
    'int':    'int64',
    'number': 'float64',
    'string': 'string',
    'bool':   'bool',
    'void':   '',
}

GO_KEYWORDS = {
    'break', 'case', 'chan', 'const', 'continue', 'default', 'defer',
    'else', 'fallthrough', 'for', 'func', 'go', 'goto', 'if', 'import',
    'interface', 'map', 'package', 'range', 'return', 'select', 'struct',
    'switch', 'type', 'var', 'nil', 'true', 'false', 'len', 'cap', 'make',
    'new', 'append', 'copy', 'delete', 'init', 'main',
}


def gid(name: str) -> str:
    """Avoids collision of Cryo identifiers with Go keywords."""
    return name + '_' if name in GO_KEYWORDS else name


def _split_type_pair(s: str):
    """Splits 'K,V' respecting nesting of <> and []."""
    depth = 0
    for i, c in enumerate(s):
        if c in '<[':
            depth += 1
        elif c in '>]':
            depth -= 1
        elif c == ',' and depth == 0:
            return s[:i].strip(), s[i + 1:].strip()
    raise CodeGenGoError(f"tipo map malformado: '{s}'")


def _split_top_commas(s: str):
    """Splits by top-level commas, respecting <> [] ()."""
    if not s.strip():
        return []
    out, depth, start = [], 0, 0
    for i, c in enumerate(s):
        if c in '<[(':
            depth += 1
        elif c in '>])':
            depth -= 1
        elif c == ',' and depth == 0:
            out.append(s[start:i].strip()); start = i + 1
    out.append(s[start:].strip())
    return out


def _go_fn_type(t: str) -> str:
    """'fn(P1,P2)->R' -> 'func(goP1, goP2) goR' (R empty if void)."""
    i, depth = 3, 1
    while i < len(t) and depth:
        if t[i] == '(':
            depth += 1
        elif t[i] == ')':
            depth -= 1
        if depth == 0:
            break
        i += 1
    params = t[3:i]
    rest = t[i + 1:]
    ret = rest[2:] if rest.startswith('->') else ''
    gp = ', '.join(go_type(p) for p in _split_top_commas(params))
    gr = go_type(ret) if ret and ret != 'void' else ''
    return f"func({gp})" + (f" {gr}" if gr else "")


def go_type(t: str) -> str:
    if not t:
        return ''
    if t.startswith('(') and t.endswith(')'):
        return go_type(t[1:-1])
    if t.startswith('fn(') and '->' in t:     # function type -> func(...)...
        return _go_fn_type(t)
    if t.endswith('?'):                       # optional -> pointer
        return '*' + go_type(t[:-1])
    if t.startswith('map<') and t.endswith('>'):
        k, v = _split_type_pair(t[4:-1])
        return f"map[{go_type(k)}]{go_type(v)}"
    if t.startswith('future<') and t.endswith('>'):
        # 12.11 — a future used to BE the channel, so `await f` was a receive
        # and awaiting twice blocked forever. It now holds its value.
        return f"*cryoFuture[{go_type(t[7:-1])}]"
    if t.endswith('[]'):
        return '[]' + go_type(t[:-2])
    return GO_TYPE.get(t, t)   # structs/enums pass straight through


def is_future(t: str) -> bool:
    return bool(t) and t.startswith('future<') and t.endswith('>')


def future_elem(t: str) -> str:
    return t[7:-1] if is_future(t) else 'unknown'


def go_field(name: str) -> str:
    """Exported field name in Go (necessary for encoding/json)."""
    return name[:1].upper() + name[1:] if name else name


def is_map(t: str) -> bool:
    return bool(t) and t.startswith('map<') and t.endswith('>')


def is_optional(t: str) -> bool:
    return bool(t) and t.endswith('?')


def _is_null(node, t: str) -> bool:
    """Is this operand the null literal? (ISSUES/18)

    Inference reports `null` for the literal, but a null that reached the
    generator through a typed slot can arrive as the node alone, so both are
    checked.
    """
    return t == 'null' or (isinstance(node, Literal) and node.kind == 'null')


def elem_type(arr_t: str) -> str:
    if not arr_t:
        return 'unknown'
    if arr_t.endswith('[]'):
        return arr_t[:-2]
    if is_map(arr_t):                          # value of a map
        return _split_type_pair(arr_t[4:-1])[1]
    return 'unknown'


def map_key_type(t: str) -> str:
    return _split_type_pair(t[4:-1])[0] if is_map(t) else 'unknown'


def zero_value(t: str) -> str:
    """'null'/zero value for a Go type."""
    if t == 'int':    return '0'
    if t == 'number': return '0.0'
    if t == 'string': return '""'
    if t == 'bool':   return 'false'
    return 'nil'


# ── Type Inference (for string concat, print, etc.) ─

class TypeEnv:
    def __init__(self):
        self._scopes: List[Dict[str, str]] = [{}]
        self._fns:    Dict[str, str] = {}
        self._fn_params: Dict[str, List[str]] = {}
        self._structs: Dict[str, Dict[str, str]] = {}
        self._enums:  Set[str] = set()

    def push(self): self._scopes.append({})
    def pop(self):  self._scopes.pop()
    def set(self, name, typ): self._scopes[-1][name] = typ

    def get(self, name):
        for s in reversed(self._scopes):
            if name in s:
                return s[name]
        return 'unknown'

    def reg_fn(self, name, ret, params=None):
        self._fns[name] = ret
        # 11.31 — parameter types, so an `any` argument can be asserted to what
        # the callee actually declared. Only return types were recorded, which
        # is why `takes(a)` with `any a` reached Go as `a * 2` and failed with
        # "mismatched types any and untyped int".
        if params is not None:
            self._fn_params[name] = [pt for pt, _pn in params]

    def fn_ret(self, name):      return self._fns.get(name, 'unknown')
    def fn_param(self, name, i):
        ps = self._fn_params.get(name)
        return ps[i] if ps and i < len(ps) else 'unknown'
    def reg_struct(self, name, fields): self._structs[name] = fields
    def struct_field(self, s, f): return self._structs.get(s, {}).get(f, 'unknown')
    def reg_enum(self, name):     self._enums.add(name)
    def is_enum(self, name):      return name in self._enums

    def infer(self, node) -> str:
        if node is None: return 'unknown'
        if isinstance(node, Literal):
            return {'int': 'int', 'float': 'number', 'string': 'string',
                    'bool': 'bool', 'null': 'null'}.get(node.kind, 'unknown')
        if isinstance(node, Identifier):
            return self.get(node.name)
        if isinstance(node, BinaryExpr):
            if node.op in ('==', '!=', '<', '>', '<=', '>=', '&&', '||'):
                return 'bool'
            lt = self.infer(node.left); rt = self.infer(node.right)
            if node.op == '??':
                return lt if lt not in ('null', 'unknown') else rt
            if lt == 'string' or rt == 'string': return 'string'
            if lt == 'number' or rt == 'number': return 'number'
            return lt if lt != 'unknown' else rt
        if isinstance(node, UnaryExpr):
            return 'bool' if node.op == '!' else self.infer(node.operand)
        if isinstance(node, TernaryExpr):
            t = self.infer(node.then_value)
            return t if t not in ('unknown', 'null') else self.infer(node.else_value)
        if isinstance(node, CallExpr):
            builtin = {'sqrt': 'number', 'pow': 'number', 'to_string': 'string',
                       'to_int': 'int', 'to_number': 'number', 'len': 'int',
                       'input': 'string', 'abs': 'number', 'floor': 'number',
                       'ceil': 'number', 'round': 'number', 'json_encode': 'string',
                       'skills': 'string[]', 'skill_get': 'Skill',
                       'skill_has': 'bool', 'skills_json': 'string',
                       'pyro_exec': 'string', 'pyro_env': 'string',
                       'pyro_args': 'string[]', 'pyro_time': 'int',
                       'pyro_read': 'string', 'pyro_write_file': 'bool',
                       'pyro_open': 'bool',
                       'sign': 'int', 'gcd': 'int', 'hypot': 'number',
                       'upper': 'string', 'lower': 'string', 'trim': 'string',
                       'contains': 'bool', 'find': 'int', 'replace': 'string',
                       'substr': 'string', 'split': 'string[]', 'join': 'string',
                       'starts_with': 'bool', 'ends_with': 'bool', 'repeat': 'string',
                       'http_get': 'string',
                       'http_post': 'string', 'schema_of': 'string',
                       'llm': 'string', 'tools': 'string[]',
                       # 11.17 — a stream handle is an int, its token a string
                       'llm_stream': 'int', 'llm_next': 'bool',
                       'llm_token': 'string', 'llm_close': 'bool',
                       'llm_call': 'string[]',   # 11.19 [kind, text|detail]
                       'agent_call': 'string[]',  # 11.20
                       'tools_json': 'string', 'tool_get': 'Tool',
                       'agent': 'string', 'index_of': 'int', 'count': 'int',
                       'pad_start': 'string', 'pad_end': 'string'}.get(node.callee)
            # these preserve the type of their first argument
            #
            # 13.1 — `abs` belonged here and was in the table above as
            # 'number' instead. The EMITTER already picks cryoAbsI (int64) for
            # an int argument, so the inferred type contradicted the code being
            # generated: `abs(a) > (b - a)` became
            #     cryoAbsI(a) > float64(cryoSubOvf(b, a))
            # and the Go compiler rejected it. Even `int b = abs(a) + 1;`
            # failed. Found by the differential generator on its first real
            # run, and it had gone unnoticed because a compile-only check never
            # runs the Go compiler over the result.
            if node.callee in ('abs', 'clamp', 'min', 'max', 'sort', 'reverse',
                               'slice', 'concat') and node.args:
                return self.infer(node.args[0])
            return builtin or self.fn_ret(node.callee)
        if isinstance(node, StructInit):
            return node.struct_name
        if isinstance(node, ArrayLiteral):
            return 'array'
        if isinstance(node, MapLiteral):
            return 'map'
        if isinstance(node, CastExpr):
            return node.target_type
        if isinstance(node, UnwrapExpr):
            t = self.infer(node.operand)
            return t[:-1] if t.endswith('?') else t
        if isinstance(node, SpawnExpr):
            return f"future<{self.infer(node.expr)}>"
        if isinstance(node, AwaitExpr):
            t = self.infer(node.expr)
            return t[7:-1] if t.startswith('future<') and t.endswith('>') else t
        if isinstance(node, FieldAccess):
            if node.field == 'length': return 'int'
            return self.struct_field(self.infer(node.obj), node.field)
        if isinstance(node, IndexAccess):
            return elem_type(self.infer(node.obj))
        return 'unknown'


# ── CodeGen Go ──────────────────────────────────────────────

class CodeGenGo:
    def __init__(self, safe: bool = True, sandbox: bool = False):
        self.te = TypeEnv()
        self._safe = safe
        self._sandbox = sandbox
        self._imports: Set[str] = set()
        self._helpers: Set[str] = set()
        self._enum_defs:   List[str] = []
        self._struct_defs: List[str] = []
        self._global_defs: List[str] = []
        self._fn_defs:     List[str] = []
        self._main:        List[str] = []
        self._cur:  List[str] = self._main
        self._indent = 1
        self._safe_stack: List[bool] = []
        self._loop_depth = 0
        self._ntmp = 0                 # fresh temporaries (e.g.: '?' propagation)
        self._cur_fn_ret = 'void'
        self._cur_fn_name = ''        # named in foreign-block diagnostics
        # ── Pyro layer: native skills and machine access ──
        self._skills: List[SkillDecl] = []
        self._use_skills = False
        # ── Phase 3: Native LLM — 'tool' functions exposed to models ──
        self._tools: List[FunctionDecl] = []
        self._use_tools = False
        self._member_to_enum: Dict[str, str] = {}
        # 12.9 — member spelling -> the Go constant, for enums with NO data.
        # Such an enum compiles to `const ( E_A E = iota; E_B )`, so a bare `A`
        # was simply undefined. Kept separate from the data-carrying path,
        # whose members are constructor functions, not constants.
        self._plain_enum_member: Dict[str, str] = {}
        # 11.30 — generated variant struct name -> its Cryo member name, so
        # cryoStr can lead an enum value with `tag:` the way pyro and node do.
        self._enum_tags: Dict[str, str] = {}

    @property
    def _safe_mode(self) -> bool:
        return self._safe_stack[-1] if self._safe_stack else self._safe

    # ── emission ─────────────────────────────────────────────

    def _emit(self, line: str = ''):
        self._cur.append(('\t' * self._indent + line) if line else '')

    # ── main entry ───────────────────────────────────

    def generate(self, program: Program) -> str:
        self._pre_scan(program.statements)
        # Roadmap 11.1 — a top-level `var` is module state, not a local of
        # main. Types are registered before ANY body is generated, so a
        # function defined above the declaration still infers it.
        self._module_vars = {n.name for n in program.statements
                             if isinstance(n, VarDecl)}
        for n in program.statements:
            if isinstance(n, VarDecl):
                self.te.set(n.name, n.var_type)
        self._imported_langs = collect_imports(program)
        for node in program.statements:
            if isinstance(node, EnumDecl):
                self._cur, self._indent = self._enum_defs, 0
                self._enum(node)
            elif isinstance(node, StructDecl):
                self._cur, self._indent = self._struct_defs, 0
                self._struct(node)
            elif isinstance(node, FunctionDecl):
                self._cur, self._indent = self._fn_defs, 0
                self._fn(node)
            elif isinstance(node, ConstDecl):
                self._cur, self._indent = self._global_defs, 0
                self._const(node)
            elif isinstance(node, VarDecl):
                self._cur, self._indent = self._global_defs, 0
                self._var(node, module=True)
            elif isinstance(node, SkillDecl):
                self._skills.append(node)   # registered; emitted in _assemble
            elif isinstance(node, Library):
                # library >go pkg< -> adds the package to Go imports
                if resolve_library_lang(node, self._imported_langs) == 'go':
                    self._imports.add(node.name)
            elif isinstance(node, Import):
                pass  # enables the language; no code to emit here
            else:
                self._cur, self._indent = self._main, 1
                self._gen(node)
        return self._assemble()

    def _pre_scan(self, stmts):
        # native types always known by the type-checker
        self.te.reg_struct('Skill', {
            'name': 'string', 'desc': 'string', 'model': 'string',
            'tools': 'string[]', 'config': 'map<string,string>'})
        self.te.reg_struct('Tool', {'name': 'string', 'parameters': 'string'})
        for n in stmts:
            if isinstance(n, StructDecl):
                self.te.reg_struct(n.name, {f.name: f.field_type for f in n.fields})
            elif isinstance(n, EnumDecl):
                self.te.reg_enum(n.name)
                # 12.9 — an enum with no data at all compiles to plain Go
                # constants, so its members are VALUES, not constructors.
                if not any(len(m.fields) > 0 for m in n.members):
                    for m in n.members:
                        q = f"{n.name}_{m.name}"
                        self._plain_enum_member[m.name] = q
                        self._plain_enum_member[q] = q
                for m in n.members:
                    self._member_to_enum[m.name] = n.name
                    self._member_to_enum[f"{n.name}_{m.name}"] = n.name
                    # A variant with data compiles to a constructor function, so
                    # register its RETURN TYPE (the enum). Without this,
                    # infer(Ok(x)) is 'unknown' and every context that needs a
                    # concrete Go type falls back to `any` â€” which does not
                    # satisfy the enum interface. That is what made
                    #     Res r = cond ? Ok(x) : Err("e");
                    # emit `func() any {â€¦}()` and fail to compile.
                    self.te.reg_fn(m.name, n.name)
                    self.te.reg_fn(f"{n.name}_{m.name}", n.name)
            elif isinstance(n, FunctionDecl):
                self.te.reg_fn(n.name, n.return_type or 'void', n.params)
            elif isinstance(n, ConstDecl):
                self.te.set(n.name, n.var_type)

    def _assemble(self) -> str:
        # generates helpers and skills FIRST: both can register imports
        # (fmt, bufio, sort, os/exec...) before we assemble the import block.
        helper_lines = self._helper_defs()
        skill_lines = self._skill_defs() if (self._skills or self._use_skills) else []
        tool_lines = self._tool_defs() if (self._tools or self._use_tools) else []
        out = [
            "// ================================================",
            "// [PYRO] Compiled from Cryo -> Native Go  (v0.5)",
            "// Compile: go build file.go   |   Run: go run file.go",
            "// ================================================",
            "package main",
            "",
        ]
        if self._imports:
            out.append("import (")
            for imp in sorted(self._imports):
                out.append(f'\t"{imp}"')
            out.append(")")
            out.append("")
        out += helper_lines
        if skill_lines:
            out += skill_lines + [""]
        if tool_lines:
            out += tool_lines + [""]
        if self._enum_defs:   out += self._enum_defs + [""]
        if self._struct_defs: out += self._struct_defs + [""]
        if self._global_defs: out += self._global_defs + [""]
        if self._fn_defs:     out += self._fn_defs + [""]
        out.append("func main() {")
        out += self._main
        out.append("}")
        out.append("")
        return '\n'.join(out)

    # ── runtime helpers (emitted on demand) ───────────

    def _helper_defs(self) -> List[str]:
        H: List[str] = []
        # sandbox: runtime policy that rejects network/machine natives.
        # Emitted whenever a sensitive builtin is used, so that
        # PYRO_SANDBOX=1 works even without --sandbox; the "baked" value
        # reflects the compilation flag (only tightens, never loosens).
        if 'sandbox' in self._helpers:
            self._imports.update(('os', 'fmt'))
            baked = 'true' if self._sandbox else 'false'
            H += [f'var cryoSandbox = {baked} || os.Getenv("PYRO_SANDBOX") == "1"',
                  "func cryoSandboxGuard(op string) {",
                  "\tif cryoSandbox {",
                  '\t\tfmt.Fprintln(os.Stderr, "[Cryo Security] Sandbox: "+op+'
                  ' " blocked by sandbox policy")',
                  "\t\tos.Exit(1)", "\t}", "}", ""]
        if 'str' in self._helpers:
            self._imports.update(('fmt', 'reflect', 'sort', 'strconv', 'strings'))
            # value_to_string (PYRO_RUNTIME.md §3.1). This used to be
            # `fmt.Sprint(v)`, which renders a slice in Go's OWN notation —
            # "[0 1 2]", space-separated — so `print(a)` and `to_string(a)` read
            # differently on every backend (invariant 1). The canonical form is
            # the VM's: "[a, b, c]" for arrays, "{k: v, ...}" for maps ordered by
            # the key's own text, strings never quoted, nil as "null".
            # 11.30 — variant struct name -> tag. Always emitted (empty when the
            # program has no data-carrying enum) so cryoStr can reference it
            # unconditionally rather than being generated in two variants.
            H += ["var cryoEnumTag = map[string]string{"]
            for struct_name, member in sorted(self._enum_tags.items()):
                H += [f'\t"{struct_name}": "{member}",']
            H += ["}", ""]
            H += ["func cryoStrPairs(keys, vals []string) string {",
                  "\tidx := make([]int, len(keys))",
                  "\tfor i := range idx { idx[i] = i }",
                  "\tsort.SliceStable(idx, func(a, b int) bool "
                  "{ return keys[idx[a]] < keys[idx[b]] })",
                  "\tparts := make([]string, len(idx))",
                  "\tfor i, j := range idx { parts[i] = keys[j] + \": \" + vals[j] }",
                  '\treturn "{" + strings.Join(parts, ", ") + "}"', "}", "",
                  "func cryoStr(v any) string {",
                  '\tif v == nil { return "null" }',
                  "\tswitch x := v.(type) {",
                  "\tcase string:",
                  "\t\treturn x",
                  "\tcase bool:",
                  '\t\tif x { return "true" }',
                  '\t\treturn "false"',
                  "\tcase int64:",
                  "\t\treturn strconv.FormatInt(x, 10)",
                  "\tcase float64:",
                  "\t\treturn strconv.FormatFloat(x, 'g', -1, 64)",
                  "\t}",
                  "\trv := reflect.ValueOf(v)",
                  "\tswitch rv.Kind() {",
                  "\tcase reflect.Pointer, reflect.Interface:",
                  "\t\t// an optional (T?) is a *T; an unset one prints as null",
                  '\t\tif rv.IsNil() { return "null" }',
                  "\t\treturn cryoStr(rv.Elem().Interface())",
                  "\tcase reflect.Slice, reflect.Array:",
                  "\t\tparts := make([]string, rv.Len())",
                  "\t\tfor i := range parts { parts[i] = cryoStr(rv.Index(i).Interface()) }",
                  '\t\treturn "[" + strings.Join(parts, ", ") + "]"',
                  "\tcase reflect.Map:",
                  "\t\tks := rv.MapKeys()",
                  "\t\tkeys := make([]string, len(ks))",
                  "\t\tvals := make([]string, len(ks))",
                  "\t\tfor i, k := range ks {",
                  "\t\t\tkeys[i] = cryoStr(k.Interface())",
                  "\t\t\tvals[i] = cryoStr(rv.MapIndex(k).Interface())",
                  "\t\t}",
                  "\t\treturn cryoStrPairs(keys, vals)",
                  "\tcase reflect.Struct:",
                  "\t\t// structs are maps in the VM, so they render as maps here;",
                  "\t\t// the json tag carries the field's original Cryo name.",
                  "\t\tt := rv.Type()",
                  "\t\tkeys := make([]string, 0, t.NumField()+1)",
                  "\t\tvals := make([]string, 0, t.NumField()+1)",
                  # 11.30 — an enum variant leads with its tag, as on pyro/node
                  '\t\tif tag, ok := cryoEnumTag[t.Name()]; ok {',
                  '\t\t\tkeys = append(keys, "tag")',
                  "\t\t\tvals = append(vals, tag)",
                  "\t\t}",
                  "\t\tfor i := 0; i < t.NumField(); i++ {",
                  "\t\t\tf := t.Field(i)",
                  '\t\t\tif f.PkgPath != "" { continue }',
                  '\t\t\tname := f.Tag.Get("json")',
                  '\t\t\tif name == "" { name = strings.ToLower(f.Name[:1]) + f.Name[1:] }',
                  "\t\t\tkeys = append(keys, name)",
                  "\t\t\tvals = append(vals, cryoStr(rv.Field(i).Interface()))",
                  "\t\t}",
                  "\t\treturn cryoStrPairs(keys, vals)",
                  "\tcase reflect.Bool:",
                  '\t\tif rv.Bool() { return "true" }',
                  '\t\treturn "false"',
                  "\tcase reflect.String:",
                  "\t\treturn rv.String()",
                  "\tcase reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:",
                  "\t\treturn strconv.FormatInt(rv.Int(), 10)",
                  "\tcase reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:",
                  "\t\treturn strconv.FormatUint(rv.Uint(), 10)",
                  "\tcase reflect.Float32, reflect.Float64:",
                  "\t\treturn strconv.FormatFloat(rv.Float(), 'g', -1, 64)",
                  "\t}",
                  "\treturn fmt.Sprint(v)", "}", ""]
        if 'or' in self._helpers:
            H += ["func cryoOr[T comparable](a, b T) T {",
                  "\tvar zero T",
                  "\tif a == zero {", "\t\treturn b", "\t}",
                  "\treturn a", "}", ""]
        if 'assert' in self._helpers:
            # 12.12 — takes only the message: the condition is tested at the
            # call site so the message is not built when the assertion holds.
            H += ["func cryoAssertFail(msg string) {",
                  '\tpanic("[Cryo Assert] " + msg)', "}", ""]
        if 'index' in self._helpers:
            # 12.13 — the Pyro VM's text, verbatim, including the asymmetry
            # that GET reports the length and SET does not, and that a string
            # index has its own wording with neither. Those are the shapes the
            # VM and the C VM already produce, so they are what to match rather
            # than tidy up: a fourth spelling is the problem being fixed.
            H += ["func cryoIndex[T any](a []T, i int64) T {",
                  "\tif i < 0 || i >= int64(len(a)) {",
                  '\t\tpanic(fmt.Sprintf("[Cryo Security] IndexError: index %d out of bounds (len=%d)", i, len(a)))',
                  "\t}",
                  "\treturn a[i]", "}",
                  "",
                  "func cryoSetIndex[T any](a []T, i int64, v T) {",
                  "\tif i < 0 || i >= int64(len(a)) {",
                  '\t\tpanic(fmt.Sprintf("[Cryo Security] IndexError: index %d out of bounds", i))',
                  "\t}",
                  "\ta[i] = v", "}",
                  "",
                  "func cryoStrIndex(s string, i int64) string {",
                  "\tif i < 0 || i >= int64(len(s)) {",
                  '\t\tpanic("[Cryo Security] IndexError: string index out of bounds")',
                  "\t}",
                  "\treturn string(s[i])", "}", ""]
            self._imports.add('fmt')
        if 'future' in self._helpers:
            # 12.11 — a future HOLDS its result; it does not hand it over.
            #
            # It used to be a buffered channel, and `await` a receive. That made
            # `await f` twice a deadlock ("all goroutines are asleep"), because
            # the single buffered value had already been taken — while the same
            # program on the pyro backend printed the value twice. A crash on
            # one backend and a result on another is invariant 1 broken, and
            # pyro has the defensible reading: a future is a value you can look
            # at, not a queue you drain.
            #
            # `close` rather than a send is what fixes it: a closed channel
            # makes every receive return immediately, for any number of readers
            # and any number of times. The write to v happens-before the close
            # and the read happens-after the receive, so there is no race
            # despite v carrying no lock.
            H += ["type cryoFuture[T any] struct {",
                  "\tdone chan struct{}",
                  "\tv    T",
                  "}",
                  "",
                  "func cryoSpawn[T any](f func() T) *cryoFuture[T] {",
                  "\tfu := &cryoFuture[T]{done: make(chan struct{})}",
                  "\tgo func() {",
                  "\t\tfu.v = f()",
                  "\t\tclose(fu.done)",
                  "\t}()",
                  "\treturn fu",
                  "}",
                  "",
                  "func cryoAwait[T any](fu *cryoFuture[T]) T {",
                  "\t<-fu.done",
                  "\treturn fu.v",
                  "}", ""]
        if 'anycast' in self._helpers:
            # 11.31 — an `any` reaching a typed slot. go is the only backend
            # where the interface is explicit, so this supplies what pyro and
            # node do implicitly.
            #
            # The numeric cases are not laxity. A Cryo `int` is int64 here, but
            # a value that entered the interface as an untyped constant, or
            # came back from json_decode as a float64, is a different dynamic
            # type carrying the same number — and on pyro and node it converts
            # without comment. Refusing it would make go disagree with them
            # again, in the other direction.
            #
            # Anything else PANICS rather than yielding a zero value: `int b =
            # a` succeeding with b == 0 when `a` held a string is the failure
            # the dynamic backends do not have, and a silent wrong number is
            # worse than a stop.
            H += ["func cryoAs[T any](v any) T {",
                  "\tif t, ok := v.(T); ok {", "\t\treturn t", "\t}",
                  "\tvar zero T",
                  "\tif v == nil {", "\t\treturn zero", "\t}",
                  "\tswitch any(zero).(type) {",
                  "\tcase int64:",
                  "\t\tswitch n := v.(type) {",
                  "\t\tcase int:     return any(int64(n)).(T)",
                  "\t\tcase int32:   return any(int64(n)).(T)",
                  "\t\tcase float64: return any(int64(n)).(T)",
                  "\t\t}",
                  "\tcase float64:",
                  "\t\tswitch n := v.(type) {",
                  "\t\tcase int:     return any(float64(n)).(T)",
                  "\t\tcase int32:   return any(float64(n)).(T)",
                  "\t\tcase int64:   return any(float64(n)).(T)",
                  "\t\t}",
                  "\t}",
                  '\tpanic(fmt.Sprintf("[Cryo] value of type %T cannot be used '
                  'as %T", v, zero))',
                  "}", ""]
        if 'addovf' in self._helpers:
            H += ["func cryoAddOvf(a, b int64) int64 {",
                  "\ts := a + b",
                  "\tif (b > 0 && s < a) || (b < 0 && s > a) {",
                  '\t\tpanic("[Cryo Security] Overflow: integer addition")', "\t}",
                  "\treturn s", "}", ""]
        if 'subovf' in self._helpers:
            H += ["func cryoSubOvf(a, b int64) int64 {",
                  "\ts := a - b",
                  "\tif (b < 0 && s < a) || (b > 0 && s > a) {",
                  '\t\tpanic("[Cryo Security] Overflow: integer subtraction")', "\t}",
                  "\treturn s", "}", ""]
        if 'mulovf' in self._helpers:
            H += ["func cryoMulOvf(a, b int64) int64 {",
                  "\tif a == 0 || b == 0 {", "\t\treturn 0", "\t}",
                  "\tif a == -1<<63 && b == -1 || b == -1<<63 && a == -1 {",
                  '\t\tpanic("[Cryo Security] Overflow: integer multiplication")', "\t}",
                  "\ts := a * b",
                  "\tif s/b != a {",
                  '\t\tpanic("[Cryo Security] Overflow: integer multiplication")', "\t}",
                  "\treturn s", "}", ""]
        if 'idiv' in self._helpers:
            H += ["func cryoIDivChk(a, b int64) int64 {",
                  "\tif b == 0 {",
                  '\t\tpanic("[Cryo Security] DivByZero: integer division")', "\t}",
                  "\tif a == -1<<63 && b == -1 {",
                  '\t\tpanic("[Cryo Security] Overflow: INT64_MIN / -1")', "\t}",
                  "\treturn a / b", "}", ""]
        if 'imod' in self._helpers:
            # INT64_MIN % -1 is 0 (well-defined in Go); only division overflows.
            H += ["func cryoIModChk(a, b int64) int64 {",
                  "\tif b == 0 {",
                  '\t\tpanic("[Cryo Security] DivByZero: modulo")', "\t}",
                  "\tif a == -1<<63 && b == -1 {",
                  "\t\treturn 0", "\t}",
                  "\treturn a % b", "}", ""]
        if 'absi' in self._helpers:
            H += ["func cryoAbsI(x int64) int64 { if x < 0 { return -x }; return x }", ""]
        if 'sign' in self._helpers:
            H += ["func cryoSign(x float64) int64 { if x < 0 { return -1 }; if x > 0 { return 1 }; return 0 }", ""]
        if 'gcd' in self._helpers:
            H += ["func cryoGcd(a, b int64) int64 {",
                  "\tif a < 0 { a = -a }; if b < 0 { b = -b }",
                  "\tfor b != 0 { a, b = b, a%b }",
                  "\treturn a", "}", ""]
        if 'repeat' in self._helpers:
            self._imports.add('strings')
            H += ["func cryoRepeat(s string, n int64) string {",
                  "\tif n < 0 { n = 0 }",
                  "\treturn strings.Repeat(s, int(n))", "}", ""]
        if 'sort' in self._helpers:
            self._imports.update(('slices', 'cmp'))
            H += ["// cryoSort returns a new ascending-sorted copy (original unchanged).",
                  "func cryoSort[T cmp.Ordered](a []T) []T {",
                  "\tb := append([]T(nil), a...)",
                  "\tslices.Sort(b)",
                  "\treturn b", "}", ""]
        if 'reverse' in self._helpers:
            H += ["func cryoReverse[T any](a []T) []T {",
                  "\tb := make([]T, len(a))",
                  "\tfor i, v := range a { b[len(a)-1-i] = v }",
                  "\treturn b", "}", ""]
        if 'slice' in self._helpers:
            H += ["// cryoSlice: subarray [start, end) with safe bounds, as a new slice.",
                  "func cryoSlice[T any](a []T, start, end int64) []T {",
                  "\tn := int64(len(a))",
                  "\tif start < 0 { start = 0 }",
                  "\tif end > n { end = n }",
                  "\tif start > end { start = end }",
                  "\tb := make([]T, end-start)",
                  "\tcopy(b, a[start:end])",
                  "\treturn b", "}", ""]
        if 'strslice' in self._helpers:
            H += ["// cryoStrSlice: substring [start, end) with safe bounds.",
                  "// Byte-indexed, matching the VM's slice() on strings.",
                  "func cryoStrSlice(s string, start, end int64) string {",
                  "\tn := int64(len(s))",
                  "\tif start < 0 { start = 0 }",
                  "\tif end > n { end = n }",
                  "\tif start > end { start = end }",
                  "\treturn s[start:end]", "}", ""]
        if 'pad' in self._helpers:
            self._imports.add('strings')
            H += ["// cryoPad: pad_start/pad_end (JS padStart/padEnd semantics).",
                  "func cryoPad(s string, width int, pad string, atStart bool) string {",
                  "\tif len(s) >= width || pad == \"\" { return s }",
                  "\tneed := width - len(s)",
                  "\tvar b strings.Builder",
                  "\tfor b.Len() < need { b.WriteString(pad) }",
                  "\tfiller := b.String()[:need]",
                  "\tif atStart { return filler + s }",
                  "\treturn s + filler", "}", ""]
        if 'concat' in self._helpers:
            H += ["func cryoConcat[T any](a, b []T) []T {",
                  "\tc := make([]T, 0, len(a)+len(b))",
                  "\tc = append(c, a...)",
                  "\tc = append(c, b...)",
                  "\treturn c", "}", ""]
        if 'count' in self._helpers:
            H += ["func cryoCount[T comparable](a []T, x T) int64 {",
                  "\tvar n int64",
                  "\tfor _, v := range a { if v == x { n++ } }",
                  "\treturn n", "}", ""]
        if 'sum' in self._helpers:
            H += ["func cryoSum[T int64 | float64](a []T) T {",
                  "\tvar s T",
                  "\tfor _, v := range a { s += v }",
                  "\treturn s", "}", ""]
        if 'jsonenc' in self._helpers:
            # 12.10 — json.Marshal sorts MAP keys but emits a struct's fields in
            # declaration order, so `json_encode(p)` on a struct disagreed with
            # pyro, where a struct is a map at runtime and every container is
            # rendered by the key's own text. Normalising structs to maps first
            # makes Marshal sort them too, so all three backends agree and the
            # rule is the one cryoStr already states: pairs ordered by key text.
            self._imports.update(('reflect', 'strings', 'fmt'))
            H += ["func cryoJSONNorm(v any) any {",
                  "\trv := reflect.ValueOf(v)",
                  "\tfor rv.Kind() == reflect.Ptr || rv.Kind() == reflect.Interface {",
                  "\t\tif rv.IsNil() {", "\t\t\treturn nil", "\t\t}",
                  "\t\trv = rv.Elem()", "\t}",
                  "\tswitch rv.Kind() {",
                  "\tcase reflect.Struct:",
                  "\t\tm := map[string]any{}",
                  "\t\tt := rv.Type()",
                  "\t\tfor i := 0; i < t.NumField(); i++ {",
                  "\t\t\tf := t.Field(i)",
                  '\t\t\tif f.PkgPath != "" {', "\t\t\t\tcontinue", "\t\t\t}",
                  "\t\t\tname := f.Name",
                  '\t\t\tif tag := f.Tag.Get("json"); tag != "" && tag != "-" {',
                  '\t\t\t\tif c := strings.Index(tag, ","); c >= 0 {',
                  "\t\t\t\t\ttag = tag[:c]", "\t\t\t\t}",
                  '\t\t\t\tif tag != "" {', "\t\t\t\t\tname = tag", "\t\t\t\t}", "\t\t\t}",
                  "\t\t\tm[name] = cryoJSONNorm(rv.Field(i).Interface())", "\t\t}",
                  "\t\treturn m",
                  "\tcase reflect.Slice, reflect.Array:",
                  "\t\tout := make([]any, rv.Len())",
                  "\t\tfor i := 0; i < rv.Len(); i++ {",
                  "\t\t\tout[i] = cryoJSONNorm(rv.Index(i).Interface())", "\t\t}",
                  "\t\treturn out",
                  "\tcase reflect.Map:",
                  "\t\tm := map[string]any{}",
                  "\t\tfor _, k := range rv.MapKeys() {",
                  "\t\t\tm[fmt.Sprint(k.Interface())] = cryoJSONNorm(rv.MapIndex(k).Interface())",
                  "\t\t}",
                  "\t\treturn m",
                  "\t}",
                  "\treturn v", "}",
                  "",
                  "func cryoJSONEncode(v any) string {",
                  "\tb, err := json.Marshal(cryoJSONNorm(v))",
                  "\tif err != nil {", '\t\tpanic("[Cryo] json_encode: " + err.Error())', "\t}",
                  "\treturn string(b)", "}", ""]
        if 'ptr' in self._helpers:
            H += ["func cryoPtr[T any](v T) *T { return &v }", ""]
        if 'orptr' in self._helpers:
            H += ["func cryoOrPtr[T any](p *T, d T) T {",
                  "\tif p != nil {", "\t\treturn *p", "\t}",
                  "\treturn d", "}", ""]
        if 'unwrap' in self._helpers:
            H += ["func cryoUnwrap[T any](p *T) T {",
                  "\tif p == nil {", '\t\tpanic("[Cryo Security] NullPointer: unwrap of null optional")', "\t}",
                  "\treturn *p", "}", ""]
        if 'sameptr' in self._helpers:
            self._imports.add('reflect')
            H += ["func cryoSamePtr(a, b any) bool {",
                  "\tif a == nil && b == nil { return true }",
                  "\tif a == nil || b == nil { return false }",
                  "\treturn reflect.ValueOf(a).Pointer() == reflect.ValueOf(b).Pointer()",
                  "}", ""]
        if 'keys' in self._helpers:
            self._imports.add('sort')
            # Sorted by textual form (PYRO_RUNTIME.md §4). Returning Go's map
            # iteration order raw made keys() NONDETERMINISTIC — Go randomizes
            # it per run — so the same program could print two different orders
            # on the same binary, let alone agree with pyro/node.
            H += ["func cryoKeys[K comparable, V any](m map[K]V) []K {",
                  "\tks := make([]K, 0, len(m))",
                  "\tfor k := range m {", "\t\tks = append(ks, k)", "\t}",
                  "\tsort.SliceStable(ks, func(i, j int) bool "
                  "{ return cryoStr(ks[i]) < cryoStr(ks[j]) })",
                  "\treturn ks", "}", ""]
        if 'listdir' in self._helpers:
            self._imports.update(('os', 'sort'))
            H += ["func cryoListDir(path string) []string {",
                  "\tents, err := os.ReadDir(path)",
                  "\tif err != nil { return []string{} }",
                  "\tnames := make([]string, 0, len(ents))",
                  "\tfor _, e := range ents { names = append(names, e.Name()) }",
                  "\tsort.Strings(names)",
                  "\treturn names", "}", ""]
        if 'parseint' in self._helpers:
            self._imports.update(('strconv', 'strings'))
            H += ["func cryoParseInt(s string) int64 {",
                  "\tn, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64)",
                  "\tif err != nil {",
                  "\t\tpanic(\"[Cryo Security] to_int: '\" + s + \"' is not a valid integer\")",
                  "\t}",
                  "\treturn n", "}", ""]
        if 'parsenum' in self._helpers:
            self._imports.update(('strconv', 'strings'))
            H += ["func cryoParseNum(s string) float64 {",
                  "\tf, err := strconv.ParseFloat(strings.TrimSpace(s), 64)",
                  "\tif err != nil {",
                  "\t\tpanic(\"[Cryo Security] to_number: '\" + s + \"' is not a valid number\")",
                  "\t}",
                  "\treturn f", "}", ""]
        if 'substr' in self._helpers:
            H += ["// cryoSubstr: string slicing with safe limits.",
                  "func cryoSubstr(s string, i, n int64) string {",
                  "\tif i < 0 {", "\t\ti = 0", "\t}",
                  "\tif i > int64(len(s)) {", "\t\ti = int64(len(s))", "\t}",
                  "\tend := i + n",
                  "\tif n < 0 || end > int64(len(s)) {", "\t\tend = int64(len(s))", "\t}",
                  "\treturn s[i:end]", "}", ""]
        if 'input' in self._helpers:
            self._imports.update(('bufio', 'os', 'fmt', 'strings'))
            H += ["var cryoStdin = bufio.NewReader(os.Stdin)",
                  "func cryoInput(prompt string) string {",
                  "\tif prompt != \"\" { fmt.Print(prompt) }",
                  "\ts, _ := cryoStdin.ReadString('\\n')",
                  "\treturn strings.TrimRight(s, \"\\r\\n\")", "}", ""]
        if 'prng' in self._helpers:
            H += ["var cryoPrngState uint64 = 0x853c49e6748fea9b",
                  "func cryoSplitMix64Next() uint64 {",
                  "\tcryoPrngState += 0x9e3779b97f4a7c15",
                  "\tz := cryoPrngState",
                  "\tz = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9",
                  "\tz = (z ^ (z >> 27)) * 0x94d049bb133111eb",
                  "\treturn z ^ (z >> 31)",
                  "}",
                  "func cryoRandom() float64 { return float64(cryoSplitMix64Next()>>11) / 9007199254740992.0 }",
                  "func cryoRandomInt(lo, hi int64) int64 {",
                  "\tif hi < lo { lo, hi = hi, lo }",
                  "\tspan := uint64(hi - lo + 1)",
                  "\treturn lo + int64(cryoSplitMix64Next()%span)",
                  "}",
                  "func cryoSeed(n int64) { cryoPrngState = uint64(n) }", ""]
        if 'monotime' in self._helpers:
            self._imports.add('time')
            H += ["var cryoStartTime = time.Now()",
                  "func cryoMonotonicMs() int64 { return time.Since(cryoStartTime).Milliseconds() }", ""]
        if 'httpget' in self._helpers:
            self._imports.update(('net/http', 'io'))
            H += ["func cryoHTTPGet(url string) string {",
                  '\tcryoSandboxGuard("http_get")',
                  "\tresp, err := http.Get(url)",
                  '\tif err != nil { return "" }',
                  "\tdefer resp.Body.Close()",
                  "\tb, _ := io.ReadAll(resp.Body)",
                  "\treturn string(b)", "}", ""]
        if 'httppost' in self._helpers:
            self._imports.update(('net/http', 'io', 'strings'))
            H += ["func cryoHTTPPost(url, body string) string {",
                  '\tcryoSandboxGuard("http_post")',
                  '\tresp, err := http.Post(url, "application/json", strings.NewReader(body))',
                  '\tif err != nil { return "" }',
                  "\tdefer resp.Body.Close()",
                  "\tb, _ := io.ReadAll(resp.Body)",
                  "\treturn string(b)", "}", ""]
        if 'writebytes' in self._helpers:
            self._imports.add('os')
            H += ["// cryoWriteBytes: writes an int[] as bytes to a file.",
                  "func cryoWriteBytes(path string, data []int64) bool {",
                  '\tcryoSandboxGuard("write_bytes")',
                  "\tbuf := make([]byte, len(data))",
                  "\tfor i, v := range data { buf[i] = byte(v & 0xFF) }",
                  "\treturn os.WriteFile(path, buf, 0644) == nil", "}", ""]
        if 'llm' in self._helpers:
            self._imports.update(('os', 'net/http', 'io', 'encoding/json', 'bytes', 'fmt', 'time'))
            self._imports.add('strconv')
            H += ["// cryoLLMPost: POST to CRYO_LLM_URL. Returns (text, kind,",
                  "// detail); kind is \"\" on success (roadmap 11.19).",
                  "//",
                  "// It used to retry three times with no pause and no",
                  "// discrimination — including on 400 and 401, which cannot",
                  "// improve by being asked again — and returned \"\" for every",
                  "// failure, indistinguishable from an empty completion.",
                  "func cryoLLMPost(payload map[string]any, timeoutMs int64, retries int) (string, string, string) {",
                  '\tcryoSandboxGuard("llm/agent")',
                  '\turl := os.Getenv("CRYO_LLM_URL")',
                  '\tif url == "" {',
                  '\t\tfmt.Fprintln(os.Stderr, "[Cryo LLM] CRYO_LLM_URL undefined; returning empty")',
                  '\t\treturn "", "no_endpoint", "CRYO_LLM_URL is not set"', "\t}",
                  "\tbody, _ := json.Marshal(payload)",
                  "\tif retries < 0 { retries = 0 }",
                  '\tkind, detail := "transport", "no attempt was made"',
                  "\tfor attempt := 0; attempt <= retries; attempt++ {",
                  "\t\tif attempt > 0 {",
                  "\t\t\t// Exponential: 200ms, 400ms, 800ms… capped at 8s. A",
                  "\t\t\t// rate limit answered immediately is just a second",
                  "\t\t\t// rate limit.",
                  "\t\t\twait := time.Duration(200<<uint(attempt-1)) * time.Millisecond",
                  "\t\t\tif wait > 8*time.Second { wait = 8 * time.Second }",
                  "\t\t\tif cryoRetryAfter > 0 {",
                  "\t\t\t\twait = time.Duration(cryoRetryAfter) * time.Second",
                  "\t\t\t\tcryoRetryAfter = 0",
                  "\t\t\t}",
                  "\t\t\ttime.Sleep(wait)",
                  "\t\t}",
                  '\t\treq, _ := http.NewRequest("POST", url, bytes.NewReader(body))',
                  '\t\treq.Header.Set("Content-Type", "application/json")',
                  '\t\tif key := os.Getenv("CRYO_LLM_KEY"); key != "" {',
                  '\t\t\treq.Header.Set("Authorization", "Bearer "+key)', "\t\t}",
                  "\t\tclient := http.DefaultClient",
                  "\t\tif timeoutMs > 0 {",
                  "\t\t\tclient = &http.Client{Timeout: time.Duration(timeoutMs) * time.Millisecond}",
                  "\t\t}",
                  "\t\tresp, err := client.Do(req)",
                  "\t\tif err != nil {",
                  '\t\t\tkind, detail = "transport", err.Error()',
                  '\t\t\tif strings.Contains(strings.ToLower(err.Error()), "timeout") ||',
                  '\t\t\t\tstrings.Contains(strings.ToLower(err.Error()), "deadline") {',
                  '\t\t\t\tkind = "timeout"', "\t\t\t}",
                  "\t\t\tcontinue", "\t\t}",
                  "\t\tout, _ := io.ReadAll(resp.Body)",
                  '\t\tra := resp.Header.Get("Retry-After")',
                  "\t\tresp.Body.Close()",
                  '\t\tif resp.StatusCode < 300 { return string(out), "", "" }',
                  "\t\tdetail = fmt.Sprintf(\"HTTP %d: %s\", resp.StatusCode,",
                  "\t\t\tstrings.TrimSpace(cryoClip(string(out), 200)))",
                  "\t\tswitch {",
                  "\t\tcase resp.StatusCode == 429:",
                  '\t\t\tkind = "rate_limited"',
                  "\t\t\tif n, e := strconv.Atoi(strings.TrimSpace(ra)); e == nil && n > 0 {",
                  "\t\t\t\tcryoRetryAfter = n", "\t\t\t}",
                  "\t\tcase resp.StatusCode == 408:",
                  '\t\t\tkind = "timeout"',
                  "\t\tcase resp.StatusCode >= 500:",
                  '\t\t\tkind = "server_error"',
                  "\t\tdefault:",
                  "\t\t\t// 400, 401, 403, 404 … asking again changes nothing,",
                  "\t\t\t// and each retry costs the caller real time.",
                  '\t\t\tkind = "refused"',
                  "\t\t\treturn string(out), kind, detail",
                  "\t\t}",
                  "\t}",
                  '\treturn "", kind, detail', "}",
                  "",
                  "// Honoured once, on the next attempt, when a 429 supplies it.",
                  "var cryoRetryAfter int",
                  "",
                  "func cryoClip(s string, n int) string {",
                  '\tif len(s) <= n { return s }',
                  '\treturn s[:n] + "…"', "}", "",
                  "// cryoLLM: contrato POST {model, prompt, schema?} -> corpo JSON.",
                  "func cryoLLM(model, prompt, schema string, opts map[string]any) string {",
                  '\tpayload := map[string]any{"model": model, "prompt": prompt}',
                  '\tif schema != "" {',
                  "\t\tvar sc any",
                  '\t\tif json.Unmarshal([]byte(schema), &sc) == nil { payload["schema"] = sc }',
                  "\t}",
                  "\t// 11.16 — generation controls ride in the same payload;",
                  "\t// timeout, repair and retries are ours, not the",
                  "\t// provider's, so they are consumed here.",
                  "\tvar timeoutMs int64",
                  "\tretries := 2",
                  "\tfor k, v := range opts {",
                  '		if k == "repair" { continue }',
                  '\t\tif k == "retries" { retries = cryoOptInt(v); continue }',
                  '\t\tif k == "timeout" {',
                  "\t\t\tswitch n := v.(type) {",
                  "\t\t\tcase int64: timeoutMs = n",
                  "\t\t\tcase int: timeoutMs = int64(n)",
                  "\t\t\tcase float64: timeoutMs = int64(n)",
                  "\t\t\t}",
                  "\t\t\tcontinue", "\t\t}",
                  "\t\tpayload[k] = v", "\t}",
                  "\ttext, _, _ := cryoLLMPost(payload, timeoutMs, retries)",
                  "\treturn text", "}",
                  "",
                  "// Options arrive as any; accept whichever numeric shape.",
                  "func cryoOptInt(v any) int {",
                  "\tswitch n := v.(type) {",
                  "\tcase int64: return int(n)",
                  "\tcase int: return n",
                  "\tcase float64: return int(n)",
                  "\t}",
                  "\treturn 0", "}",
                  "",
                  "// cryoLLMCall: the same request, but the outcome is a VALUE.",
                  "// [0] is the failure kind (empty on success), [1] the text or",
                  "// the detail. Returning it lets `llm_try` build a match-able",
                  "// result with no global error state — which would race the",
                  "// moment two calls ran under spawn (roadmap 11.19).",
                  "func cryoLLMCall(model, prompt, schema string, opts map[string]any) []string {",
                  '\tpayload := map[string]any{"model": model, "prompt": prompt}',
                  '\tif schema != "" {',
                  "\t\tvar sc any",
                  '\t\tif json.Unmarshal([]byte(schema), &sc) == nil { payload["schema"] = sc }',
                  "\t}",
                  "\tvar timeoutMs int64",
                  "\tretries := 2",
                  "\tfor k, v := range opts {",
                  '\t\tif k == "repair" { continue }',
                  '\t\tif k == "retries" { retries = cryoOptInt(v); continue }',
                  '\t\tif k == "timeout" {',
                  "\t\t\tswitch n := v.(type) {",
                  "\t\t\tcase int64: timeoutMs = n",
                  "\t\t\tcase int: timeoutMs = int64(n)",
                  "\t\t\tcase float64: timeoutMs = int64(n)",
                  "\t\t\t}",
                  "\t\t\tcontinue", "\t\t}",
                  "\t\tpayload[k] = v", "\t}",
                  "\ttext, kind, detail := cryoLLMPost(payload, timeoutMs, retries)",
                  '\tif kind == "" { return []string{"", text} }',
                  "\treturn []string{kind, detail}", "}", ""]
        if 'llmtyped' in self._helpers:
            self._imports.update(('strings', 'math', 'fmt', 'os',
                                  'encoding/json'))
            H += ["// ── validated structured output (roadmap 11.18) ──",
                  "// `llm(...) as T` used to json.Unmarshal and discard the",
                  "// error, so a missing field, a wrong type, a fenced reply or",
                  "// prose all produced a zero-valued T. age=0 is",
                  "// indistinguishable from a real zero, which is the worst way",
                  "// for this to fail.",
                  "",
                  "// cryoJSONClean: models wrap JSON in ``` fences or prose.",
                  "// Recovering the object costs nothing and removes the most",
                  "// common reason a well-formed answer is unusable.",
                  "func cryoJSONClean(s string) string {",
                  "\tt := strings.TrimSpace(s)",
                  '\tif strings.HasPrefix(t, "```") {',
                  '\t\tif i := strings.IndexByte(t, \'\\n\'); i >= 0 { t = t[i+1:] }',
                  '\t\tif j := strings.LastIndex(t, "```"); j >= 0 { t = t[:j] }',
                  "\t\tt = strings.TrimSpace(t)", "\t}",
                  '\tif !strings.HasPrefix(t, "{") && !strings.HasPrefix(t, "[") {',
                  '\t\tif i := strings.IndexAny(t, "{["); i >= 0 {',
                  "\t\t\tend := byte('}')",
                  "\t\t\tif t[i] == '[' { end = ']' }",
                  "\t\t\tif j := strings.LastIndexByte(t, end); j > i {",
                  "\t\t\t\tt = t[i : j+1]", "\t\t\t}", "\t\t}", "\t}",
                  "\treturn t", "}",
                  "",
                  "func cryoSchemaPath(base, name string) string {",
                  '\tif base == "" { return name }',
                  '\treturn base + "." + name', "}",
                  "",
                  "// cryoValidate: the JSON-Schema subset this compiler emits —",
                  "// object/array/string/integer/number/boolean, with required.",
                  "// Returns \"\" when the value fits, else what is wrong and where.",
                  "func cryoValidate(v any, schema map[string]any, path string) string {",
                  '\ttyp, _ := schema["type"].(string)',
                  "\tswitch typ {",
                  '\tcase "object":',
                  "\t\tm, ok := v.(map[string]any)",
                  '\t\tif !ok { return cryoWant(path, "an object", v) }',
                  '\t\tif req, ok := schema["required"].([]any); ok {',
                  "\t\t\tfor _, r := range req {",
                  "\t\t\t\tname, _ := r.(string)",
                  "\t\t\t\tif val, present := m[name]; !present || val == nil {",
                  '\t\t\t\t\treturn cryoSchemaPath(path, name) + " is missing"',
                  "\t\t\t\t}", "\t\t\t}", "\t\t}",
                  '\t\tif props, ok := schema["properties"].(map[string]any); ok {',
                  "\t\t\tfor name, ps := range props {",
                  "\t\t\t\tval, present := m[name]",
                  "\t\t\t\tif !present || val == nil { continue }",
                  "\t\t\t\tsub, ok := ps.(map[string]any)",
                  "\t\t\t\tif !ok { continue }",
                  "\t\t\t\tif e := cryoValidate(val, sub, cryoSchemaPath(path, name)); e != \"\" {",
                  "\t\t\t\t\treturn e", "\t\t\t\t}", "\t\t\t}", "\t\t}",
                  '\tcase "array":',
                  "\t\tarr, ok := v.([]any)",
                  '\t\tif !ok { return cryoWant(path, "an array", v) }',
                  '\t\tif items, ok := schema["items"].(map[string]any); ok {',
                  "\t\t\tfor i, e := range arr {",
                  '\t\t\t\tif er := cryoValidate(e, items, fmt.Sprintf("%s[%d]", path, i)); er != "" {',
                  "\t\t\t\t\treturn er", "\t\t\t\t}", "\t\t\t}", "\t\t}",
                  '\tcase "string":',
                  '\t\tif _, ok := v.(string); !ok { return cryoWant(path, "a string", v) }',
                  '\tcase "integer":',
                  "\t\tf, ok := v.(float64)",
                  '\t\tif !ok { return cryoWant(path, "an integer", v) }',
                  '\t\tif f != math.Trunc(f) { return cryoWant(path, "an integer", v) }',
                  '\tcase "number":',
                  '\t\tif _, ok := v.(float64); !ok { return cryoWant(path, "a number", v) }',
                  '\tcase "boolean":',
                  '\t\tif _, ok := v.(bool); !ok { return cryoWant(path, "a boolean", v) }',
                  "\t}",
                  '\treturn ""', "}",
                  "",
                  "func cryoWant(path, want string, got any) string {",
                  "\twhere := path",
                  '\tif where == "" { where = "the reply" }',
                  '\treturn fmt.Sprintf("%s should be %s, got %v", where, want, got)',
                  "}",
                  "",
                  "// cryoLLMTyped: ask, validate, and on failure ask again with",
                  "// the problem stated. Bounded by the `repair` option (default",
                  "// 2 retries, 0 disables). Returns the JSON text to unmarshal.",
                  "func cryoLLMTyped(model, prompt, schema string, opts map[string]any) string {",
                  "\trepairs := 2",
                  '\tif v, ok := opts["repair"]; ok { repairs = cryoOptInt(v) }',
                  "\task := prompt",
                  "\tfor attempt := 0; ; attempt++ {",
                  "\t\ttext := cryoJSONClean(cryoLLM(model, ask, schema, opts))",
                  "\t\tproblem := \"\"",
                  "\t\tvar v any",
                  "\t\tif err := json.Unmarshal([]byte(text), &v); err != nil {",
                  '\t\t\tproblem = "the reply is not valid JSON (" + err.Error() + ")"',
                  "\t\t} else {",
                  "\t\t\tvar sc map[string]any",
                  "\t\t\tif json.Unmarshal([]byte(schema), &sc) == nil {",
                  '\t\t\t\tproblem = cryoValidate(v, sc, "")',
                  "\t\t\t}", "\t\t}",
                  '\t\tif problem == "" { return text }',
                  "\t\tif attempt >= repairs {",
                  '\t\t\tfmt.Fprintf(os.Stderr, "[Cryo LLM] structured reply still invalid after %d attempt(s): %s\\n", attempt+1, problem)',
                  "\t\t\treturn text", "\t\t}",
                  '\t\task = prompt + "\\n\\nYour previous reply could not be used: " +',
                  '\t\t\tproblem + ".\\nReply with JSON only — no prose, no code fences — " +',
                  '\t\t\t"matching this schema exactly:\\n" + schema',
                  "\t}", "}", ""]
        if 'llmstream' in self._helpers:
            self._imports.update(('bufio', 'strings', 'sync'))
            H += ["// ── LLM streaming (roadmap 11.17) ──",
                  "// The request runs in a goroutine feeding a channel, so the",
                  "// program sees each token as it lands instead of waiting for",
                  "// the whole completion. Handles are ints, not opaque values:",
                  "// an `any` cannot be passed to a typed parameter here.",
                  "type cryoStream struct {",
                  "\tch   chan string",
                  "\tcur  string",
                  "\tdone chan struct{}",
                  "\tonce sync.Once",
                  "}",
                  "var cryoStreams = map[int64]*cryoStream{}",
                  "var cryoStreamMu sync.Mutex",
                  "var cryoStreamSeq int64",
                  "",
                  "func cryoLLMStream(model, prompt string, opts map[string]any) int64 {",
                  '\tcryoSandboxGuard("llm/agent")',
                  "\tst := &cryoStream{ch: make(chan string, 64), done: make(chan struct{})}",
                  "\tcryoStreamMu.Lock()",
                  "\tcryoStreamSeq++",
                  "\tid := cryoStreamSeq",
                  "\tcryoStreams[id] = st",
                  "\tcryoStreamMu.Unlock()",
                  '\turl := os.Getenv("CRYO_LLM_URL")',
                  '\tif url == "" {',
                  '\t\tfmt.Fprintln(os.Stderr, "[Cryo LLM] CRYO_LLM_URL undefined; empty stream")',
                  "\t\tclose(st.ch)",
                  "\t\treturn id", "\t}",
                  '\tpayload := map[string]any{"model": model, "prompt": prompt, "stream": true}',
                  "\tvar timeoutMs int64",
                  "\tfor k, v := range opts {",
                  '		if k == "repair" { continue }',
                  '\t\tif k == "timeout" {',
                  "\t\t\tswitch n := v.(type) {",
                  "\t\t\tcase int64: timeoutMs = n",
                  "\t\t\tcase int: timeoutMs = int64(n)",
                  "\t\t\tcase float64: timeoutMs = int64(n)",
                  "\t\t\t}",
                  "\t\t\tcontinue", "\t\t}",
                  "\t\tpayload[k] = v", "\t}",
                  "\tbody, _ := json.Marshal(payload)",
                  "\tgo func() {",
                  "\t\tdefer close(st.ch)",
                  '\t\treq, err := http.NewRequest("POST", url, bytes.NewReader(body))',
                  "\t\tif err != nil { return }",
                  '\t\treq.Header.Set("Content-Type", "application/json")',
                  '\t\treq.Header.Set("Accept", "text/event-stream")',
                  '\t\tif key := os.Getenv("CRYO_LLM_KEY"); key != "" {',
                  '\t\t\treq.Header.Set("Authorization", "Bearer "+key)', "\t\t}",
                  "\t\tclient := http.DefaultClient",
                  "\t\tif timeoutMs > 0 {",
                  "\t\t\tclient = &http.Client{Timeout: time.Duration(timeoutMs) * time.Millisecond}",
                  "\t\t}",
                  "\t\tresp, err := client.Do(req)",
                  "\t\tif err != nil { return }",
                  "\t\tdefer resp.Body.Close()",
                  "\t\t// Server-Sent Events: `data: {json}` per line, `data: [DONE]`",
                  "\t\t// to finish. A non-SSE body is read as one chunk per line,",
                  "\t\t// so a plain provider still streams something usable.",
                  "\t\tsc := bufio.NewScanner(resp.Body)",
                  "\t\tsc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)",
                  "\t\tfor sc.Scan() {",
                  "\t\t\t// `raw` keeps the token's own spacing — providers",
                  "\t\t\t// stream \"word \" with the trailing space, and",
                  "\t\t\t// trimming it would silently reflow the text.",
                  '\t\t\traw := strings.TrimRight(sc.Text(), "\\r")',
                  "\t\t\tline := strings.TrimSpace(raw)",
                  '\t\t\tif line == "" { continue }',
                  "\t\t\tpayload := line",
                  '\t\t\tif strings.HasPrefix(line, "data:") {',
                  '\t\t\t\tpayload = strings.TrimSpace(line[5:])',
                  "\t\t\t}",
                  '\t\t\tif payload == "[DONE]" { return }',
                  "\t\t\tvar chunk struct {",
                  '\t\t\t\tContent string `json:"content"`',
                  '\t\t\t\tDelta   string `json:"delta"`',
                  "\t\t\t}",
                  "\t\t\tif json.Unmarshal([]byte(payload), &chunk) == nil {",
                  '\t\t\t\ttok := chunk.Content',
                  '\t\t\t\tif tok == "" { tok = chunk.Delta }',
                  '\t\t\t\tif tok != "" {',
                  "\t\t\t\t\tselect {",
                  "\t\t\t\t\tcase st.ch <- tok:",
                  "\t\t\t\t\tcase <-st.done: return",
                  "\t\t\t\t\t}",
                  "\t\t\t\t}",
                  "\t\t\t\tcontinue", "\t\t\t}",
                  "\t\t\tselect {",
                  "\t\t\tcase st.ch <- raw:",
                  "\t\t\tcase <-st.done: return",
                  "\t\t\t}",
                  "\t\t}",
                  "\t}()",
                  "\treturn id", "}",
                  "",
                  "// cryoLLMNext: blocks until the next token or the end of the",
                  "// stream. false means finished — the token is read separately",
                  "// so both calls stay concretely typed.",
                  "func cryoLLMNext(id int64) bool {",
                  "\tcryoStreamMu.Lock()",
                  "\tst := cryoStreams[id]",
                  "\tcryoStreamMu.Unlock()",
                  "\tif st == nil { return false }",
                  "\ttok, ok := <-st.ch",
                  "\tif !ok {",
                  "\t\tcryoStreamMu.Lock()",
                  "\t\tdelete(cryoStreams, id)",
                  "\t\tcryoStreamMu.Unlock()",
                  "\t\treturn false", "\t}",
                  "\tst.cur = tok",
                  "\treturn true", "}",
                  "",
                  "// cryoLLMClose: release a stream abandoned early. Without",
                  "// it a `break` mid-stream leaves the producer goroutine",
                  "// blocked on a full channel for the life of the process —",
                  "// harmless in a script, a leak per request in a server.",
                  "func cryoLLMClose(id int64) bool {",
                  "\tcryoStreamMu.Lock()",
                  "\tst := cryoStreams[id]",
                  "\tdelete(cryoStreams, id)",
                  "\tcryoStreamMu.Unlock()",
                  "\tif st == nil { return false }",
                  "\tst.once.Do(func() { close(st.done) })",
                  "\treturn true", "}",
                  "",
                  "func cryoLLMToken(id int64) string {",
                  "\tcryoStreamMu.Lock()",
                  "\tst := cryoStreams[id]",
                  "\tcryoStreamMu.Unlock()",
                  '\tif st == nil { return "" }',
                  "\treturn st.cur", "}", ""]
        if 'agent' in self._helpers:
            self._imports.update(('sync', 'fmt', 'os'))
            H += ["// ── agent loop (roadmap 11.20) ──",
                  "// Contract: POST {model, messages, tools} ->",
                  "//   {\"tool_call\": {name, arguments}}      one call",
                  "//   {\"tool_calls\": [{name, arguments}…]}  several, run together",
                  "//   {\"content\": \"…\"}                      the answer",
                  "type cryoToolReq struct {",
                  '\tName      string          `json:"name"`',
                  '\tArguments json.RawMessage `json:"arguments"`',
                  "}",
                  "",
                  "// cryoAgentCall: [kind, text|detail]; kind is \"\" on success.",
                  "// The step budget used to end with return \"\", which is also",
                  "// what an empty answer looks like — a long run that hit the",
                  "// ceiling was indistinguishable from one that finished.",
                  "func cryoAgentCall(model, prompt string, only []string, maxSteps, maxContext int) []string {",
                  "\ttools := cryoToolList()",
                  "\tif len(only) > 0 {",
                  "\t\tset := map[string]bool{}",
                  "\t\tfor _, n := range only { set[n] = true }",
                  "\t\tf := []Tool{}",
                  "\t\tfor _, t := range tools { if set[t.Name] { f = append(f, t) } }",
                  "\t\ttools = f", "\t}",
                  "\tif maxSteps <= 0 { maxSteps = 8 }",
                  "\tif maxContext <= 0 { maxContext = 24000 }",
                  '\tmessages := []map[string]any{{"role": "user", "content": prompt}}',
                  "\tfor step := 0; step < maxSteps; step++ {",
                  "\t\tmessages = cryoTrimContext(messages, maxContext)",
                  '\t\tresp, kind, detail := cryoLLMPost(map[string]any{"model": model, "messages": messages, "tools": tools}, 0, 2)',
                  '\t\tif kind != "" { return []string{kind, detail} }',
                  "\t\tvar dec struct {",
                  '\t\t\tToolCall  *cryoToolReq  `json:"tool_call"`',
                  '\t\t\tToolCalls []cryoToolReq `json:"tool_calls"`',
                  '\t\t\tContent   string        `json:"content"`',
                  "\t\t}",
                  "\t\tif err := json.Unmarshal([]byte(resp), &dec); err != nil {",
                  '\t\t\treturn []string{"bad_reply", "the agent reply is not valid JSON (" + err.Error() + ")"}',
                  "\t\t}",
                  "\t\tcalls := dec.ToolCalls",
                  "\t\tif dec.ToolCall != nil { calls = append([]cryoToolReq{*dec.ToolCall}, calls...) }",
                  "\t\tif len(calls) == 0 {",
                  '\t\t\treturn []string{"", dec.Content}', "\t\t}",
                  "\t\t// Several tools in one step run TOGETHER: they are",
                  "\t\t// independent by construction — the model asked for them",
                  "\t\t// without seeing any of their results — so serialising",
                  "\t\t// them only adds latency.",
                  "\t\tresults := make([]string, len(calls))",
                  "\t\tif len(calls) == 1 {",
                  "\t\t\tresults[0] = cryoToolCall(calls[0].Name, string(calls[0].Arguments))",
                  "\t\t} else {",
                  "\t\t\tvar wg sync.WaitGroup",
                  "\t\t\tfor i, c := range calls {",
                  "\t\t\t\twg.Add(1)",
                  "\t\t\t\tgo func(i int, c cryoToolReq) {",
                  "\t\t\t\t\tdefer wg.Done()",
                  "\t\t\t\t\tresults[i] = cryoToolCall(c.Name, string(c.Arguments))",
                  "\t\t\t\t}(i, c)",
                  "\t\t\t}",
                  "\t\t\twg.Wait()",
                  "\t\t}",
                  "\t\tfor i, c := range calls {",
                  "\t\t\tmessages = append(messages,",
                  '\t\t\t\tmap[string]any{"role": "assistant", "tool_call": c},',
                  '\t\t\t\tmap[string]any{"role": "tool", "name": c.Name, "content": results[i]})',
                  "\t\t}",
                  "\t}",
                  '\treturn []string{"step_budget",',
                  '\t\tfmt.Sprintf("the agent used all %d steps without reaching an answer", maxSteps)}',
                  "}",
                  "",
                  "// cryoTrimContext: keep the conversation under a character",
                  "// budget by dropping the OLDEST tool exchanges. The first",
                  "// message is the task and is never dropped — losing it leaves",
                  "// the model working on a question it can no longer see.",
                  "func cryoTrimContext(messages []map[string]any, maxContext int) []map[string]any {",
                  "\tsize := func(ms []map[string]any) int {",
                  "\t\tb, _ := json.Marshal(ms)",
                  "\t\treturn len(b)",
                  "\t}",
                  "\tif size(messages) <= maxContext { return messages }",
                  "\tdropped := 0",
                  "\tfor len(messages) > 3 && size(messages) > maxContext {",
                  "\t\tmessages = append(messages[:1], messages[3:]...)   // a call+result pair",
                  "\t\tdropped += 2",
                  "\t}",
                  "\tif dropped > 0 {",
                  '\t\tfmt.Fprintf(os.Stderr, "[Cryo agent] context over %d bytes: dropped the %d oldest tool messages\\n", maxContext, dropped)',
                  "\t}",
                  "\treturn messages", "}",
                  "",
                  "func cryoAgent(model, prompt string, only []string, maxSteps int) string {",
                  "\tr := cryoAgentCall(model, prompt, only, maxSteps, 0)",
                  '\tif r[0] != "" { return "" }',
                  "\treturn r[1]", "}", ""]
        if 'open' in self._helpers:
            self._imports.update(('os/exec', 'runtime'))
            H += ["// cryoOpen: opens a file/URL in the OS default app (browser).",
                  "func cryoOpen(target string) bool {",
                  '\tcryoSandboxGuard("pyro_open")',
                  "\tvar c *exec.Cmd",
                  "\tswitch runtime.GOOS {",
                  '\tcase "windows":',
                  '\t\tc = exec.Command("cmd", "/c", "start", "", target)',
                  '\tcase "darwin":',
                  '\t\tc = exec.Command("open", target)',
                  "\tdefault:",
                  '\t\tc = exec.Command("xdg-open", target)',
                  "\t}",
                  "\treturn c.Start() == nil", "}", ""]
        if 'exec' in self._helpers:
            self._imports.update(('os/exec', 'runtime'))
            H += ["func cryoExec(command string) string {",
                  '\tcryoSandboxGuard("pyro_exec")',
                  "\tvar c *exec.Cmd",
                  '\tif runtime.GOOS == "windows" {',
                  '\t\tc = exec.Command("cmd", "/c", command)',
                  "\t} else {",
                  '\t\tc = exec.Command("sh", "-c", command)',
                  "\t}",
                  "\tout, _ := c.CombinedOutput()",
                  "\treturn string(out)", "}", ""]
        return H

    # ── Pyro: native skills (compiled in the binary) ────────

    def _skill_defs(self) -> List[str]:
        """Emits the Skill type, global registry, and introspection helpers."""
        D = ["// [PYRO] Native LLM Skills — compact, without .md files",
             "type Skill struct {",
             '\tName   string            `json:"name"`',
             '\tDesc   string            `json:"desc"`',
             '\tModel  string            `json:"model"`',
             '\tTools  []string          `json:"tools"`',
             '\tConfig map[string]string `json:"config"`',
             "}", ""]
        # global registry
        entries = []
        for sk in self._skills:
            entries.append(f'\t{self._go_string(sk.name)}: {self._skill_literal(sk)},')
        D.append("var cryoSkills = map[string]Skill{")
        D += entries
        D.append("}")
        D.append("")
        # sorted names (stable output)
        self._imports.add('sort')
        D += ["func cryoSkillNames() []string {",
              "\tns := make([]string, 0, len(cryoSkills))",
              "\tfor n := range cryoSkills {", "\t\tns = append(ns, n)", "\t}",
              "\tsort.Strings(ns)", "\treturn ns", "}", "",
              "func cryoSkillList() []Skill {",
              "\tout := make([]Skill, 0, len(cryoSkills))",
              "\tfor _, n := range cryoSkillNames() {", "\t\tout = append(out, cryoSkills[n])", "\t}",
              "\treturn out", "}", ""]
        return D

    def _skill_literal(self, sk: SkillDecl) -> str:
        known = dict(sk.fields)
        desc  = self._skill_str(known.get('desc'))
        model = self._skill_str(known.get('model'))
        tools = self._skill_tools(known.get('tools'))
        cfg = []
        for k, v in sk.fields:
            if k in ('desc', 'model', 'tools'):
                continue
            cfg.append(f'{self._go_string(k)}: {self._go_string(self._literal_str(v))}')
        config = "map[string]string{" + ", ".join(cfg) + "}"
        return (f'Skill{{Name: {self._go_string(sk.name)}, Desc: {desc}, '
                f'Model: {model}, Tools: {tools}, Config: {config}}}')

    def _skill_str(self, node) -> str:
        if node is None:
            return '""'
        if isinstance(node, Literal) and node.kind == 'string':
            return self._go_string(node.value)
        return self._expr(node)

    def _skill_tools(self, node) -> str:
        if node is None:
            return "[]string{}"
        if isinstance(node, ArrayLiteral):
            items = ', '.join(self._skill_str(e) for e in node.elements)
            return f"[]string{{{items}}}"
        raise CodeGenGoError("'tools' of a skill must be an array of strings.")

    def _literal_str(self, node) -> str:
        """Converts a skill config literal to a string (compiled)."""
        if isinstance(node, Literal):
            if node.kind == 'bool':   return 'true' if node.value else 'false'
            if node.kind == 'string': return str(node.value)
            if node.kind == 'float':  return repr(float(node.value))
            return str(node.value)
        if isinstance(node, UnaryExpr) and node.op == '-' \
                and isinstance(node.operand, Literal):
            return '-' + self._literal_str(node.operand)
        raise CodeGenGoError(
            "skill config values must be literals (string/number/bool).")

    # ── Phase 3: Native LLM (schema, llm, tools) ─────────────

    _JSON_PRIM = {'int': 'integer', 'number': 'number',
                  'string': 'string', 'bool': 'boolean'}

    def _schema_obj(self, typ: str):
        """Recursive JSON Schema (dict) of a Cryo type."""
        if typ in self._JSON_PRIM:
            return {"type": self._JSON_PRIM[typ]}
        if typ.endswith('[]'):
            return {"type": "array", "items": self._schema_obj(typ[:-2])}
        if is_map(typ):
            return {"type": "object"}
        if typ in self.te._structs:
            fields = self.te._structs[typ]
            props = {fn: self._schema_obj(ft) for fn, ft in fields.items()}
            return {"type": "object", "properties": props,
                    "required": list(fields.keys())}
        return {}

    def _json_schema(self, typ: str) -> str:
        """Literal Go string with JSON Schema (generated at compile time)."""
        return self._go_string(json.dumps(self._schema_obj(typ), ensure_ascii=False))

    def _tool_params_schema(self, fn: FunctionDecl):
        props = {pn: self._schema_obj(pt) for pt, pn in fn.params}
        return {"type": "object", "properties": props,
                "required": [pn for _pt, pn in fn.params]}

    def _tool_defs(self) -> List[str]:
        """Tool type + global registry + introspection helpers."""
        # cryoToolArgs/cryoToolErr need these
        self._imports.update(('strings', 'fmt', 'encoding/json'))
        D = ["// [PYRO] LLM Tools — schema derived from function signature",
             "type Tool struct {",
             '\tName       string `json:"name"`',
             '\tParameters string `json:"parameters"`',  # JSON Schema (string)
             "}", "",
             "var cryoTools = map[string]Tool{"]
        for fn in self._tools:
            sch = json.dumps(self._tool_params_schema(fn), ensure_ascii=False)
            D.append(f'\t{self._go_string(fn.name)}: {{Name: {self._go_string(fn.name)}, '
                     f'Parameters: {self._go_string(sch)}}},')
        D += ["}", ""]
        self._imports.add('sort')
        D += ["func cryoToolNames() []string {",
              "\tns := make([]string, 0, len(cryoTools))",
              "\tfor n := range cryoTools {", "\t\tns = append(ns, n)", "\t}",
              "\tsort.Strings(ns)", "\treturn ns", "}", "",
              "func cryoToolList() []Tool {",
              "\tout := make([]Tool, 0, len(cryoTools))",
              "\tfor _, n := range cryoToolNames() {", "\t\tout = append(out, cryoTools[n])", "\t}",
              "\treturn out", "}", ""]
        # dispatcher: receives (name, argsJSON) -> calls real tool -> result
        self._imports.add('encoding/json')
        D += ["// cryoToolCall: executes the 'name' tool with JSON arguments and returns the result.",
              "// cryoToolErr: a failed tool is a RESULT, not the end of the",
              "// loop (roadmap 11.20). The model reads it and can correct",
              "// itself; aborting would throw away the whole run over one bad",
              "// argument.",
              "func cryoToolErr(msg string) string {",
              '\tb, _ := json.Marshal(map[string]string{"error": msg})',
              "\treturn string(b)",
              "}",
              "",
              "// cryoToolArgs: `arguments` is an object in some providers and",
              "// a JSON-ENCODED STRING in others (OpenAI sends",
              '//   "arguments": "{\\"a\\":1}"',
              "// ). Only the object form ever parsed here, and the error was",
              "// discarded — so with the string form every tool ran on zero",
              "// arguments and reported a confident answer about nothing.",
              "func cryoToolArgs(raw string) string {",
              "\tt := strings.TrimSpace(raw)",
              '\tif strings.HasPrefix(t, "\\"") {',
              "\t\tvar s string",
              "\t\tif json.Unmarshal([]byte(t), &s) == nil { return s }",
              "\t}",
              "\treturn t", "}",
              "",
              "func cryoToolCall(name, rawArgs string) (out string) {",
              "\targs := cryoToolArgs(rawArgs)",
              "\t// A tool is ordinary Cryo code and can abort — a division by",
              "\t// zero, an index out of range. Without this the agent loop",
              "\t// dies with it and everything done so far is lost.",
              "\tdefer func() {",
              "\t\tif r := recover(); r != nil {",
              '\t\t\tout = cryoToolErr(fmt.Sprintf("tool %q failed: %v", name, r))',
              "\t\t}",
              "\t}()",
              "\tswitch name {"]
        for fn in self._tools:
            D.append(f"\tcase {self._go_string(fn.name)}:")
            # arguments struct (exported fields + json tag = parameter name)
            fields = '; '.join(
                f'{go_field(pn)} {go_type(pt)} `json:"{pn}"`' for pt, pn in fn.params)
            D.append(f"\t\tvar _a struct {{ {fields} }}")
            # The error used to be discarded, so arguments the model got wrong
            # silently became zero values and the tool ran on them.
            D.append("\t\tif _e := json.Unmarshal([]byte(args), &_a); _e != nil {")
            D.append('\t\t\treturn cryoToolErr("could not read the arguments for '
                     + fn.name + ': " + _e.Error())')
            D.append("\t\t}")
            call_args = ', '.join(f"_a.{go_field(pn)}" for _pt, pn in fn.params)
            if fn.return_type and fn.return_type != 'void':
                D.append(f"\t\t_r := {gid(fn.name)}({call_args})")
                D.append("\t\t_b, _ := json.Marshal(_r)")
                D.append("\t\treturn string(_b)")
            else:
                D.append(f"\t\t{gid(fn.name)}({call_args})")
                D.append('\t\treturn "null"')
        # An unknown name returned "", which reads to the model as a tool that
        # ran and produced nothing.
        D += ["\t}",
              '\treturn cryoToolErr("unknown tool: " + name)', "}", ""]
        return D

    # ── declarations ─────────────────────────────────────────

    def _enum(self, n: EnumDecl):
        has_data = any(len(m.fields) > 0 for m in n.members)
        if not has_data:
            self._enum_defs.append(f"type {gid(n.name)} int64")
            self._enum_defs.append("const (")
            for i, m in enumerate(n.members):
                suffix = f" {gid(n.name)} = iota" if i == 0 else ""
                self._enum_defs.append(f"\t{n.name}_{m.name}{suffix}")
            self._enum_defs.append(")")
        else:
            self._enum_defs.append(f"type {gid(n.name)} interface {{")
            self._enum_defs.append(f"\tis{gid(n.name)}()")
            self._enum_defs.append("}")
            for m in n.members:
                struct_name = f"{gid(n.name)}_{gid(m.name)}"
                self._enum_defs.append(f"type {struct_name} struct {{")
                for idx, t in enumerate(m.fields):
                    self._enum_defs.append(f"\tVal{idx} {go_type(t)}")
                self._enum_defs.append("}")
                self._enum_defs.append(f"func ({struct_name}) is{gid(n.name)}() {{}}")
                # 11.30 — the variant TAG, for printing. On pyro and node an
                # enum value is a tagged map and prints as `{tag: Ok, val0: 5}`;
                # on go it is a struct and cryoStr rendered only its fields —
                # `{val0: 5}` — losing the one part that says WHICH variant it
                # is. Reflection cannot recover the member name from the type,
                # so it is recorded here, where it is known.
                self._enum_tags[struct_name] = m.name
                
                params = ', '.join(f"v{idx} {go_type(t)}" for idx, t in enumerate(m.fields))
                args_struct = ', '.join(f"Val{idx}: v{idx}" for idx in range(len(m.fields)))
                
                # short canonical constructor: Ok(...) / Err(...).
                # (the prefixed 'Result_Ok' would collide with the homonymous struct-type.)
                self._enum_defs.append(f"func {gid(m.name)}({params}) {gid(n.name)} {{")
                self._enum_defs.append(f"\treturn {struct_name}{{{args_struct}}}")
                self._enum_defs.append("}")

    def _struct(self, n: StructDecl):
        # Exported fields (capitalized) + json tag with the original name,
        # so that encoding/json (json_encode/json_decode) works.
        self._struct_defs.append(f"type {gid(n.name)} struct {{")
        for f in n.fields:
            self._struct_defs.append(
                f'\t{go_field(f.name)} {go_type(f.field_type)} `json:"{f.name}"`')
        self._struct_defs.append("}")

    def _fn(self, n: FunctionDecl):
        if getattr(n, 'is_tool', False):
            self._tools.append(n)          # registers as a tool exposed to LLMs
        params = ', '.join(f"{gid(pn)} {go_type(pt)}" for pt, pn in n.params)
        ret = go_type(n.return_type or 'void')
        ret_s = f" {ret}" if ret else ""
        prev_ret = self._cur_fn_ret
        self._cur_fn_ret = n.return_type or 'void'
        prev_name = self._cur_fn_name
        self._cur_fn_name = n.name
        self._emit(f"func {gid(n.name)}({params}){ret_s} {{")
        self.te.push()
        for pt, pn in n.params:
            self.te.set(pn, pt)
        self._indent += 1
        for s in n.body:
            self._gen(s)
        self._indent -= 1
        self.te.pop()
        self._emit("}")
        self._emit()
        self._cur_fn_ret = prev_ret
        self._cur_fn_name = prev_name

    def _const(self, n: ConstDecl):
        self.te.set(n.name, n.var_type)
        # package var: unused is not an error in Go
        self._global_defs.append(
            f"var {gid(n.name)} {go_type(n.var_type)} = {self._expr(n.value)}")

    # ── statements ──────────────────────────────────────────

    def _gen(self, node: Node):
        if   isinstance(node, VarDecl):            self._var(node)
        elif isinstance(node, ConstDecl):          self._local_const(node)
        elif isinstance(node, Assignment):         self._assign(node)
        elif isinstance(node, IndexAssignment):    self._index_assign(node)
        elif isinstance(node, CompoundAssignment): self._compound(node)
        elif isinstance(node, Increment):          self._incr(node)
        elif isinstance(node, Return):             self._return(node)
        elif isinstance(node, If):                 self._if(node)
        elif isinstance(node, While):              self._while(node)
        elif isinstance(node, DoWhile):            self._do_while(node)
        elif isinstance(node, For):                self._for(node)
        elif isinstance(node, ForEach):            self._foreach(node)
        elif isinstance(node, Switch):             self._switch(node)
        elif isinstance(node, MatchStatement):     self._match(node)
        elif isinstance(node, Break):              self._emit("break")
        elif isinstance(node, Continue):           self._emit("continue")
        elif isinstance(node, Assert):             self._assert(node)
        elif isinstance(node, SafetyBlock):        self._safety(node)
        elif isinstance(node, Block):              self._emit("{"); self._indent += 1; [self._gen(s) for s in node.body]; self._indent -= 1; self._emit("}")
        elif isinstance(node, TryCatch):           self._try(node)
        elif isinstance(node, ForeignBlock):       self._foreign(node)
        elif isinstance(node, TryExpr):
            okv = self._go_try(node.operand)
            self._emit(f"_ = {okv}")
        elif isinstance(node, (CallExpr, MethodCallExpr)):
            self._emit(self._stmt_call(node))
        else:
            self._emit(f"// [Go] NOT SUPPORTED: {type(node).__name__}")

    # ── error propagation: expr?  (Phase 8.3) ───────────────
    def _go_try(self, inner: Node) -> str:
        """Emits the temporary + propagation guard and returns the string
        of the 'Ok value' expression (or base of the optional). In the error/null path,
        the function returns early with Err/nil — the function's return type
        must be the same Result (or an optional)."""
        t = self.te.infer(inner)
        self._ntmp += 1
        tmp = f"__try{self._ntmp}"
        self._emit(f"{tmp} := {self._expr(inner)}")
        if is_optional(t):
            self._emit(f"if {tmp} == nil {{ return nil }}")
            return f"(*{tmp})"
        ok = f"{gid(t)}_Ok"
        self._emit(f"if _, __ok := interface{{}}({tmp}).({ok}); !__ok {{ return {tmp} }}")
        return f"{tmp}.({ok}).Val0"

    def _var(self, n: VarDecl, module: bool = False):
        """A variable declaration. `module` emits it at Go PACKAGE scope.

        Roadmap 11.1 — module state is the same declaration in a different
        place, so it shares this method rather than getting its own copy. Only
        two things differ at package scope, and both are hard Go rules:
        `name := value` is a statement and illegal there, and the `_ = name`
        unused-guard is a statement too (package-level vars may go unused).
        """
        self.te.set(n.name, n.var_type)
        gt = go_type(n.var_type)
        vt = n.var_type
        name = gid(n.name)

        if isinstance(n.value, TryExpr):
            okv = self._go_try(n.value.operand)
            self._emit(f"var {name} {gt} = {okv}")
            if not module:
                self._emit(f"_ = {name}")
            return

        if isinstance(n.value, ArrayLiteral):
            lit = self._array_literal(n.value, vt)
            self._emit(f"var {name} {gt} = {lit}" if module else f"{name} := {lit}")
        elif isinstance(n.value, MapLiteral):
            self._emit(f"var {name} {gt} = {self._map_literal(n.value, vt)}")
        elif is_map(vt) and n.value is None:
            # map without value: initializes empty and writable
            self._emit(f"var {name} {gt} = {gt}{{}}" if module else f"{name} := {gt}{{}}")
        elif is_optional(vt) and n.value is not None:
            self._emit(f"var {name} {gt} = {self._to_optional(n.value, vt)}")
        elif is_future(vt) and isinstance(n.value, SpawnExpr):
            # uses the declared element type (avoids 'chan any' by failed inference)
            self._emit(f"var {name} {gt} = {self._spawn(n.value, future_elem(vt))}")
        elif n.value is not None:
            val = self._expr_typed(n.value, vt)
            self._emit(f"var {name} {gt} = {val}")
        else:
            self._emit(f"var {name} {gt}")
        if not module:
            self._emit(f"_ = {name}")   # Go: unused locals are an error

    def _to_optional(self, value: Node, opt_type: str) -> str:
        """Coerces 'value' to optional T?: null->nil; if it's already optional,
        uses directly; else wraps the base value in pointer (cryoPtr)."""
        if isinstance(value, Literal) and value.kind == 'null':
            return 'nil'
        if is_optional(self.te.infer(value)):     # already T? (e.g.: call returning T?)
            return self._expr(value)
        self._helpers.add('ptr')
        base = opt_type[:-1]                      # 'int?' -> 'int'
        base_go = go_type(base)
        inner = self._expr(value)
        if base in ('int', 'number', 'string', 'bool'):
            return f"cryoPtr[{base_go}]({base_go}({inner}))"
        return f"cryoPtr[{base_go}]({inner})"

    def _local_const(self, n: ConstDecl):
        self.te.set(n.name, n.var_type)
        self._emit(f"const {gid(n.name)} {go_type(n.var_type)} = {self._expr(n.value)}")

    def _assign(self, n: Assignment):
        if isinstance(n.value, TryExpr):
            okv = self._go_try(n.value.operand)
            self._emit(f"{gid(n.name)} = {okv}")
            return
        # _expr_typed, not _expr: the declared type of the target is what makes
        # an `any` on the right assertable (11.31).
        self._emit(f"{gid(n.name)} = "
                   f"{self._expr_typed(n.value, self.te.get(n.name))}")

    def _index_assign(self, n: IndexAssignment):
        # 12.13 — same as the read path; maps are left alone, since assigning a
        # new key is how a map grows.
        if self._safe_mode and self.te.infer(n.obj).endswith('[]'):
            self._helpers.add('index')
            self._emit(f"cryoSetIndex({self._expr(n.obj)}, "
                       f"{self._expr(n.index)}, {self._expr(n.value)})")
            return
        self._emit(f"{self._expr(n.obj)}[{self._expr(n.index)}] = {self._expr(n.value)}")

    def _compound(self, n: CompoundAssignment):
        self._emit(f"{gid(n.name)} {n.op} {self._expr(n.value)}")

    def _incr(self, n: Increment):
        self._emit(f"{gid(n.name)}{n.op}")

    def _array_literal(self, node: ArrayLiteral, typ) -> str:
        """Emit an array literal, using the type the CONTEXT expects.

        Go cannot infer an element type from `[]any{…}`, so a literal has to be
        spelled with the type of the place it is going into (declared variable,
        function return, …). Without such a hint we fall back to []any, which is
        right only in a genuinely typeless position.

        The hint has to be passed DOWN as well. Elements used to be emitted
        with plain _expr, which threw the context away one level in, so
        `int[][] n = [[1, 2], [3]]` produced `[][]int64{[]any{…}, …}` and Go
        refused it: "cannot use []any{…} as []int64 value". A nested literal is
        exactly the case where Go can least infer anything, so it is the case
        that most needs the type.
        """
        et = elem_type(typ) if typ and typ.endswith('[]') else None
        parts = []
        for e in node.elements:
            if isinstance(e, ArrayLiteral):
                parts.append(self._array_literal(e, et))
            elif isinstance(e, MapLiteral):
                parts.append(self._map_literal(e, et))
            else:
                # _expr_typed, so an `any` element is asserted to the element
                # type rather than landing in a typed slice untouched (11.31).
                parts.append(self._expr_typed(e, et) if et else self._expr(e))
        elems = ', '.join(parts)
        if typ and typ.endswith('[]'):
            return f"{go_type(typ)}{{{elems}}}"
        return f"[]any{{{elems}}}"

    def _return(self, n: Return):
        if isinstance(n.value, TryExpr):
            okv = self._go_try(n.value.operand)
            self._emit(f"return {okv}")
            return
        if n.value is None:
            self._emit("return")
        elif is_optional(self._cur_fn_ret):
            self._emit(f"return {self._to_optional(n.value, self._cur_fn_ret)}")
        elif isinstance(n.value, ArrayLiteral):
            # `return [1, 2, 3]` in a `-> int[]` function: the literal must be
            # []int64{…}, not []any{…}, or Go rejects the return statement.
            self._emit(f"return {self._array_literal(n.value, self._cur_fn_ret)}")
        else:
            self._emit(f"return {self._expr(n.value)}")

    def _if(self, n: If):
        self._emit(f"if {self._expr(n.condition)} {{")
        self._indent += 1
        self.te.push()
        for s in n.then_body: self._gen(s)
        self.te.pop()
        self._indent -= 1
        if n.else_body:
            if len(n.else_body) == 1 and isinstance(n.else_body[0], If):
                inner = n.else_body[0]
                self._emit(f"}} else if {self._expr(inner.condition)} {{")
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
        self._emit(f"for {self._expr(n.condition)} {{")
        self._indent += 1
        self._loop_depth += 1
        self.te.push()
        for s in n.body: self._gen(s)
        self.te.pop()
        self._loop_depth -= 1
        self._indent -= 1
        self._emit("}")

    def _do_while(self, n: DoWhile):
        self._emit("for {")
        self._indent += 1
        self._loop_depth += 1
        self.te.push()
        for s in n.body: self._gen(s)
        self.te.pop()
        self._emit(f"if !({self._expr(n.condition)}) {{")
        self._indent += 1
        self._emit("break")
        self._indent -= 1
        self._emit("}")
        self._loop_depth -= 1
        self._indent -= 1
        self._emit("}")

    def _for(self, n: For):
        init = self._for_part(n.init) if n.init else ''
        cond = self._expr(n.condition) if n.condition else ''
        upd  = self._for_part(n.update) if n.update else ''
        self.te.push()
        # declares the init variable in scope before emitting
        self._emit(f"for {init}; {cond}; {upd} {{")
        self._indent += 1
        self._loop_depth += 1
        for s in n.body: self._gen(s)
        self._loop_depth -= 1
        self._indent -= 1
        self.te.pop()
        self._emit("}")

    def _foreach(self, n: ForEach):
        self.te.push()
        self.te.set(n.var_name, n.var_type)
        it_t = self.te.infer(n.iterable)
        if it_t == 'string':
            # iterates characters: Go gives runes; converts each to string
            v = gid(n.var_name)
            self._emit(f"for _, _r_{v} := range {self._expr(n.iterable)} {{")
            self._indent += 1
            self._loop_depth += 1
            self._emit(f"{v} := string(_r_{v})")
            self._emit(f"_ = {v}")
            for s in n.body: self._gen(s)
            self._loop_depth -= 1
            self._indent -= 1
            self.te.pop()
            self._emit("}")
            return
        self._emit(f"for _, {gid(n.var_name)} := range {self._expr(n.iterable)} {{")
        self._indent += 1
        self._loop_depth += 1
        self._emit(f"_ = {gid(n.var_name)}")
        for s in n.body: self._gen(s)
        self._loop_depth -= 1
        self._indent -= 1
        self.te.pop()
        self._emit("}")

    def _for_part(self, node: Node) -> str:
        if isinstance(node, VarDecl):
            self.te.set(node.name, node.var_type)
            val = self._expr(node.value) if node.value else zero_value(node.var_type)
            # for-init requires ':='; we explicitly type int/number to
            # avoid inferred 'int' colliding with int64 in the rest of the system
            gt = go_type(node.var_type)
            if node.var_type in ('int', 'number'):
                val = f"{gt}({val})"
            return f"{gid(node.name)} := {val}"
        if isinstance(node, Assignment):
            return f"{gid(node.name)} = {self._expr(node.value)}"
        if isinstance(node, CompoundAssignment):
            return f"{gid(node.name)} {node.op} {self._expr(node.value)}"
        if isinstance(node, Increment):
            return f"{gid(node.name)}{node.op}"
        return self._expr(node)

    def _switch(self, n: Switch):
        self._emit(f"switch {self._expr(n.subject)} {{")
        for case in n.cases:
            vals = ', '.join(self._expr(v) for v in case.values)
            self._emit(f"case {vals}:")
            self._indent += 1
            self.te.push()
            for s in case.body: self._gen(s)
            self.te.pop()
            self._indent -= 1
        if n.default_body is not None:
            self._emit("default:")
            self._indent += 1
            self.te.push()
            for s in n.default_body: self._gen(s)
            self.te.pop()
            self._indent -= 1
        self._emit("}")

    def _match(self, n: MatchStatement):
        expr_str = self._expr(n.subject)
        self._emit(f"switch __m := interface{{}}({expr_str}).(type) {{")
        for case in n.cases:
            if case.pattern_name == '_':
                self._emit("default:")
                self._indent += 1
                self.te.push()
                for s in case.body:
                    self._gen(s)
                self.te.pop()
                self._indent -= 1
                continue
            enum_name = self._member_to_enum.get(case.pattern_name, "Result")
            short_name = case.pattern_name.split('_')[-1]
            struct_name = f"{gid(enum_name)}_{gid(short_name)}"
            self._emit(f"case {struct_name}:")
            self._indent += 1
            self.te.push()
            for idx, var_name in enumerate(case.pattern_vars):
                self._emit(f"{gid(var_name)} := __m.Val{idx}")
                # Go makes an unused variable an error, so an arm that ignores
                # part of a payload — `Err(a, b) => print(a)` — failed to
                # compile here while running fine on pyro and node. A pattern
                # binding is a name the author chose to READ, not to use.
                self._emit(f"_ = {gid(var_name)}")
                self.te.set(var_name, "any")
            for s in case.body:
                self._gen(s)
            self.te.pop()
            self._indent -= 1
        self._emit("}")

    def _assert(self, n: Assert):
        self._helpers.add('assert')
        cond = self._expr(n.condition)
        msg = self._expr(n.message) if n.message is not None \
            else f'"assert failed (line {n.line})"'
        # 12.12 — lazy: Go evaluates call arguments eagerly, so handing the
        # message to cryoAssert built it on every assert, passing or not.
        self._emit(f"if !({cond}) {{ cryoAssertFail({msg}) }}")

    def _safety(self, n: SafetyBlock):
        tag = 'safe' if n.safe else 'unsafe'
        self._emit(f"{{ // bloco {tag}")
        self._indent += 1
        self._safe_stack.append(n.safe)
        self.te.push()
        for s in n.body: self._gen(s)
        self.te.pop()
        self._safe_stack.pop()
        self._indent -= 1
        self._emit("}")

    def _try(self, n: TryCatch):
        # Go has no exceptions: uses closure + defer/recover.
        self._emit("func() {")
        self._indent += 1
        if n.catch_body is not None or n.finally_body:
            self._emit("defer func() {")
            self._indent += 1
            if n.catch_body is not None:
                self._helpers.add('str')
                self._emit("if r := recover(); r != nil {")
                self._indent += 1
                var = n.catch_name or "_cryo_err"
                self._emit(f"{gid(var)} := cryoStr(r)")
                self._emit(f"_ = {gid(var)}")
                self.te.push()
                self.te.set(var, 'string')
                for s in n.catch_body: self._gen(s)
                self.te.pop()
                self._indent -= 1
                self._emit("}")
            if n.finally_body:
                self.te.push()
                for s in n.finally_body: self._gen(s)
                self.te.pop()
            self._indent -= 1
            self._emit("}()")
        self.te.push()
        for s in n.try_body: self._gen(s)
        self.te.pop()
        self._indent -= 1
        self._emit("}()")

    def _foreign(self, n: ForeignBlock):
        lang = n.lang.lower()
        if lang == 'go':
            self._emit("// -- [bloco Go] --")
            for line in n.code.strip().split('\n'):
                self._emit(line.strip())
            self._emit("// -- [/bloco Go] --")
        elif lang in ('html', 'css'):
            # These used to become a lone comment, so a function whose entire
            # body was a page fragment compiled to an empty function and the
            # program silently produced nothing. Refusing and naming the
            # backend that renders pages is the honest answer — go cannot
            # return the text either, since the enclosing function is
            # typically declared void.
            raise CodeGenGoError(
                f"'>{n.lang}(' blocks build a page and the Go backend does "
                f"not render one — it would drop the block and leave "
                f"'{self._cur_fn_name or 'this function'}' empty. Use "
                f"--backend frontend (or --backend auto, which now picks it), "
                f"adding --emit pyro if the page calls Cryo functions.")
        else:
            self._emit(f"// [Cryo] >{n.lang}< block omitted in Go backend "
                       f"(use print(...) or >Go( ... ))")

    # ── expressions ──────────────────────────────────────────

    def _expr_typed(self, node: Node, target: str) -> str:
        """Expression with knowledge of the target type (handles null)."""
        if isinstance(node, Literal) and node.kind == 'null':
            return zero_value(target)
        return self._assert_any(self._expr(node), self.te.infer(node), target)

    # 11.31 — an `any` value reaching a typed slot.
    #
    # `any a = 5; int b = a;` runs on pyro and node, where the conversion is
    # implicit, and failed to COMPILE on go: "cannot use a (variable of
    # interface type any) as int64 value: need type assertion". Go is the only
    # backend that makes the interface explicit, so the code generator has to
    # supply what the dynamic backends do for free.
    #
    # It is a CHECKED assertion, `v.(T)`, not `v.(T)` with the comma-ok form
    # discarded: a wrong type must panic at the point of the mistake. Silently
    # substituting a zero value would make `int b = a` succeed with b == 0 when
    # `a` held a string — the dynamic backends raise there, and matching them
    # matters more than avoiding a panic.
    def _assert_any(self, src: str, from_t: str, to_t: str) -> str:
        if from_t != 'any' or not to_t or to_t in ('any', 'unknown'):
            return src
        gt = go_type(to_t)
        if not gt or gt in ('any', 'interface{}'):
            return src
        self._helpers.add('anycast')
        return f"cryoAs[{gt}]({src})"

    def _expr(self, node: Node) -> str:
        if isinstance(node, TryExpr):
            raise CodeGenGoError(
                "propagation '?' is only supported at the level of an assignment, "
                "declaration ('T x = expr?;'), return or expression-statement — "
                "not nested inside another expression.")
        if isinstance(node, Literal):
            if node.kind == 'null':   return 'nil'
            if node.kind == 'bool':   return 'true' if node.value else 'false'
            if node.kind == 'string': return self._go_string(node.value)
            if node.kind == 'int':    return str(node.value)
            if node.kind == 'float':  return repr(float(node.value))
            return str(node.value)

        if isinstance(node, Identifier):
            # 12.9 — a bare member of a data-less enum is the Go constant it
            # was declared as. Without this the emitted `A` was undefined: the
            # program compiled here and failed in the Go compiler.
            if self.te.get(node.name) == 'unknown' and node.name in self._plain_enum_member:
                return self._plain_enum_member[node.name]
            return gid(node.name)

        if isinstance(node, BinaryExpr):
            return self._binary(node)

        if isinstance(node, TernaryExpr):
            return self._ternary(node)

        if isinstance(node, UnaryExpr):
            op = {'!': '!', '~': '^', '-': '-'}.get(node.op, node.op)
            return f"({op}{self._expr(node.operand)})"

        if isinstance(node, CallExpr):
            return self._call(node)

        if isinstance(node, CallValueExpr):
            # calling the result of an expression: `f(a)(b)`
            args = ', '.join(self._expr(x) for x in node.args)
            return f"{self._expr(node.callee)}({args})"

        if isinstance(node, MethodCallExpr):
            return self._method(node)

        if isinstance(node, FieldAccess):
            # 12.9 — `Status.ATIVO` is a qualified enum member, not a field
            # read. A variable of the same name still wins, so a struct value
            # called `Status` keeps reading its own field.
            if (isinstance(node.obj, Identifier)
                    and self.te.is_enum(node.obj.name)
                    and self.te.get(node.obj.name) == 'unknown'):
                q = self._plain_enum_member.get(f"{node.obj.name}_{node.field}")
                if q:
                    return q
            obj = self._expr(node.obj)
            if node.field == 'length':
                return f"int64(len({obj}))"
            return f"{obj}.{go_field(node.field)}"

        if isinstance(node, IndexAccess):
            # 12.13 — go had NO bounds check at all. An out-of-range index
            # surfaced Go's own `panic: runtime error: index out of range [5]
            # with length 2`, so the same program aborted with a message the
            # other three engines never produce — and a constant bad index did
            # not even compile ("must not be negative"), which is a third
            # failure mode again. Routing through a helper gives all four the
            # VM's text and turns the compile error into the same abort.
            ot = self.te.infer(node.obj)
            if self._safe_mode and ot == 'string':
                self._helpers.add('index')
                return f"cryoStrIndex({self._expr(node.obj)}, {self._expr(node.index)})"
            if self._safe_mode and ot.endswith('[]'):
                self._helpers.add('index')
                return f"cryoIndex({self._expr(node.obj)}, {self._expr(node.index)})"
            return f"{self._expr(node.obj)}[{self._expr(node.index)}]"

        if isinstance(node, ArrayLiteral):
            return self._array_literal(node, None)   # typeless context

        if isinstance(node, MapLiteral):
            return self._map_literal(node, None)

        if isinstance(node, StructInit):
            fields_code = []
            for k, v in node.fields:
                f_type = self.te.struct_field(node.struct_name, k)
                if isinstance(v, ArrayLiteral):
                    v_code = self._array_literal(v, f_type)
                elif isinstance(v, MapLiteral):
                    v_code = self._map_literal(v, f_type)
                else:
                    v_code = self._expr(v)
                fields_code.append(f"{go_field(k)}: {v_code}")
            return f"{gid(node.struct_name)}{{{', '.join(fields_code)}}}"

        if isinstance(node, CastExpr):
            return self._cast(node)

        if isinstance(node, UnwrapExpr):
            self._helpers.add('unwrap')
            return f"cryoUnwrap({self._expr(node.operand)})"

        if isinstance(node, SpawnExpr):
            return self._spawn(node)

        if isinstance(node, AwaitExpr):
            self._helpers.add('future')
            return f"cryoAwait({self._expr(node.expr)})"

        if isinstance(node, Lambda):
            return self._lambda(node)

        return f"/* EXPR? {type(node).__name__} */"

    def _lambda_ret(self, node: 'Lambda') -> str:
        """Infers the return type by the first Return in the body."""
        self.te.push()
        for pt, pn in node.params:
            self.te.set(pn, pt)
        rt = 'void'
        for s in node.body:
            if isinstance(s, Return) and s.value is not None:
                rt = self.te.infer(s.value)
                break
        self.te.pop()
        return rt

    def _lambda(self, node: 'Lambda') -> str:
        params = ', '.join(f"{gid(pn)} {go_type(pt)}" for pt, pn in node.params)
        ret_t = node.return_type or self._lambda_ret(node)
        gr = go_type(ret_t) if ret_t and ret_t not in ('void', 'unknown', 'null') else ''
        sig = f"func({params})" + (f" {gr}" if gr else "")
        prev_cur, prev_ret = self._cur, self._cur_fn_ret
        buf: List[str] = []
        self._cur = buf
        self._cur_fn_ret = ret_t or 'void'
        self.te.push()
        for pt, pn in node.params:
            self.te.set(pn, pt)
        self._indent += 1
        for s in node.body:
            self._gen(s)
        self._indent -= 1
        self.te.pop()
        self._cur = prev_cur
        self._cur_fn_ret = prev_ret
        body = '\n'.join(buf)
        return sig + " {\n" + body + "\n" + ('\t' * self._indent) + "}"

    def _spawn(self, node: SpawnExpr, elem: Optional[str] = None) -> str:
        # spawn e  ->  a goroutine writing into a cryoFuture (12.11)
        t = elem or self.te.infer(node.expr)
        gt = go_type(t) if t not in ('unknown', 'null', 'array') else 'any'
        inner = self._expr(node.expr)
        self._helpers.add('future')
        return f"cryoSpawn(func() {gt} {{ return {inner} }})"

    def _map_literal(self, node: MapLiteral, map_type: Optional[str]) -> str:
        if map_type and is_map(map_type):
            gt = go_type(map_type)
        else:
            gt = "map[any]any"   # no target type: generic fallback
        pairs = ', '.join(f"{self._expr(k)}: {self._expr(v)}" for k, v in node.pairs)
        return f"{gt}{{{pairs}}}"

    def _cast(self, node: CastExpr) -> str:
        target = node.target_type
        gt = go_type(target)
        inner = node.expr
        # json_decode(s) as T  ->  typed Unmarshal
        if isinstance(inner, CallExpr) and inner.callee == 'json_decode':
            self._imports.add('encoding/json')
            src = self._expr(inner.args[0]) if inner.args else '""'
            return (f"func() {gt} {{ var _v {gt}; "
                    f"_ = json.Unmarshal([]byte({src}), &_v); return _v }}()")
        # llm("model", prompt) as T  ->  typed structured output (Phase 3)
        if isinstance(inner, CallExpr) and inner.callee == 'llm':
            self._imports.add('encoding/json')
            self._helpers.update(('llm', 'sandbox'))
            model = self._expr(inner.args[0]) if inner.args else '""'
            prompt = self._expr(inner.args[1]) if len(inner.args) > 1 else '""'
            schema = self._json_schema(target)
            opts = self._llm_opts(inner.args[2] if len(inner.args) > 2 else None)
            # 11.18 — cryoLLMTyped validates against the schema and re-asks on
            # failure. The Unmarshal error is reported rather than dropped: it
            # used to be assigned to _, so any bad reply became a zero value.
            self._helpers.add('llmtyped')
            self._imports.update(('fmt', 'os'))
            return (f"func() {gt} {{ var _v {gt}; "
                    f"if _e := json.Unmarshal([]byte(cryoLLMTyped({model}, {prompt}, {schema}, {opts})), &_v); _e != nil {{ "
                    f'fmt.Fprintln(os.Stderr, "[Cryo LLM] could not read the structured reply:", _e) }}; '
                    f"return _v }}()")
        # numeric conversions
        if target in ('int', 'number'):
            return f"{gt}({self._expr(inner)})"
        # type assertion (any -> T)
        return f"{self._expr(inner)}.({gt})"

    def _binary(self, node: BinaryExpr) -> str:
        lt = self.te.infer(node.left)
        rt = self.te.infer(node.right)
        l  = self._expr(node.left)
        r  = self._expr(node.right)
        op = node.op

        # 11.31 — an `any` operand in arithmetic. Go will not add an interface
        # to a number, so the side that is `any` is asserted to the other's
        # type. Doing it here rather than only at declarations and call
        # arguments matters because the 11.21 optimizer INLINES small
        # functions: `takes(a)` with `fn takes(int n) = n * 2` arrives here as
        # `a * 2` with the parameter's declared type already gone, so a fix
        # that only looked at call sites would work until the optimizer ran.
        if op not in ('&&', '||', '??') and lt != rt:
            if lt == 'any' and rt not in ('any', 'unknown', 'null'):
                l, lt = self._assert_any(l, 'any', rt), rt
            elif rt == 'any' and lt not in ('any', 'unknown', 'null'):
                r, rt = self._assert_any(r, 'any', lt), lt

        if op == '&&': return f"({l} && {r})"
        if op == '||': return f"({l} || {r})"
        if op == '??':
            if is_optional(lt):
                self._helpers.add('orptr')
                return f"cryoOrPtr({l}, {r})"
            self._helpers.add('or')
            return f"cryoOr({l}, {r})"

        # string concatenation (converts non-string operand)
        if op == '+' and (lt == 'string' or rt == 'string'):
            ls = l if lt == 'string' else self._to_str(l, node.left)
            rs = r if rt == 'string' else self._to_str(r, node.right)
            return f"({ls} + {rs})"

        # bitwise and shift: direct
        if op in ('&', '|', '^', '<<', '>>'):
            return f"({l} {op} {r})"

        # container equality (slice / map identity)
        is_cont = lambda t, n: (t.endswith('[]') or t.startswith('map<') or t in ('array', 'map') or
                                isinstance(n, (ArrayLiteral, MapLiteral)))
        if op in ('==', '!=') and (lt == 'null' or isinstance(node.left, Literal) and node.left.kind == 'null') and (rt == 'null' or isinstance(node.right, Literal) and node.right.kind == 'null'):
            return 'true' if op == '==' else 'false'
        if op in ('==', '!=') and (is_cont(lt, node.left) or is_cont(rt, node.right)):
            if lt == 'null' or rt == 'null' or (isinstance(node.left, Literal) and node.left.kind == 'null') or (isinstance(node.right, Literal) and node.right.kind == 'null'):
                return f"({l} {op} nil)"
            self._helpers.add('sameptr')
            if op == '==':
                return f"cryoSamePtr({l}, {r})"
            return f"(!cryoSamePtr({l}, {r}))"

        # ISSUES/18 — a NON-nilable value compared against null.
        #
        # `string s = ""; print(s == null);` is false on both VMs and on node,
        # and PYRO_RUNTIME.md says so: null is equal only to null. Go is the
        # only backend where the comparison has to type-check, and falling
        # through to the generic path emitted `("" == nil)`, which the Go
        # compiler rejects outright — the program did not build at all.
        #
        # An int, a number, a string, a bool and a struct have no nil in Go, so
        # the answer is a constant and is folded here. Slices, maps, optionals
        # (*T), function values, futures and `any` are all nilable and are
        # handled above or by the generic path; `unknown` is left alone rather
        # than guessed at.
        if op in ('==', '!=') and (_is_null(node.left, lt) or _is_null(node.right, rt)):
            other_t = rt if _is_null(node.left, lt) else lt
            other_n = node.right if _is_null(node.left, lt) else node.left
            if other_t not in ('unknown', 'any', 'null') and not is_optional(other_t) \
                    and not is_future(other_t) and not other_t.startswith('fn(') \
                    and not is_cont(other_t, other_n):
                return 'false' if op == '==' else 'true'

        # int<->number coercion: Go does not mix int64 and float64. If one side is
        # 'number' and the other 'int', converts the integer to float64.
        if op in ('+', '-', '*', '/', '%', '<', '>', '<=', '>=', '==', '!=') \
                and {lt, rt} == {'int', 'number'}:
            if lt == 'int': l = f"float64({l})"
            if rt == 'int': r = f"float64({r})"
            return f"({l} {op} {r})"

        # security instrumentation (integers)
        both_int = (lt == 'int' and rt == 'int')
        if both_int and op in ('+', '-', '*') and self._safe_mode:
            fn = {'+': 'cryoAddOvf', '-': 'cryoSubOvf', '*': 'cryoMulOvf'}[op]
            self._helpers.add({'+': 'addovf', '-': 'subovf', '*': 'mulovf'}[op])
            return f"{fn}({l}, {r})"
        if both_int and op == '/':
            self._helpers.add('idiv'); return f"cryoIDivChk({l}, {r})"
        if both_int and op == '%':
            self._helpers.add('imod'); return f"cryoIModChk({l}, {r})"

        return f"({l} {op} {r})"

    def _llm_opts(self, node) -> str:
        """The generation-options argument of llm() as a Go map (11.16).

        Built here rather than by _expr on the map literal, because that infers
        one element type from the first value — and these options are a mix of
        float, int and string by nature, so `map[string]any` is the only shape
        that holds them. The parser has already checked the names and the
        literal kinds; values may be arbitrary expressions.
        """
        if node is None:
            return "nil"
        if not isinstance(node, MapLiteral):
            return "nil"
        parts = []
        for k, v in node.pairs:
            key = k.value if isinstance(k, Literal) else getattr(k, 'name', '')
            parts.append(f'"{key}": {self._expr(v)}')
        return "map[string]any{" + ", ".join(parts) + "}"

    def _ternary(self, node: TernaryExpr) -> str:
        # Go has no ?:; uses IIFE with inferred type (lazy evaluation)
        t = self.te.infer(node.then_value)
        gt = go_type(t) if t not in ('unknown', 'null', 'array') else 'any'
        cond = self._expr(node.condition)
        a = self._expr(node.then_value)
        b = self._expr(node.else_value)
        return f"func() {gt} {{ if {cond} {{ return {a} }}; return {b} }}()"

    def _to_str(self, expr: str, node: Node) -> str:
        self._helpers.add('str')
        return f"cryoStr({expr})"

    def _enum_ctor_name(self, callee: str) -> str:
        """`Result_Ok` -> `Ok` when that names an enum variant (11.33).

        The generated Go has a struct `Result_Ok` and a constructor `Ok`, so
        the mangled call has to be routed to the latter. Only rewritten when
        the prefix really is the enum this member belongs to — a user function
        that merely happens to contain an underscore is left alone.
        """
        enum = self._member_to_enum.get(callee)
        if enum and callee.startswith(enum + '_'):
            bare = callee[len(enum) + 1:]
            if self._member_to_enum.get(bare) == enum:
                return gid(bare)
        return gid(callee)

    def _call(self, node: CallExpr) -> str:
        c = node.callee
        a = node.args
        # user-defined functions shadow stdlib builtins (print/len/has/keys stay reserved)
        if c in self.te._fns and c not in ('print', 'len', 'has', 'keys'):
            # 11.33 — the mangled spelling of an enum constructor. Both `Ok` and
            # `Result_Ok` are deliberately accepted (semantic.py registers the
            # arity for each) and both work on pyro and node. On go the mangled
            # one collided with the generated STRUCT of the same name, so
            # `Result_Ok(5)` was read by Go as a type conversion and failed with
            # "cannot convert 5 to type Result_Ok". The constructor function is
            # the bare member name, so emit that.
            callee = self._enum_ctor_name(c)
            args = ', '.join(self._expr_typed(x, self.te.fn_param(c, i))
                             for i, x in enumerate(a))
            return f"{callee}({args})"
        if c == 'print':
            self._imports.add('fmt')
            if not a: return "fmt.Println()"
            # via cryoStr, not fmt.Println's own formatting: Println would
            # render a slice as "[0 1 2]" and a struct as "{1 ana}", neither of
            # which is the canonical form (PYRO_RUNTIME.md §3.1).
            self._helpers.add('str')
            return f"fmt.Println(cryoStr({self._expr(a[0])}))"
        if c == 'sqrt':
            self._imports.add('math'); return f"math.Sqrt({self._expr(a[0])})"
        if c == 'pow':
            self._imports.add('math')
            return f"math.Pow({self._expr(a[0])}, {self._expr(a[1])})"
        if c in ('abs', 'fabs'):
            t = self.te.infer(a[0])
            if t == 'int':
                self._helpers.add('absi'); return f"cryoAbsI({self._expr(a[0])})"
            self._imports.add('math'); return f"math.Abs({self._expr(a[0])})"
        if c in ('min', 'max') and len(a) == 2:
            # Go native builtins (>=1.21): work for int64 and float64
            return f"{c}({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'floor':
            self._imports.add('math'); return f"math.Floor({self._expr(a[0])})"
        if c == 'ceil':
            self._imports.add('math'); return f"math.Ceil({self._expr(a[0])})"
        if c == 'round':
            self._imports.add('math'); return f"math.Round({self._expr(a[0])})"
        if c == 'clamp' and len(a) == 3:
            # Go builtins min/max (>=1.21) handle int64 and float64
            return f"max({self._expr(a[1])}, min({self._expr(a[0])}, {self._expr(a[2])}))"
        if c == 'sign' and len(a) == 1:
            self._helpers.add('sign')
            return f"cryoSign(float64({self._expr(a[0])}))"
        if c == 'gcd' and len(a) == 2:
            self._helpers.add('gcd')
            return f"cryoGcd(int64({self._expr(a[0])}), int64({self._expr(a[1])}))"
        if c == 'hypot' and len(a) == 2:
            self._imports.add('math')
            return f"math.Hypot({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'to_string':
            self._helpers.add('str'); return f"cryoStr({self._expr(a[0])})"
        if c == 'to_int':
            # string -> parse with strconv; numeric -> direct cast
            if self.te.infer(a[0]) == 'string':
                self._helpers.add('parseint')
                return f"cryoParseInt({self._expr(a[0])})"
            return f"int64({self._expr(a[0])})"
        if c == 'to_number':
            if self.te.infer(a[0]) == 'string':
                self._helpers.add('parsenum')
                return f"cryoParseNum({self._expr(a[0])})"
            return f"float64({self._expr(a[0])})"
        if c == 'len':
            return f"int64(len({self._expr(a[0])}))"
        if c == 'input':
            self._helpers.add('input')
            prompt = self._expr(a[0]) if a else '""'
            return f"cryoInput({prompt})"
        if c == 'throw':
            return f"panic({self._expr(a[0])})"
        # ── JSON ──
        if c == 'json_encode':
            self._imports.add('encoding/json')
            self._helpers.add('jsonenc')
            return f"cryoJSONEncode({self._expr(a[0])})"
        if c == 'json_decode':
            raise CodeGenGoError(
                "json_decode(s) requires a target type: use 'json_decode(s) as Type'.")
        # ── maps ──
        if c == 'has' and len(a) == 2:
            # has(map, key) -> existence
            return f"func() bool {{ _, ok := {self._expr(a[0])}[{self._expr(a[1])}]; return ok }}()"
        if c == 'remove' and len(a) == 2:
            return f"delete({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'keys' and len(a) == 1:
            self._helpers.add('keys')
            self._helpers.add('str')     # cryoKeys sorts by the key's text
            return f"cryoKeys({self._expr(a[0])})"
        # ── stateless collection ops (Phase 10.2) ──
        if c == 'sort' and len(a) == 1:
            self._helpers.add('sort')
            return f"cryoSort({self._expr(a[0])})"
        if c == 'reverse' and len(a) == 1:
            self._helpers.add('reverse')
            return f"cryoReverse({self._expr(a[0])})"
        if c == 'slice' and len(a) == 3:
            # slice() is polymorphic over array|string (10.9), but Go's generic
            # cryoSlice only unifies with []T — a string needs its own helper.
            if self.te.infer(a[0]) == 'string':
                self._helpers.add('strslice')
                return (f"cryoStrSlice({self._expr(a[0])}, "
                        f"int64({self._expr(a[1])}), int64({self._expr(a[2])}))")
            self._helpers.add('slice')
            return (f"cryoSlice({self._expr(a[0])}, "
                    f"int64({self._expr(a[1])}), int64({self._expr(a[2])}))")
        if c == 'index_of' and len(a) == 2:
            self._imports.add('slices')
            return f"int64(slices.Index({self._expr(a[0])}, {self._expr(a[1])}))"
        if c == 'concat' and len(a) == 2:
            self._helpers.add('concat')
            return f"cryoConcat({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'count' and len(a) == 2:
            self._helpers.add('count')
            return f"cryoCount({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'sum' and len(a) == 1:
            self._helpers.add('sum')
            return f"cryoSum({self._expr(a[0])})"
        # ── strings ──
        if c == 'upper' and len(a) == 1:
            self._imports.add('strings')
            return f"strings.ToUpper({self._expr(a[0])})"
        if c == 'lower' and len(a) == 1:
            self._imports.add('strings')
            return f"strings.ToLower({self._expr(a[0])})"
        if c == 'trim' and len(a) == 1:
            self._imports.add('strings')
            return f"strings.TrimSpace({self._expr(a[0])})"
        if c == 'contains' and len(a) == 2:
            self._imports.add('strings')
            return f"strings.Contains({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'find' and len(a) == 2 and self.te.infer(a[0]) == 'string':
            self._imports.add('strings')
            return f"int64(strings.Index({self._expr(a[0])}, {self._expr(a[1])}))"
        if c == 'replace' and len(a) == 3:
            self._imports.add('strings')
            return (f"strings.ReplaceAll({self._expr(a[0])}, "
                    f"{self._expr(a[1])}, {self._expr(a[2])})")
        if c == 'substr' and len(a) == 3:
            self._helpers.add('substr')
            return (f"cryoSubstr({self._expr(a[0])}, int64({self._expr(a[1])}), "
                    f"int64({self._expr(a[2])}))")
        if c == 'split' and len(a) == 2:
            self._imports.add('strings')
            return f"strings.Split({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'join' and len(a) == 2:
            self._imports.add('strings')
            return f"strings.Join({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'starts_with' and len(a) == 2:
            self._imports.add('strings')
            return f"strings.HasPrefix({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'ends_with' and len(a) == 2:
            self._imports.add('strings')
            return f"strings.HasSuffix({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'repeat' and len(a) == 2:
            self._helpers.add('repeat')
            return f"cryoRepeat({self._expr(a[0])}, int64({self._expr(a[1])}))"
        if c in ('pad_start', 'pad_end') and len(a) == 3:
            self._helpers.add('pad')
            at_start = 'true' if c == 'pad_start' else 'false'
            return (f"cryoPad({self._expr(a[0])}, int({self._expr(a[1])}), "
                    f"{self._expr(a[2])}, {at_start})")
        if c == 'now_ms':
            self._imports.add('time'); return "time.Now().UnixMilli()"
        if c == 'monotonic_ms':
            self._helpers.add('monotime'); return "cryoMonotonicMs()"
        if c == 'random':
            self._helpers.add('prng'); return "cryoRandom()"
        if c == 'random_int' and len(a) == 2:
            self._helpers.add('prng'); return f"cryoRandomInt({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'seed' and len(a) == 1:
            self._helpers.add('prng'); return f"cryoSeed({self._expr(a[0])})"
        # ── Pyro: introspection of native skills (no .md files) ──
        if c == 'skills':
            self._use_skills = True
            return "cryoSkillNames()"
        if c == 'skill_get' and len(a) == 1:
            self._use_skills = True
            return f"cryoSkills[{self._expr(a[0])}]"
        if c == 'skill_has' and len(a) == 1:
            self._use_skills = True
            return f"func() bool {{ _, ok := cryoSkills[{self._expr(a[0])}]; return ok }}()"
        if c == 'skills_json':
            self._use_skills = True
            self._imports.add('encoding/json')
            self._helpers.add('jsonenc')
            return "cryoJSONEncode(cryoSkillList())"
        # ── Pyro: direct machine access ──
        if c == 'pyro_exec' and len(a) == 1:
            self._helpers.update(('exec', 'sandbox'))
            return f"cryoExec({self._expr(a[0])})"
        if c == 'pyro_env' and len(a) == 1:
            self._imports.add('os')
            return f"os.Getenv({self._expr(a[0])})"
        if c == 'pyro_args':
            self._imports.add('os')
            return "os.Args"
        if c == 'pyro_exit' and len(a) == 1:
            self._imports.add('os')
            return f"os.Exit(int({self._expr(a[0])}))"
        if c == 'pyro_time':
            self._imports.add('time')
            return "time.Now().UnixMilli()"
        if c == 'pyro_write' and len(a) == 1:
            self._imports.add('fmt')
            return f"fmt.Print({self._expr(a[0])})"
        if c == 'pyro_read':
            self._helpers.add('input')
            return 'cryoInput("")'
        if c == 'pyro_write_file' and len(a) == 2:
            self._imports.add('os')
            self._helpers.add('sandbox')
            return (f'func() bool {{ cryoSandboxGuard("pyro_write_file"); '
                    f"return os.WriteFile({self._expr(a[0])}, "
                    f"[]byte({self._expr(a[1])}), 0644) == nil }}()")
        if c == 'pyro_open' and len(a) == 1:
            self._helpers.update(('open', 'sandbox'))
            return f"cryoOpen({self._expr(a[0])})"
        # ── Filesystem & process natives (Roadmap 11.7) ──
        if c == 'file_exists' and len(a) == 1:
            self._imports.add('os')
            return f"func() bool {{ _, err := os.Stat({self._expr(a[0])}); return err == nil }}()"
        if c == 'is_dir' and len(a) == 1:
            self._imports.add('os')
            return f"func() bool {{ st, err := os.Stat({self._expr(a[0])}); return err == nil && st.IsDir() }}()"
        if c == 'list_dir' and len(a) == 1:
            self._helpers.add('listdir')
            return f"cryoListDir({self._expr(a[0])})"
        if c == 'make_dir' and len(a) == 1:
            self._imports.add('os')
            self._helpers.add('sandbox')
            return f"func() bool {{ cryoSandboxGuard(\"make_dir\"); return os.MkdirAll({self._expr(a[0])}, 0755) == nil }}()"
        if c == 'delete_file' and len(a) == 1:
            self._imports.add('os')
            self._helpers.add('sandbox')
            return (f"func() bool {{ cryoSandboxGuard(\"delete_file\"); "
                    f"st, err := os.Stat({self._expr(a[0])}); "
                    f"if err == nil && st.IsDir() {{ return false }}; "
                    f"return os.Remove({self._expr(a[0])}) == nil }}()")
        if c == 'file_size' and len(a) == 1:
            self._imports.add('os')
            return f"func() int64 {{ st, err := os.Stat({self._expr(a[0])}); if err != nil {{ return -1 }}; return st.Size() }}()"
        if c == 'write_file' and len(a) == 2:
            self._imports.add('os')
            self._helpers.add('sandbox')
            return (f"func() bool {{ cryoSandboxGuard(\"write_file\"); "
                    f"return os.WriteFile({self._expr(a[0])}, []byte({self._expr(a[1])}), 0644) == nil }}()")
        if c == 'read_file' and len(a) == 1:
            self._imports.add('os')
            return f"func() string {{ b, err := os.ReadFile({self._expr(a[0])}); if err != nil {{ return \"\" }}; return string(b) }}()"
        if c == 'env' and len(a) == 1:
            self._imports.add('os')
            self._helpers.add('sandbox')
            return f"func() string {{ cryoSandboxGuard(\"env\"); return os.Getenv({self._expr(a[0])}) }}()"
        if c == 'exec' and len(a) == 1:
            self._helpers.update(('exec', 'sandbox'))
            return f"cryoExec({self._expr(a[0])})"
        if c == 'args' and len(a) == 0:
            self._imports.add('os')
            return "os.Args[1:]"
        # ── Phase 2: concurrency / HTTP ──
        if c == 'sleep' and len(a) == 1:
            self._imports.add('time')
            return f"time.Sleep(time.Duration({self._expr(a[0])}) * time.Millisecond)"
        if c == 'http_get' and len(a) == 1:
            self._helpers.update(('httpget', 'sandbox'))
            return f"cryoHTTPGet({self._expr(a[0])})"
        if c == 'http_post' and len(a) == 2:
            self._helpers.update(('httppost', 'sandbox'))
            return f"cryoHTTPPost({self._expr(a[0])}, {self._expr(a[1])})"
        if c == 'write_bytes' and len(a) == 2:
            self._helpers.update(('writebytes', 'sandbox'))
            return f"cryoWriteBytes({self._expr(a[0])}, {self._expr(a[1])})"
        # ── Phase 3: Native LLM ──
        if c == 'schema_of' and len(a) == 1 and isinstance(a[0], Identifier):
            return self._json_schema(a[0].name)
        if c == 'llm':
            self._helpers.update(('llm', 'sandbox'))
            model  = self._expr(a[0]) if a else '""'
            prompt = self._expr(a[1]) if len(a) > 1 else '""'
            opts   = self._llm_opts(a[2] if len(a) > 2 else None)
            return f'cryoLLM({model}, {prompt}, "", {opts})'   # no schema (raw completion)
        # ── 11.20: the agent outcome as a value ──
        if c == 'agent_call':
            self._use_tools = True
            self._helpers.update(('llm', 'sandbox', 'agent'))
            model  = self._expr(a[0]) if a else '""'
            prompt = self._expr(a[1]) if len(a) > 1 else '""'
            only, steps, ctx = "[]string{}", "0", "0"
            if len(a) > 2 and isinstance(a[2], MapLiteral):
                for k, v in a[2].pairs:
                    key = k.value if isinstance(k, Literal) else getattr(k, 'name', '')
                    if key == 'tools' and isinstance(v, ArrayLiteral):
                        elems = ', '.join(self._expr(e) for e in v.elements)
                        only = f"[]string{{{elems}}}"
                    elif key == 'tools':
                        only = self._expr(v)
                    elif key == 'steps':
                        steps = f"int({self._expr(v)})"
                    elif key == 'max_context':
                        ctx = f"int({self._expr(v)})"
            return f'cryoAgentCall({model}, {prompt}, {only}, {steps}, {ctx})'
        # ── 11.19: the outcome as a value ──
        if c == 'llm_call':
            self._helpers.update(('llm', 'sandbox'))
            model  = self._expr(a[0]) if a else '""'
            prompt = self._expr(a[1]) if len(a) > 1 else '""'
            opts   = self._llm_opts(a[2] if len(a) > 2 else None)
            return f'cryoLLMCall({model}, {prompt}, "", {opts})'
        # ── 11.17: streaming ──
        if c == 'llm_stream':
            self._helpers.update(('llmstream', 'sandbox'))
            # not 'io': the streaming reader is bufio, and Go rejects an
            # unused import — the non-streaming helper adds 'io' itself
            self._imports.update(('os', 'net/http', 'encoding/json',
                                  'bytes', 'fmt', 'time'))
            model  = self._expr(a[0]) if a else '""'
            prompt = self._expr(a[1]) if len(a) > 1 else '""'
            opts   = self._llm_opts(a[2] if len(a) > 2 else None)
            return f'cryoLLMStream({model}, {prompt}, {opts})'
        if c == 'llm_next' and len(a) == 1:
            self._helpers.update(('llmstream', 'sandbox'))
            return f'cryoLLMNext({self._expr(a[0])})'
        if c == 'llm_token' and len(a) == 1:
            self._helpers.update(('llmstream', 'sandbox'))
            return f'cryoLLMToken({self._expr(a[0])})'
        if c == 'llm_close' and len(a) == 1:
            self._helpers.update(('llmstream', 'sandbox'))
            return f'cryoLLMClose({self._expr(a[0])})'
        if c == 'tools':
            self._use_tools = True
            return "cryoToolNames()"
        # (see _llm_opts below for the generation-controls argument)
        if c == 'tool_get' and len(a) == 1:
            self._use_tools = True
            return f"cryoTools[{self._expr(a[0])}]"
        if c == 'tools_json':
            self._use_tools = True
            self._imports.add('encoding/json'); self._helpers.add('jsonenc')
            return "cryoJSONEncode(cryoToolList())"
        if c == 'agent':
            self._use_tools = True
            self._helpers.update(('llm', 'sandbox')); self._helpers.add('agent')
            model  = self._expr(a[0]) if a else '""'
            prompt = self._expr(a[1]) if len(a) > 1 else '""'
            # 3rd optional arg: subset of tools (string[]); 4th: step limit
            if len(a) > 2 and isinstance(a[2], ArrayLiteral):
                elems = ', '.join(self._expr(e) for e in a[2].elements)
                tools_arg = f"[]string{{{elems}}}"
            elif len(a) > 2:
                tools_arg = self._expr(a[2])
            else:
                tools_arg = "[]string{}"
            steps_arg = f"int({self._expr(a[3])})" if len(a) > 3 else "8"
            return f"cryoAgent({model}, {prompt}, {tools_arg}, {steps_arg})"
        args = ', '.join(self._expr(x) for x in a)
        return f"{gid(c)}({args})"

    def _method(self, node: MethodCallExpr) -> str:
        obj = self._expr(node.obj)
        m = node.method
        args = [self._expr(x) for x in node.args]
        if m in ('length', 'size'):
            return f"int64(len({obj}))"
        if m == 'upper':
            self._imports.add('strings'); return f"strings.ToUpper({obj})"
        if m == 'lower':
            self._imports.add('strings'); return f"strings.ToLower({obj})"
        if m == 'contains':
            self._imports.add('strings')
            arg = args[0] if args else '""'
            return f"strings.Contains({obj}, {arg})"
        if m == 'slice':
            s, e = (args + ['0', '0'])[:2]
            return f"{obj}[{s}:{e}]"
        if m == 'pop_last':
            return f"{obj}[len({obj})-1]"
        # fallback: unknown method
        return f"{obj}.{gid(m)}({', '.join(args)})"

    def _stmt_call(self, node) -> str:
        """Call in statement position (handles push -> append)."""
        if isinstance(node, MethodCallExpr) and node.method == 'push':
            obj = self._expr(node.obj)
            arg = self._expr(node.args[0]) if node.args else 'nil'
            return f"{obj} = append({obj}, {arg})"
        return self._expr(node)

    # ── util ────────────────────────────────────────────────

    @staticmethod
    def _go_string(s: str) -> str:
        esc = (s.replace('\\', '\\\\').replace('"', '\\"')
                .replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r'))
        return f'"{esc}"'
