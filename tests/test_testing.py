#!/usr/bin/env python3
# ============================================================
#  test_testing.py — the test framework itself (roadmap 12.1)
#
#  There is an obvious joke here, and also a real point: this is
#  the last suite that has to be written in Python. Everything it
#  checks is the machinery that lets the NEXT suite be written in
#  Cryo.
#
#  What matters is not that a passing test passes. It is:
#
#    * ISOLATION — one failing test must not end the run. Without
#      it the first failure hides every later one, which is the
#      single worst thing a runner can do.
#    * THE EXIT CODE — a suite that reports failures and exits 0
#      is invisible to CI, which is the only reader that never
#      looks at the output.
#    * PARITY — the runner is a front-end lowering, so the same
#      suite must behave identically on every backend. If it does
#      not, the lowering has leaked into a code generator.
#    * `test` STAYING AN IDENTIFIER — it is a contextual keyword,
#      and a framework that invalidates `int test = 0;` to
#      introduce itself has started badly.
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

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def run_test(work, src, backend='pyro', extra=()):
    p = os.path.join(work, 'suite.cryo')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(src)
    r = subprocess.run([sys.executable, CRYOC, 'test', p, '--backend', backend, *extra],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=900)
    return r


PASSING = '''fn add(int a, int b) -> int ={ return a + b; }

test fn arithmetic() ={
    assert(add(2, 3) == 5, "2+3");
}

test fn strings_concat() ={
    assert("cr" + "yo" == "cryo", "concat");
}
'''

MIXED = '''fn add(int a, int b) -> int ={ return a + b; }

test fn first_passes() ={ assert(add(1, 1) == 2, "1+1"); }
test fn middle_fails() ={ assert(add(2, 2) == 5, "deliberate failure"); }
test fn last_still_runs() ={ assert(add(3, 3) == 6, "3+3"); }
'''


def main():
    print("[12.1] a test framework in the language")
    work = tempfile.mkdtemp(prefix='cryo_12_1_')
    try:
        # ── discovery ──────────────────────────────────────
        print("\n── discovery ──")
        r = run_test(work, PASSING, extra=('--list',))
        check("--list names the tests", 'arithmetic' in r.stdout
              and 'strings_concat' in r.stdout, r.stdout[:200])
        check("--list does not run them", 'passed,' not in r.stdout, r.stdout[:200])
        check("--list reports where each one is", '.cryo:' in r.stdout, r.stdout[:200])

        # A file with no tests must NOT report success: that is how a suite
        # silently stops running and nobody notices for a month.
        r = run_test(work, 'fn f() -> int ={ return 1; }\n')
        check("a file with no tests fails rather than passing", r.returncode != 0)
        check("and says how to declare one",
              'test fn' in (r.stdout + r.stderr), (r.stdout + r.stderr)[:200])

        # ── the happy path ─────────────────────────────────
        print("\n── a passing suite ──")
        r = run_test(work, PASSING)
        check("exits 0", r.returncode == 0, r.stdout[-300:])
        check("reports each test by name",
              'ok   arithmetic' in r.stdout and 'ok   strings_concat' in r.stdout,
              r.stdout[-300:])
        check("and a summary", '2 passed, 0 failed' in r.stdout, r.stdout[-200:])

        # ── isolation: the property that matters most ──────
        print("\n── isolation ──")
        r = run_test(work, MIXED)
        out = r.stdout
        check("a failing test does not end the run",
              'last_still_runs' in out, out[-400:])
        check("the tests before it still report", 'ok   first_passes' in out, out[-400:])
        check("the failing one is named", 'FAIL middle_fails' in out, out[-400:])
        check("and its assert message is shown",
              'deliberate failure' in out, out[-400:])
        check("the summary counts both", '2 passed, 1 failed' in out, out[-200:])
        # Without this, CI sees a green build for a red suite.
        check("a suite with a failure exits NON-zero", r.returncode != 0,
              f"exit {r.returncode}")

        # ── parity across backends ─────────────────────────
        # The runner is a front-end lowering; if a backend disagrees, the
        # lowering has leaked into a code generator.
        print("\n── the same suite on every backend ──")
        outs = {}
        for b in ('pyro', 'node', 'go', 'c'):
            if b == 'node' and not shutil.which('node'):
                continue
            if b == 'go' and not shutil.which('go'):
                continue
            if b == 'c' and not any(shutil.which(x) for x in ('gcc', 'clang', 'cc')):
                continue
            r = run_test(work, PASSING, backend=b)
            outs[b] = (r.returncode, '2 passed, 0 failed' in r.stdout)
        for b, (rc, ok) in outs.items():
            check(f"{b}: passes and exits 0", rc == 0 and ok, str(outs[b]))
        check("every available backend agreed",
              len(set(outs.values())) <= 1, str(outs))

        # The failing suite must also fail everywhere, not just on pyro.
        fails = {}
        for b in list(outs):
            r = run_test(work, MIXED, backend=b)
            fails[b] = (r.returncode != 0, '2 passed, 1 failed' in r.stdout)
        for b, v in fails.items():
            check(f"{b}: the failing suite fails", v == (True, True), str(v))

        # ── `test` is contextual ───────────────────────────
        print("\n── `test` is still an identifier ──")
        r = run_test(work, 'int test = 7;\n'
                           'test fn it_runs() ={ assert(test == 7, "var"); }\n')
        check("`int test = 7;` still declares a variable", r.returncode == 0,
              (r.stdout + r.stderr)[-300:])
        check("...in the same file as a `test fn`",
              '1 passed, 0 failed' in r.stdout, r.stdout[-200:])

        # ── the runner's own names cannot collide ──────────
        print("\n── the runner does not stand on the program's names ──")
        r = run_test(work,
                     'int passed = 99;\nint failed = 1;\n'
                     'test fn t() ={ assert(passed == 99, "untouched"); }\n')
        check("a program may declare `passed` and `failed`", r.returncode == 0,
              (r.stdout + r.stderr)[-300:])

        # ── test fn parameter rejection ────────────────────
        print("\n── test fn cannot declare parameters ──")
        r = run_test(work, 'test fn with_param(int x) ={ assert(x == 1, "bad"); }\n')
        check("test fn with parameters is rejected", r.returncode != 0)
        check("and explains that test functions must not declare parameters",
              "must not declare parameters" in (r.stderr + r.stdout),
              (r.stderr + r.stdout)[:200])

        # ── multi-file and directory discovery ─────────────
        print("\n── directory test discovery ──")
        test_dir = os.path.join(work, 'pkg_tests')
        os.makedirs(test_dir, exist_ok=True)
        with open(os.path.join(test_dir, 't1.cryo'), 'w', encoding='utf-8') as f:
            f.write('test fn t1() ={ assert(1 == 1, "t1"); }\n')
        with open(os.path.join(test_dir, 't2.cryo'), 'w', encoding='utf-8') as f:
            f.write('test fn t2() ={ assert(2 == 2, "t2"); }\n')
        r_dir = subprocess.run([sys.executable, CRYOC, 'test', test_dir, '--backend', 'pyro'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
        check("directory test run succeeds", r_dir.returncode == 0, r_dir.stdout[-300:])
        check("both test files are executed",
              'ok   t1' in r_dir.stdout and 'ok   t2' in r_dir.stdout,
              r_dir.stdout[-300:])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
