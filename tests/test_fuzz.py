#!/usr/bin/env python3
# ============================================================
#  test_fuzz.py — malformed .pyro files (roadmap 11.13)
#
#  The loader and the dispatch loop parse UNTRUSTED BINARY INPUT
#  IN C. That makes them the project's most exposed surface, so a
#  crash on a malformed file is a release blocker, not a curiosity.
#
#  This builds a corpus from valid programs — truncations at every
#  section boundary, absurd counts and lengths in the u16/u32
#  fields the loader used to trust, and random bit flips — and
#  runs every case on every engine available.
#
#  PASS means: a clean error message or a normal run.
#  FAIL means: a segfault / access violation.
#
#  Hangs are REPORTED BUT NOT FAILED. A malformed jump can make a
#  program loop forever, and so can a perfectly valid one — the
#  taskapp's `while (true)` accept loop is exactly that. The VM
#  cannot tell them apart, so bounding execution time is a quota
#  concern (11.11), not a memory-safety one.
#
#  When this suite first ran, the C VM crashed on 249 of 592
#  inputs. Every one was an out-of-bounds read.
# ============================================================
import os
import random
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
BUILD = os.path.join(ROOT, 'build')
GOVM = os.path.join(BUILD, 'pyrovm.exe' if sys.platform == 'win32' else 'pyrovm')
CVM = os.path.join(BUILD, 'pyroc.exe' if sys.platform == 'win32' else 'pyroc')

SIMPLE = 'fn add(int a, int b) -> int ={ return a + b; }\nprint(add(2,3));\nprint(upper("hi"));\n'
RICH = '''struct P { int x; string name; }
fn fact(int n) -> int ={ if (n <= 1) { return 1; } return n * fact(n - 1); }
int[] xs = [1,2,3];
map<string,int> m = {"a":1,"b":2};
int total = 0;
for (int v in xs) { total += v; }
try { if (total > 2) { throw("boom"); } } catch (string e) { print(e); }
P p = P{ x: 7, name: "hi" };
print(fact(5)); print(total); print(len(m)); print(p.name);
int? o = null; print(o ?? 9);
'''

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def mutations(blob, seed):
    rnd = random.Random(seed)
    out, n = [], len(blob)
    for c in sorted({6, 8, 10, 16, 24, 32, 48, 64, n // 4, n // 2,
                     3 * n // 4, n - 1, n - 4} & set(range(n))):
        out.append((f"truncated@{c}", blob[:c]))
    for off in range(6, min(n - 4, 96)):
        for val, tag in ((0xFFFFFFFF, "u32max"), (0x7FFFFFFF, "i32max")):
            m = bytearray(blob)
            m[off:off + 4] = struct.pack('<I', val)
            out.append((f"len@{off}={tag}", bytes(m)))
    for i in range(250):
        m = bytearray(blob)
        for _ in range(rnd.randint(1, 4)):
            m[rnd.randrange(len(m))] ^= 1 << rnd.randrange(8)
        out.append((f"flip#{i}", bytes(m)))
    m = bytearray(blob)
    m[5] = 0xFF
    out.append(("flags=0xFF", bytes(m)))
    return out


def crashed(rc):
    """A hard crash, as opposed to a refusal. Windows reports an access
    violation as 0xC0000005; POSIX as a negative signal."""
    return rc < 0 or (rc & 0xFFFFFFFF) > 0xC0000000


def fuzz(engine, name, pyro, workdir):
    blob = open(pyro, 'rb').read()
    cases = mutations(blob, seed=1234)
    tmp = os.path.join(workdir, '_fuzz.pyro')
    crashes, hangs = [], 0
    for label, data in cases:
        with open(tmp, 'wb') as f:
            f.write(data)
        try:
            p = subprocess.run([engine, tmp], capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            hangs += 1
            continue
        if crashed(p.returncode):
            crashes.append((label, hex(p.returncode & 0xFFFFFFFF)))
    check(f"{name}: no crash on {len(cases)} malformed inputs",
          not crashes,
          "; ".join(f"{l} -> {r}" for l, r in crashes[:6]))
    if hangs:
        print(f"       ({hangs} input(s) looped — not a failure: a valid "
              f"program may loop too; see 11.11 for quotas)")


def main():
    print("-- malformed .pyro: the loader and dispatch loop (11.13) --")
    if not os.path.isfile(GOVM) and not os.path.isfile(CVM):
        print("  SKIP  no VM built")
        sys.exit(0)

    work = tempfile.mkdtemp(prefix='fuzz_')
    try:
        for tag, src in (('simple', SIMPLE), ('rich', RICH)):
            cf = os.path.join(work, f'{tag}.cryo')
            pf = os.path.join(work, f'{tag}.pyro')
            open(cf, 'w', encoding='utf-8').write(src)
            r = subprocess.run([sys.executable, CRYOC, cf, '--backend', 'pyro',
                                '-o', pf, '--no-banner'],
                               capture_output=True, text=True, cwd=ROOT)
            if r.returncode != 0 or not os.path.isfile(pf):
                check(f"corpus seed '{tag}' compiles", False,
                      (r.stdout + r.stderr)[-200:])
                continue
            check(f"corpus seed '{tag}' compiles and is valid", True)
            for engine, name in ((GOVM, 'Go VM'), (CVM, 'C VM')):
                if os.path.isfile(engine):
                    fuzz(engine, f"{name} / {tag}", pf, work)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
