#!/usr/bin/env python3
# ============================================================
#  test_selfhost_semantic.py — the self-hosted analyser (13.2)
#
#  The two compilers must agree on what is an ERROR, not only on
#  what is valid. Before this, the self-hosted front end had no
#  analyser at all and emitted a `.pyro` for every program it
#  could parse:
#
#    print(x);      undeclared -> emitted, ran, printed 0
#    nosuch(1);     no such fn -> emitted a call to function index
#                   65535; the VM died with a Go runtime panic
#    f(1, 2)        wrong arity -> emitted, ran, printed 1
#
#  A compiler that turns a typo into a silent 0 is worse than one
#  that cannot compile the file, because nothing tells you to look.
#
#  THE ORACLE is the reference compiler's OWN pipeline —
#  monomorphize -> lower_traits -> semantic_check — not a bare
#  `semantic.check` on a fresh parse. Those passes rewrite traits
#  and generics away before analysis, so checking against the bare
#  call reports disagreements that the real compiler never has.
#  That mistake cost several false "differences" while this was
#  being built.
#
#  SCOPE: the corpus is the self-hosted SUBSET. The self-hosted
#  compiler has no module resolution, generics or traits, so
#  programs needing those are out of its scope entirely and are
#  not a fair comparison.
# ============================================================
import os
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
SELF = os.path.join(ROOT, 'Cryo', 'selfhost')
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
EXE = '.exe' if sys.platform == 'win32' else ''
VM = os.path.join(ROOT, 'build', 'pyrovm' + EXE)
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


# ── the reference's answer, via its real pipeline ───────────
from lexer import Lexer                     # noqa: E402
from parser import Parser                   # noqa: E402
from generics import monomorphize           # noqa: E402
from traits import lower_traits             # noqa: E402
import semantic                             # noqa: E402


def reference_accepts(src):
    try:
        ast = Parser(Lexer(src).tokenize()).parse()
        ast = monomorphize(ast)
        ast = lower_traits(ast)
        semantic.check(ast, src, 'x.cryo')
        return True
    except Exception:
        return False


# ── the self-hosted analyser's answer, on the VM ────────────
BS = chr(92)
_WORK = tempfile.mkdtemp(prefix='cryo_shsem_')


def _embed(s):
    return (s.replace(BS, BS * 2).replace('"', BS + '"')
             .replace('$', BS + '$').replace(chr(10), BS + 'n'))


def selfhost_accepts(src):
    """(accepted, errors-text) from analyze() running on the Pyro VM."""
    driver = ('import "lexer.cryo"' + chr(10) +
              'import "semantic.cryo"' + chr(10) +
              'string[] tt = []; string[] tv = [];' + chr(10) +
              f'lex("{_embed(src)}", tt, tv);' + chr(10) +
              'string[] errs = [];' + chr(10) +
              'bool ok = analyze(tt, tv, errs);' + chr(10) +
              'print(ok);' + chr(10) + 'print(errs);' + chr(10))
    dpath = os.path.join(SELF, '_sem_driver.cryo')
    out = os.path.join(_WORK, 'd.pyro')
    with open(dpath, 'w', encoding='utf-8') as f:
        f.write(driver)
    try:
        c = subprocess.run([sys.executable, CRYOC, dpath, '--backend', 'pyro',
                            '-o', out, '--no-banner'],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=600)
        if c.returncode != 0:
            return None, (c.stdout or '') + (c.stderr or '')
        r = subprocess.run([VM, out], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=300)
    finally:
        try:
            os.remove(dpath)
        except OSError:
            pass
    lines = [l.strip() for l in (r.stdout or '').splitlines()]
    ok = [l for l in lines if l in ('true', 'false')]
    errs = [l for l in lines if l.startswith('[')]
    return (ok[0] == 'true' if ok else None), (errs[0] if errs else '')


