#!/usr/bin/env python3
# ============================================================
#  Cryo Language Server (LSP)
#
#  Minimal LSP server, no dependencies: speaks JSON-RPC over
#  stdio (Content-Length framing) and reuses the front-end
#  from the compiler (lexer/parser/semantics/foreign/modules).
#
#  Resources:
#    - textDocument/publishDiagnostics  (lexical, syntax, semântico,
#      módulos e foreign blocks) ao abrir/editar
#    - textDocument/hover               (builtins, keywords,
#      user functions/structs/enums)
#    - textDocument/definition          (functions/structs/enums)
#    - textDocument/documentSymbol      (file outline)
#
#  Usage:  python burnout/lsp.py         (stdio; an editor starts it)
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

# ── builtins/keywords documentation for hover ───────────
_BUILTIN_DOC = {
    'print': "`print(v)` — prints a value.",
    'input': "`input(prompt) -> string` — reads a line from the terminal.",
    'len': "`len(x) -> int` — length of string/array/map.",
    'assert': "`assert(cond)` / `assert(cond, msg)` — aborts if false.",
    'to_string': "`to_string(v) -> string` — converts to text.",
    'to_int': "`to_int(v) -> int` — converts to integer.",
    'to_number': "`to_number(v) -> number` — converts to floating point.",
    'sqrt': "`sqrt(x) -> number`", 'pow': "`pow(x, y) -> number`",
    'abs': "`abs(x) -> number`", 'min': "`min(a, b)`", 'max': "`max(a, b)`",
    'floor': "`floor(x) -> number`", 'ceil': "`ceil(x) -> number`",
    'round': "`round(x) -> number`",
    'has': "`has(m, k) -> bool` — does the key exist in the map?",
    'keys': "`keys(m) -> K[]` — map keys.",
    'remove': "`remove(m, k)` — removes the key from the map.",
    'upper': "`upper(s) -> string`", 'lower': "`lower(s) -> string`",
    'trim': "`trim(s) -> string`",
    'contains': "`contains(s, sub) -> bool`", 'find': "`find(s, sub) -> int`",
    'replace': "`replace(s, velho, novo) -> string`",
    'substr': "`substr(s, inicio, n) -> string`",
    'split': "`split(s, sep) -> string[]`", 'join': "`join(arr, sep) -> string`",
    'json_encode': "`json_encode(v) -> string` — serializes to JSON.",
    'json_decode': "`json_decode(s) as T` — deserializes JSON.",
    'http_get': "`http_get(url) -> string` — HTTP GET (\"\" on error).",
    'http_post': "`http_post(url, body) -> string` — POST HTTP.",
    'sleep': "`sleep(ms)` — pauses in milliseconds.",
    'llm': "`llm(model, prompt) [as T]` — LLM call (structured output).",
    'agent': "`agent(model, prompt[, tools, steps])` — tool-calling loop.",
    'schema_of': "`schema_of(T) -> string` — JSON Schema of a type.",
    'tools': "`tools() -> string[]` — nomes das tools.",
    'tools_json': "`tools_json() -> string` — JSON catalog of tools.",
    'pyro_exec': "`pyro_exec(cmd) -> string` — executes a shell command.",
    'pyro_write_file': "`pyro_write_file(path, s) -> bool` — writes a file.",
    'pyro_open': "`pyro_open(path) -> bool` — opens in the OS default app.",
}
_KEYWORD_DOC = {
    'fn': "Function declaration: `fn name(params) -> type ={ ... }`.",
    'tool': "`tool fn` — function exposed to an LLM (schema derived from signature).",
    'schema': "Declares a `schema` (struct with native JSON Schema).",
    'struct': "Declares a record of named fields.",
    'enum': "Declares a set of constants (`Enum_MEMBER`).",
    'skill': "Declares an LLM skill compiled into the binary.",
    'const': "Declares an immutable constant.",
    'if': "Condicional. `if (cond) { ... } else { ... }`.",
    'for': "Loop: `for (init; cond; upd)` or `for (T x in coll)`.",
    'while': "Loop while condition is true.",
    'return': "Returns from a function.",
    'spawn': "`spawn expr` — inicia trabalho concorrente (future<T>).",
    'await': "`await f` — waits for the result of a future.",
    'try': "Error handling: `try { } catch (string e) { } finally { }`.",
    'import': "`import \"file.cryo\"` (module) or `import >Lang<` (language).",
    'library': "`library >lang name<` — imports a foreign library.",
    'map': "Tipo mapa: `map<K, V>`.", 'future': "Pending result type.",
    'unsafe': "Block without security instrumentation.",
    'safe': "Reactivates instrumentation inside an unsafe block.",
}


# ── JSON-RPC protocol (stdio, Content-Length) ──────────────
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


# ── text/position utilities ─────────────────────────────
def _uri_to_path(uri):
    if uri.startswith('file://'):
        p = uri[7:]
        if re.match(r'/[A-Za-z]:', p):    # /C:/... on Windows
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
    # lexical
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
    # syntax
    try:
        ast = Parser(tokens).parse()
    except ParseError as e:
        m = _PARSE_RE.search(str(e))
        diags.append(_full_line_diag(text, int(m.group(1)), m.group(2)) if m
                     else _diag(0, 0, 1, str(e)))
        return diags
    # modules (best-effort; error becomes diagnostic on line 0)
    try:
        base = os.path.dirname(_uri_to_path(uri))
        resolved = resolve_modules(ast, base)
    except ModuleError as e:
        diags.append(_diag(0, 0, 1, f"módulo: {e}"))
        resolved = ast
    # semantics
    try:
        for line, msg in semantic.findings(resolved):
            diags.append(_full_line_diag(text, line, msg) if line
                         else _diag(0, 0, 1, msg))
    except Exception as e:                       # never brings down the server
        diags.append(_diag(0, 0, 1, f"análise: {e}"))
    # foreign blocks
    try:
        verify_foreign(resolved)
    except ForeignError as e:
        diags.append(_diag(0, 0, 1, str(e)))
    return diags


def _decls(text):
    """(name -> (kind, line, detail)) of top-level file declarations."""
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
                            f"enum {n.name} {{ {', '.join(m.name for m in n.members)} }}")
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


# ── server ────────────────────────────────────────────────
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
                sys.stderr.write(f"[cryo-lsp] error in {method}: {e}\n")
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
                self.docs[uri] = changes[-1]["text"]    # full sync: last text
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
            self._respond(mid, None)                    # method not supported


def main():
    Server(sys.stdin.buffer, sys.stdout.buffer).serve()


if __name__ == "__main__":
    main()
