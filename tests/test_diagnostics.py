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
import re
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


# ── parser error recovery (13.6) ─────────────────────────────
#
# 11.24 made every SEMANTIC pass report all of its problems at once; the parser
# still stopped at the first, so fixing five typos took five compiles. It now
# recovers at statement boundaries and reports them together.
#
# Recovery is only worth having if the extra errors are REAL. A parser that
# resumes in the wrong place invents a cascade from one genuine mistake, and a
# list of ten problems where nine are noise is worse than one true problem —
# you stop reading them. That failure mode is why parsers so often report only
# the first, so it gets as many assertions here as the counting does, each one
# a shape that actually produced a phantom during implementation.

def _problem_count(log):
    """How many problems the compiler says it found.

    The batched header only appears for two or more; a single syntax error
    keeps the exact one-error shape it has always had, which `test_syntax`
    pins separately.
    """
    # 'at least N' when the cap fired, plain 'N' otherwise. Both report the
    # number of PROBLEMS, which never includes the cap notice itself.
    m = re.search(r'found (?:at least )?(\d+) problems', log)
    if m:
        return int(m.group(1))
    return 1 if '[Syntax Error]' in log else 0


def test_recovery_reports_all(work):
    print("\n── syntax errors: all of them, not the first ──")

    # Three independent bad statements at the top level.
    rc, log = compile_bad(work, (
        'int a = 1 +;\n'
        'int b = 2;\n'
        'int c = * 3;\n'
        'int d = 4;\n'
        'int e = ;\n'
        'print(d);\n'), name='three_top.cryo')
    check("the program is rejected", rc != 0)
    check("three top-level syntax errors report three",
          _problem_count(log) == 3, log[:400])
    check("each is rendered against its own line",
          log.count('-->') == 3, log[-500:])
    for ln in ('three_top.cryo:1', 'three_top.cryo:3', 'three_top.cryo:5'):
        check(f"and points at {ln.split(':')[1]}", ln in log, log[-500:])
    check("the good lines in between are not reported",
          'three_top.cryo:2' not in log and 'three_top.cryo:4' not in log,
          log[-500:])

    # The same three, inside a function body. This is the case that needs
    # recovery INSIDE a block: before it, the first bad statement unwound the
    # whole function and the parser resumed at the top level mid-body.
    rc, log = compile_bad(work, (
        'fn f(int n) -> int ={\n'
        '    int a = n +;\n'
        '    int b = * 2;\n'
        '    int c = ;\n'
        '    return n;\n'
        '}\n'
        'print(f(1));\n'), name='three_body.cryo')
    check("three errors in one function body report three",
          _problem_count(log) == 3, log[:400])
    check("each one in the body is rendered too",
          log.count('-->') == 3, log[-500:])


