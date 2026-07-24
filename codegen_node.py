# ============================================================
#  Cryo Compiler - Node.js / JavaScript Code Generator
#  .cryo  ->  .js  (JavaScript CommonJS, executable with `node`)
#
#  Covers the CORE of the language: scalars, strings, bool, arrays,
#  maps, structs (objetos), enums, functions, controle de fluxo,
#  operators, print/assert, JSON, and foreign blocks/libraries
#  of JavaScript (>Node( ... ) / >JS( ... ) and `library >node x<`).
#
#  LLM/agent/concurrency resources (schema/tool/llm/agent/
#  spawn/await) and machine access (pyro_*) are NOT supported
#  here — use --backend go. The generator emits a clear error in these cases.
# ============================================================
import json
from typing import List, Set

from ast_nodes import *
from foreign import collect_imports, resolve_library_lang


class CodeGenNodeError(Exception):
    pass


# foreign languages treated as JavaScript by this backend
_JS_LANGS = ('node', 'js', 'javascript')

_JS_KEYWORDS = {
    'arguments', 'await', 'break', 'case', 'catch', 'class', 'const',
    'continue', 'debugger', 'default', 'delete', 'do', 'else', 'enum',
    'export', 'extends', 'false', 'finally', 'for', 'function', 'if',
    'implements', 'import', 'in', 'instanceof', 'interface', 'let', 'new',
    'null', 'of', 'package', 'private', 'protected', 'public', 'return',
    'static', 'super', 'switch', 'this', 'throw', 'true', 'try', 'typeof',
    'var', 'void', 'while', 'with', 'yield',
}


def jsid(name: str) -> str:
    """Avoids collision of Cryo identifiers with JS reserved words."""
    return name + '_' if name in _JS_KEYWORDS else name


# Cryo builtins not supported in Node backend (route: --backend go)
_UNSUPPORTED = {
    'llm', 'agent', 'tools', 'tools_json', 'tool_get', 'schema_of',
    'http_get', 'http_post', 'sleep', 'skills', 'skill_get', 'skill_has',
    'skills_json', 'pyro_exec', 'pyro_env', 'pyro_args', 'pyro_time',
    'pyro_read', 'pyro_write', 'pyro_write_file', 'pyro_open', 'pyro_exit',
    'input',
}


class _Types:
    """Lightweight type inference for the Node backend: distinguishes int/number
    (integer division vs. float) and array/map (index bounds-check)."""

    def __init__(self):
        self.scopes: List[dict] = [{}]
        self.fns: dict = {}
        self.structs: dict = {}
        self.enums: Set[str] = set()

    def push(self):
        self.scopes.append({})

    def pop(self):
        if len(self.scopes) > 1:
            self.scopes.pop()

    def set(self, name, typ):
        if typ:
            self.scopes[-1][name] = typ

    def get(self, name):
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return 'unknown'

    def infer(self, node) -> str:
        if node is None:
            return 'unknown'
        if isinstance(node, Literal):
            return {'int': 'int', 'float': 'number', 'string': 'string',
                    'bool': 'bool', 'null': 'null'}.get(node.kind, 'unknown')
        if isinstance(node, Identifier):
            return self.get(node.name)
        if isinstance(node, BinaryExpr):
            if node.op in ('==', '!=', '<', '>', '<=', '>=', '&&', '||'):
                return 'bool'
            lt = self.infer(node.left)
            rt = self.infer(node.right)
            if node.op == '??':
                return lt if lt not in ('null', 'unknown') else rt
            if lt == 'string' or rt == 'string':
                return 'string'
            if lt == 'number' or rt == 'number':
                return 'number'
            if lt == 'int' and rt == 'int':
                return 'int'
            return lt if lt != 'unknown' else rt
        if isinstance(node, UnaryExpr):
            return 'bool' if node.op == '!' else self.infer(node.operand)
        if isinstance(node, TernaryExpr):
            t = self.infer(node.then_value)
            return t if t not in ('unknown', 'null') else self.infer(node.else_value)
        if isinstance(node, CallExpr):
            b = {'len': 'int', 'to_int': 'int', 'to_string': 'string',
                 'to_number': 'number', 'sqrt': 'number', 'pow': 'number',
                 'abs': 'number', 'floor': 'number', 'ceil': 'number',
                 'round': 'number', 'min': 'number', 'max': 'number',
                 'clamp': 'number', 'sign': 'int', 'gcd': 'int', 'hypot': 'number',
                 'json_encode': 'string', 'has': 'bool',
                 'upper': 'string', 'lower': 'string', 'trim': 'string',
                 'contains': 'bool', 'find': 'int', 'replace': 'string',
                 'substr': 'string', 'split': 'string[]', 'join': 'string',
                 'starts_with': 'bool', 'ends_with': 'bool', 'repeat': 'string',
                 'index_of': 'int', 'count': 'int', 'sum': 'number',
                 'pad_start': 'string', 'pad_end': 'string',
                 'keys': 'string[]'}.get(node.callee)
            return b or self.fns.get(node.callee, 'unknown')
        if isinstance(node, StructInit):
            return node.struct_name
        if isinstance(node, ArrayLiteral):
            et = self.infer(node.elements[0]) if node.elements else 'unknown'
            return (et + '[]') if et != 'unknown' else 'array'
        if isinstance(node, MapLiteral):
            return 'map'
        if isinstance(node, CastExpr):
            return node.target_type
        if isinstance(node, UnwrapExpr):
            t = self.infer(node.operand)
            return t[:-1] if t.endswith('?') else t
        if isinstance(node, FieldAccess):
            if node.field == 'length':
                return 'int'
            return self.structs.get(self.infer(node.obj), {}).get(node.field, 'unknown')
        if isinstance(node, IndexAccess):
            ot = self.infer(node.obj)
            if ot.endswith('[]'):
                return ot[:-2]
            if ot == 'string':
                return 'string'
            return 'unknown'
        return 'unknown'

    def is_array(self, node) -> bool:
        return self.infer(node).endswith('[]')


