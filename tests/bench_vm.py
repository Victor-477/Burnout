#!/usr/bin/env python3
# ============================================================
#  bench_vm.py — VM benchmarks with tracked numbers (roadmap 11.22)
#
#  "Faster" is a claim, and a claim needs a number. This builds
#  both VMs, runs a fixed set of programs on each, and prints a
#  table. `--save` writes it to Pyro/BENCHMARKS.md so a later
#  change can be compared against it rather than against memory.
#
#  HOW THE NUMBERS ARE TAKEN
#  Each program runs REPS times and the MINIMUM is reported, not
#  the mean. On a desktop the noise is all upward — a scheduler
#  slice lost to something else — so the minimum is the closest
#  thing to the machine's actual speed, and it is far more stable
#  across runs than an average.
#
#  Process start-up is measured separately and subtracted. A Go
#  binary takes a few milliseconds to start and a C one less; on a
#  short benchmark that difference would otherwise show up as
#  "dispatch speed", which it is not.
#
#  This is not a test: it never fails, it reports. Correctness is
#  test_c_vm.py's job, and any dispatch change must pass that and
#  test_fuzz.py before its numbers mean anything at all.
# ============================================================
import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
VMDIR = os.path.join(ROOT, 'Pyro', 'vm')
BUILD = os.path.join(ROOT, 'build')
EXE = '.exe' if sys.platform == 'win32' else ''
GO_VM = os.path.join(BUILD, 'bench_govm' + EXE)
C_VM = os.path.join(BUILD, 'bench_cvm' + EXE)
REPORT = os.path.join(ROOT, 'Pyro', 'BENCHMARKS.md')

REPS = 5

# Each program isolates one kind of work, so a dispatch change shows up where
# it should: a loop of cheap instructions is where threading can matter, while
# one dominated by string building is mostly library time.
PROGRAMS = {
    'arith': (
        '// tight integer loop: the densest opcode mix there is\n'
        'int total = 0;\n'
        'for (int i = 0; i < 2000000; i++) {\n'
        '    total = total + i * 3 - (i / 2);\n'
        '}\n'
        'print(total);\n'),
    'calls': (
        '// function call overhead: frames, arguments, returns\n'
        'fn add3(int a, int b, int c) -> int ={ return a + b + c; }\n'
        'int total = 0;\n'
        'for (int i = 0; i < 400000; i++) { total = total + add3(i, 1, 2); }\n'
        'print(total);\n'),
    'fib': (
        '// recursion: call-heavy and branch-heavy at once\n'
        'fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n-1) + fib(n-2); }\n'
        'print(fib(25));\n'),
    'array': (
        '// indexed reads and writes, with the bounds checks that go with them\n'
        'int[] xs = [];\n'
        'for (int i = 0; i < 200000; i++) { xs.push(i); }\n'
        'int total = 0;\n'
        'for (int i = 0; i < 200000; i++) { total = total + xs[i]; }\n'
        'print(total);\n'),
    'branch': (
        '// unpredictable branching: where indirect-branch prediction shows\n'
        'int hits = 0;\n'
        'for (int i = 0; i < 1000000; i++) {\n'
        '    if (i % 3 == 0) { hits = hits + 1; }\n'
        '    else if (i % 5 == 0) { hits = hits + 2; }\n'
        '    else { hits = hits - 1; }\n'
        '}\n'
        'print(hits);\n'),
    'strings': (
        '// library-dominated: a control for how much dispatch matters\n'
        'string s = "";\n'
        'for (int i = 0; i < 20000; i++) { s = s + "x"; }\n'
        'print(len(s));\n'),
}

# An empty program: whatever this costs is start-up, not execution.
EMPTY = 'print(0);\n'


def build_vms(verbose=True):
    os.makedirs(BUILD, exist_ok=True)
    if verbose:
        print("building the Go VM ...", end=' ', flush=True)
    r = subprocess.run(['go', 'build', '-o', GO_VM,
                        os.path.join(VMDIR, 'main.go')],
                       capture_output=True, text=True)
    go_ok = r.returncode == 0
    if verbose:
        print("ok" if go_ok else "FAILED\n" + r.stderr[-400:])

    cc = next((c for c in ('gcc', 'clang', 'cc') if shutil.which(c)), None)
    c_ok = False
    if cc:
        if verbose:
            print(f"building the C VM ({cc}) ...", end=' ', flush=True)
        src = [os.path.join(VMDIR, 'main.c'),
               os.path.join(VMDIR, 'pyro_runtime.c')]
        libs = ['-lws2_32'] if sys.platform == 'win32' else ['-lm']
        r = subprocess.run([cc, '-O2', '-std=c11', '-finput-charset=UTF-8',
                            '-fexec-charset=UTF-8', '-o', C_VM] + src + libs,
                           capture_output=True, text=True)
        c_ok = r.returncode == 0
        if verbose:
            print("ok" if c_ok else "FAILED\n" + r.stderr[-400:])
    elif verbose:
        print("no C toolchain — the C VM is skipped")
    return go_ok, c_ok


