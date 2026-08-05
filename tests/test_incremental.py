#!/usr/bin/env python3
# ============================================================
#  test_incremental.py — the compilation cache (roadmap 11.23)
#
#  A cache that returns a stale artifact is worse than no cache:
#  the compiler reports success and hands back the previous
#  program. So almost everything here is about INVALIDATION, and
#  the shape of each test is the same — compile, change one thing,
#  compile again, and require the output to differ.
#
#  The things that must invalidate:
#      the entry file            the obvious one
#      an imported module        the reason it is a whole-program key
#      a compiler flag           --unsafe must not reuse a safe build
#      the backend               go must not reuse pyro's artifact
#      the COMPILER'S OWN SOURCE the one that would be most confusing
#                                to get wrong: editing a code
#                                generator and seeing no change
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
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))
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


def build(work, src_name, *flags, backend='pyro', out='o.bin'):
    """Compile inside `work`, so the cache lands there and not in the repo."""
    outp = os.path.join(work, out)
    if os.path.exists(outp):
        os.remove(outp)
    p = subprocess.run([sys.executable, CRYOC, os.path.join(work, src_name),
                        '--backend', backend, '-o', outp, '--no-banner', *flags],
                       capture_output=True, text=True, cwd=work, timeout=300,
                       errors='replace')
    data = None
    if os.path.isfile(outp):
        with open(outp, 'rb') as f:
            data = f.read()
    return p.returncode, data, (p.stdout + p.stderr)


def write(work, name, text):
    with open(os.path.join(work, name), 'w', encoding='utf-8') as f:
        f.write(text)


def test_reuse_and_correctness(work):
    print("\n── a cached build equals a fresh one ──")
    write(work, 'a.cryo', 'print(1 + 1);\nprint("hello");\n')
    rc1, d1, log1 = build(work, 'a.cryo')
    check("first build succeeds", rc1 == 0 and d1, log1[-200:])
    rc2, d2, _ = build(work, 'a.cryo')
    check("second build succeeds", rc2 == 0 and d2)
    check("the cached artifact is byte-identical", d1 == d2)
    rc3, d3, _ = build(work, 'a.cryo', '--no-cache')
    check("and identical to a build with the cache off", d1 == d3)
    check("a cache directory was created",
          os.path.isdir(os.path.join(work, '.cryocache')))


def test_entry_change(work):
    print("\n── changing the program changes the output ──")
    write(work, 'b.cryo', 'print(1);\n')
    _, d1, _ = build(work, 'b.cryo', out='b.bin')
    write(work, 'b.cryo', 'print(2);\n')
    _, d2, _ = build(work, 'b.cryo', out='b.bin')
    check("editing the entry file is not served from cache", d1 != d2)
    write(work, 'b.cryo', 'print(1);\n')
    _, d3, _ = build(work, 'b.cryo', out='b.bin')
    check("changing it back returns the original artifact", d1 == d3)


def test_import_change(work):
    """The key covers every file the compilation read, not just the entry —
    otherwise editing a module would be invisible."""
    print("\n── changing an imported module invalidates ──")
    write(work, 'lib.cryo', 'pub fn val() -> int ={ return 1; }\n')
    write(work, 'main.cryo', 'import "lib.cryo"\nprint(val());\n')
    rc1, d1, log1 = build(work, 'main.cryo', out='m.bin')
    check("a program with an import compiles", rc1 == 0 and d1, log1[-250:])
    write(work, 'lib.cryo', 'pub fn val() -> int ={ return 2; }\n')
    _, d2, _ = build(work, 'main.cryo', out='m.bin')
    check("editing the IMPORT changes the artifact", d1 != d2, "stale cache")


def test_settings(work):
    print("\n── flags and backends do not share an entry ──")
    # Checked on go, where safe mode is a codegen difference (cryoAddOvf). On
    # pyro it is not — the VM checks overflow itself — so the two builds are
    # legitimately identical there and would prove nothing about the cache.
    # Through an array so 11.21 cannot fold it: `int x = 2000000000;
    # print(x + x);` collapses to a constant, leaving no arithmetic for safe
    # mode to instrument and making both builds legitimately identical.
    write(work, 'c.cryo', 'int[] xs = [2000000000];\nprint(xs[0] + xs[0]);\n')
    _, safe, _ = build(work, 'c.cryo', backend='go', out='c1.go')
    _, unsafe, _ = build(work, 'c.cryo', '--unsafe', backend='go', out='c2.go')
    check("--unsafe does not reuse the safe artifact",
          safe and unsafe and safe != unsafe)
    # A program the optimizer demonstrably changes, so the two builds differ.
    write(work, 'g.cryo', 'int base = 10;\nint total = base * 60;\nprint(total);\n')
    _, opt, _ = build(work, 'g.cryo', backend='go', out='g1.go')
    _, noopt, _ = build(work, 'g.cryo', '--no-opt', backend='go', out='g2.go')
    check("--no-opt does not reuse the optimised artifact",
          opt and noopt and opt != noopt)
    write(work, 'd.cryo', 'print(1);\n')
    _, py, _ = build(work, 'd.cryo', backend='pyro', out='d1.bin')
    rc, go, log = build(work, 'd.cryo', backend='go', out='d2.go')
    check("a different backend does not reuse the artifact",
          rc == 0 and go and py != go, log[-200:])