def test_recovery_no_cascade(work):
    print("\n── syntax errors: no phantom cascade ──")

    # Each of these has EXACTLY ONE real mistake. Everything after it is valid
    # Cryo, so anything beyond one report is invented. The trailing construct
    # in each is the one that broke: recovery has to land somewhere that lets
    # the ENCLOSING construct finish, or its continuation keyword arrives with
    # nothing open and reads as a fresh error.
    singles = [
        ('one_else.cryo',
         'fn f(int n) -> int ={\n'
         '    if (n >) {\n'
         '        return 1;\n'
         '    } else {\n'
         '        return 2;\n'
         '    }\n'
         '}\n',
         "a valid `else` after a bad `if` condition"),

        ('one_match.cryo',
         'enum R { Ok(int), Err(string) }\n'
         'fn f(R r) -> int ={\n'
         '    match r {\n'
         '        Ok(v) => { int q = +; return v; }\n'
         '        Err(e) => { return 0; }\n'
         '    }\n'
         '    return 1;\n'
         '}\n',
         "a valid second match arm after a bad first one"),

        ('one_catch.cryo',
         'fn f(int n) -> int ={\n'
         '    try {\n'
         '        int a = +;\n'
         '    } catch (string e) {\n'
         '        print(e);\n'
         '    }\n'
         '    return 0;\n'
         '}\n',
         "a valid `catch` after a bad `try` body"),

        ('one_case.cryo',
         'fn f(int n) -> int ={\n'
         '    switch (n) {\n'
         '        case 1:\n'
         '            int a = +;\n'
         '            return 1;\n'
         '        case 2:\n'
         '            return 2;\n'
         '        default:\n'
         '            return 0;\n'
         '    }\n'
         '}\n',
         "a valid second `case` after a bad first one"),

        ('one_deep.cryo',
         'fn f(int n) -> int ={\n'
         '    while (n > 0) {\n'
         '        if (n == 1) {\n'
         '            int x = * 2;\n'
         '        } else {\n'
         '            n = n - 1;\n'
         '        }\n'
         '    }\n'
         '    return n;\n'
         '}\n',
         "an error two blocks deep, with valid code closing both"),

        ('one_then_decl.cryo',
         'fn f() -> int ={\n'
         '    int a = +;\n'
         '    return 1;\n'
         '}\n'
         'struct P { int x; int y; }\n'
         'fn g(P p) -> int ={ return p.x; }\n',
         "valid declarations following a bad function body"),

        ('one_then_loop.cryo',
         'fn f(int n) -> int ={\n'
         '    int a = +;\n'
         '    while (n > 0) {\n'
         '        n = n - 1;\n'
         '        if (n == 2) { break; }\n'
         '        continue;\n'
         '    }\n'
         '    return n;\n'
         '}\n',
         "`break`/`continue`, which are legal only inside a loop"),
    ]
    for name, src, why in singles:
        rc, log = compile_bad(work, src, name=name)
        check(f"still rejected — {why}", rc != 0, log[-200:])
        n = _problem_count(log)
        check(f"one real error stays one, not a cascade — {why}",
              n == 1, f"reported {n}\n{log[-600:]}")

    # The counting above would also pass if recovery gave up and reported the
    # first error only. This is the control: the same file with a SECOND real
    # mistake after the `else` must report two, so the single-error results
    # above mean "no phantoms", not "no recovery".
    rc, log = compile_bad(work, (
        'fn f(int n) -> int ={\n'
        '    if (n >) {\n'
        '        return 1;\n'
        '    } else {\n'
        '        return 2;\n'
        '    }\n'
        '}\n'
        'int z = * 2;\n'), name='else_plus_one.cryo')
    check("recovery is still live — a real second error is found",
          _problem_count(log) == 2, log[-600:])
    check("and it is the one after the else, not the else",
          'else_plus_one.cryo:8' in log and 'ELSE' not in log, log[-600:])


def test_recovery_cap(work):
    print("\n── syntax errors: the cap ──")
    # Past a certain point the parse has lost the thread and the honest advice
    # is to fix these and recompile. The cap must LATCH: the block that gives
    # up still unwinds past its own `}`, and every frame on the way out used to
    # record again — a 12-error file reported 13, with the "stopping here" line
    # buried in the middle instead of ending the list.
    src = ('fn f() -> int ={\n'
           + ''.join(f'    int v{i} = +;\n' for i in range(12))
           + '    return 1;\n}\n')
    rc, log = compile_bad(work, src, name='many.cryo')
    check("the program is rejected", rc != 0)
    # The count is the number of PROBLEMS, and the cap notice is not one of
    # them: it is a sentence about the report. Counting it made a 12-mistake
    # file announce 11 while listing 10 mistakes and a note — a number that
    # matched nothing the reader could see. `_MAX_ERRORS` is 10, so 10 it is.
    check("the report is capped, not one per mistake",
          _problem_count(log) == 10, f"reported {_problem_count(log)}")
    check("and the count matches what is actually rendered",
          log.count('-->') == 10, f"{log.count('-->')} rendered")
    # "at least", because the parser stopped looking. It cannot claim there are
    # exactly 10 when it gave up at 10, and it must not claim there are more
    # when a file with exactly 10 mistakes hits the same path.
    check("the count is hedged, since the parser stopped looking",
          'found at least 10 problems' in log, log[:200])
    check("the cap is announced once",
          log.count('stopping here') == 1, log[-400:])
    check("and it is the last thing said",
          log.rstrip().endswith('compile again to see whether more remain'),
          log[-300:])


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
        test_recovery_reports_all(work)
        test_recovery_no_cascade(work)
        test_recovery_cap(work)
        test_still_compiles(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