def compile_program(src, work, name):
    cf = os.path.join(work, name + '.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    out = os.path.join(work, name + '.pyro')
    r = subprocess.run([sys.executable, CRYOC, cf, '--backend', 'pyro',
                        '-o', out, '--no-banner'],
                       capture_output=True, text=True, timeout=300)
    return out if r.returncode == 0 and os.path.isfile(out) else None


def time_run(vm, pyro, reps=REPS):
    """Minimum wall time over `reps` runs, or None if it will not run."""
    best = None
    for _ in range(reps):
        t0 = time.perf_counter()
        r = subprocess.run([vm, pyro], capture_output=True, timeout=600)
        dt = time.perf_counter() - t0
        if r.returncode != 0:
            return None
        best = dt if best is None else min(best, dt)
    return best


def main():
    ap = argparse.ArgumentParser(description="Pyro VM benchmarks")
    ap.add_argument('--save', action='store_true',
                    help='write the table to Pyro/BENCHMARKS.md')
    ap.add_argument('--reps', type=int, default=REPS)
    ap.add_argument('--only', help='run one program by name')
    args = ap.parse_args()

    go_ok, c_ok = build_vms()
    if not (go_ok or c_ok):
        print("no VM could be built")
        return 1

    work = tempfile.mkdtemp(prefix='cryo_bench_')
    rows = []
    try:
        # Start-up, measured the same way and subtracted below.
        base = compile_program(EMPTY, work, 'empty')
        go_start = time_run(GO_VM, base, args.reps) if (go_ok and base) else 0.0
        c_start = time_run(C_VM, base, args.reps) if (c_ok and base) else 0.0
        go_start = go_start or 0.0
        c_start = c_start or 0.0
        print(f"\nprocess start-up: go {go_start*1000:.0f} ms, "
              f"c {c_start*1000:.0f} ms  (subtracted below)\n")

        names = [args.only] if args.only else list(PROGRAMS)
        for name in names:
            src = PROGRAMS[name]
            pyro = compile_program(src, work, name)
            if pyro is None:
                print(f"  {name}: did not compile")
                continue
            size = os.path.getsize(pyro)
            g = time_run(GO_VM, pyro, args.reps) if go_ok else None
            c = time_run(C_VM, pyro, args.reps) if c_ok else None
            g = max(0.0, g - go_start) if g is not None else None
            c = max(0.0, c - c_start) if c is not None else None
            rows.append((name, size, g, c))
            gs = f"{g*1000:8.0f}" if g is not None else "       -"
            cs = f"{c*1000:8.0f}" if c is not None else "       -"
            ratio = f"{g/c:5.2f}x" if (g and c) else "    -"
            print(f"  {name:<9} {size:>7} B   go {gs} ms   c {cs} ms   "
                  f"go/c {ratio}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if args.save:
        _write_report(rows)
        print(f"\nwritten to {REPORT}")
    return 0


def _write_report(rows):
    cpu = platform.processor() or platform.machine()
    lines = [
        "# Pyro VM benchmarks",
        "",
        "Produced by `python Burnout/tests/bench_vm.py --save`. Times are the",
        "**minimum** of several runs with process start-up subtracted; the",
        "minimum is used because benchmark noise on a desktop is all upward.",
        "",
        "These numbers exist so a change to the VM can be **measured** rather",
        "than asserted (roadmap 11.22). They are not a test and never fail —",
        "correctness is `test_c_vm.py` and `test_fuzz.py`, and a dispatch",
        "change means nothing until both of those pass.",
        "",
        f"- machine: {platform.system()} {platform.release()}, {cpu}",
        f"- python: {platform.python_version()}",
        "- C VM built with `-O2 -std=c11`; Go VM with `go build`",
        "",
        "| Program | .pyro | Go VM | C VM | go/c |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, size, g, c in rows:
        gs = f"{g*1000:.0f} ms" if g is not None else "—"
        cs = f"{c*1000:.0f} ms" if c is not None else "—"
        ratio = f"{g/c:.2f}×" if (g and c) else "—"
        lines.append(f"| `{name}` | {size} B | {gs} | {cs} | {ratio} |")
    lines += [
        "",
        "## What each program isolates",
        "",
        "| Program | Stresses |",
        "|---|---|",
        "| `arith` | a tight integer loop — the densest opcode mix, where dispatch cost is most visible |",
        "| `calls` | call overhead: frames, arguments, returns |",
        "| `fib` | recursion — call-heavy and branch-heavy together |",
        "| `array` | indexed reads and writes, with their bounds checks |",
        "| `branch` | unpredictable branching, where indirect-branch prediction shows |",
        "| `strings` | library-dominated work, as a control for how much dispatch matters at all |",
        "",
    ]
    with open(REPORT, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


if __name__ == '__main__':
    sys.exit(main())
