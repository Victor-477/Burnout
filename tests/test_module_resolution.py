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

// Node types with no case of their own reach the generic rebuild.
safe { print(m::two() * 10); }
fn(int)->int twice = (int x) => x * m::two();
print(twice(4));

// `?` on a T? propagates null (it used to index "tag" and abort the VM).
print(propagate(9)!);
int? none = propagate(-1);
print(none == null);
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


# L-17 — a qualified type INSIDE a composite type: `k::T[]`, `k::T?`,
# `map<string,k::T>`, `fn(k::T)->int`, a struct field, a for-each variable, a
# lambda parameter. Only a type that was exactly `k::T` used to be mangled, so
# Go got the raw `::` text (`syntax error: unexpected :`). The module side had
# the same gap the other way round: its own `T[]` stayed `T[]` after `T` became
# `k__T`. Everything runs inside functions — the Go backend initialises
# top-level variables before the top-level statements run.
COMPOSITE_LIB = '''pub struct T { int v; }
struct Hidden { int v; }
pub struct Bag { T one; T[] many; map<string,T> byname; }
pub fn mk(int v) -> T ={ return T { v: v }; }
pub fn sum(T[] xs) -> int ={ int s = 0; for (T t in xs) { s = s + t.v; } return s; }
pub fn pick(T[] xs) -> T? ={ if (len(xs) > 0) { return xs[0]; } return null; }
pub fn each(T[] xs, fn(T)->int f) -> int ={
    int s = 0;
    for (T t in xs) { s = s + f(t); }
    return s;
}
pub fn bag() -> Bag ={
    T[] xs = [mk(2), mk(3)];
    map<string,T> m = {};
    m["a"] = mk(4);
    return Bag { one: mk(1), many: xs, byname: m };
}
'''

COMPOSITE = '''import "lib.cryo" as k;

struct Holder { k::T one; k::T[] list; }

fn apply(fn(k::T)->int f, k::T t) -> int ={ return f(t); }
fn first(k::T[] xs) -> k::T? ={ if (len(xs) > 0) { return xs[0]; } return null; }

fn run() -> int ={
    k::T[] a = [k::mk(1), k::mk(2)];
    map<string,k::T> m = {};
    m["x"] = k::mk(5);
    fn(k::T)->int g = (k::T t) => t.v * 10;
    print(apply(g, m["x"]));
    for (k::T t in a) { print(t.v); }
    Holder h = Holder { one: k::mk(7), list: a };
    print(h.one.v);
    print(k::sum(h.list));
    k::T? f = first(a);
    print(f!.v);
    k::Bag b = k::bag();
    print(b.one.v + k::sum(b.many) + b.byname["a"].v);
    print(k::each(a, (k::T t) => t.v + 100));
    k::T? p = k::pick(a);
    print(p!.v);
    return 0;
}
run();
'''
COMPOSITE_WANT = ['50', '1', '2', '7', '3', '1', '10', '203', '1']


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
                  'CastExpr', 'UnwrapExpr', 'TryExpr', 'SafetyBlock', 'Lambda'):
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
        # try 1, finally 2, switch 11, match 3, cast 7, unwrap 5, safe 20,
        # lambda 8, `?` on a value 9, `?` on null
        want = ['1', '2', '11', '3', '7', '5', '20', '8', '9', 'true']
        check("every node type runs with the module's values",
              r.stdout.strip().splitlines()[-len(want):] == want,
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

        # ── L-17: qualified names inside composite types ────
        print("\n── ns::T inside composite types (L-17) ──")
        cd = os.path.join(work, 'composite')
        os.makedirs(cd)
        write(cd, 'lib.cryo', COMPOSITE_LIB)
        write(cd, 'main.cryo', COMPOSITE)
        prog = resolve_modules(Parser(Lexer(COMPOSITE).tokenize()).parse(), cd)
        left = leftover_qualified(prog)
        check("no `ns::` survives in any type string", not left, ', '.join(left[:6]))
        by_name = {getattr(n, 'name', None): n for n in prog.statements}
        holder = by_name.get('Holder')
        check("struct fields of the entry program are mangled",
              holder is not None and
              [f.field_type for f in holder.fields] == ['k__T', 'k__T[]'],
              holder and [f.field_type for f in holder.fields])
        apply_fn = by_name.get('apply')
        check("fn-typed parameter is mangled",
              apply_fn is not None and apply_fn.params[0][0] == 'fn(k__T)->int',
              apply_fn and apply_fn.params)
        check("optional return type is mangled",
              by_name.get('first') is not None and by_name['first'].return_type == 'k__T?')
        # the module's own composites follow its mangling
        bag = by_name.get('k__Bag')
        check("module struct fields use the mangled own type",
              bag is not None and [f.field_type for f in bag.fields] ==
              ['k__T', 'k__T[]', 'map<string,k__T>'],
              bag and [f.field_type for f in bag.fields])
        each = by_name.get('k__each')
        check("module fn params use the mangled own type",
              each is not None and each.params ==
              [('k__T[]', 'xs'), ('fn(k__T)->int', 'f')], each and each.params)
        check("module optional return uses the mangled own type",
              by_name.get('k__pick') is not None and by_name['k__pick'].return_type == 'k__T?')

        from modules import _rename_type                  # noqa: E402
        check("only TYPE names are renamed inside a composite",
              _rename_type('map<string,T>', {'map': 'k__map', 'T': 'k__T'},
                           {'T': 'k__T'}) == 'map<string,k__T>')
        check("another module's `ns::T` is left to its own alias",
              _rename_type('j::T[]', {'T': 'k__T'}, {'T': 'k__T'}) == 'j::T[]')

        for backend in ('pyro', 'node', 'go'):
            if backend == 'node' and not shutil.which('node'):
                check("node is installed (needed for the L-17 parity run)", False)
                continue
            if backend == 'go' and not shutil.which('go'):
                check("go is installed (needed for the L-17 parity run)", False)
                continue
            r = cryoc('main.cryo', '--backend', backend, '--run', '--no-cache', cwd=cd)
            got = r.stdout.strip().splitlines()[-len(COMPOSITE_WANT):]
            check(f"composite qualified types compile and run on {backend}",
                  got == COMPOSITE_WANT, (r.stdout + r.stderr)[-600:])

        for src, msg in (('import "lib.cryo" as k;\nk::Hidden[] h = [];\n', "is not pub"),
                         ('import "lib.cryo" as k;\nmap<string,k::Nope> m = {};\n',
                          "unknown type 'Nope'")):
            try:
                resolve_modules(Parser(Lexer(src).tokenize()).parse(), cd)
                check(f"composite type: {msg}", False, "no error raised")
            except Exception as e:                       # ModuleError
                check(f"composite type: {msg}", msg in str(e), str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
