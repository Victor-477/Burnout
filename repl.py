#!/usr/bin/env python3
# ============================================================
#  Burnout — `cryoc repl`  (roadmap 12.2)
#
#  An interactive loop over the Pyro VM: type an expression, see its value;
#  declare something, and it is still there on the next line.
#
#  THE MODEL, AND WHY IT IS THIS ONE
#
#  The VM has no persistent-session mode: every .pyro run is a fresh process
#  that ends when the program does. There is nowhere to keep `x` between two
#  evaluations.
#
#  So the REPL keeps a PRELUDE — the statements typed so far that define state
#  — and on each line compiles `prelude + new line` as one program and runs it.
#  Bindings survive because the whole program is rebuilt from scratch every
#  time, which also means they are always consistent: there is no incremental
#  state to get out of step with the source.
#
#  WHAT PERSISTS, AND WHY THE RULE IS BLUNT
#
#  Declarations and assignments persist. Everything else is evaluated once and
#  not replayed.
#
#  The rule has to be blunt because the alternative is dangerous. If arbitrary
#  statements were replayed, then typing `write_file("x", data)` once would
#  re-run it on every subsequent line of the session — twenty times by the end,
#  silently. Losing `xs.push(1)` is a surprise; re-writing a file twenty times
#  is damage. `:list` shows exactly what will be replayed, so the surprise is
#  always inspectable.
#
#  A persistent VM session would remove the whole question, and is the right
#  long-term answer. This is not it, and does not pretend to be.
# ============================================================
import os
import subprocess
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
for _p in ('Cryo', 'Pyro'):
    sys.path.insert(0, os.path.join(_root, _p))
sys.path.insert(0, _here)

try:
    import readline
except ImportError:
    pass

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

BANNER = """Cryo REPL — running on the Pyro VM.
Type an expression to see its value; `:help` for commands, `:quit` to leave."""

HELP = """  :help            this
  :list            the statements that persist (what gets replayed)
  :reset           forget them and start over
  :load PATH       load a .cryo file into the session
  :backend NAME    pyro (default), go, node or c
  :quit  /  :q     leave

Declarations and assignments persist between lines. Everything else — a call,
a loop, a bare expression — is evaluated once and not replayed, because the
whole program is recompiled and re-run on every line, and replaying a
`write_file(...)` on each subsequent line would be damage rather than a
surprise. `:list` always shows exactly what will be replayed."""


def _persists(node):
    """True if re-running this statement on every later line is both safe and
    necessary to keep the session's state."""
    from ast_nodes import (VarDecl, ConstDecl, Assignment, CompoundAssignment,
                           Increment, FunctionDecl, StructDecl, EnumDecl,
                           ModuleImport, Import, Library, IndexAssignment)
    return isinstance(node, (VarDecl, ConstDecl, Assignment, CompoundAssignment,
                             Increment, IndexAssignment, FunctionDecl,
                             StructDecl, EnumDecl, ModuleImport, Import, Library))


def _is_value_expression(node):
    """A bare expression whose value is worth showing.

    A call to print is excluded: it already prints, and wrapping it would give
    `print(print(x))` — which on a void result is not even well typed.
    """
    from ast_nodes import (CallExpr, BinaryExpr, UnaryExpr, Identifier, Literal,
                           IndexAccess, FieldAccess, MethodCallExpr, TernaryExpr)
    if isinstance(node, CallExpr) and node.callee in ('print', 'println'):
        return False
    return isinstance(node, (CallExpr, BinaryExpr, UnaryExpr, Identifier,
                             Literal, IndexAccess, FieldAccess, MethodCallExpr,
                             TernaryExpr))


def _open_depth(text):
    """Unclosed brackets, ignoring anything inside a string or a comment.

    This is what decides whether to keep reading: a line ending inside a `{`
    is a continuation, not a syntax error, and reporting it as one would make
    multi-line input impossible.
    """
    depth = 0
    i, n = 0, len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in '"\'':
            quote = c
        elif c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        i += 1
    return depth


