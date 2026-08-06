#!/usr/bin/env python3
# ============================================================
#  test_frontend.py — roadmap 10.11 / 10.12 / 10.13
#
#  Covers the three front-end items together, because they are one
#  feature split three ways:
#    10.12  the general `<k = v>` structure-parameter mechanism
#    10.11  its html/javascript/CSS application
#    10.13  the two output modes
#
#  What matters here is not "it produced some HTML" but that the
#  WRONG programs are rejected with a message in Cryo's own terms.
#  A misrouted structure parameter would otherwise surface as a blank
#  page in a browser, which is the worst place to debug it.
# ============================================================
import os
import shutil
import subprocess
import sys
import tempfile

ROOT  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))

from lexer import Lexer                      # noqa: E402
from parser import Parser, ParseError        # noqa: E402
import foreign                               # noqa: E402
import frontend                              # noqa: E402

_passed = 0
_failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def parse(src):
    return Parser(Lexer(src).tokenize()).parse()


def rejects(label, src, expect_sub):
    """The program must be refused, and the message must be the USEFUL one."""
    try:
        ast = parse(src)
        foreign.verify(ast)
        frontend.collect(ast)
    except (ParseError, foreign.ForeignError, frontend.FrontendError) as e:
        check(label, expect_sub.lower() in str(e).lower(),
              f"expected {expect_sub!r} in: {e}")
        return
    check(label, False, "was accepted, but should have been rejected")


PAGE = '''
import >html<
import >javascript<
import >CSS<
fn styles()   ={ >CSS( body { color: #eee; } ) }
fn behavior() ={ >javascript( document.title = "T"; ) }
fn page()     ={ >html( <h1>Hi</h1> )<script = behavior, style = styles> }
'''


def test_struct_params():
    print("[10.12] structure parameters on foreign blocks")
    from ast_nodes import ForeignBlock, FunctionDecl

    def params_of(src, fn):
        for s in parse(src).statements:
            if isinstance(s, FunctionDecl) and s.name == fn:
                for b in s.body:
                    if isinstance(b, ForeignBlock):
                        return b.params
        return None

    check("parses <k = v, k2 = v2> in order",
          params_of(PAGE, 'page') == [('script', 'behavior'), ('style', 'styles')])
    check("a block without a tail has no params",
          params_of(PAGE, 'styles') == [])
    check("empty <> is legal and yields no params",
          params_of('import >Java<\nfn a() ={ >Java( x(); )<> }', 'a') == [])
    check("works on a non-front-end language too (Java)",
          params_of('import >Java<\nfn h() ={ int q = 1; }\n'
                    'fn a() ={ >Java( x(); )<util = h> }', 'a') == [('util', 'h')])

    # `<` is also less-than: the tail must only be consumed when unambiguous.
    check("plain block still parses when nothing follows",
          params_of('import >c<\nfn a() ={ >c( puts("x"); ) }', 'a') == [])

    rejects("duplicate key is refused",
            'import >Java<\nfn h() ={ int q=1; }\n'
            'fn a() ={ >Java( x; )<u = h, u = h> }', "given twice")
    rejects("missing comma is refused",
            'import >Java<\nfn h() ={ int q=1; }\n'
            'fn a() ={ >Java( x; )<u = h v = h> }', "expected ',' or '>'")
    rejects("a parameter must name something declared",
            'import >Java<\nfn a() ={ >Java( x; )<u = ghost> }', "not declared")


def test_resolution():
    print("[10.11] front-end structure resolution")
    mod = frontend.collect(parse(PAGE))
    check("page block found", mod.page.name == 'page')
    check("script slot resolved to the javascript block",
          mod.script is not None and mod.script.name == 'behavior')
    check("style slot resolved to the CSS block",
          mod.style is not None and mod.style.name == 'styles')

    rejects("a file with no html block is refused",
            'import >CSS<\nfn s() ={ >CSS( a{} ) }', "no html block")
    rejects("two html blocks are ambiguous",
            'import >html<\nfn a() ={ >html( <b>1</b> ) }\n'
            'fn b() ={ >html( <b>2</b> ) }', "ambiguous")
    rejects("script= pointing at a CSS block is refused",
            'import >html<\nimport >CSS<\nfn s() ={ >CSS( a{} ) }\n'
            'fn p() ={ >html( <b>x</b> )<script = s> }', "expects a javascript")
    rejects("a slot pointing at ordinary Cryo is refused",
            'import >html<\nfn h() ={ int q = 1; }\n'
            'fn p() ={ >html( <b>x</b> )<script = h> }', "not a front-end block")
    rejects("an unknown slot key is refused",
            'import >html<\nfn p() ={ >html( <b>x</b> )<layout = p> }',
            "not a known structure parameter")

    # A function with a block AND other statements is ordinary code, not a
    # named block — treating it as one would silently drop the rest.
    mixed = ('import >html<\nimport >CSS<\n'
             'fn s() ={ int q = 1; >CSS( a{} ) }\n'
             'fn p() ={ >html( <b>x</b> ) }')
    m = frontend.collect(parse(mixed))
    check("a block mixed with other statements is not a named block",
          m.style is None)


