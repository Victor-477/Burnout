#!/usr/bin/env python3
# ============================================================
#  test_optimizer.py — the AST optimizer (roadmap 11.21)
#
#  Every case is checked TWICE: the program must produce the same
#  output with the optimizer on and off, and the optimized code
#  must actually differ. Only the pair means anything — an
#  optimizer that changes nothing passes the first check, and one
#  that breaks the program passes the second.
#
#  The cases that shaped the design are the ones it must NOT do,
#  and both were found by the existing suite rather than by
#  inspection:
#
#    fn mk() -> int[] ={ return [1,2,3]; }   inlining this loses the
#                                            element type; go then
#                                            infers []any and the
#                                            program stops compiling
#    int? y = 5; print(y == null);           propagating the 5 turns
#                                            a valid question into
#                                            5 == null
#
#  Both come from one fact: the AST carries no types, so a value may
#  only be moved out of a declaration when it means the same thing
#  without it. That is why the SEMANTIC list below is longer than
#  the list of things the optimizer actually does.
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
PYRO = os.path.join(ROOT, 'Burnout', 'pyro.py')

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def gen_go(src, work, opt=True, stem='p'):
    """Compile to Go; returns (rc, generated source, log, exe path)."""
    cf = os.path.join(work, stem + '.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    out = os.path.join(work, stem + '.go')
    args = [sys.executable, CRYOC, cf, '--backend', 'go', '-o', out,
            '--no-banner']
    if not opt:
        args.append('--no-opt')
    p = subprocess.run(args, capture_output=True, text=True, timeout=300,
                       errors='replace')
    text = ''
    if os.path.isfile(out):
        with open(out, encoding='utf-8', errors='replace') as f:
            text = f.read()
    exe = os.path.join(work, stem + ('.exe' if sys.platform == 'win32' else ''))
    return p.returncode, text, (p.stdout + p.stderr), exe


def run_pyro(src, work):
    cf = os.path.join(work, 'r.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    p = subprocess.run([sys.executable, PYRO, 'run', cf], capture_output=True,
                       text=True, timeout=300, errors='replace')
    return p.stdout.replace('\r\n', '\n').strip()


def run_go(src, work, opt=True):
    rc, _, log, exe = gen_go(src, work, opt, stem='g')
    if rc != 0:
        return f"<compile failed> {log[-200:]}"
    if not os.path.isfile(exe):
        return '<no binary>'
    r = subprocess.run([exe], capture_output=True, text=True, timeout=180,
                       errors='replace')
    return (r.stdout + r.stderr).replace('\r\n', '\n').strip()


# (label, source, expected output, what must vanish from the generated Go).
# The markers are chosen so they cannot appear as a substring of the answer —
# "60" would match inside "600".
CASES = [
    ('constant folding',
     'print(2 + 3 * 4);\n', '14', 'cryoMulOvf'),

    ('constant propagation across statements',
     'int base = 10;\nint total = base * 60;\nprint(total);\n', '600',
     'cryoMulOvf'),

    ('copy propagation',
     'int a = 5;\nint b = a;\nprint(b);\n', '5', 'var b '),

    ('dead local elimination',
     'int used = 1;\nint unused = 99;\nprint(used);\n', '1', 'unused'),

    ('inlining a small leaf function',
     'fn double(int n) -> int ={ return n * 2; }\nprint(double(21));\n',
     '42', 'double(21)'),

    ('folding reaches into a call argument',
     'print(to_string(6 * 7));\n', '42', 'cryoMulOvf'),

    ('string concatenation folds',
     'print("a" + "b" + "c");\n', 'abc', '"b"'),

    ('comparisons fold',
     'print(3 < 4);\n', 'true', '(3 < 4)'),

    ('a chain settles to one constant',
     'int a = 2;\nint b = a + 3;\nint c = b * 10;\nprint(c);\n', '50',
     'cryoMulOvf'),
]

# Programs whose meaning must survive exactly. No claim about size — these are
# here because getting any of them wrong fails silently.
SEMANTIC = [
    ('division by zero still traps, it is not folded away',
     'int z = 0;\nprint(10 / z);\n'),
    ('a literal division by zero is left to the runtime',
     'print(10 / 0);\n'),
    ('an optional keeps its type when it holds a literal',
     'int? y = 5;\nprint(y == null);\nint? n = null;\nprint(n == null);\n'),
    ('a function returning an array is not inlined into a loop',
     'fn mk() -> int[] ={ return [1, 2, 3]; }\n'
     'int t = 0;\nfor (int v in mk()) { t += v; }\nprint(t);\n'),
    ('a reassigned variable is not treated as constant',
     'int x = 1;\nx = 2;\nprint(x);\n'),
    ('a loop counter is not folded',
     'int s = 0;\nfor (int i = 0; i < 4; i++) { s += i; }\nprint(s);\n'),
    ('a call keeps happening even if its result is unused',
     'fn shout() -> int ={ print("side effect"); return 1; }\n'
     'int ignored = shout();\nprint("after");\n'),
    ('module state that is assigned is not propagated',
     'int counter = 0;\nfn bump() ={ counter = counter + 1; }\n'
     'bump();\nbump();\nprint(counter);\n'),
    ('the largest int survives untouched',
     'int big = 9223372036854775807;\nprint(big);\n'),
    ('a lambda parameter shadows an outer name',
     'int n = 10;\nfn apply(fn(int) -> int f, int v) -> int ={ return f(v); }\n'
     'print(apply((int n) => n * 2, 4));\nprint(n);\n'),
    ('a conditional keeps both branches',
     'int a = 1;\nif (a > 0) { print("yes"); } else { print("no"); }\n'),
    ('a string built at runtime is unaffected',
     'string who = "world";\nprint("hello " + who);\n'),
]


def test_effect(work):
    print("\n── the optimizer changes the code, and not the result ──")
    for label, src, expected, gone in CASES:
        on, off = run_pyro(src, work), None
        check(f"{label}: the answer is right", on == expected,
              f"got {on!r}, want {expected!r}")
        rc_on, code_on, log_on, _ = gen_go(src, work, opt=True)
        rc_off, code_off, _, _ = gen_go(src, work, opt=False)
        if rc_on != 0 or rc_off != 0:
            check(f"{label}: compiles both ways", False, log_on[-200:])
            continue
        check(f"{label}: '{gone}' is gone once optimised",
              gone in code_off and gone not in code_on,
              f"unoptimised has it: {gone in code_off}; "
              f"optimised has it: {gone in code_on}")


def _trace_free(out: str) -> str:
    """Drop Go's goroutine dump. A trap must still be a trap with the same
    message, but optimised code is shorter, so the line numbers in the trace
    legitimately differ — comparing those would be testing the file layout."""
    lines = []
    for ln in out.split('\n'):
        if ln.startswith('goroutine '):
            break
        lines.append(ln)
    return '\n'.join(lines).strip()


def test_semantics(work):
    print("\n── meaning is preserved, failures included ──")
    for label, src in SEMANTIC:
        # The optimizer runs for pyro too, so compare the two Go binaries:
        # one built with it, one with --no-opt.
        on = _trace_free(run_go(src, work, opt=True))
        off = _trace_free(run_go(src, work, opt=False))
        check(label, on == off and not on.startswith('<'),
              f"optimised={on!r}\n       plain={off!r}")


def test_cross_backend(work):
    """The pass runs before code generation, so every backend sees the same
    tree — which is only worth anything if they still agree."""
    print("\n── the backends still agree ──")
    src = ('int base = 10;\nint total = base * 60;\n'
           'fn double(int n) -> int ={ return n * 2; }\n'
           'print(total);\nprint(double(21));\nprint(2 + 3 * 4);\n')
    py = run_pyro(src, work)
    go = run_go(src, work)
    check("pyro and go agree, and are right", py == go == '600\n42\n14',
          f"pyro={py!r} go={go!r}")


def test_flag(work):
    print("\n── --no-opt turns it off ──")
    src = 'int base = 10;\nint total = base * 60;\nprint(total);\n'
    _, on, _, _ = gen_go(src, work, opt=True)
    _, off, _, _ = gen_go(src, work, opt=False)
    check("optimised code holds the folded constant", '600' in on)
    check("unoptimised code still multiplies", 'cryoMulOvf' in off)
    check("--no-opt really produces different code", on != off)


def main():
    work = tempfile.mkdtemp(prefix='cryo_opt_')
    try:
        test_effect(work)
        test_semantics(work)
        test_cross_backend(work)
        test_flag(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
