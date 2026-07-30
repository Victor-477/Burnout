#!/usr/bin/env python3
# ============================================================
#  test_match_guards.py — guarded match cases (roadmap 11.4)
#
#      match r {
#          Ok(v) if v > 100 => ...
#          Ok(v) if v > 10  => ...
#          Ok(v)            => ...
#          Err(e)           => ...
#      }
#
#  Guards are lowered in the PARSER: cases sharing a constructor
#  collapse into one case whose body is an if/else chain. No code
#  generator ever sees a guard, which is why this landed with zero
#  backend changes — and why the parity assertions below matter, as
#  they are what proves that claim rather than asserting it.
#
#  The lowering reorders cases, so the tests pin the two ways that
#  can change meaning: an unguarded case must not be able to hide a
#  guarded one, and falling off the end of a guarded group must land
#  in the wildcard.
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

ENUM = 'enum R { Ok(int), Err(string) }\n'


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def write(src, work, name='prog.cryo'):
    p = os.path.join(work, name)
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


CASES = [
    ('picks the first guard that holds',
     ENUM + 'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if v > 100 => print("huge ${v}");\n'
            '    Ok(v) if v > 10  => print("big ${v}");\n'
            '    Ok(v)            => print("small ${v}");\n'
            '    Err(e)           => print("err ${e}");\n'
            '  }\n}\n'
            'f(Ok(500)); f(Ok(50)); f(Ok(5)); f(Err("nope"));\n',
     'huge 500\nbig 50\nsmall 5\nerr nope'),

    # Every case guarded: when no guard holds, control must reach the wildcard.
    ('an all-guarded group falls through to the wildcard',
     ENUM + 'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if v < 0  => print("negative");\n'
            '    Ok(v) if v == 0 => print("zero");\n'
            '    _ => print("fell through");\n'
            '  }\n}\n'
            'f(Ok(0-3)); f(Ok(0)); f(Ok(9)); f(Err("x"));\n',
     'negative\nzero\nfell through\nfell through'),

    # With no wildcard, a group whose guards all fail simply does nothing —
    # it must not fall into a DIFFERENT constructor's case.
    ('no wildcard and no guard holds does nothing',
     ENUM + 'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if v > 99 => print("big");\n'
            '    Err(e) => print("err ${e}");\n'
            '  }\n'
            '  print("after");\n}\n'
            'f(Ok(1)); f(Ok(100)); f(Err("e"));\n',
     'after\nbig\nafter\nerr e\nafter'),

    ('guards on two different constructors stay independent',
     ENUM + 'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v)  if v > 5     => print("ok big");\n'
            '    Ok(v)               => print("ok small");\n'
            '    Err(e) if e == "x"  => print("err x");\n'
            '    Err(e)              => print("err other ${e}");\n'
            '  }\n}\n'
            'f(Ok(9)); f(Ok(1)); f(Err("x")); f(Err("y"));\n',
     'ok big\nok small\nerr x\nerr other y'),

    # A lambda is recognised by "balanced parens followed by =>", so a
    # parenthesised guard is exactly the shape that lookahead would misread.
    ('a parenthesised guard is not mistaken for a lambda',
     ENUM + 'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if (v > 1) => print("gt1 ${v}");\n'
            '    _ => print("other");\n'
            '  }\n}\n'
            'f(Ok(7)); f(Ok(0));\n',
     'gt1 7\nother'),

    ('a real lambda inside a guard still works',
     ENUM + 'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if ((int x) => x > 3)(v) => print("yes ${v}");\n'
            '    _ => print("no");\n'
            '  }\n}\n'
            'f(Ok(9)); f(Ok(1));\n',
     'yes 9\nno'),

    ('a compound guard',
     ENUM + 'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if v > 1 && v < 9 => print("mid ${v}");\n'
            '    _ => print("no");\n'
            '  }\n}\n'
            'f(Ok(5)); f(Ok(50));\n',
     'mid 5\nno'),

    ('a guard may call a function',
     ENUM + 'fn even(int n) -> bool ={ return n % 2 == 0; }\n'
            'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if even(v) => print("even ${v}");\n'
            '    Ok(v) => print("odd ${v}");\n'
            '    _ => print("err");\n'
            '  }\n}\n'
            'f(Ok(4)); f(Ok(7));\n',
     'even 4\nodd 7'),

    ('a guarded wildcard',
     ENUM + 'int limit = 3;\n'
            'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) => print("ok ${v}");\n'
            '    _ if limit > 2 => print("wild guarded");\n'
            '    _ => print("wild plain");\n'
            '  }\n}\n'
            'f(Ok(1)); f(Err("e"));\n',
     'ok 1\nwild guarded'),

    ('a guard can read module state, not only the binding',
     ENUM + 'int threshold = 10;\n'
            'fn f(R r) ={\n'
            '  match r {\n'
            '    Ok(v) if v > threshold => print("over");\n'
            '    Ok(v) => print("under");\n'
            '    _ => print("err");\n'
            '  }\n}\n'
            'f(Ok(50)); f(Ok(1));\n',
     'over\nunder'),

    # Guards must not disturb the unguarded path, including `?` propagation
    # (Phase 8.3), which is the other half of Result ergonomics.
    ('an unguarded match is unchanged, and ? still propagates',
     ENUM + 'fn parse(string s) -> R ={\n'
            '  if (s == "bad") { return Err("not a number"); }\n'
            '  return Ok(len(s));\n}\n'
            'fn twice(string s) -> R ={\n'
            '  int v = parse(s)?;\n'
            '  return Ok(v * 2);\n}\n'
            'fn show(R r) ={\n'
            '  match r {\n'
            '    Ok(v) => print("ok ${v}");\n'
            '    Err(e) => print("err ${e}");\n'
            '  }\n}\n'
            'show(twice("abcd")); show(twice("bad"));\n',
     'ok 8\nerr not a number'),
]


