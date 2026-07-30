#!/usr/bin/env python3
# ============================================================
#  test_iteration.py — the iteration protocol (roadmap 11.5)
#
#  Two things:
#
#    for (k, v in m)          a map, iterated directly
#    for (x in obj)           a user type that implements `iter`
#
#  The first is lowered in the parser, the second in the traits
#  pass (it needs the receiver's TYPE, which the parser has none
#  of). Both run on pyro, go and node here, because the map form
#  is a case where "it works" was assumed and was not true: the
#  documented `for (k, v in pairs(m))` had never compiled on the
#  go backend, since the desugaring bound the map to an `any` temp
#  and go cannot index one. That regression test is below.
#
#  The protocol rewrite is deliberately conservative — an
#  identifier with a declared type, or a struct literal. The tests
#  pin what it must NOT touch as firmly as what it must.
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

ITERABLE = (
    'trait Iterable { fn iter() -> int[]; }\n'
    'struct Countdown { int n; }\n'
    'impl Iterable for Countdown {\n'
    '    fn iter() -> int[] ={\n'
    '        int[] out = [];\n'
    '        for (int i = this.n; i > 0; i = i - 1) { out.push(i); }\n'
    '        return out;\n'
    '    }\n'
    '}\n'
)


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def write(src, work):
    p = os.path.join(work, 'prog.cryo')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(src)
    return p


def compile_to(src, work, backend, out):
    p = subprocess.run([sys.executable, CRYOC, write(src, work), '--backend',
                        backend, '-o', os.path.join(work, out), '--no-banner'],
                       capture_output=True, text=True, timeout=180,
                       errors='replace')
    return p.returncode, (p.stdout + p.stderr)


def run_pyro(src, work):
    p = subprocess.run([sys.executable, PYRO, 'run', write(src, work)],
                       capture_output=True, text=True, timeout=180,
                       errors='replace')
    return p.stdout.replace('\r\n', '\n').strip()


def run_go(src, work):
    rc, log = compile_to(src, work, 'go', 'prog.go')
    if rc != 0:
        return f"<compile failed> {log[-200:]}"
    p = subprocess.run(['go', 'run', 'prog.go'], cwd=work, capture_output=True,
                       text=True, timeout=300, errors='replace')
    return (p.stdout + p.stderr).replace('\r\n', '\n').strip()


def run_node(src, work):
    rc, log = compile_to(src, work, 'node', 'prog.js')
    if rc != 0:
        return f"<compile failed> {log[-200:]}"
    p = subprocess.run(['node', os.path.join(work, 'prog.js')],
                       capture_output=True, text=True, timeout=180,
                       errors='replace')
    return (p.stdout + p.stderr).replace('\r\n', '\n').strip()


MAP = 'map<string,int> m = {"a": 1, "b": 2};\n'

CASES = [
    # ── maps, directly ────────────────────────────────────────
    ('a map iterates as key/value',
     MAP + 'for (string k, int v in m) { print("${k}=${v}"); }\n',
     'a=1\nb=2'),

    # This is the form the docs have always shown. It ran on pyro and node and
    # did NOT compile on go, because the desugaring bound the map to an `any`
    # temp. Binding the identifier directly keeps its real type.
    ('pairs(m) still works, and now compiles on go too',
     MAP + 'for (string k, int v in pairs(m)) { print("${k}=${v}"); }\n',
     'a=1\nb=2'),

    ('the loop variables are usable as their declared types',
     MAP + 'int total = 0;\n'
           'for (string k, int v in m) { total = total + v + len(k); }\n'
           'print(total);\n',
     '5'),

    ('an empty map iterates zero times',
     'map<string,int> e = {};\n'
     'print("before");\n'
     'for (string k, int v in e) { print("never"); }\n'
     'print("after");\n',
     'before\nafter'),

    ('a map with int keys',
     'map<int,string> m2 = {1: "one", 2: "two"};\n'
     'for (int k, string v in m2) { print("${k}->${v}"); }\n',
     '1->one\n2->two'),

    ('break and continue work inside the loop',
     'map<string,int> m3 = {"a": 1, "b": 2, "c": 3};\n'
     'for (string k, int v in m3) {\n'
     '    if (v == 2) { continue; }\n'
     '    if (v == 3) { break; }\n'
     '    print("${k}=${v}");\n'
     '}\n',
     'a=1'),

    ('single-variable iteration over an array is unchanged',
     'int[] a = [1, 2, 3];\n'
     'for (int x in a) { print(x); }\n',
     '1\n2\n3'),

    ('enumerate stays the array form',
     'string[] xs = ["p", "q"];\n'
     'for (int i, string s in enumerate(xs)) { print("${i}:${s}"); }\n',
     '0:p\n1:q'),

    # ── the user-type protocol ────────────────────────────────
    ('a user type with iter() can be iterated directly',
     ITERABLE + 'Countdown c = Countdown { n: 3 };\n'
                'for (int x in c) { print("tick ${x}"); }\n',
     'tick 3\ntick 2\ntick 1'),

    ('the receiver may be a function parameter',
     ITERABLE + 'fn total(Countdown cd) -> int ={\n'
                '    int sum = 0;\n'
                '    for (int x in cd) { sum = sum + x; }\n'
                '    return sum;\n}\n'
                'Countdown c = Countdown { n: 4 };\n'
                'print(total(c));\n',
     '10'),

    ('a struct literal used directly as the iterable',
     ITERABLE + 'for (int x in Countdown { n: 2 }) { print(x); }\n',
     '2\n1'),

    ('calling .iter() explicitly still works',
     ITERABLE + 'Countdown c = Countdown { n: 2 };\n'
                'for (int x in c.iter()) { print(x); }\n',
     '2\n1'),

    # What the rewrite must NOT touch: an ordinary array in the same program
    # as an iterable type. A misfire here would call iter() on a []int.
    ('a plain array is left alone when an iterable type exists',
     ITERABLE + 'int[] plain = [7, 8];\n'
                'for (int y in plain) { print("plain ${y}"); }\n',
     'plain 7\nplain 8'),

    ('a local shadowing name of another type is not rewritten',
     ITERABLE + 'fn f() ={\n'
                '    int[] c = [5];\n'
                '    for (int x in c) { print("arr ${x}"); }\n'
                '}\n'
                'f();\n',
     'arr 5'),

    ('a type without iter() is not affected',
     'struct Point { int x; }\n'
     'int[] a = [1];\n'
     'Point p = Point { x: 9 };\n'
     'for (int v in a) { print(v + p.x); }\n',
     '10'),
]


def test_all(work):
    print("\n── behaviour on the Pyro VM ──")
    for label, src, expected in CASES:
        got = run_pyro(src, work)
        check(label, got == expected, f"got {got!r}\n       want {expected!r}")

    print("\n── identical on go and node ──")
    for label, src, expected in CASES:
        g, n = run_go(src, work), run_node(src, work)
        check(f"{label}: go == node == pyro",
              g == expected and n == expected,
              f"go={g!r}\n       node={n!r}\n       want={expected!r}")


def main():
    work = tempfile.mkdtemp(prefix='cryo_iter_')
    try:
        test_all(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
