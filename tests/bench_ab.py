#!/usr/bin/env python3
# ============================================================
#  bench_ab.py — A/B the Go VM against an earlier revision (13.4)
#
#  bench_vm.py records what the machine does TODAY. It cannot tell
#  you whether a change to the VM helped, because it neither warms
#  the binaries nor alternates their order, and two --save runs are
#  two different machines-in-time (and, once the front end moves,
#  two different .pyro files).
#
#  This answers the other question: same .pyro files, two binaries
#  built from one source tree each, is B faster than A?
#
#  THE PROCEDURE, and why each part is there — every one of these
#  was paid for by a wrong answer recorded in BENCHMARKS.md:
#
#    * WARM-UP. First touch of a binary costs page-cache population
#      and, on Windows, an on-access scan. It lands on whichever
#      file is timed first and min() cannot remove it: it is paid
#      once, before the first sample, not spread across them. Under
#      that bias an A/A control between two copies of ONE binary
#      reported -7.5%, and two real changes were read backwards.
#
#    * ORDER ALTERNATION. Timing A then B every rep gives B a warm
#      cache; that alone was worth ~15% in 11.25.
#
#    * A/A FIRST. The floor is measured, not assumed. Any A/B result
#      inside the A/A band is not a result.
#
#    * BOTH ORIENTATIONS. Run the A/B again with the binaries
#      swapped. If the sign does not follow the change, it is
#      position, not the code.
#
#  Like bench_vm.py this is not a test: it reports, it never fails.
#  Correctness is test_c_vm.py and test_fuzz.py, and no number here
#  means anything until those pass.
#
#  Usage:
#      python Burnout/tests/bench_ab.py                 # HEAD vs working tree
#      python Burnout/tests/bench_ab.py --ref v1.1.0
#      python Burnout/tests/bench_ab.py --reps 21 --only arith
# ============================================================
import argparse
import glob
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_vm as B                                    # noqa: E402

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = B.ROOT
VMDIR = B.VMDIR
EXE = B.EXE
REPS = 11          # odd, so alternation splits the reps rather than pairing them


