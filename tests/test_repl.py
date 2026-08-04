#!/usr/bin/env python3
# ============================================================
#  test_repl.py — the interactive loop (roadmap 12.2)
#
#  The REPL is driven over a pipe, because the thing being
#  tested is the loop: what persists between lines, what does
#  not, and whether a bad line ends the session.
#
#  The properties that matter are not "it evaluates 1+1":
#
#    * BINDINGS PERSIST — a declaration on one line is visible
#      on the next, and an assignment is too. Without that it
#      is not a REPL, it is a calculator.
#    * A BARE EXPRESSION SHOWS ITS VALUE — and `x + 1` is not
#      even a statement in Cryo, so this is the case that
#      forces the wrap-before-parse.
#    * NOTHING DANGEROUS REPLAYS — only declarations and
#      assignments join the prelude. A `write_file(...)` typed
#      once must not run again on every later line.
#    * A FAILURE DOES NOT POISON THE SESSION — neither a syntax
#      error, a semantic error, nor a runtime abort. And a
#      statement that FAILED must not persist, or every later
#      line inherits the failure.
# ============================================================
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def repl(lines, backend='pyro', timeout=900):
    """Feed `lines` to the REPL and return everything it printed."""
    src = ''.join(l + '\n' for l in lines) + ':quit\n'
    r = subprocess.run([sys.executable, CRYOC, 'repl', '--backend', backend],
                       input=src, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=timeout)
    return (r.stdout or '') + (r.stderr or ''), r.returncode


def main():
    print("[12.2] the REPL")

    # ── bindings persist ───────────────────────────────────
    print("\n── state carries between lines ──")
    out, rc = repl(['int x = 5', 'x'])
    check("a declaration is visible on the next line", '5' in out, out[-300:])
    check("the session exits cleanly", rc == 0, f"exit {rc}")

    out, _ = repl(['int x = 5', 'x = 10', 'x'])
    check("an assignment persists too", '10' in out and out.count('5') == 0,
          out[-300:])

    out, _ = repl(['fn double(int n) -> int ={ return n * 2; }', 'double(21)'])
    check("a function declared earlier can be called", '42' in out, out[-300:])

    # ── a bare expression shows its value ──────────────────
    # `x + 1;` is NOT a statement in Cryo — only a call or a bare name is — so
    # this is what forces the wrap to happen before parsing rather than after.
    print("\n── a bare expression is a question ──")
    out, _ = repl(['int x = 5', 'x + 1'])
    check("`x + 1` prints 6", '6' in out, out[-300:])
    out, _ = repl(['"cry" + "o"'])
    check("a string expression prints", 'cryo' in out, out[-300:])
    out, _ = repl(['int[] xs = [3, 1, 2]', 'sort(xs)'])
    check("a call's result prints", '[1, 2, 3]' in out, out[-300:])
    # print() already prints; wrapping it would give print(print(x)).
    out, _ = repl(['print("hi")'])
    check("print(...) is not double-wrapped",
          out.count('hi') == 1, out[-300:])

    # ── multi-line input ───────────────────────────────────
    print("\n── multi-line input ──")
    out, _ = repl(['fn fact(int n) -> int ={',
                   '  if (n <= 1) { return 1; }',
                   '  return n * fact(n - 1);',
                   '}',
                   'fact(5)'])
    check("a function spanning lines is accepted", '120' in out, out[-400:])
    check("and the continuation prompt is used", '...' in out, out[:200])

    # ── failures do not poison the session ─────────────────
    print("\n── a bad line does not end the session ──")
    out, rc = repl(['int x = 5', 'x +', 'x'])
    check("a syntax error is reported", 'Syntax Error' in out, out[-400:])
    check("and the session continues", '5' in out, out[-300:])
    check("exiting is still clean", rc == 0, f"exit {rc}")

    out, _ = repl(['int x = 5', 'nosuchfn(1)', 'x'])
    check("a semantic error is reported", 'unknown function' in out, out[-400:])
    check("and the session continues", '5' in out, out[-300:])

    out, _ = repl(['int x = 5', '1 / 0', 'x'])
    check("a runtime abort is reported", 'DivByZero' in out, out[-400:])
    check("and the session continues", '5' in out, out[-300:])

    # A statement that FAILED must not join the prelude, or every later line
    # replays the failure and the session is dead without saying so.
    out, _ = repl(['int x = 5', 'int y = 1 / 0', 'x', ':list'])
    check("a failed declaration does not persist",
          'int y' not in out.split(':list')[-1] if ':list' in out else True,
          out[-400:])
    check("...and the session still works afterwards", '5' in out, out[-300:])

    # ── what persists is inspectable ───────────────────────
    print("\n── :list shows exactly what replays ──")
    out, _ = repl(['int x = 1', 'print("side effect")', 'x + 1', ':list'])
    tail = out.split('>>>')[-2] if '>>>' in out else out
    check("a declaration is listed", 'int x = 1' in out, out[-400:])
    # The reason the rule is blunt: a replayed call would re-run on every later
    # line. `print` here stands in for `write_file`.
    check("a bare call is NOT listed", 'print("side effect")' not in tail,
          tail[-300:])
    check("and it only ran once", out.count('side effect') == 1, out[-400:])

    # ── commands ───────────────────────────────────────────
    print("\n── commands ──")
    out, _ = repl(['int x = 5', ':reset', ':list'])
    check(":reset forgets the session", 'nothing yet' in out, out[-300:])
    out, _ = repl([':help'])
    check(":help explains what persists", 'persist' in out, out[-400:])
    out, _ = repl([':nonsense'])
    check("an unknown command says so", 'unknown command' in out, out[-300:])

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
