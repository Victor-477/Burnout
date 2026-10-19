#!/usr/bin/env python3
# ============================================================
#  test_library_blockers.py — what broke while writing a library
#
#  Every check here is something that failed while building
#  Cryo-Math (a pure-Cryo math library) and a calculator on top
#  of it. Each one forced a workaround on the library's users, so
#  each one is pinned:
#
#    * `m::f()` written directly inside try/catch, switch, assert,
#      unsafe { }, match, `x!`, `expr?` was "unknown function".
#    * the go backend typed `[]` / `{}` / `[1.0, 2.5]` as []any in
#      a plain assignment or a call argument, and Go refused it.
#    * `+ - *` on int wrapped silently on the Pyro VM while the go
#      and c backends aborted, so one program printed a wrong
#      number on one engine and stopped on another.
#    * `1.5e3` was a syntax error.
#
#  The `cryoc test` side (imports, VM build, the C report) is in
#  test_testing.py, next to the rest of the runner's checks.
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
CRYOC = os.path.join(os.path.dirname(HERE), 'cryoc.py')
PYRO_CLI = os.path.join(os.path.dirname(HERE), 'pyro.py')

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def run(work, name, src, backend):
    """Compile and run `src` on `backend`; (returncode, stdout, stderr)."""
    path = os.path.join(work, name + '.cryo')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)
    ext = {'pyro': '.pyro', 'go': '.go', 'node': '.js', 'c': '.c'}[backend]
    out = os.path.join(work, f"{name}_{backend}{ext}")
    r = subprocess.run([sys.executable, CRYOC, path, '--backend', backend,
                        '-o', out, '--run', '--no-banner'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=600, cwd=work)
    return r.returncode, r.stdout, r.stderr


def program_lines(stdout):
    """The program's own output: what follows the driver's `── Running` rule."""
    lines = stdout.replace('\r\n', '\n').split('\n')
    for i, ln in enumerate(lines):
        if ln.startswith('── Running'):
            return [x for x in lines[i + 1:] if x.strip()]
    return []


def available(backend):
    return {'pyro': shutil.which('go') or shutil.which('gcc'),
            'go': shutil.which('go'),
            'node': shutil.which('node'),
            'c': shutil.which('gcc')}[backend]


LIB = '''pub fn f(int x) -> int ={ return x + 1; }
pub fn boom(string s) -> int ={ throw("boom " + s); }
'''

NESTED = '''import "lib.cryo" as m;
enum R { Ok(int), Err(string) }
fn wrap(int x) -> R ={ return Ok(m::f(x)); }
fn chain(int x) -> R ={ int v = wrap(x)?; return Ok(m::f(v)); }
try { print(m::f(1)); } catch (string e) { print(e); } finally { print(m::f(100)); }
try { print(m::boom("x")); } catch (string e) { print(e); }
switch (m::f(0)) { case 1: print(m::f(10)); break; default: print(0); }
assert(m::f(1) == 2, "assert " + to_string(m::f(1)));
unsafe { print(m::f(20)); }
match chain(5) { Ok(v) => { print(m::f(v)); } Err(e) => { print(e); } }
int? o = m::f(3);
print(o!);
'''
NESTED_OUT = ['2', '101', 'boom x', '11', '21', '8', '4']

LITERALS = '''fn total(number[] xs) -> number ={ number s = 0.0; for (number x in xs) { s = s + x; } return s; }
fn size(map<string, int> m) -> int ={ return len(m); }
string[] names = ["a"];
names = [];
names.push("z");
map<string, number> vs = {"a": 1.0};
vs = {};
vs["k"] = 2.0;
int[][] grid = [[1]];
grid = [[1, 2], [3]];
print(len(names));
print(len(vs));
print(total([1.0, 2.5]));
print(total([]));
print(size({"x": 1, "y": 2}));
print(len(grid[0]));
'''
LITERALS_OUT = ['1', '1', '3.5', '0', '2', '2']

EXPONENT = '''number a = 1.5e3;
print(a);
print(2E-2 * 100.0);
print(1e3 + 1.0);
print(6.022e+23 > 1e23);
'''
EXPONENT_OUT = ['1500', '2', '1001', 'true']

OVERFLOW = '''int big = 9223372036854775807;
print(big - 1 + 1);
print(3037000499 * 3037000499);
print(big + 1);
'''


def main():
    print("[library blockers] what a pure-Cryo library tripped over")
    work = tempfile.mkdtemp(prefix='cryo_libblock_')
    try:
        with open(os.path.join(work, 'lib.cryo'), 'w', encoding='utf-8') as f:
            f.write(LIB)

        print("\n── `ns::name` inside every statement kind ──")
        for be in ('pyro', 'node', 'go'):
            if not available(be):
                print(f"  skip {be}: toolchain not found")
                continue
            rc, out, err = run(work, 'nested', NESTED, be)
            got = program_lines(out)
            check(f"{be}: try/finally, switch, assert, unsafe, match, `?`, `!` resolve m::f",
                  rc == 0 and got == NESTED_OUT, (err or str(got))[-400:])

        # Privacy is still enforced inside the newly-walked nodes: walking a
        # node is not the same as trusting it.
        with open(os.path.join(work, 'priv.cryo'), 'w', encoding='utf-8') as f:
            f.write('fn hidden() -> int ={ return 1; }\n')
        rc, out, err = run(work, 'privtry',
                           'import "priv.cryo" as p;\n'
                           'try { print(p::hidden()); } catch (string e) { print(e); }\n',
                           'pyro')
        check("a private member is still refused inside try",
              rc != 0 and "not pub" in (out + err), (out + err)[-300:])

        print("\n── go: a literal takes the type of its slot ──")
        for be in ('pyro', 'go'):
            if not available(be):
                print(f"  skip {be}: toolchain not found")
                continue
            rc, out, err = run(work, 'literals', LITERALS, be)
            got = program_lines(out)
            check(f"{be}: `x = []`, `x = {{}}`, `f([1.0, 2.5])`, `f({{...}})` compile and run",
                  rc == 0 and got == LITERALS_OUT, (err or str(got))[-400:])

        print("\n── exponent literals ──")
        for be in ('pyro', 'node', 'go'):
            if not available(be):
                print(f"  skip {be}: toolchain not found")
                continue
            rc, out, err = run(work, 'expo', EXPONENT, be)
            got = program_lines(out)
            check(f"{be}: 1.5e3, 2E-2, 1e3, 6.022e+23 are float literals",
                  rc == 0 and got == EXPONENT_OUT, (err or str(got))[-300:])
        # `2e` has no digit after the `e`, so it is the int 2 followed by the
        # identifier `e` — which is a syntax error, not a silent exponent.
        rc, out, err = run(work, 'expo_bad', 'int e = 3;\nprint(2e);\n', 'pyro')
        check("`2e` (no digit after the e) is not read as an exponent",
              'Syntax Error' in (out + err), (out + err)[-200:])

        print("\n── int overflow aborts on every engine, with one message ──")
        msg = "[Cryo Security] Overflow: integer addition"
        for be in ('pyro', 'go', 'c'):
            if not available(be):
                print(f"  skip {be}: toolchain not found")
                continue
            rc, out, err = run(work, 'ovf', OVERFLOW, be)
            got = program_lines(out)
            # the driver does not forward the program's exit code, so the
            # abort is observed through its message and the missing output
            check(f"{be}: the values in range print, then `big + 1` aborts",
                  got[:2] == ['9223372036854775807', '9223372030926249001']
                  and msg in (out + err) and len(got) == 2,
                  (out + err)[-400:])
        if shutil.which('gcc'):
            src = os.path.join(work, 'ovf.cryo')
            exe = os.path.join(work, 'ovf_native')
            b = subprocess.run([sys.executable, PYRO_CLI, 'build', src, '-o', exe],
                               capture_output=True, text=True, timeout=600)
            if b.returncode == 0 and os.path.isfile(exe):
                r = subprocess.run([exe], capture_output=True, text=True, timeout=60)
                check("AOT native binary: aborts with the same message",
                      r.returncode != 0 and msg in r.stderr, (r.stdout + r.stderr)[-300:])
            else:
                check("AOT native binary builds", False, (b.stdout + b.stderr)[-400:])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