def test_render_modes():
    print("[10.13] both output modes")
    ast = parse(PAGE)
    html = frontend.render(ast, 'html')
    pyro = frontend.render(ast, 'pyro')

    for name, doc in (('html', html), ('pyro', pyro)):
        check(f"[{name}] is a complete document",
              doc.startswith('<!doctype html>') and doc.rstrip().endswith('</html>'))
        check(f"[{name}] inlines the CSS block", 'color: #eee;' in doc)
        check(f"[{name}] carries the html block body", '<h1>Hi</h1>' in doc)
        check(f"[{name}] contains the author's javascript",
              'document.title = "T";' in doc)

    check("[html] is self-contained (fetches nothing)",
          'fetch(' not in html)
    check("[pyro] loads the companion binary",
          "fetch('app.wasm')" in pyro)
    check("[pyro] defers the author's script until the binary is ready",
          "addEventListener('cryo:ready'" in pyro)
    check("[html] does NOT defer (there is nothing to wait for)",
          'cryo:ready' not in html)

    try:
        frontend.render(ast, 'bogus')
        check("an unknown --emit mode is refused", False)
    except frontend.FrontendError as e:
        check("an unknown --emit mode is refused", 'html' in str(e))


def test_strip():
    print("[10.13] stripping the page from the program")
    src = PAGE + '\nfn fib(int n) -> int ={ return n; }\n'
    stripped = frontend.strip_frontend(parse(src))
    names = [getattr(s, 'name', None) for s in stripped.statements]
    check("front-end block functions are removed",
          'page' not in names and 'styles' not in names and 'behavior' not in names)
    check("ordinary Cryo functions survive", 'fib' in names)
    check("front-end imports are removed (a codegen would reject them)",
          not any(type(s).__name__ == 'Import' for s in stripped.statements))
    check("a page with no logic is detected", not frontend.has_logic(parse(PAGE)))
    check("a page with logic is detected", frontend.has_logic(parse(src)))
    # strip must not mutate the caller's AST
    ast = parse(src)
    frontend.strip_frontend(ast)
    check("the original AST is not mutated", len(ast.statements) == len(parse(src).statements))


def _run(args):
    p = subprocess.run([sys.executable, CRYOC] + args,
                       capture_output=True, text=True, cwd=ROOT)
    return p.returncode, p.stdout + p.stderr


def test_cli_end_to_end():
    print("[10.11-10.13] via the CLI, on the shipped example")
    example = os.path.join(ROOT, 'Cryo', 'examples', 'frontend', 'app.cryo')
    if not os.path.isfile(example):
        check("example present", False, example)
        return

    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 'index.html')
        rc, log = _run([example, '--backend', 'frontend', '--emit', 'html',
                        '-o', out, '--no-banner'])
        check("--emit html exits 0", rc == 0, log[-400:])
        if rc == 0:
            doc = open(out, encoding='utf-8').read()
            check("--emit html has no companion binary",
                  not os.path.exists(os.path.join(d, 'app.wasm')))
            check("--emit html inlines everything",
                  '<style>' in doc and '<script>' in doc)

    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 'index.html')
        rc, log = _run([example, '--backend', 'frontend', '--emit', 'pyro',
                        '-o', out, '--no-banner'])
        check("--emit pyro exits 0", rc == 0, log[-400:])
        if rc == 0:
            wasm = os.path.join(d, 'app.wasm')
            check("--emit pyro writes the companion binary", os.path.exists(wasm))
            if os.path.exists(wasm):
                blob = open(wasm, 'rb').read()
                check("the companion is a real wasm module",
                      blob[:4] == b'\x00asm', repr(blob[:8]))

    # A page with no Cryo logic cannot produce a binary — say so, do not
    # emit a shell that fetches a file which will never exist.
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'p.cryo')
        open(src, 'w', encoding='utf-8').write(PAGE)
        rc, log = _run([src, '--backend', 'frontend', '--emit', 'pyro',
                        '-o', os.path.join(d, 'i.html'), '--no-banner'])
        check("--emit pyro on a logic-free page fails clearly",
              rc != 0 and 'emit html' in log, log[-300:])


