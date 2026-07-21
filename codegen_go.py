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
    if t.startswith('fn(') and '->' in t:     # function type -> func(...)...
        return _go_fn_type(t)
    if t.endswith('?'):                       # optional -> pointer
        return '*' + go_type(t[:-1])
    if t.startswith('map<') and t.endswith('>'):
        k, v = _split_type_pair(t[4:-1])
        return f"map[{go_type(k)}]{go_type(v)}"
    if t.startswith('future<') and t.endswith('>'):   # future -> channel
        return f"chan {go_type(t[7:-1])}"
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

    def reg_fn(self, name, ret): self._fns[name] = ret
    def fn_ret(self, name):      return self._fns.get(name, 'unknown')
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
                       'upper': 'string', 'lower': 'string', 'trim': 'string',
                       'contains': 'bool', 'find': 'int', 'replace': 'string',
                       'substr': 'string', 'split': 'string[]', 'join': 'string',
                       'http_get': 'string',
                       'http_post': 'string', 'schema_of': 'string',
                       'llm': 'string', 'tools': 'string[]',
                       'tools_json': 'string', 'tool_get': 'Tool',
                       'agent': 'string'}.get(node.callee)
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
        # ── Pyro layer: native skills and machine access ──
        self._skills: List[SkillDecl] = []
        self._use_skills = False
        # ── Phase 3: Native LLM — 'tool' functions exposed to models ──
        self._tools: List[FunctionDecl] = []
        self._use_tools = False
        self._member_to_enum: Dict[str, str] = {}

    @property
    def _safe_mode(self) -> bool:
        return self._safe_stack[-1] if self._safe_stack else self._safe

    # ── emission ─────────────────────────────────────────────

    def _emit(self, line: str = ''):
        self._cur.append(('\t' * self._indent + line) if line else '')

    # ── main entry ───────────────────────────────────

    def generate(self, program: Program) -> str:
        self._pre_scan(program.statements)
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
                for m in n.members:
                    self._member_to_enum[m.name] = n.name
                    self._member_to_enum[f"{n.name}_{m.name}"] = n.name
            elif isinstance(n, FunctionDecl):
                self.te.reg_fn(n.name, n.return_type or 'void')
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
            self._imports.add('fmt')
            H += ["func cryoStr(v any) string { return fmt.Sprint(v) }", ""]
        if 'or' in self._helpers:
            H += ["func cryoOr[T comparable](a, b T) T {",
                  "\tvar zero T",
                  "\tif a == zero {", "\t\treturn b", "\t}",
                  "\treturn a", "}", ""]
        if 'assert' in self._helpers:
            H += ["func cryoAssert(cond bool, msg string) {",
                  "\tif !cond {", '\t\tpanic("[Cryo Assert] " + msg)', "\t}", "}", ""]
        if 'addovf' in self._helpers:
            H += ["func cryoAddOvf(a, b int64) int64 {",
                  "\ts := a + b",
                  "\tif (b > 0 && s < a) || (b < 0 && s > a) {",
                  '\t\tpanic("[Cryo Security] Overflow: adicao de inteiros")', "\t}",
                  "\treturn s", "}", ""]
        if 'subovf' in self._helpers:
            H += ["func cryoSubOvf(a, b int64) int64 {",
                  "\ts := a - b",
                  "\tif (b < 0 && s < a) || (b > 0 && s > a) {",
                  '\t\tpanic("[Cryo Security] Overflow: subtracao de inteiros")', "\t}",
                  "\treturn s", "}", ""]
        if 'mulovf' in self._helpers:
            H += ["func cryoMulOvf(a, b int64) int64 {",
                  "\tif a == 0 || b == 0 {", "\t\treturn 0", "\t}",
                  "\tif a == -1<<63 && b == -1 || b == -1<<63 && a == -1 {",
                  '\t\tpanic("[Cryo Security] Overflow: multiplicacao de inteiros")', "\t}",
                  "\ts := a * b",
                  "\tif s/b != a {",
                  '\t\tpanic("[Cryo Security] Overflow: multiplicacao de inteiros")', "\t}",
                  "\treturn s", "}", ""]
        if 'idiv' in self._helpers:
            H += ["func cryoIDivChk(a, b int64) int64 {",
                  "\tif b == 0 {",
                  '\t\tpanic("[Cryo Security] DivisaoPorZero: divisao inteira")', "\t}",
                  "\tif a == -1<<63 && b == -1 {",
                  '\t\tpanic("[Cryo Security] Overflow: INT64_MIN / -1")', "\t}",
                  "\treturn a / b", "}", ""]
        if 'imod' in self._helpers:
            # INT64_MIN % -1 is 0 (well-defined in Go); only division overflows.
            H += ["func cryoIModChk(a, b int64) int64 {",
                  "\tif b == 0 {",
                  '\t\tpanic("[Cryo Security] DivisaoPorZero: modulo")', "\t}",
                  "\tif a == -1<<63 && b == -1 {",
                  "\t\treturn 0", "\t}",
                  "\treturn a % b", "}", ""]
        if 'absi' in self._helpers:
            H += ["func cryoAbsI(x int64) int64 { if x < 0 { return -x }; return x }", ""]
        if 'jsonenc' in self._helpers:
            H += ["func cryoJSONEncode(v any) string {",
                  "\tb, err := json.Marshal(v)",
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
                  "\tif p == nil {", '\t\tpanic("[Cryo Security] NullPointer: unwrap de opcional nulo")', "\t}",
                  "\treturn *p", "}", ""]
        if 'keys' in self._helpers:
            H += ["func cryoKeys[K comparable, V any](m map[K]V) []K {",
                  "\tks := make([]K, 0, len(m))",
                  "\tfor k := range m {", "\t\tks = append(ks, k)", "\t}",
                  "\treturn ks", "}", ""]
        if 'parseint' in self._helpers:
            self._imports.update(('strconv', 'strings'))
            H += ["func cryoParseInt(s string) int64 {",
                  "\tn, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64)",
                  "\tif err != nil {",
                  "\t\tpanic(\"[Cryo Security] to_int: '\" + s + \"' não é um inteiro válido\")",
                  "\t}",
                  "\treturn n", "}", ""]
        if 'parsenum' in self._helpers:
            self._imports.update(('strconv', 'strings'))
            H += ["func cryoParseNum(s string) float64 {",
                  "\tf, err := strconv.ParseFloat(strings.TrimSpace(s), 64)",
                  "\tif err != nil {",
                  "\t\tpanic(\"[Cryo Security] to_number: '\" + s + \"' não é um número válido\")",
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
            self._imports.update(('os', 'net/http', 'io', 'encoding/json', 'bytes', 'fmt'))
            H += ["// cryoLLMPost: POST of payload to CRYO_LLM_URL with 3 retries.",
                  "func cryoLLMPost(payload map[string]any) string {",
                  '\tcryoSandboxGuard("llm/agent")',
                  '\turl := os.Getenv("CRYO_LLM_URL")',
                  '\tif url == "" {',
                  '\t\tfmt.Fprintln(os.Stderr, "[Cryo LLM] CRYO_LLM_URL undefined; returning empty")',
                  '\t\treturn ""', "\t}",
                  "\tbody, _ := json.Marshal(payload)",
                  "\tfor attempt := 0; attempt < 3; attempt++ {",
                  '\t\treq, _ := http.NewRequest("POST", url, bytes.NewReader(body))',
                  '\t\treq.Header.Set("Content-Type", "application/json")',
                  '\t\tif key := os.Getenv("CRYO_LLM_KEY"); key != "" {',
                  '\t\t\treq.Header.Set("Authorization", "Bearer "+key)', "\t\t}",
                  "\t\tresp, err := http.DefaultClient.Do(req)",
                  "\t\tif err != nil { continue }",
                  "\t\tout, _ := io.ReadAll(resp.Body)",
                  "\t\tresp.Body.Close()",
                  "\t\tif resp.StatusCode < 300 { return string(out) }",
                  "\t}",
                  '\treturn ""', "}", "",
                  "// cryoLLM: contrato POST {model, prompt, schema?} -> corpo JSON.",
                  "func cryoLLM(model, prompt, schema string) string {",
                  '\tpayload := map[string]any{"model": model, "prompt": prompt}',
                  '\tif schema != "" {',
                  "\t\tvar sc any",
                  '\t\tif json.Unmarshal([]byte(schema), &sc) == nil { payload["schema"] = sc }',
                  "\t}",
                  "\treturn cryoLLMPost(payload)", "}", ""]
        if 'agent' in self._helpers:
            # agent loop: LLM requests tool -> runtime executes -> returns -> repeats
            # 'only' filters the exposed tools; maxSteps limits iterations.
            H += ["// cryoAgent: tool-calling loop. POST contract",
                  "// {model, messages, tools} -> {\"tool_call\":{name,arguments}} | {\"content\":...}.",
                  "func cryoAgent(model, prompt string, only []string, maxSteps int) string {",
                  "\ttools := cryoToolList()",
                  "\tif len(only) > 0 {",
                  "\t\tset := map[string]bool{}",
                  "\t\tfor _, n := range only { set[n] = true }",
                  "\t\tf := []Tool{}",
                  "\t\tfor _, t := range tools { if set[t.Name] { f = append(f, t) } }",
                  "\t\ttools = f", "\t}",
                  "\tif maxSteps <= 0 { maxSteps = 8 }",
                  '\tmessages := []map[string]any{{"role": "user", "content": prompt}}',
                  "\tfor step := 0; step < maxSteps; step++ {",
                  '\t\tresp := cryoLLMPost(map[string]any{"model": model, "messages": messages, "tools": tools})',
                  "\t\tvar dec struct {",
                  "\t\t\tToolCall *struct {",
                  '\t\t\t\tName      string          `json:"name"`',
                  '\t\t\t\tArguments json.RawMessage `json:"arguments"`',
                  '\t\t\t} `json:"tool_call"`',
                  '\t\t\tContent string `json:"content"`',
                  "\t\t}",
                  "\t\tjson.Unmarshal([]byte(resp), &dec)",
                  "\t\tif dec.ToolCall == nil {",
                  "\t\t\treturn dec.Content", "\t\t}",
                  "\t\tresult := cryoToolCall(dec.ToolCall.Name, string(dec.ToolCall.Arguments))",
                  "\t\tmessages = append(messages,",
                  '\t\t\tmap[string]any{"role": "assistant", "tool_call": dec.ToolCall},',
                  '\t\t\tmap[string]any{"role": "tool", "name": dec.ToolCall.Name, "content": result})',
                  "\t}",
                  '\treturn ""', "}", ""]
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
        """Tipo Tool + global registry + helpers de introspecção."""
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
              "func cryoToolCall(name, args string) string {",
              "\tswitch name {"]
        for fn in self._tools:
            D.append(f"\tcase {self._go_string(fn.name)}:")
            # arguments struct (exported fields + json tag = parameter name)
            fields = '; '.join(
                f'{go_field(pn)} {go_type(pt)} `json:"{pn}"`' for pt, pn in fn.params)
            D.append(f"\t\tvar _a struct {{ {fields} }}")
            D.append("\t\tjson.Unmarshal([]byte(args), &_a)")
            call_args = ', '.join(f"_a.{go_field(pn)}" for _pt, pn in fn.params)
            if fn.return_type and fn.return_type != 'void':
                D.append(f"\t\t_r := {gid(fn.name)}({call_args})")
                D.append("\t\t_b, _ := json.Marshal(_r)")
                D.append("\t\treturn string(_b)")
            else:
                D.append(f"\t\t{gid(fn.name)}({call_args})")
                D.append('\t\treturn "null"')
        D += ["\t}", '\treturn ""', "}", ""]
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

    def _var(self, n: VarDecl):
        if isinstance(n.value, TryExpr):
            self.te.set(n.name, n.var_type)
            okv = self._go_try(n.value.operand)
            self._emit(f"var {gid(n.name)} {go_type(n.var_type)} = {okv}")
            self._emit(f"_ = {gid(n.name)}")
            return
        self.te.set(n.name, n.var_type)
        gt = go_type(n.var_type)
        vt = n.var_type
        name = gid(n.name)
        if isinstance(n.value, ArrayLiteral):
            elems = ', '.join(self._expr(e) for e in n.value.elements)
            self._emit(f"{name} := {gt}{{{elems}}}")
        elif isinstance(n.value, MapLiteral):
            self._emit(f"var {name} {gt} = {self._map_literal(n.value, vt)}")
        elif is_map(vt) and n.value is None:
            # map without value: initializes empty and writable
            self._emit(f"{name} := {gt}{{}}")
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
        self._emit(f"{gid(n.name)} = {self._expr(n.value)}")

    def _index_assign(self, n: IndexAssignment):
        self._emit(f"{self._expr(n.obj)}[{self._expr(n.index)}] = {self._expr(n.value)}")

    def _compound(self, n: CompoundAssignment):
        self._emit(f"{gid(n.name)} {n.op} {self._expr(n.value)}")

    def _incr(self, n: Increment):
        self._emit(f"{gid(n.name)}{n.op}")

    def _return(self, n: Return):
        if isinstance(n.value, TryExpr):
            okv = self._go_try(n.value.operand)
            self._emit(f"return {okv}")
            return
        if n.value is None:
            self._emit("return")
        elif is_optional(self._cur_fn_ret):
            self._emit(f"return {self._to_optional(n.value, self._cur_fn_ret)}")
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
        self._emit(f"cryoAssert({cond}, {msg})")

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
        if n.lang.lower() == 'go':
            self._emit("// -- [bloco Go] --")
            for line in n.code.strip().split('\n'):
                self._emit(line.strip())
            self._emit("// -- [/bloco Go] --")
        else:
            self._emit(f"// [Cryo] >{n.lang}< block omitted in Go backend "
                       f"(use print(...) or >Go( ... ))")

    # ── expressions ──────────────────────────────────────────

    def _expr_typed(self, node: Node, target: str) -> str:
        """Expression with knowledge of the target type (handles null)."""
        if isinstance(node, Literal) and node.kind == 'null':
            return zero_value(target)
        return self._expr(node)

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

        if isinstance(node, MethodCallExpr):
            return self._method(node)

        if isinstance(node, FieldAccess):
            obj = self._expr(node.obj)
            if node.field == 'length':
                return f"int64(len({obj}))"
            return f"{obj}.{go_field(node.field)}"

        if isinstance(node, IndexAccess):
            return f"{self._expr(node.obj)}[{self._expr(node.index)}]"

        if isinstance(node, ArrayLiteral):
            elems = ', '.join(self._expr(e) for e in node.elements)
            return f"[]any{{{elems}}}"   # typeless context: fallback

        if isinstance(node, MapLiteral):
            return self._map_literal(node, None)

        if isinstance(node, StructInit):
            fields = ', '.join(
                f"{go_field(k)}: {self._expr(v)}" for k, v in node.fields)
            return f"{gid(node.struct_name)}{{{fields}}}"

        if isinstance(node, CastExpr):
            return self._cast(node)

        if isinstance(node, UnwrapExpr):
            self._helpers.add('unwrap')
            return f"cryoUnwrap({self._expr(node.operand)})"

        if isinstance(node, SpawnExpr):
            return self._spawn(node)

        if isinstance(node, AwaitExpr):
            return f"(<-{self._expr(node.expr)})"

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
        # spawn and  ->  goroutine + buffered channel (Future<T>)
        t = elem or self.te.infer(node.expr)
        gt = go_type(t) if t not in ('unknown', 'null', 'array') else 'any'
        inner = self._expr(node.expr)
        return (f"func() chan {gt} {{ __ch := make(chan {gt}, 1); "
                f"go func() {{ __ch <- {inner} }}(); return __ch }}()")

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
            return (f"func() {gt} {{ var _v {gt}; "
                    f"_ = json.Unmarshal([]byte(cryoLLM({model}, {prompt}, {schema})), &_v); "
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

    def _call(self, node: CallExpr) -> str:
        c = node.callee
        a = node.args
        if c == 'print':
            self._imports.add('fmt')
            if not a: return "fmt.Println()"
            return f"fmt.Println({self._expr(a[0])})"
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
            return f"cryoKeys({self._expr(a[0])})"
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
        if c == 'find' and len(a) == 2:
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
            return f'cryoLLM({model}, {prompt}, "")'   # without schema (raw completion)
        if c == 'tools':
            self._use_tools = True
            return "cryoToolNames()"
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