class Repl:
    def __init__(self, backend='pyro', safe=True):
        self.prelude = []          # source text of the statements that persist
        self.backend = backend
        self.safe = safe
        self.work = tempfile.mkdtemp(prefix='cryo_repl_')

    # ── evaluation ─────────────────────────────────────────
    def _parse(self, text):
        """(statements, error) — never raises."""
        from lexer import Lexer, LexerError
        from parser import Parser, ParseError
        try:
            return Parser(Lexer(text).tokenize()).parse().statements, None
        except (LexerError, ParseError) as e:
            return None, str(e).strip()

    def evaluate(self, line):
        """Compile prelude+line, run it, print whatever it produced.

        A bare expression has to be WRAPPED BEFORE PARSING, not after. Cryo
        does not accept `x + 1;` as a statement — only a call or a bare name —
        so the wrap is what makes it parse at all, and doing it afterwards
        would mean deciding an expression is an expression from an error
        message. `print(x + 1);` either parses or the line really was wrong.
        """
        body = line.strip().rstrip(';').strip()
        stmts, err = self._parse(line)

        if stmts is None:
            # Not a statement. It may still be an expression worth showing.
            wrapped = f'print({body});'
            alt, _ = self._parse(wrapped)
            if alt is None:
                print(err)          # report the STATEMENT error: more likely
                return False         # to be what they meant
            return self._go(wrapped, persist=[])

        if not stmts:
            return False

        # It parsed, but `f(1);` and `x;` are values worth showing too.
        if _is_value_expression(stmts[-1]) and len(stmts) == 1:
            wrapped = f'print({body});'
            alt, _ = self._parse(wrapped)
            if alt is not None:
                return self._go(wrapped, persist=[])

        return self._go(line, persist=[line] if any(_persists(s) for s in stmts) else [])

    def _go(self, text, persist):
        src = "\n".join(self.prelude + [text]) + "\n"
        ok, out = self._run(src)
        if out:
            print(out.rstrip("\n"))
        if ok:
            self.prelude.extend(persist)
        return ok

    def _run(self, source):
        import compiler
        try:
            # path='<repl>' so a diagnostic says where the line came from
            # instead of naming the temporary directory, which tells the reader
            # nothing and changes every session.
            code = compiler.compile_source(
                source, self.backend, self.safe, compiler.default_abi(),
                base_dir=self.work, use_cache=True, path='<repl>')
        except Exception as e:
            # The compiler's own message already names the kind of error and
            # renders the offending line (11.24); prefixing it with the Python
            # class name says the same thing twice.
            return False, str(e).strip()
        return self._exec(code)

    def _exec(self, code):
        exe = '.exe' if sys.platform == 'win32' else ''
        if self.backend == 'pyro':
            out = os.path.join(self.work, 'r.pyro')
            open(out, 'wb').write(code)
            vm = _find_vm()
            if vm is None:
                return False, "no Pyro VM binary found — build Pyro/vm"
            cmd = [vm, out]
        elif self.backend == 'node':
            out = os.path.join(self.work, 'r.js')
            open(out, 'w', encoding='utf-8').write(code)
            cmd = ['node', out]
        elif self.backend == 'go':
            out = os.path.join(self.work, 'r.go')
            open(out, 'w', encoding='utf-8').write(code)
            cmd = ['go', 'run', out]
        else:
            import shutil
            out = os.path.join(self.work, 'r.c')
            open(out, 'w', encoding='utf-8').write(code)
            cc = next((c for c in ('gcc', 'clang', 'cc') if shutil.which(c)), None)
            if cc is None:
                return False, "no C toolchain found"
            exe_path = os.path.join(self.work, 'r' + exe)
            rt = os.path.join(_here, 'runtime', 'cryo_runtime.c')
            libs = ['-lm'] + (['-lws2_32'] if sys.platform == 'win32' else [])
            r = subprocess.run([cc, '-O2', '-std=c11', '-I',
                                os.path.join(_here, 'runtime'), '-o', exe_path,
                                out, rt] + libs, capture_output=True, text=True)
            if r.returncode != 0:
                return False, r.stderr.strip()[-400:]
            cmd = [exe_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace',
                               timeout=60, cwd=self.work)
        except subprocess.TimeoutExpired:
            return False, "timed out after 60s (an endless loop?)"
        return r.returncode == 0, (r.stdout or '') + (r.stderr or '')


def _find_vm():
    exe = '.exe' if sys.platform == 'win32' else ''
    for c in (os.path.join(_root, 'build', 'pyrovm' + exe),
              os.path.join(_root, 'Pyro', 'vm', 'pyrovm_go' + exe),
              os.path.join(_root, 'Pyro', 'vm', 'pyrovm' + exe)):
        if os.path.isfile(c):
            return c
    return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='cryoc repl',
                                 description='Interactive Cryo over the Pyro VM.')
    ap.add_argument('--backend', default='pyro',
                    choices=['pyro', 'go', 'node', 'c'])
    ap.add_argument('--unsafe', action='store_true')
    args = ap.parse_args(argv)

    rp = Repl(backend=args.backend, safe=not args.unsafe)
    print(BANNER)
    buf = ''
    while True:
        try:
            line = input('... ' if buf else '>>> ')
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not buf and line.strip().startswith(':'):
            cmd, _, rest = line.strip().partition(' ')
            if cmd in (':quit', ':q'):
                break
            if cmd == ':help':
                print(HELP)
            elif cmd == ':list':
                if rp.prelude:
                    for s in rp.prelude:
                        print('  ' + s)
                else:
                    print('  (nothing yet)')
            elif cmd == ':reset':
                rp.prelude.clear()
                print('  forgotten.')
            elif cmd == ':load':
                path = rest.strip()
                if not path:
                    print('  usage: :load path/to/file.cryo')
                elif not os.path.isfile(path):
                    print(f'  no such file: {path}')
                else:
                    try:
                        content = open(path, encoding='utf-8').read()
                        if rp.evaluate(content):
                            print(f'  loaded {path}')
                    except Exception as e:
                        print(f'  failed to load {path}: {e}')
            elif cmd == ':backend':
                if rest.strip() in ('pyro', 'go', 'node', 'c'):
                    rp.backend = rest.strip()
                    print(f'  backend: {rp.backend}')
                else:
                    print('  usage: :backend pyro|go|node|c')
            else:
                print(f"  unknown command {cmd} — try :help")
            continue

        buf = (buf + '\n' + line) if buf else line
        if _open_depth(buf) > 0:      # unclosed brace: keep reading
            continue
        text, buf = buf, ''
        if text.strip():
            rp.evaluate(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