def build_tree(dst, ref=None):
    """A VM binary built from its own directory. `ref` = that git revision."""
    os.makedirs(dst, exist_ok=True)
    for f in glob.glob(os.path.join(VMDIR, '*.go')) + [os.path.join(VMDIR, 'go.mod')]:
        shutil.copy(f, dst)
    if ref:
        # Only main.go is fetched from the ref: the point is to isolate the
        # interpreter change, and pulling the whole directory would drag in
        # whatever else moved. Widen this if a change spans files.
        r = subprocess.run(['git', '-C', os.path.join(ROOT, 'Pyro'),
                            'show', f'{ref}:vm/main.go'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"could not read main.go at {ref}: {r.stderr.strip()[:200]}")
            return None
        with open(os.path.join(dst, 'main.go'), 'w', encoding='utf-8',
                  newline='') as f:
            f.write(r.stdout)
    out = os.path.join(dst, 'vm' + EXE)
    r = subprocess.run(['go', 'build', '-o', out, '.'], cwd=dst,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"build failed in {dst}:\n{r.stderr[-600:]}")
        return None
    return out


def once(vm, pyro):
    t0 = time.perf_counter()
    r = subprocess.run([vm, pyro], capture_output=True, timeout=600)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        raise SystemExit(f"{vm} failed on {pyro}: {r.stderr[:300]!r}")
    return dt


def startup(exes, empty, reps):
    st = {}
    for e in exes:
        once(e, empty)                                  # warm before measuring
        st[e] = min(once(e, empty) for _ in range(reps))
    return st


def compare(a, b, programs, st, reps):
    out = {}
    for name, pyro in programs.items():
        once(a, pyro)                                   # warm BOTH, on THIS
        once(b, pyro)                                   # program, before timing
        av, bv = [], []
        for i in range(reps):
            if i % 2 == 0:
                av.append(once(a, pyro)); bv.append(once(b, pyro))
            else:
                bv.append(once(b, pyro)); av.append(once(a, pyro))
        out[name] = (max(0.0, min(av) - st[a]), max(0.0, min(bv) - st[b]),
                     max(0.0, statistics.median(av) - st[a]),
                     max(0.0, statistics.median(bv) - st[b]))
    return out


def report(title, res):
    print(f"\n=== {title} ===")
    print(f"{'program':<10} {'A min':>9} {'B min':>9} {'min':>8}"
          f" {'A med':>9} {'B med':>9} {'med':>8}")
    ta = tb = tam = tbm = 0.0
    for name, (a, b, am, bm) in res.items():
        ta += a; tb += b; tam += am; tbm += bm
        print(f"{name:<10} {a*1000:8.1f}m {b*1000:8.1f}m "
              f"{((b-a)/a*100 if a else 0):+7.2f}%"
              f" {am*1000:8.1f}m {bm*1000:8.1f}m "
              f"{((bm-am)/am*100 if am else 0):+7.2f}%")
    dmin = (tb - ta) / ta * 100 if ta else 0.0
    dmed = (tbm - tam) / tam * 100 if tam else 0.0
    print(f"{'TOTAL':<10} {ta*1000:8.1f}m {tb*1000:8.1f}m {dmin:+7.2f}%"
          f" {tam*1000:8.1f}m {tbm*1000:8.1f}m {dmed:+7.2f}%")
    return dmin, dmed


def main():
    ap = argparse.ArgumentParser(description="A/B the Go VM (roadmap 13.4)")
    ap.add_argument('--ref', default='HEAD',
                    help="git revision for side A (default HEAD)")
    ap.add_argument('--reps', type=int, default=REPS)
    ap.add_argument('--only', help='one program by name')
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix='cryo_ab_')
    try:
        print(f"building side A from {args.ref} ...", end=' ', flush=True)
        a = build_tree(os.path.join(work, 'a'), args.ref)
        print("ok" if a else "FAILED")
        print("building side B from the working tree ...", end=' ', flush=True)
        b = build_tree(os.path.join(work, 'b'))
        print("ok" if b else "FAILED")
        if not (a and b):
            return 1
        # The A/A control runs two COPIES, not one path twice: the same path
        # twice shares a file handle and reports a floor that is too good.
        b2 = os.path.join(work, 'b_copy' + EXE)
        shutil.copy(b, b2)

        names = [args.only] if args.only else list(B.PROGRAMS)
        programs = {}
        for n in names:
            p = B.compile_program(B.PROGRAMS[n], work, n)
            if p is None:
                print(f"  {n}: did not compile")
                continue
            programs[n] = p
        empty = B.compile_program(B.EMPTY, work, 'empty')
        if not programs or not empty:
            return 1

        st = startup([a, b, b2], empty, args.reps)
        print("start-up (subtracted): "
              + ", ".join(f"{k.split(os.sep)[-2]} {v*1000:.0f}ms"
                          for k, v in st.items()))

        floor = report("A/A control: the working tree against a copy of itself",
                       compare(b, b2, programs, st, args.reps))
        fwd = report(f"A/B: {args.ref} in slot A, working tree in slot B",
                     compare(a, b, programs, st, args.reps))
        rev = report(f"A/B SWAPPED: working tree in slot A, {args.ref} in slot B",
                     compare(b, a, programs, st, args.reps))

        print("\n── reading it ──")
        print(f"  floor (A/A)      {floor[0]:+.2f}% min   {floor[1]:+.2f}% median")
        print(f"  change           {fwd[0]:+.2f}% min   {fwd[1]:+.2f}% median")
        print(f"  change, swapped  {rev[0]:+.2f}% min   {rev[1]:+.2f}% median")
        if abs(fwd[0]) <= abs(floor[0]):
            print("  -> inside the floor: NOT a result.")
        elif (fwd[0] < 0) == (rev[0] < 0):
            print("  -> the sign did NOT flip when swapped: position, not code.")
        else:
            print("  -> outside the floor and the sign follows the change.")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
