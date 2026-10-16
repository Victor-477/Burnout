#!/usr/bin/env python3
# ============================================================
#  test_module_resolution.py — `ns::name` everywhere, and in `cryoc test`
#
#  Two bugs found while building Cryo-Math, both of the form
#  "unknown function 'm::f'" for a call that is plainly there:
#
#    * resolve_modules rewrites `m::f` to the mangled `m__f` by
#      rebuilding the tree, and the rebuild had no case for
#      try/catch, switch, assert, match, `as`, `!` or `?`. It
#      returned those nodes untouched, so a qualified call inside
#      one was never rewritten. The same call in a helper function
#      worked, which is what made it look like a fluke.
#    * `cryoc test` skipped module resolution altogether, so any
#      test file that imported anything failed the same way.
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

HERE = os.path.dirname(os.path.abspath(__file__))
BURNOUT = os.path.dirname(HERE)
ROOT = os.path.dirname(BURNOUT)
CRYOC = os.path.join(BURNOUT, 'cryoc.py')
sys.path.insert(0, os.path.join(ROOT, 'cryo'))

from dataclasses import fields                          # noqa: E402
from lexer import Lexer                                 # noqa: E402
from parser import Parser                               # noqa: E402
from modules import resolve_modules                     # noqa: E402
from ast_nodes import Node, TryCatch, Assert, Switch    # noqa: E402

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


LIB = '''pub fn one() -> int ={ return 1; }
pub fn two() -> int ={ return 2; }
pub fn maybe(int x) -> int? ={ if (x > 0) { return x; } return null; }
pub fn text() -> string ={ return "7"; }
'''

# The exact report: a qualified call directly inside a top-level try.
REPRO = '''import "lib.cryo" as m;
try { print(m::f()); } catch (string e) { print(e); }
'''
REPRO_LIB = 'pub fn f() -> int ={ return 1; }\n'

# One qualified reference inside each node type the rewrite used to skip.
EVERY_NODE = '''import "lib.cryo" as m;

enum Shape { Dot(int), Empty }

fn propagate(int x) -> int? ={
    int v = m::maybe(x)?;
    return v;
}

try { print(m::one()); } catch (string e) { print(m::two()); } finally { print(m::two()); }

switch (m::two()) {
    case 1: print(0);
    case 2: print(m::one() + 10);
    default: print(m::two());
}

assert(m::one() == 1, "assert sees one");

Shape d = Dot(m::two());
match d {
    Dot(n) => { print(n + m::one()); }
    _ => { print(m::two()); }
}

string s = m::text() as string;
print(s);

int u = m::maybe(5)!;
print(u);
'''

TEST_FILE = '''import "lib.cryo" as m;

test fn alias_call() ={
    assert(m::one() + m::two() == 3, "m::one + m::two");
}

test fn alias_in_try() ={
    int got = 0;
    try { got = m::two(); } catch (string e) { got = -1; }
    assert(got == 2, "m::two inside try");
}
'''


def write(d, name, src):
    with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
        f.write(src)
    return os.path.join(d, name)


def cryoc(*args, cwd):
    return subprocess.run([sys.executable, CRYOC, *args], cwd=cwd,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=900)


def walk(node):
    if isinstance(node, (list, tuple)):
        for x in node:
            yield from walk(x)
    elif isinstance(node, Node):
        yield node
        for f in fields(node):
            yield from walk(getattr(node, f.name))


def leftover_qualified(program):
    """Every string field still holding `ns::name` after resolution."""
    out = []
    for n in walk(program):
        for f in fields(n):
            v = getattr(n, f.name)
            if isinstance(v, str) and '::' in v:
                out.append(f"{type(n).__name__}.{f.name}={v}")
    return out


def main():
    print("[modules] ns::name inside every statement and expression")
    work = tempfile.mkdtemp(prefix='cryo_modres_')
    try:
        write(work, 'lib.cryo', LIB)

        # ── the rewrite reaches every node type ─────────────
        print("\n── resolve_modules ──")
        prog = resolve_modules(Parser(Lexer(EVERY_NODE).tokenize()).parse(), work)
        left = leftover_qualified(prog)
        check("no `ns::name` survives resolution", not left, ', '.join(left[:6]))
        kinds = {type(n).__name__ for n in walk(prog)}
        for k in ('TryCatch', 'Switch', 'SwitchCase', 'Assert', 'MatchStatement',
                  'CastExpr', 'UnwrapExpr', 'TryExpr'):
            check(f"{k} is still in the tree", k in kinds)

        # Rebuilt nodes keep their source line (transform_node / carry_meta).
        for cls in (TryCatch, Switch, Assert):
            n = next((x for x in walk(prog) if isinstance(x, cls)), None)
            check(f"{cls.__name__} keeps its line",
                  n is not None and getattr(n, 'line', 0) > 0,
                  f"line={getattr(n, 'line', None)}")

        # ── the report, end to end ──────────────────────────
        print("\n── the original repro ──")
        rd = os.path.join(work, 'repro')
        os.makedirs(rd)
        write(rd, 'lib.cryo', REPRO_LIB)
        write(rd, 'main.cryo', REPRO)
        r = cryoc('main.cryo', '--backend', 'pyro', '--run', cwd=rd)
        out = r.stdout + r.stderr
        check("compiles without 'unknown function'",
              'unknown function' not in out, out[-400:])
        check("and prints 1", r.stdout.rstrip().endswith('1'), out[-400:])

        write(work, 'every.cryo', EVERY_NODE)
        r = cryoc('every.cryo', '--backend', 'pyro', '--run', cwd=work)
        # try 1, finally 2, switch 11, match 3, cast 7, unwrap 5
        check("every node type runs with the module's values",
              r.stdout.strip().splitlines()[-6:] == ['1', '2', '11', '3', '7', '5'],
              (r.stdout + r.stderr)[-500:])

        # ── cryoc test resolves imports ─────────────────────
        print("\n── cryoc test ──")
        td = os.path.join(work, 'suite')
        os.makedirs(td)
        write(td, 'lib.cryo', LIB)
        p = write(td, 'suite.cryo', TEST_FILE)
        # Run from elsewhere: the import must resolve against the test file.
        r = cryoc('test', p, cwd=work)
        out = r.stdout + r.stderr
        check("a test file importing a module with an alias passes",
              r.returncode == 0, out[-500:])
        check("both tests ran", '2 passed, 0 failed' in r.stdout, out[-300:])

        r = cryoc('test', p, '--list', cwd=work)
        check("--list still works on a file with imports",
              r.returncode == 0 and 'alias_call' in r.stdout, (r.stdout + r.stderr)[-300:])

        bad = write(td, 'bad.cryo', 'import "missing.cryo" as q;\n'
                    'test fn t() ={ assert(q::f() == 1, "x"); }\n')
        r = cryoc('test', bad, cwd=work)
        check("a missing module is a clean module error",
              r.returncode != 0 and 'module not found' in r.stderr, r.stderr[-300:])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