class CodeGenNode:
    def __init__(self, safe: bool = True):
        self.safe = safe
        self._t = _Types()
        self._requires: List[str] = []
        self._enums:    List[str] = []
        self._funcs:    List[str] = []
        self._main:     List[str] = []
        self._helpers:  Set[str] = set()
        self._cur = self._main
        self._indent = 0
        self._ntmp = 0                 # fresh temporaries ('?' propagation)
        self._imported_langs: Set[str] = set()

    # ── util ────────────────────────────────────────────────
    def _emit(self, line: str):
        self._cur.append('  ' * self._indent + line if line else '')

    def _err(self, msg):
        raise CodeGenNodeError(msg)

    # ── main entry ───────────────────────────────────
    def _prescan(self, program: Program):
        for n in program.statements:
            if isinstance(n, FunctionDecl):
                self._t.fns[n.name] = n.return_type or 'void'
            elif isinstance(n, StructDecl):
                self._t.structs[n.name] = {f.name: f.field_type for f in n.fields}
            elif isinstance(n, EnumDecl):
                self._t.enums.add(n.name)

    def generate(self, program: Program) -> str:
        self._imported_langs = collect_imports(program)
        self._prescan(program)

        for node in program.statements:
            if isinstance(node, FunctionDecl):
                if node.is_tool:
                    self._err("'tool fn' (LLM) is not supported in the node backend; "
                              "use --backend go.")
                self._cur, self._indent = self._funcs, 0
                self._function(node)
            elif isinstance(node, EnumDecl):
                self._cur, self._indent = self._enums, 0
                self._enum(node)
            elif isinstance(node, StructDecl):
                pass  # structs = literal objects; no declaration necessary
            elif isinstance(node, SkillDecl):
                self._err("'skill' (LLM) is not supported in the node backend; "
                          "use --backend go.")
            elif isinstance(node, Library):
                if resolve_library_lang(node, self._imported_langs) in _JS_LANGS:
                    self._require(node.name)
            elif isinstance(node, Import):
                pass
            else:
                self._cur, self._indent = self._main, 0
                self._stmt(node)

        return self._assemble()

    def _require(self, name: str):
        # library >node fs<  ->  const fs = require("fs");
        var = jsid(name.split('/')[-1].replace('-', '_').replace('.', '_'))
        line = f'const {var} = require("{name}");'
        if line not in self._requires:
            self._requires.append(line)

    def _enum(self, n: EnumDecl):
        has_data = any(len(m.fields) > 0 for m in n.members)
        if not has_data:
            for i, m in enumerate(n.members):
                self._emit(f"const {n.name}_{m.name} = {i};")
        else:
            for m in n.members:
                params = ', '.join(f"val{idx}" for idx in range(len(m.fields)))
                args_object = ', '.join(f"val{idx}" for idx in range(len(m.fields)))
                comma = ", " if args_object else ""
                self._emit(f"const {jsid(m.name)} = ({params}) => ({{ tag: \"{m.name}\"{comma}{args_object} }});")
                self._emit(f"const {n.name}_{jsid(m.name)} = ({params}) => ({{ tag: \"{m.name}\"{comma}{args_object} }});")

    def _assemble(self) -> str:
        out: List[str] = ['"use strict";', '']
        if self._requires:
            out += self._requires + ['']
        out += self._helper_defs()
        if self._enums:
            out += self._enums + ['']
        if self._funcs:
            out += self._funcs + ['']
        out += self._main
        return '\n'.join(out).rstrip() + '\n'

    def _helper_defs(self) -> List[str]:
        H: List[str] = []
        if 'len' in self._helpers:
            H += ["function cryoLen(x) {",
                  "  if (x == null) return 0;",
                  "  if (Array.isArray(x) || typeof x === 'string') return x.length;",
                  "  return Object.keys(x).length;",
                  "}", ""]
        if 'div' in self._helpers:
            H += ["function cryoDiv(a, b) {",
                  "  if (b === 0) throw new Error('[Cryo Security] DivisaoPorZero');",
                  "  return a / b;",
                  "}", ""]
        if 'idiv' in self._helpers:
            H += ["function cryoIDiv(a, b) {",
                  "  if (b === 0) throw new Error('[Cryo Security] DivByZero');",
                  "  return Math.trunc(a / b);   // integer division (truncates toward zero)",
                  "}", ""]
        if 'index' in self._helpers:
            H += ["function cryoIndex(a, i) {",
                  "  if (i < 0 || i >= a.length)",
                  "    throw new Error('[Cryo Security] IndexError: índice ' + i + ' fora dos limites (len=' + a.length + ')');",
                  "  return a[i];",
                  "}", ""]
        if 'setindex' in self._helpers:
            H += ["function cryoSetIndex(a, i, v) {",
                  "  if (i < 0 || i >= a.length)",
                  "    throw new Error('[Cryo Security] IndexError: índice ' + i + ' fora dos limites (len=' + a.length + ')');",
                  "  a[i] = v;",
                  "}", ""]
        if 'substr' in self._helpers:
            H += ["function cryoSubstr(s, i, n) {",
                  "  s = String(s);",
                  "  if (i < 0) i = 0;",
                  "  if (i > s.length) i = s.length;",
                  "  let end = i + n;",
                  "  if (n < 0 || end > s.length) end = s.length;",
                  "  return s.slice(i, end);",
                  "}", ""]
        if 'mod' in self._helpers:
            H += ["function cryoMod(a, b) {",
                  "  if (b === 0) throw new Error('[Cryo Security] DivisaoPorZero');",
                  "  return a % b;",
                  "}", ""]
        if 'unwrap' in self._helpers:
            H += ["function cryoUnwrap(x) {",
                  "  if (x == null) throw new Error('[Cryo Security] unwrap of null');",
                  "  return x;",
                  "}", ""]
        return H

    # ── functions ─────────────────────────────────────────────
    def _function(self, n: FunctionDecl):
        params = ', '.join(jsid(p[1]) for p in n.params)
        self._emit(f"function {jsid(n.name)}({params}) {{")
        self._t.push()
        for p in n.params:
            self._t.set(p[1], p[0])   # p = (type, name)
        self._indent += 1
        self._block(n.body)
        self._indent -= 1
        self._t.pop()
        self._emit("}")
        self._emit("")

    def _block(self, body: List[Node]):
        for s in body:
            self._stmt(s)

    # ── statements ──────────────────────────────────────────
    def _node_try(self, inner: Node) -> str:
        """Error propagation '?': emits the temporary + guard and returns the
        of the Ok value expression (or the value itself, if optional). In the
        error/null, returns early the Err/null."""
        t = self._t.infer(inner)
        self._ntmp += 1
        tmp = f"__try{self._ntmp}"
        self._emit(f"const {tmp} = {self._expr(inner)};")
        if t.endswith('?') or t == 'null':
            self._emit(f"if ({tmp} === null || {tmp} === undefined) return null;")
            return tmp
        self._emit(f'if ({tmp}.tag !== "Ok") return {tmp};')
        return f"{tmp}.val0"

    def _stmt(self, n: Node):
        if isinstance(n, VarDecl):
            if isinstance(n.value, TryExpr):
                okv = self._node_try(n.value.operand)
                self._emit(f"let {jsid(n.name)} = {okv};")
            elif n.value is None:
                self._emit(f"let {jsid(n.name)};")
            else:
                self._emit(f"let {jsid(n.name)} = {self._expr(n.value)};")
            self._t.set(n.name, n.var_type)
        elif isinstance(n, ConstDecl):
            self._emit(f"const {jsid(n.name)} = {self._expr(n.value)};")
            self._t.set(n.name, n.var_type)
        elif isinstance(n, Assignment):
            if isinstance(n.value, TryExpr):
                okv = self._node_try(n.value.operand)
                self._emit(f"{jsid(n.name)} = {okv};")
            else:
                self._emit(f"{jsid(n.name)} = {self._expr(n.value)};")
        elif isinstance(n, IndexAssignment):
            if self.safe and self._t.is_array(n.obj):
                self._helpers.add('setindex')
                self._emit(f"cryoSetIndex({self._expr(n.obj)}, {self._expr(n.index)}, "
                           f"{self._expr(n.value)});")
            else:
                self._emit(f"{self._expr(n.obj)}[{self._expr(n.index)}] = {self._expr(n.value)};")
        elif isinstance(n, CompoundAssignment):
            self._emit(f"{jsid(n.name)} {n.op} {self._expr(n.value)};")
        elif isinstance(n, Increment):
            self._emit(f"{jsid(n.name)}{n.op};")
        elif isinstance(n, Return):
            if isinstance(n.value, TryExpr):
                okv = self._node_try(n.value.operand)
                self._emit(f"return {okv};")
            else:
                self._emit("return;" if n.value is None else f"return {self._expr(n.value)};")
        elif isinstance(n, If):
            self._if(n)
        elif isinstance(n, While):
            self._emit(f"while ({self._expr(n.condition)}) {{")
            self._indent += 1; self._block(n.body); self._indent -= 1
            self._emit("}")
        elif isinstance(n, DoWhile):
            self._emit("do {")
            self._indent += 1; self._block(n.body); self._indent -= 1
            self._emit(f"}} while ({self._expr(n.condition)});")
        elif isinstance(n, For):
            if isinstance(n.init, VarDecl):
                self._t.set(n.init.name, n.init.var_type)
            init = self._inline(n.init) if n.init else ''
            cond = self._expr(n.condition) if n.condition else ''
            upd  = self._inline(n.update) if n.update else ''
            self._emit(f"for ({init}; {cond}; {upd}) {{")
            self._indent += 1; self._block(n.body); self._indent -= 1
            self._emit("}")
        elif isinstance(n, ForEach):
            self._t.set(n.var_name, n.var_type)
            self._emit(f"for (const {jsid(n.var_name)} of {self._expr(n.iterable)}) {{")
            self._indent += 1; self._block(n.body); self._indent -= 1
            self._emit("}")
        elif isinstance(n, Switch):
            self._switch(n)
        elif isinstance(n, MatchStatement):
            self._match(n)
        elif isinstance(n, Break):
            self._emit("break;")
        elif isinstance(n, Continue):
            self._emit("continue;")
        elif isinstance(n, TryCatch):
            self._try(n)
        elif isinstance(n, Assert):
            msg = self._expr(n.message) if n.message else '"assert failed"'
            self._emit(f"if (!({self._expr(n.condition)})) throw new Error({msg});")
        elif isinstance(n, SafetyBlock):
            self._block(n.body)   # JS has no 'unsafe'; emits the body
        elif isinstance(n, ForeignBlock):
            self._foreign(n)
        elif isinstance(n, CallExpr) and n.callee == 'throw':
            arg = self._expr(n.args[0]) if n.args else '"erro"'
            self._emit(f"throw {arg};")
        elif isinstance(n, TryExpr):
            self._node_try(n.operand)   # discarded value (statement)
        else:
            # statement-expression (e.g.: function call, m.push(...))
            self._emit(f"{self._expr(n)};")

    def _if(self, n: If):
        self._emit(f"if ({self._expr(n.condition)}) {{")
        self._indent += 1; self._block(n.then_body); self._indent -= 1
        if n.else_body:
            # else-if flattened when the else is a single If
            if len(n.else_body) == 1 and isinstance(n.else_body[0], If):
                self._emit("} else")
                # reassembles as 'else if' pasting on the next line
                tail = []
                save = self._cur; self._cur = tail
                self._if(n.else_body[0])
                self._cur = save
                self._cur[-1] = self._cur[-1] + " " + tail[0].lstrip()
                self._cur.extend(tail[1:])
            else:
                self._emit("} else {")
                self._indent += 1; self._block(n.else_body); self._indent -= 1
                self._emit("}")
        else:
            self._emit("}")

    def _switch(self, n: Switch):
        self._emit(f"switch ({self._expr(n.subject)}) {{")
        self._indent += 1
        for c in n.cases:
            for v in c.values:
                self._emit(f"case {self._expr(v)}:")
            self._indent += 1
            self._block(c.body)
            if not self._terminates(c.body):
                self._emit("break;")
            self._indent -= 1
        if n.default_body is not None:
            self._emit("default:")
            self._indent += 1
            self._block(n.default_body)
            self._indent -= 1
        self._indent -= 1
        self._emit("}")

    def _match(self, n: MatchStatement):
        subj_var = f"__match_subj_{n.line}"
        self._emit(f"const {subj_var} = {self._expr(n.subject)};")
        self._emit(f"switch ({subj_var} && {subj_var}.tag) {{")
        self._indent += 1
        for case in n.cases:
            if case.pattern_name == '_':
                self._emit("default: {")
                self._indent += 1
                self._block(case.body)
                self._indent -= 1
                self._emit("}")
                continue
            short_name = case.pattern_name.split('_')[-1]
            self._emit(f"case \"{short_name}\": {{")
            self._indent += 1
            for idx, var_name in enumerate(case.pattern_vars):
                self._emit(f"const {jsid(var_name)} = {subj_var}.val{idx};")
                self._t.set(var_name, "any")
            self._block(case.body)
            self._emit("break;")
            self._indent -= 1
            self._emit("}")
        self._indent -= 1
        self._emit("}")

    @staticmethod
    def _terminates(body: List[Node]) -> bool:
        return bool(body) and isinstance(body[-1], (Return, Break, Continue))

    def _try(self, n: TryCatch):
        self._emit("try {")
        self._indent += 1; self._block(n.try_body); self._indent -= 1
        name = jsid(n.catch_name) if n.catch_name else '_e'
        self._emit(f"}} catch ({name}) {{")
        self._indent += 1
        if n.catch_body:
            self._block(n.catch_body)
        self._indent -= 1
        if n.finally_body:
            self._emit("} finally {")
            self._indent += 1; self._block(n.finally_body); self._indent -= 1
        self._emit("}")

    def _foreign(self, n: ForeignBlock):
        if n.lang.lower() in _JS_LANGS:
            self._emit(f"// -- [bloco {n.lang}] --")
            for line in n.code.strip().split('\n'):
                self._emit(line.strip())
            self._emit(f"// -- [/bloco {n.lang}] --")
        else:
            self._emit(f"// [Cryo] >{n.lang}< block omitted in Node backend "
                       f"(use >Node( ... ))")

    def _inline(self, n: Node) -> str:
        """Form without ';' for for init/update."""
        if isinstance(n, VarDecl):
            return f"let {jsid(n.name)} = {self._expr(n.value)}" if n.value is not None \
                else f"let {jsid(n.name)}"
        if isinstance(n, Assignment):
            return f"{jsid(n.name)} = {self._expr(n.value)}"
        if isinstance(n, CompoundAssignment):
            return f"{jsid(n.name)} {n.op} {self._expr(n.value)}"
        if isinstance(n, Increment):
            return f"{jsid(n.name)}{n.op}"
        return self._expr(n)

    # ── expressions ──────────────────────────────────────────
    def _expr(self, n: Node) -> str:
        if isinstance(n, TryExpr):
            raise CodeGenNodeError(
                "propagation '?' is only supported at the assignment level, "
                "declaration, return or expression-statement — not nested.")
        if isinstance(n, Literal):
            return self._literal(n)
        if isinstance(n, Identifier):
            return jsid(n.name)
        if isinstance(n, BinaryExpr):
            return self._binary(n)
        if isinstance(n, UnaryExpr):
            return f"({n.op}{self._expr(n.operand)})"
        if isinstance(n, TernaryExpr):
            return (f"({self._expr(n.condition)} ? {self._expr(n.then_value)}"
                    f" : {self._expr(n.else_value)})")
        if isinstance(n, CallExpr):
            return self._call(n)
        if isinstance(n, MethodCallExpr):
            args = ', '.join(self._expr(a) for a in n.args)
            return f"{self._expr(n.obj)}.{n.method}({args})"
        if isinstance(n, FieldAccess):
            if n.field == 'length':
                return f"{self._expr(n.obj)}.length"
            return f"{self._expr(n.obj)}.{n.field}"
        if isinstance(n, IndexAccess):
            ot = self._t.infer(n.obj)
            # bounds-check on arrays AND strings (both have .length and [i]);
            # maps are left out (missing key -> undefined is expected)
            if self.safe and (ot.endswith('[]') or ot == 'string'):
                self._helpers.add('index')
                return f"cryoIndex({self._expr(n.obj)}, {self._expr(n.index)})"
            return f"{self._expr(n.obj)}[{self._expr(n.index)}]"
        if isinstance(n, ArrayLiteral):
            return "[" + ", ".join(self._expr(e) for e in n.elements) + "]"
        if isinstance(n, MapLiteral):
            pairs = ", ".join(f"[{self._expr(k)}]: {self._expr(v)}" for k, v in n.pairs)
            return "{" + pairs + "}"
        if isinstance(n, StructInit):
            fields = ", ".join(f"{k}: {self._expr(v)}" for k, v in n.fields)
            return "{" + fields + "}"
        if isinstance(n, CastExpr):
            return self._expr(n.expr)   # types are dynamic in JS
        if isinstance(n, UnwrapExpr):
            if self.safe:
                self._helpers.add('unwrap')
                return f"cryoUnwrap({self._expr(n.operand)})"
            return self._expr(n.operand)
        if isinstance(n, (SpawnExpr, AwaitExpr)):
            self._err("concurrency (spawn/await) is not supported in the node backend; "
                      "use --backend go.")
        if isinstance(n, Lambda):
            return self._lambda(n)
        self._err(f"expression not supported in node backend: {type(n).__name__}")

    def _lambda(self, n: Lambda) -> str:
        params = ', '.join(jsid(pn) for _pt, pn in n.params)
        prev_cur = self._cur
        buf: List[str] = []
        self._cur = buf
        self._t.push()
        for pt, pn in n.params:
            self._t.set(pn, pt)
        self._indent += 1
        self._block(n.body)
        self._indent -= 1
        self._t.pop()
        self._cur = prev_cur
        body = '\n'.join(buf)
        ind = '  ' * self._indent
        return f"(({params}) => {{\n{body}\n{ind}}})"

    def _literal(self, n: Literal) -> str:
        if n.kind == 'string':
            return json.dumps(n.value, ensure_ascii=False)
        if n.kind == 'bool':
            return 'true' if n.value else 'false'
        if n.kind == 'null':
            return 'null'
        return str(n.value)   # int / float

    def _binary(self, n: BinaryExpr) -> str:
        l = self._expr(n.left)
        r = self._expr(n.right)
        op = n.op
        if op == '==':
            return f"({l} === {r})"
        if op == '!=':
            return f"({l} !== {r})"
        if op == '/':
            int_div = (self._t.infer(n.left) == 'int' and self._t.infer(n.right) == 'int')
            if self.safe:
                if int_div:
                    self._helpers.add('idiv'); return f"cryoIDiv({l}, {r})"
                self._helpers.add('div'); return f"cryoDiv({l}, {r})"
            return f"Math.trunc({l} / {r})" if int_div else f"({l} / {r})"
        if op == '%' and self.safe:
            self._helpers.add('mod')
            return f"cryoMod({l}, {r})"
        return f"({l} {op} {r})"

    def _call(self, n: CallExpr) -> str:
        c = n.callee
        a = n.args
        if c in _UNSUPPORTED:
            self._err(f"'{c}' is not supported in the node backend; use --backend go.")

        def A(i):
            return self._expr(a[i])

        args = ', '.join(self._expr(x) for x in a)

        # user-defined functions shadow stdlib builtins (print/len/has/keys reserved)
        if c in self._t.fns and c not in ('print', 'len', 'has', 'keys'):
            return f"{jsid(c)}({args})"

        if c == 'print':
            return f"console.log({args})"
        if c == 'len':
            self._helpers.add('len')
            return f"cryoLen({A(0)})"
        if c == 'to_string':
            return f"String({A(0)})"
        if c == 'to_int':
            return f"Math.trunc(Number({A(0)}))"
        if c == 'to_number':
            return f"Number({A(0)})"
        if c in ('sqrt', 'pow', 'abs', 'min', 'max', 'floor', 'ceil', 'round'):
            jsname = {'sqrt': 'sqrt', 'pow': 'pow', 'abs': 'abs', 'min': 'min',
                      'max': 'max', 'floor': 'floor', 'ceil': 'ceil',
                      'round': 'round'}[c]
            return f"Math.{jsname}({args})"
        if c == 'clamp':
            return f"Math.max({A(1)}, Math.min({A(0)}, {A(2)}))"
        if c == 'sign':
            return f"Math.sign({A(0)})"
        if c == 'gcd':
            return (f"((a,b)=>{{a=Math.abs(a);b=Math.abs(b);"
                    f"while(b){{const t=a%b;a=b;b=t;}}return a;}})({A(0)}, {A(1)})")
        if c == 'hypot':
            return f"Math.hypot({A(0)}, {A(1)})"
        if c == 'upper':
            return f"String({A(0)}).toUpperCase()"
        if c == 'lower':
            return f"String({A(0)}).toLowerCase()"
        if c == 'trim':
            return f"String({A(0)}).trim()"
        if c == 'contains':
            return f"String({A(0)}).includes({A(1)})"
        if c == 'find':
            return f"String({A(0)}).indexOf({A(1)})"
        if c == 'replace':
            return f"String({A(0)}).split({A(1)}).join({A(2)})"
        if c == 'substr':
            self._helpers.add('substr')
            return f"cryoSubstr({A(0)}, {A(1)}, {A(2)})"
        if c == 'split':
            return f"String({A(0)}).split({A(1)})"
        if c == 'join':
            return f"({A(0)}).join({A(1)})"
        if c == 'starts_with':
            return f"String({A(0)}).startsWith({A(1)})"
        if c == 'ends_with':
            return f"String({A(0)}).endsWith({A(1)})"
        if c == 'repeat':
            return f"String({A(0)}).repeat(Math.max(0, {A(1)}))"
        if c == 'has':
            return f"Object.prototype.hasOwnProperty.call({A(0)}, {A(1)})"
        if c == 'keys':
            return f"Object.keys({A(0)})"
        # ── stateless collection ops (Phase 10.2) ──
        if c == 'sort':
            # numbers sort numerically, everything else by string form (matches the VM)
            return (f"[...{A(0)}].sort((x,y)=>{{const nx=typeof x===\"number\",ny=typeof y===\"number\";"
                    f"if(nx&&ny)return x-y;const sx=String(x),sy=String(y);"
                    f"return sx<sy?-1:sx>sy?1:0;}})")
        if c == 'reverse':
            return f"[...{A(0)}].reverse()"
        if c == 'slice':
            # clamp bounds to [0, len] like the VM (JS slice treats negatives as from-end)
            return (f"((a,s,e)=>{{const n=a.length;if(s<0)s=0;if(e>n)e=n;"
                    f"if(s>e)s=e;return a.slice(s,e);}})({A(0)}, {A(1)}, {A(2)})")
        if c == 'index_of':
            return f"({A(0)}).indexOf({A(1)})"
        if c == 'pad_start':
            return f"String({A(0)}).padStart({A(1)}, {A(2)})"
        if c == 'pad_end':
            return f"String({A(0)}).padEnd({A(1)}, {A(2)})"
        if c == 'concat':
            return f"[...{A(0)}, ...{A(1)}]"
        if c == 'count':
            return f"({A(0)}).filter(e=>e==={A(1)}).length"
        if c == 'sum':
            return f"({A(0)}).reduce((s,v)=>s+v, 0)"
        if c == 'remove':
            return f"(delete {A(0)}[{A(1)}])"
        if c == 'json_encode':
            return f"JSON.stringify({A(0)})"
        if c == 'json_decode':
            return f"JSON.parse({A(0)})"
        # user function call
        return f"{jsid(c)}({args})"
