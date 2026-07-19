#!/usr/bin/env python3
# ============================================================
#  Cryo Language Server (LSP)
#
#  Servidor LSP mínimo, sem dependências: fala JSON-RPC sobre
#  stdio (enquadramento Content-Length) e reaproveita o front-end
#  do compilador (lexer/parser/semântica/foreign/módulos).
#
#  Recursos:
#    - textDocument/publishDiagnostics  (léxico, sintático, semântico,
#      módulos e blocos estrangeiros) ao abrir/editar
#    - textDocument/hover               (builtins, palavras-chave,
#      funções/structs/enums do usuário)
#    - textDocument/definition          (funções/structs/enums)
#    - textDocument/documentSymbol      (outline do arquivo)
#
#  Uso:  python burnout/lsp.py         (stdio; um editor o inicia)
# ============================================================
import json
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
for _p in ('cryo', 'pyro'):
    _d = os.path.join(_root, _p)
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)
sys.path.insert(0, _here)

from lexer import Lexer, LexerError               # noqa: E402
from parser import Parser, ParseError             # noqa: E402
import semantic                                    # noqa: E402
from foreign import verify as verify_foreign, ForeignError  # noqa: E402
from modules import resolve_modules, ModuleError   # noqa: E402
from ast_nodes import (                            # noqa: E402
    FunctionDecl, StructDecl, EnumDecl, ConstDecl, SkillDecl,
)

# ── documentação de builtins/keywords para hover ───────────
_BUILTIN_DOC = {
    'print': "`print(v)` — imprime um valor.",
    'input': "`input(prompt) -> string` — lê uma linha do terminal.",
    'len': "`len(x) -> int` — tamanho de string/array/map.",
    'assert': "`assert(cond)` / `assert(cond, msg)` — aborta se falso.",
    'to_string': "`to_string(v) -> string` — converte para texto.",
    'to_int': "`to_int(v) -> int` — converte para inteiro.",
    'to_number': "`to_number(v) -> number` — converte para ponto flutuante.",
    'sqrt': "`sqrt(x) -> number`", 'pow': "`pow(x, y) -> number`",
    'abs': "`abs(x) -> number`", 'min': "`min(a, b)`", 'max': "`max(a, b)`",
    'floor': "`floor(x) -> number`", 'ceil': "`ceil(x) -> number`",
    'round': "`round(x) -> number`",
    'has': "`has(m, k) -> bool` — a chave existe no mapa?",
    'keys': "`keys(m) -> K[]` — chaves do mapa.",
    'remove': "`remove(m, k)` — remove a chave do mapa.",
    'upper': "`upper(s) -> string`", 'lower': "`lower(s) -> string`",
    'trim': "`trim(s) -> string`",
    'contains': "`contains(s, sub) -> bool`", 'find': "`find(s, sub) -> int`",
    'replace': "`replace(s, velho, novo) -> string`",
    'substr': "`substr(s, inicio, n) -> string`",
    'split': "`split(s, sep) -> string[]`", 'join': "`join(arr, sep) -> string`",
    'json_encode': "`json_encode(v) -> string` — serializa para JSON.",
    'json_decode': "`json_decode(s) as T` — desserializa JSON.",
    'http_get': "`http_get(url) -> string` — GET HTTP (\"\" em erro).",
    'http_post': "`http_post(url, body) -> string` — POST HTTP.",
    'sleep': "`sleep(ms)` — pausa em milissegundos.",
    'llm': "`llm(modelo, prompt) [as T]` — chamada de LLM (structured output).",
    'agent': "`agent(modelo, prompt[, tools, passos])` — laço de tool-calling.",
    'schema_of': "`schema_of(T) -> string` — JSON Schema de um tipo.",
    'tools': "`tools() -> string[]` — nomes das tools.",
    'tools_json': "`tools_json() -> string` — catálogo de tools em JSON.",
    'pyro_exec': "`pyro_exec(cmd) -> string` — executa um comando de shell.",
    'pyro_write_file': "`pyro_write_file(path, s) -> bool` — grava um arquivo.",
    'pyro_open': "`pyro_open(path) -> bool` — abre no app padrão do SO.",
}
_KEYWORD_DOC = {
    'fn': "Declaração de função: `fn nome(params) -> tipo ={ ... }`.",
    'tool': "`tool fn` — função exposta a um LLM (schema derivado da assinatura).",
    'schema': "Declara um `schema` (struct com JSON Schema nativo).",
    'struct': "Declara um registro de campos nomeados.",
    'enum': "Declara um conjunto de constantes (`Enum_MEMBRO`).",
    'skill': "Declara uma skill de LLM compilada no binário.",
    'const': "Declara uma constante imutável.",
    'if': "Condicional. `if (cond) { ... } else { ... }`.",
    'for': "Laço: `for (init; cond; upd)` ou `for (T x in coll)`.",
    'while': "Laço enquanto a condição for verdadeira.",
    'return': "Retorna de uma função.",
    'spawn': "`spawn expr` — inicia trabalho concorrente (future<T>).",
    'await': "`await f` — aguarda o resultado de um future.",
    'try': "Tratamento de erro: `try { } catch (string e) { } finally { }`.",
    'import': "`import \"arquivo.cryo\"` (módulo) ou `import >Lang<` (linguagem).",
    'library': "`library >lang nome<` — importa uma biblioteca estrangeira.",
    'map': "Tipo mapa: `map<K, V>`.", 'future': "Tipo de resultado pendente.",
    'unsafe': "Bloco sem instrumentação de segurança.",
    'safe': "Reativa a instrumentação dentro de um bloco unsafe.",
}