def test_behaviour_and_parity(work):
    print("\n── guard behaviour on the Pyro VM ──")
    for label, src, expected in CASES:
        got = run_pyro(src, work)
        check(label, got == expected, f"got {got!r}\n       want {expected!r}")

    print("\n── identical output on go and node (no backend knows about guards) ──")
    for label, src, expected in CASES:
        g, n = run_go(src, work), run_node(src, work)
        check(f"{label}: go == node == pyro",
              g == expected and n == expected,
              f"go={g!r}\n       node={n!r}\n       want={expected!r}")


def test_errors(work):
    print("\n── the ways a guarded match can be wrong ──")
    bad = [
        # Merging requires one binding per constructor. Renaming the body
        # instead would mean silently rewriting the author's identifiers.
        ('different binding names are refused',
         '    Ok(a) if a > 1 => print("x");\n    Ok(b) => print("y");',
         'must bind the same name'),
        # The lowering reorders, so an unguarded case that would have shadowed
        # a guarded one must be reported rather than quietly winning.
        ('an unguarded case above a guarded one is refused as unreachable',
         '    Ok(v) => print("y");\n    Ok(v) if v > 1 => print("x");',
         'unreachable'),
        ('an empty guard is refused',
         '    Ok(v) if => print("x");',
         'empty match guard'),
        ('a guard with no => is refused',
         '    Ok(v) if v > 1 print("x");',
         "without a following '=>'"),
    ]
    for label, arms, needle in bad:
        src = ENUM + 'fn f(R r) ={\n  match r {\n%s\n  }\n}\nf(Ok(1));\n' % arms
        rc, log = compile_to(src, work, 'pyro', 'bad.pyro')
        check(label, rc != 0 and needle in log, f"rc={rc} log={log[-200:]}")


def main():
    work = tempfile.mkdtemp(prefix='cryo_guard_')
    try:
        test_behaviour_and_parity(work)
        test_errors(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
