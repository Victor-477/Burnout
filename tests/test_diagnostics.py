#!/usr/bin/env python3
# ============================================================
#  test_diagnostics.py — errors that show the mistake (11.24)
#
#  Before, an error was a coordinate and a sentence:
#
#      - Line 6: [Semantic Error] unknown function 'lenght'
#
#  enough to find the line, not enough to see the problem. Now the
#  line is printed with a caret under the offending name and a
#  suggestion when there is a close one.
#
#  Two properties are worth more than the formatting, and both are
#  tested here:
#
#    * ALL the errors in a pass are reported, not the first. One
#      recompile per mistake is the slowest way to fix a file.
#    * A suggestion is only offered when it is close. A wrong one
#      sends the reader hunting in the wrong place, which is worse
#      than none — so `suggest` is checked for what it declines to
#      say as much as for what it says.
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
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))

import diagnostics as dx     # noqa: E402

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def compile_bad(work, src, name='bad.cryo'):
    p = os.path.join(work, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(src)
    r = subprocess.run([sys.executable, CRYOC, p, '--backend', 'pyro',
                        '-o', os.path.join(work, 'o.pyro'), '--no-banner'],
                       capture_output=True, text=True, cwd=work, timeout=300,
                       errors='replace')
    return r.returncode, (r.stdout + r.stderr)


# ── the suggestion engine ────────────────────────────────────

def test_suggest():
    print("\n── did-you-mean ──")
    names = ['total', 'compute', 'length', 'items', 'print']

    check("a dropped character", dx.suggest('totl', names)[:1] == ['total'],
          str(dx.suggest('totl', names)))
    # The reason the old three-character prefix match was replaced: `len` and
    # `lenght` share a prefix, `length` and `lenght` are a transposition.
    check("a transposition", dx.suggest('lenght', names)[:1] == ['length'],
          str(dx.suggest('lenght', names)))
    check("a doubled character",
          dx.suggest('computee', names)[:1] == ['compute'],
          str(dx.suggest('computee', names)))
    check("wrong case", 'items' in dx.suggest('Items', names),
          str(dx.suggest('Items', names)))

    # What it must NOT say. A confident wrong suggestion is worse than none.
    check("nothing is offered for an unrelated name",
          dx.suggest('zzzzzzzz', names) == [], str(dx.suggest('zzzzzzzz', names)))
    check("the name itself is never suggested",
          'total' not in dx.suggest('total', names), str(dx.suggest('total', names)))
    check("an empty pool yields nothing", dx.suggest('x', []) == [])
    check("hint() is empty when there is no near name",
          dx.hint('zzzzzzzz', names) == '')


def test_render():
    print("\n── the rendered block ──")
    src = 'fn f() ={\n    return nope;\n}\n'
    out = dx.render(src, 2, "unknown name 'nope'", 'a.cryo', 'nope',
                    'did you mean `note`?')
    check("it names the file, line and column", 'a.cryo:2:12' in out, out)
    check("it prints the offending line", 'return nope;' in out, out)
    check("the caret is as wide as the name", '^^^^' in out, out)
    # The caret column must equal the name's column in the line above it —
    # both lines carry the same gutter, so the indices are comparable.
    lines = out.split('\n')
    check("the caret sits under the name",
          lines[4].index('^') == lines[3].index('nope'),
          f"caret at {lines[4].index('^')}, name at {lines[3].index('nope')}")
    check("the note is shown", 'did you mean `note`?' in out, out)

    # A name that also appears inside a longer word must not be underlined
    # there: reporting `n` should not point at the `n` in `int`.
    out2 = dx.render('int n = 1;\n', 1, "x", 'a.cryo', 'n')
    caret = [l for l in out2.split('\n') if '^' in l][0]
    check("a short name is not matched inside a longer word",
          caret.index('^') == out2.split('\n')[3].index('n = 1'), out2)

    check("no source still yields the message",
          dx.render(None, 3, 'boom') == 'boom')
    check("a line past the end still yields the message",
          'boom' in dx.render('one\n', 99, 'boom'))


# ── end to end ───────────────────────────────────────────────

def test_semantic(work):
    print("\n── semantic errors ──")
    rc, log = compile_bad(work, (
        'fn compute(int n) -> int ={\n'
        '    return n * 2;\n'
        '}\n'
        '\n'
        'int[] items = [1, 2, 3];\n'
        'int total = lenght(items);\n'
        'print(totl);\n'
        'computee(3);\n'))
    check("the program is rejected", rc != 0)
    check("all three problems are reported at once",
          'found 3 problems' in log, log[:200])
    for name, want in (('lenght', 'len'), ('totl', 'total'),
                       ('computee', 'compute')):
        check(f"'{name}' is reported with a suggestion of `{want}`",
              f"'{name}'" in log and f'`{want}`' in log, log[-400:])
    check("each one shows its source line",
          log.count('-->') == 3, log[-400:])
    check("the file name is used, not the directory",
          'bad.cryo:6' in log, log[-400:])


def test_syntax(work):
    print("\n── syntax errors ──")
    rc, log = compile_bad(work, 'fn f(int n) -> int ={\n    return n *;\n}\n',
                          name='syn.cryo')
    check("the program is rejected", rc != 0)
    check("the offending line is shown", 'return n *;' in log, log[-300:])
    check("a caret points at the token", '^' in log, log[-300:])
    check("the position names the file", 'syn.cryo:2' in log, log[-300:])
    # The handler printed the tag once, and the raise site had already
    # included it: "[Syntax Error] [Syntax Error] Line 2: ..."
    check("the error tag is not doubled",
          log.count('[Syntax Error]') == 1, log[-300:])


def test_still_compiles(work):
    print("\n── a correct program is unaffected ──")
    p = os.path.join(work, 'good.cryo')
    with open(p, 'w', encoding='utf-8') as f:
        f.write('fn add(int a, int b) -> int ={ return a + b; }\n'
                'print(add(2, 3));\n')
    r = subprocess.run([sys.executable, CRYOC, p, '--backend', 'pyro',
                        '-o', os.path.join(work, 'g.pyro'), '--no-banner'],
                       capture_output=True, text=True, cwd=work, timeout=300,
                       errors='replace')
    check("it compiles", r.returncode == 0, (r.stdout + r.stderr)[-200:])
    check("and says nothing about diagnostics",
          '-->' not in (r.stdout + r.stderr))


def main():
    work = tempfile.mkdtemp(prefix='cryo_diag_')
    try:
        test_suggest()
        test_render()
        test_semantic(work)
        test_syntax(work)
        test_still_compiles(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
