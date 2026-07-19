# ============================================================
#  Cryo Compiler - Node.js / JavaScript Code Generator
#  .cryo  ->  .js  (JavaScript CommonJS, executavel com `node`)
#
#  Cobre o NUCLEO da linguagem: escalares, strings, bool, arrays,
#  maps, structs (objetos), enums, funcoes, controle de fluxo,
#  operadores, print/assert, JSON, e blocos/libraries estrangeiros
#  de JavaScript (>Node( ... ) / >JS( ... ) e `library >node x<`).
#
#  Recursos de LLM/agente/concorrencia (schema/tool/llm/agent/
#  spawn/await) e acesso a maquina (pyro_*) NAO sao suportados
#  aqui — use --backend go. O gerador emite erro claro nesses casos.
# ============================================================
import json
from typing import List, Set

from ast_nodes import *
from foreign import collect_imports, resolve_library_lang


class CodeGenNodeError(Exception):
    pass


# linguagens estrangeiras tratadas como JavaScript por este backend
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
    """Evita colisao de identificadores Cryo com palavras reservadas JS."""
    return name + '_' if name in _JS_KEYWORDS else name


# builtins Cryo nao suportados no backend Node (rota: --backend go)
_UNSUPPORTED = {
    'llm', 'agent', 'tools', 'tools_json', 'tool_get', 'schema_of',
    'http_get', 'http_post', 'sleep', 'skills', 'skill_get', 'skill_has',
    'skills_json', 'pyro_exec', 'pyro_env', 'pyro_args', 'pyro_time',
    'pyro_read', 'pyro_write', 'pyro_write_file', 'pyro_open', 'pyro_exit',
    'input',
}