def test_compiler_fingerprint(work):
    """The one that would be most confusing to get wrong: edit a code
    generator, rebuild, and see the previous compiler's output."""
    print("\n── editing the compiler invalidates everything ──")
    import cache as C
    before = C._compiler_fingerprint()
    victim = os.path.join(ROOT, 'Burnout', 'codegen_pyro.py')
    orig = open(victim, encoding='utf-8').read()
    try:
        with open(victim, 'w', encoding='utf-8') as f:
            f.write(orig + '\n# cache fingerprint probe\n')
        C._FINGERPRINT = None
        after = C._compiler_fingerprint()
        check("a change to a code generator changes the fingerprint",
              before != after, f"{before} == {after}")
    finally:
        with open(victim, 'w', encoding='utf-8') as f:
            f.write(orig)
        C._FINGERPRINT = None
    # NOT asserted: that restoring the file restores the fingerprint. It is
    # built from size and mtime rather than contents — reading ~40 compiler
    # sources on every invocation cost more than the cache saved — so
    # rewriting a file invalidates it even when the bytes are identical. That
    # errs toward recompiling, which is the safe direction; the unsafe one is
    # what the check above covers.
    after_restore = C._compiler_fingerprint()
    check("the fingerprint is stable across calls once the file settles",
          after_restore == C._compiler_fingerprint())


def test_warnings_replayed(work):
    """Compiling is not a pure function — it prints diagnostics — so a cache
    that skips the work must not skip those. The second build of a page whose
    javascript calls `cryo.…` fell silent about it until entries began storing
    their warnings."""
    print("\n── a cached build still reports what the first one did ──")
    write(work, 'page.cryo',
          'import >html<\n'
          'import >javascript<\n'
          'fn fib(int n) -> int ={ return n; }\n'
          'fn behavior() ={ >javascript( out.textContent = cryo.fib(2n); ) }\n'
          'fn page() ={ >html( <p id="out"></p> )<script = behavior> }\n')
    rc1, _, log1 = build(work, 'page.cryo', backend='frontend', out='p1.html')
    check("the first build warns about `cryo`",
          rc1 == 0 and 'will be undefined' in log1, log1[-200:])
    rc2, _, log2 = build(work, 'page.cryo', backend='frontend', out='p2.html')
    check("the cached build warns too", rc2 == 0 and 'will be undefined' in log2,
          log2[-200:])
    check("and names the same function",
          'cryo.fib' in log1 and 'cryo.fib' in log2, log2[-200:])


def test_clear(work):
    print("\n── --clear-cache ──")
    write(work, 'e.cryo', 'print(3);\n')
    build(work, 'e.cryo', out='e.bin')
    d = os.path.join(work, '.cryocache')
    check("the cache has entries before clearing", os.path.isdir(d))
    p = subprocess.run([sys.executable, CRYOC, '--clear-cache', '--no-banner'],
                       capture_output=True, text=True, cwd=work, timeout=120,
                       errors='replace')
    check("--clear-cache reports what it removed",
          'cleared' in (p.stdout + p.stderr), (p.stdout + p.stderr)[-150:])
    check("the directory is gone", not os.path.isdir(d))
    rc, data, log = build(work, 'e.cryo', out='e.bin')
    check("and the next build still works", rc == 0 and data, log[-200:])


def test_corrupt_entry(work):
    """A damaged entry must be a miss, never an error: the source is right
    there and recompiling costs milliseconds."""
    print("\n── a corrupt cache entry is survivable ──")
    write(work, 'f.cryo', 'print(7);\n')
    _, good, _ = build(work, 'f.cryo', out='f.bin')
    d = os.path.join(work, '.cryocache')
    n = 0
    for sub in ('parse', 'artifact'):
        p = os.path.join(d, sub)
        if not os.path.isdir(p):
            continue
        for name in os.listdir(p):
            with open(os.path.join(p, name), 'wb') as f:
                f.write(b'not a valid entry at all')
            n += 1
    check("entries were corrupted on purpose", n > 0)
    rc, data, log = build(work, 'f.cryo', out='f.bin')
    check("the build still succeeds", rc == 0 and data, log[-250:])
    check("and produces the right artifact", data == good)


def main():
    work = tempfile.mkdtemp(prefix='cryo_incr_')
    try:
        test_reuse_and_correctness(work)
        test_entry_change(work)
        test_import_change(work)
        test_settings(work)
        test_compiler_fingerprint(work)
        test_warnings_replayed(work)
        test_clear(work)
        test_corrupt_entry(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