# ── the corpus ──────────────────────────────────────────────
VALID = [
    ("a plain program", 'fn f(int a) -> int ={ return a * 2; } print(f(21));'),
    ("a loop with break and continue",
     'int s = 0; for (int i = 0; i < 5; i++) { if (i == 1) { continue; } '
     'if (i == 4) { break; } s += i; } print(s);'),
    ("a while loop", 'int i = 0; while (i < 3) { i = i + 1; } print(i);'),
    ("a struct and a field read",
     'struct P { int x; } P p = new P { x: 3 }; print(p.x);'),
    ("an enum member as a value", 'enum E { A, B } E e = B; print(e);'),
    ("a data enum and match",
     'enum R { Ok(int), Err(string) } R r = Ok(1); '
     'match r { Ok(v) => { print(v); } Err(e) => { print(e); } }'),
    ("a match wildcard",
     'enum R { Ok(int), Err(string) } R r = Ok(1); '
     'match r { Err(e) => { print(e); } _ => { print("other"); } }'),
    ("try / catch binds its variable",
     'try { throw("x"); } catch (string e) { print(e); }'),
    ("a const declaration", 'const number PI = 3.14; print(PI);'),
    ("a map parameter is one parameter, not two",
     'fn f(map<string, number> t, string s) -> int ={ return 1; } print(f({}, "x"));'),
    ("a forward reference to a function declared later",
     'print(g(2)); fn g(int n) -> int ={ return n; }'),
    ("a method call is not a function call", 'int[] a = []; a.push(1); print(a);'),
    ("a field key is not a variable",
     'struct P { int x; } P p = new P { x: 1 }; print(p.x);'),
    ("a ternary middle operand is a variable",
     'int a = 1; int b = 2; print(a > 0 ? b : 3);'),
    ("string interpolation", 'int n = 2; print("n=${n}");'),
    ("prefix ! and ~", 'bool b = false; print(!b); print(~0);'),
]

INVALID = [
    ("an undeclared variable", 'print(x);', "undeclared variable"),
    ("an undeclared assignment target", 'y = 5;', "assignment to undeclared"),
    ("an unknown function", 'nosuch(1);', "unknown function"),
    ("too many arguments",
     'fn f(int a) -> int ={ return a; } print(f(1, 2));', "expects 1"),
    ("too few arguments",
     'fn f(int a, int b) -> int ={ return a; } print(f(1));', "expects 2"),
    ("break outside a loop", 'break;', "outside loop"),
    ("continue outside a loop", 'continue;', "outside loop"),
    ("a variable that has gone out of scope",
     'if (true) { int q = 1; } print(q);', "undeclared variable"),
]


def main():
    print("[13.2] the self-hosted semantic analyser")
    if not os.path.isfile(VM):
        print("  --   no VM binary; skipped")
        return 0

    print("\n── programs both compilers accept ──")
    for label, src in VALID:
        ok, errs = selfhost_accepts(src)
        ra = reference_accepts(src)
        check(f"{label}", ok is True and ra is True,
              f"self={ok} ref={ra} {errs[:160]}")

    print("\n── programs both compilers reject ──")
    for label, src, needle in INVALID:
        ok, errs = selfhost_accepts(src)
        ra = reference_accepts(src)
        check(f"{label} — self-host rejects", ok is False, f"{errs[:160]}")
        check(f"{label} — and names it", needle in errs, errs[:160])
        check(f"{label} — the reference agrees", ra is False, f"ref={ra}")

    # ── the compiler REFUSES rather than emitting ──────────
    #
    # Analysis that only reports is worth nothing if the bytecode is written
    # anyway; the point is that the bad `.pyro` never exists.
    print("\n── compile() refuses, and writes nothing ──")
    for label, src, _needle in INVALID[:4]:
        out = os.path.join(_WORK, 'r.pyro').replace(BS, '/')
        if os.path.exists(out):
            os.remove(out)
        drv = os.path.join(SELF, '_sem_c.cryo')
        with open(drv, 'w', encoding='utf-8') as f:
            f.write('import "codegen.cryo"' + chr(10) +
                    f'compile("{_embed(src)}", "{out}");' + chr(10))
        p = os.path.join(_WORK, 'c.pyro')
        try:
            c = subprocess.run([sys.executable, CRYOC, drv, '--backend', 'pyro',
                                '-o', p, '--no-banner'], capture_output=True,
                               text=True, encoding='utf-8', errors='replace',
                               timeout=600)
            if c.returncode == 0:
                subprocess.run([VM, p], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=300)
        finally:
            try:
                os.remove(drv)
            except OSError:
                pass
        check(f"{label} — no .pyro is written", not os.path.exists(out))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