def test_node_lowering():
    """ISSUES/20 — >html< and >CSS< on --backend node.

    They used to be replaced by a comment ("block omitted in Node backend"),
    so `styles()` and `page()` compiled into functions that returned nothing.
    The page was silently empty — no error, no output, the failure mode that
    costs the most to find. They now lower to template literals.
    """
    print("[ISSUES/20] >html< and >CSS< on the node backend")
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'p.cryo')
        open(src, 'w', encoding='utf-8').write(PAGE)
        out = os.path.join(d, 'p.js')
        rc, log = _run([src, '--backend', 'node', '-o', out, '--no-banner'])
        check("compiles", rc == 0, log[-400:])
        if rc != 0:
            return
        js = open(out, encoding='utf-8').read()
        check("nothing is reported as omitted", 'omitted' not in js, js[:400])
        check("the CSS block became a returned string",
              'return `body { color: #eee; }`;' in js, js[:600])
        check("the html block became a returned string",
              'return `<h1>Hi</h1>`;' in js, js[:600])
        # A template literal is only useful if it is valid JavaScript, and an
        # unescaped backtick in the author's markup would not be.
        if shutil.which('node'):
            rc2 = subprocess.run(['node', '--check', out],
                                 capture_output=True, text=True)
            check("node parses the generated file", rc2.returncode == 0,
                  (rc2.stdout + rc2.stderr)[-300:])

    # A backtick and a ${ in the author's own markup must survive as text,
    # not terminate the template or interpolate.
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'q.cryo')
        open(src, 'w', encoding='utf-8').write(
            'import >html<\nfn page() ={ >html( <p>a ` b ${c} d</p> ) }\n')
        out = os.path.join(d, 'q.js')
        rc, log = _run([src, '--backend', 'node', '-o', out, '--no-banner'])
        check("a page whose markup contains ` and ${ compiles", rc == 0, log[-300:])
        if rc == 0:
            js = open(out, encoding='utf-8').read()
            check("the backtick is escaped", '\\`' in js, js[:400])
            check("the interpolation marker is escaped", '\\${' in js, js[:400])
            if shutil.which('node'):
                rc2 = subprocess.run(['node', '--check', out],
                                     capture_output=True, text=True)
                check("and node still parses it", rc2.returncode == 0,
                      (rc2.stdout + rc2.stderr)[-300:])


def test_backend_whitelist():
    """ISSUES/20 — the programmatic entry point accepts the page backends."""
    print("[ISSUES/20] burnout.BACKENDS")
    # Loaded by PATH, not by name: `Burnout/__init__.py` is the package entry
    # point, and these tests run with Cryo/ — not the parent of Burnout/ — on
    # sys.path, so `import Burnout` is not available here.
    sys.path.insert(0, os.path.join(ROOT, 'Burnout'))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'burnout_pkg', os.path.join(ROOT, 'Burnout', '__init__.py'))
    burnout = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(burnout)
    except Exception as e:                      # pragma: no cover
        check("Burnout/__init__.py imports", False, repr(e))
        return
    check("'frontend' is a valid backend", 'frontend' in burnout.BACKENDS,
          repr(burnout.BACKENDS))
    check("'html' is a valid backend", 'html' in burnout.BACKENDS,
          repr(burnout.BACKENDS))
    # And the whitelist still refuses a name that is not a backend, rather
    # than having been widened to accept anything.
    try:
        burnout.compile_source('print(1);', backend='nope')
        check("an unknown backend is still refused", False)
    except ValueError as e:
        check("an unknown backend is still refused", 'nope' in str(e))
    except Exception as e:
        check("an unknown backend is still refused", False, repr(e))


def main():
    print("-- front-end structure (10.11 / 10.12 / 10.13) --")
    test_struct_params()
    test_resolution()
    test_render_modes()
    test_strip()
    test_cli_end_to_end()
    test_node_lowering()
    test_backend_whitelist()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