# ── protocolo JSON-RPC (stdio, Content-Length) ──────────────
def _read_message(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode('ascii', 'replace').strip()
        if line == '':
            break
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip().lower()] = v.strip()
    n = int(headers.get('content-length', 0))
    body = stream.read(n)
    return json.loads(body.decode('utf-8'))


def _write_message(stream, obj):
    data = json.dumps(obj).encode('utf-8')
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode('ascii'))
    stream.write(data)
    stream.flush()


# ── utilidades de texto/posição ─────────────────────────────
def _uri_to_path(uri):
    if uri.startswith('file://'):
        p = uri[7:]
        if re.match(r'/[A-Za-z]:', p):    # /C:/... no Windows
            p = p[1:]
        return os.path.normpath(p.replace('/', os.sep))
    return uri


def _line_len(text, line0):
    lines = text.split('\n')
    return len(lines[line0]) if 0 <= line0 < len(lines) else 0


def _word_at(text, line0, char0):
    lines = text.split('\n')
    if not (0 <= line0 < len(lines)):
        return None
    s = lines[line0]
    if char0 > len(s):
        char0 = len(s)
    i = char0
    while i > 0 and (s[i - 1].isalnum() or s[i - 1] == '_'):
        i -= 1
    j = char0
    while j < len(s) and (s[j].isalnum() or s[j] == '_'):
        j += 1
    w = s[i:j]
    return w or None


_LEX_RE = re.compile(r'Linha (\d+), Col (\d+): (.*)', re.S)
_PARSE_RE = re.compile(r'Linha (\d+): (.*)', re.S)


def _diag(line0, ch0, ch1, message, severity=1):
    line0 = max(0, line0)
    return {
        "range": {"start": {"line": line0, "character": max(0, ch0)},
                  "end": {"line": line0, "character": max(ch0, ch1)}},
        "severity": severity, "source": "cryo", "message": message,
    }


def _full_line_diag(text, line1, message):
    line0 = max(0, line1 - 1)
    return _diag(line0, 0, _line_len(text, line0), message)


def compute_diagnostics(uri, text):
    diags = []
    # léxico
    try:
        tokens = Lexer(text).tokenize()
    except LexerError as e:
        m = _LEX_RE.search(str(e))
        if m:
            line0 = max(0, int(m.group(1)) - 1)
            col0 = max(0, int(m.group(2)) - 1)
            diags.append(_diag(line0, col0, col0 + 1, m.group(3)))
        else:
            diags.append(_diag(0, 0, 1, str(e)))
        return diags
    # sintático
    try:
        ast = Parser(tokens).parse()
    except ParseError as e:
        m = _PARSE_RE.search(str(e))
        diags.append(_full_line_diag(text, int(m.group(1)), m.group(2)) if m
                     else _diag(0, 0, 1, str(e)))
        return diags
    # módulos (best-effort; erro vira diagnóstico na linha 0)
    try:
        base = os.path.dirname(_uri_to_path(uri))
        resolved = resolve_modules(ast, base)
    except ModuleError as e:
        diags.append(_diag(0, 0, 1, f"módulo: {e}"))
        resolved = ast
    # semântica
    try:
        for line, msg in semantic.findings(resolved):
            diags.append(_full_line_diag(text, line, msg) if line
                         else _diag(0, 0, 1, msg))
    except Exception as e:                       # nunca derruba o servidor
        diags.append(_diag(0, 0, 1, f"análise: {e}"))
    # blocos estrangeiros
    try:
        verify_foreign(resolved)
    except ForeignError as e:
        diags.append(_diag(0, 0, 1, str(e)))
    return diags


def _decls(text):
    """(nome -> (kind, line, detail)) das declarações de topo do arquivo."""
    out = {}
    try:
        ast = Parser(Lexer(text).tokenize()).parse()
    except Exception:
        return out
    for n in ast.statements:
        if isinstance(n, FunctionDecl):
            params = ', '.join(f"{t} {nm}" for t, nm in n.params)
            ret = f" -> {n.return_type}" if n.return_type else ""
            kw = "tool fn" if n.is_tool else "fn"
            out[n.name] = ('fn', getattr(n, 'line', 0),
                           f"{kw} {n.name}({params}){ret}")
        elif isinstance(n, StructDecl):
            out[n.name] = ('struct', getattr(n, 'line', 0),
                           f"struct {n.name} {{ {len(n.fields)} campos }}")
        elif isinstance(n, EnumDecl):
            out[n.name] = ('enum', getattr(n, 'line', 0),
                           f"enum {n.name} {{ {', '.join(n.members)} }}")
        elif isinstance(n, ConstDecl):
            out[n.name] = ('const', getattr(n, 'line', 0),
                           f"const {n.var_type} {n.name}")
        elif isinstance(n, SkillDecl):
            out[n.name] = ('skill', getattr(n, 'line', 0), f"skill {n.name}")
    return out