class _Types:
    """Inferência de tipos leve para o backend Node: distingue int/number
    (divisão inteira vs. float) e array/map (bounds-check de índice)."""

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
                 'json_encode': 'string', 'has': 'bool',
                 'upper': 'string', 'lower': 'string', 'trim': 'string',
                 'contains': 'bool', 'find': 'int', 'replace': 'string',
                 'substr': 'string', 'split': 'string[]', 'join': 'string',
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
        self._imported_langs: Set[str] = set()

    # ── util ────────────────────────────────────────────────
    def _emit(self, line: str):
        self._cur.append('  ' * self._indent + line if line else '')

    def _err(self, msg):
        raise CodeGenNodeError(msg)

    # ── entrada principal ───────────────────────────────────
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
                    self._err("'tool fn' (LLM) nao e suportado no backend node; "
                              "use --backend go.")
                self._cur, self._indent = self._funcs, 0
                self._function(node)
            elif isinstance(node, EnumDecl):
                self._cur, self._indent = self._enums, 0
                self._enum(node)
            elif isinstance(node, StructDecl):
                pass  # structs = objetos literais; sem declaracao necessaria
            elif isinstance(node, SkillDecl):
                self._err("'skill' (LLM) nao e suportado no backend node; "
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
        for i, m in enumerate(n.members):
            self._emit(f"const {n.name}_{m} = {i};")

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
                  "  if (b === 0) throw new Error('[Cryo Seguranca] DivisaoPorZero');",
                  "  return a / b;",
                  "}", ""]
        if 'idiv' in self._helpers:
            H += ["function cryoIDiv(a, b) {",
                  "  if (b === 0) throw new Error('[Cryo Seguranca] DivisaoPorZero');",
                  "  return Math.trunc(a / b);   // divisão inteira (trunca p/ zero)",
                  "}", ""]
        if 'index' in self._helpers:
            H += ["function cryoIndex(a, i) {",
                  "  if (i < 0 || i >= a.length)",
                  "    throw new Error('[Cryo Seguranca] IndexError: índice ' + i + ' fora dos limites (len=' + a.length + ')');",
                  "  return a[i];",
                  "}", ""]
        if 'setindex' in self._helpers:
            H += ["function cryoSetIndex(a, i, v) {",
                  "  if (i < 0 || i >= a.length)",
                  "    throw new Error('[Cryo Seguranca] IndexError: índice ' + i + ' fora dos limites (len=' + a.length + ')');",
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
                  "  if (b === 0) throw new Error('[Cryo Seguranca] DivisaoPorZero');",
                  "  return a % b;",
                  "}", ""]
        if 'unwrap' in self._helpers:
            H += ["function cryoUnwrap(x) {",
                  "  if (x == null) throw new Error('[Cryo Seguranca] unwrap de nulo');",
                  "  return x;",
                  "}", ""]
        return H

    # ── funcoes ─────────────────────────────────────────────
    def _function(self, n: FunctionDecl):
        params = ', '.join(jsid(p[1]) for p in n.params)
        self._emit(f"function {jsid(n.name)}({params}) {{")
        self._t.push()
        for p in n.params:
            self._t.set(p[1], p[0])   # p = (tipo, nome)
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
    def _stmt(self, n: Node):
        if isinstance(n, VarDecl):
            if n.value is None:
                self._emit(f"let {jsid(n.name)};")
            else:
                self._emit(f"let {jsid(n.name)} = {self._expr(n.value)};")
            self._t.set(n.name, n.var_type)
        elif isinstance(n, ConstDecl):
            self._emit(f"const {jsid(n.name)} = {self._expr(n.value)};")
            self._t.set(n.name, n.var_type)
        elif isinstance(n, Assignment):
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
        elif isinstance(n, Break):
            self._emit("break;")
        elif isinstance(n, Continue):
            self._emit("continue;")
        elif isinstance(n, TryCatch):
            self._try(n)
        elif isinstance(n, Assert):
            msg = self._expr(n.message) if n.message else '"assert falhou"'
            self._emit(f"if (!({self._expr(n.condition)})) throw new Error({msg});")
        elif isinstance(n, SafetyBlock):
            self._block(n.body)   # JS nao tem 'unsafe'; emite o corpo
        elif isinstance(n, ForeignBlock):
            self._foreign(n)
        elif isinstance(n, CallExpr) and n.callee == 'throw':
            arg = self._expr(n.args[0]) if n.args else '"erro"'
            self._emit(f"throw {arg};")
        else:
            # statement-expressao (ex.: chamada de funcao, m.push(...))
            self._emit(f"{self._expr(n)};")

    def _if(self, n: If):
        self._emit(f"if ({self._expr(n.condition)}) {{")
        self._indent += 1; self._block(n.then_body); self._indent -= 1
        if n.else_body:
            # else-if achatado quando o else e um unico If
            if len(n.else_body) == 1 and isinstance(n.else_body[0], If):
                self._emit("} else")
                # remonta como 'else if' colando na proxima linha
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
            self._emit(f"// [Cryo] bloco >{n.lang}< omitido no backend Node "
                       f"(use >Node( ... ))")

    def _inline(self, n: Node) -> str:
        """Forma sem ';' para init/update de for."""
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

    # ── expressoes ──────────────────────────────────────────
    def _expr(self, n: Node) -> str:
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
            # bounds-check em arrays E strings (ambos têm .length e [i]);
            # maps ficam de fora (chave ausente -> undefined é esperado)
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
            return self._expr(n.expr)   # tipos sao dinamicos em JS
        if isinstance(n, UnwrapExpr):
            if self.safe:
                self._helpers.add('unwrap')
                return f"cryoUnwrap({self._expr(n.operand)})"
            return self._expr(n.operand)
        if isinstance(n, (SpawnExpr, AwaitExpr)):
            self._err("concorrencia (spawn/await) nao e suportada no backend node; "
                      "use --backend go.")
        self._err(f"expressao nao suportada no backend node: {type(n).__name__}")

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
            self._err(f"'{c}' nao e suportado no backend node; use --backend go.")

        def A(i):
            return self._expr(a[i])

        args = ', '.join(self._expr(x) for x in a)

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
        if c == 'has':
            return f"Object.prototype.hasOwnProperty.call({A(0)}, {A(1)})"
        if c == 'keys':
            return f"Object.keys({A(0)})"
        if c == 'remove':
            return f"(delete {A(0)}[{A(1)}])"
        if c == 'json_encode':
            return f"JSON.stringify({A(0)})"
        if c == 'json_decode':
            return f"JSON.parse({A(0)})"
        # chamada de funcao do usuario
        return f"{jsid(c)}({args})"
