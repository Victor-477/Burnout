#!/usr/bin/env python3
# ============================================================
#  test_frontend_routing.py — page programs reach the page backend
#
#  A program built from >html( and >CSS( blocks used to compile
#  "successfully" to go, which drops those blocks and leaves each
#  function empty:
#
#      func styles() {
#          // [Cryo] >CSS< block omitted in Go backend
#      }
#
#  No error, no output, nothing to indicate the program's entire
#  purpose had been discarded. `--backend auto` chose go for these
#  programs, because backends.py did not list the front-end
#  backend at all.
#
#  Three defences, all pinned here:
#    1. auto routes a page program to `frontend`
#    2. an explicit `--backend go` refuses instead of emptying it
#    3. `--emit html` warns when the page calls `cryo.…`, which
#       only exists under `--emit pyro`
# ============================================================
import os
import shutil
import subprocess
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')

_passed = _failed = 0

PAGE = (
    'import >html<\n'
    'import >javascript<\n'
    'import >CSS<\n'
    '\n'
    'fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n-1) + fib(n-2); }\n'
    '\n'
    'fn styles()   ={ >CSS( body { background: #0b0b0d; color: #e8e8ea; } ) }\n'
    'fn behavior() ={ >javascript( out.textContent = cryo.fib(20n).toString(); ) }\n'
    '\n'
    'fn page() ={\n'
    '  >html( <h1>Cryo</h1><p id="out">…</p> )<script = behavior, style = styles>\n'
    '}\n'
)

# Same page, but the script does not reach into Cryo.
PAGE_PLAIN = (
    'import >html<\n'
    'import >CSS<\n'
    'fn styles() ={ >CSS( body { color: red; } ) }\n'
    'fn page() ={ >html( <h1>Hi</h1> )<style = styles> }\n'
)


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def compile_it(src, work, *flags, out='out.html'):
    cf = os.path.join(work, 'prog.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    p = subprocess.run([sys.executable, CRYOC, cf, '-o',
                        os.path.join(work, out), '--no-banner', *flags],
                       capture_output=True, text=True, timeout=300,
                       errors='replace')
    return p.returncode, (p.stdout + p.stderr), os.path.join(work, out)


def test_auto(work):
    print("\n── --backend auto routes a page to the front-end backend ──")
    rc, log, out = compile_it(PAGE, work, '--backend', 'auto')
    check("a page program compiles", rc == 0, log[-300:])
    # Matched without the accent in "automático": the selection line is
    # localised and round-trips differently depending on the console codec.
    check("auto chooses 'frontend', not go",
          ': frontend' in log and ': go' not in log, log[-300:])
    check("the reason says why the others were rejected",
          'renders a page' in log, log[-300:])
    if rc == 0 and os.path.isfile(out):
        html = open(out, encoding='utf-8').read()
        check("the CSS block reached the page",
              'background: #0b0b0d' in html, html[:200])
        check("the html block reached the page",
              '<h1>Cryo</h1>' in html, html[:200])
        check("the javascript block reached the page",
              'out.textContent' in html, html[:200])
    else:
        check("the page was written", False, "no output file")

    # A program with no page blocks must be routed exactly as before.
    rc, log, _ = compile_it('print("hi");\n', work, '--backend', 'auto',
                            out='plain.out')
    check("a program with no page blocks is unaffected",
          rc == 0 and 'frontend' not in log, log[-200:])

    # A javascript block alone is a node program, not a page.
    rc, log, _ = compile_it('import >javascript<\n'
                            'fn f() ={ >javascript( console.log(1); ) }\n',
                            work, '--backend', 'auto', out='js.out')
    check("a javascript-only program still routes to node",
          rc == 0 and 'frontend' not in log, log[-200:])


def test_go_refuses(work):
    print("\n── an explicit --backend go refuses rather than emptying it ──")
    rc, log, _ = compile_it(PAGE, work, '--backend', 'go', out='p.go')
    check("go refuses the page program", rc != 0, log[-300:])
    check("the message names the block that cannot be rendered",
          '>CSS(' in log or '>html(' in log, log[-300:])
    check("it names the function that would have been left empty",
          "'styles'" in log or "'page'" in log, log[-300:])
    check("it names the backend to use instead",
          '--backend frontend' in log, log[-300:])
    check("no Go source is left behind claiming success",
          'omitted in Go backend' not in log, log[-300:])

    # A >Go( block must still work — the refusal is specific to page blocks.
    rc, log, _ = compile_it('import >Go<\n'
                            'fn f() ={ >Go( _ = 1 ) }\n'
                            'f();\n', work, '--backend', 'go', out='g.go')
    check("a >Go( block is unaffected", rc == 0, log[-250:])


def test_emit_warning(work):
    print("\n── --emit html warns when the page calls cryo.… ──")
    rc, log, _ = compile_it(PAGE, work, '--backend', 'frontend')
    check("the page still compiles", rc == 0, log[-250:])
    check("it warns that `cryo` will be undefined",
          'cryo` will be undefined' in log or 'will be undefined' in log,
          log[-300:])
    check("the warning names the function that was called",
          'cryo.fib' in log, log[-300:])
    check("it points at --emit pyro", '--emit pyro' in log, log[-300:])

    # --emit pyro ships the binary, so there is nothing to warn about.
    rc, log, _ = compile_it(PAGE, work, '--backend', 'frontend',
                            '--emit', 'pyro', out='p2.html')
    check("--emit pyro does not warn", rc == 0 and 'will be undefined' not in log,
          log[-250:])

    # A page whose script never touches `cryo` must stay quiet.
    rc, log, _ = compile_it(PAGE_PLAIN, work, '--backend', 'frontend',
                            out='p3.html')
    check("a page that does not call cryo is not warned about",
          rc == 0 and 'will be undefined' not in log, log[-250:])


def main():
    work = tempfile.mkdtemp(prefix='cryo_fe_')
    try:
        test_auto(work)
        test_go_refuses(work)
        test_emit_warning(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