def hover(text, line0, char0):
    w = _word_at(text, line0, char0)
    if not w:
        return None
    if w in _BUILTIN_DOC:
        return _BUILTIN_DOC[w]
    if w in _KEYWORD_DOC:
        return _KEYWORD_DOC[w]
    d = _decls(text).get(w)
    if d:
        return f"```cryo\n{d[2]}\n```"
    return None


_SYMBOL_KIND = {'fn': 12, 'struct': 23, 'enum': 10, 'const': 14, 'skill': 5}


def definition(uri, text, line0, char0):
    w = _word_at(text, line0, char0)
    if not w:
        return None
    d = _decls(text).get(w)
    if not d or not d[1]:
        return None
    ln = max(0, d[1] - 1)
    return {"uri": uri, "range": {"start": {"line": ln, "character": 0},
                                  "end": {"line": ln, "character": _line_len(text, ln)}}}


def document_symbols(text):
    syms = []
    for name, (kind, line, detail) in _decls(text).items():
        if not line:
            continue
        ln = max(0, line - 1)
        rng = {"start": {"line": ln, "character": 0},
               "end": {"line": ln, "character": _line_len(text, ln)}}
        syms.append({"name": name, "detail": detail,
                     "kind": _SYMBOL_KIND.get(kind, 12),
                     "range": rng, "selectionRange": rng})
    return syms


# ── servidor ────────────────────────────────────────────────
class Server:
    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.docs = {}
        self.shutdown = False

    def _publish(self, uri):
        self._notify("textDocument/publishDiagnostics",
                     {"uri": uri, "diagnostics": compute_diagnostics(uri, self.docs.get(uri, ""))})

    def _notify(self, method, params):
        _write_message(self.stdout, {"jsonrpc": "2.0", "method": method, "params": params})

    def _respond(self, mid, result):
        _write_message(self.stdout, {"jsonrpc": "2.0", "id": mid, "result": result})

    def serve(self):
        while True:
            msg = _read_message(self.stdin)
            if msg is None:
                break
            method = msg.get("method")
            mid = msg.get("id")
            params = msg.get("params") or {}
            try:
                self._dispatch(method, mid, params)
            except Exception as e:
                if mid is not None:
                    self._respond(mid, None)
                sys.stderr.write(f"[cryo-lsp] erro em {method}: {e}\n")
            if method == "exit":
                break

    def _dispatch(self, method, mid, params):
        if method == "initialize":
            self._respond(mid, {"capabilities": {
                "textDocumentSync": 1,                 # full sync
                "hoverProvider": True,
                "definitionProvider": True,
                "documentSymbolProvider": True,
            }, "serverInfo": {"name": "cryo-lsp", "version": "0.1"}})
        elif method in ("initialized", "$/setTrace", "workspace/didChangeConfiguration"):
            pass
        elif method == "shutdown":
            self.shutdown = True
            self._respond(mid, None)
        elif method == "textDocument/didOpen":
            doc = params["textDocument"]
            self.docs[doc["uri"]] = doc.get("text", "")
            self._publish(doc["uri"])
        elif method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            changes = params.get("contentChanges", [])
            if changes:
                self.docs[uri] = changes[-1]["text"]    # full sync: último texto
            self._publish(uri)
        elif method == "textDocument/didSave":
            uri = params["textDocument"]["uri"]
            self._publish(uri)
        elif method == "textDocument/didClose":
            self.docs.pop(params["textDocument"]["uri"], None)
        elif method == "textDocument/hover":
            uri = params["textDocument"]["uri"]
            pos = params["position"]
            h = hover(self.docs.get(uri, ""), pos["line"], pos["character"])
            self._respond(mid, {"contents": {"kind": "markdown", "value": h}} if h else None)
        elif method == "textDocument/definition":
            uri = params["textDocument"]["uri"]
            pos = params["position"]
            self._respond(mid, definition(uri, self.docs.get(uri, ""), pos["line"], pos["character"]))
        elif method == "textDocument/documentSymbol":
            uri = params["textDocument"]["uri"]
            self._respond(mid, document_symbols(self.docs.get(uri, "")))
        elif mid is not None:
            self._respond(mid, None)                    # método não suportado


def main():
    Server(sys.stdin.buffer, sys.stdout.buffer).serve()


if __name__ == "__main__":
    main()
