#!/usr/bin/env python3
# ============================================================
#  run_all.py — the whole suite, once, with a summary (14.1)
#
#  Every suite here is a SCRIPT: it prints its own checks and
#  exits 0 or 1. That is a deliberate style and it works well one
#  file at a time, but it left no way to run all forty and get one
#  answer. `python -m unittest discover` does not do it either —
#  these are not TestCases, and discovery imports them, which runs
#  them at import time and then reports the resulting SystemExit
#  as an error. So the whole suite has only ever been run in
#  pieces, by hand.
#
#  THE SKIP PROBLEM, which is the reason this exists
#  A suite that cannot run its subject prints a skip and exits 0.
#  In a log, and in an exit code, that is indistinguishable from
#  passing. `test_c_vm.py` and `test_aot.py` have exited 0 for the
#  entire recorded history of this project WITHOUT EVER RUNNING —
#  there has been no C toolchain on the author's machine — while
#  being named in BENCHMARKS.md and PYRO_RUNTIME.md as the gate
#  that proves Go/C parity.
#
#  So `--require NAME` makes a suite's skip a FAILURE. CI passes
#  it for exactly those two. Without it, a runner whose toolchain
#  silently went missing would go green and reproduce the problem
#  this item exists to remove.
# ============================================================
import argparse
import os
import re
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# Text a suite prints when it declines to run its subject. Matched
# case-insensitively against the whole output. Keep these in step with the
# suites: a marker that stops matching turns --require back into a no-op,
# which is the failure mode it was written to prevent.
SKIP_MARKERS = (
    "no c toolchain",
    "c toolchain: none",
    "parity test skipped",
    "native build skipped",
    "native build/run skipped",
)

# Long ones measured on the author's machine; CI is slower, so these are
# generous. A suite that hangs must not eat the whole budget.
TIMEOUTS = {
    'test_parity': 3600,
    'test_selfhost': 1800,
    'test_selfhost_semantic': 1800,
    'test_cli': 1800,
    'test_smoke': 1200,
    'test_c_vm': 1800,
    'test_difftest': 1800,
    'test_examples': 1800,
}
DEFAULT_TIMEOUT = 900

SUMMARY = re.compile(r'^(\d+) passed, (\d+) failed', re.M)


def suites():
    return sorted(f[:-3] for f in os.listdir(HERE)
                  if f.startswith('test_') and f.endswith('.py'))


def run(name, timeout):
    path = os.path.join(HERE, name + '.py')
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, path], capture_output=True,
                           text=True, errors='replace', timeout=timeout,
                           cwd=ROOT)
        out = (r.stdout or '') + (r.stderr or '')
        return r.returncode, out, time.time() - t0
    except subprocess.TimeoutExpired as e:
        got = (e.stdout or '') + (e.stderr or '')
        if isinstance(got, bytes):
            got = got.decode('utf-8', 'replace')
        return 124, got + f"\n[run_all] TIMEOUT after {timeout}s", time.time() - t0


def main():
    ap = argparse.ArgumentParser(description="run every suite, report once")
    ap.add_argument('--only', nargs='*', help='run just these')
    ap.add_argument('--skip', nargs='*', default=[], help='do not run these')
    ap.add_argument('--require', nargs='*', default=[],
                    help='these must RUN, not skip: a skip marker fails them')
    ap.add_argument('--verbose', action='store_true',
                    help='print each suite output as it goes')
    args = ap.parse_args()

    names = [n for n in (args.only or suites()) if n not in args.skip]
    rows, failed, skipped_required = [], [], []

    for name in names:
        timeout = TIMEOUTS.get(name, DEFAULT_TIMEOUT)
        rc, out, dt = run(name, timeout)
        low = out.lower()
        marker = next((m for m in SKIP_MARKERS if m in low), None)

        m = SUMMARY.search(out)
        counts = f"{m.group(1)}/{int(m.group(1)) + int(m.group(2))}" if m else '—'

        if name in args.require and marker:
            # The whole point of 14.1: exiting 0 without running is not passing.
            status = 'SKIPPED-BUT-REQUIRED'
            skipped_required.append((name, marker))
            failed.append(name)
        elif rc != 0:
            status = 'FAIL' if rc != 124 else 'TIMEOUT'
            failed.append(name)
        else:
            status = 'ok' + (' (skipped work)' if marker else '')

        rows.append((name, status, counts, dt))
        print(f"  {status:<22} {name:<28} {counts:>9}  {dt:6.1f}s", flush=True)
        if args.verbose or rc != 0 or (name in args.require and marker):
            print('\n'.join('      | ' + l for l in out.strip().split('\n')[-25:]))

    print(f"\n{'=' * 72}")
    print(f"{len(rows) - len(failed)}/{len(rows)} suites ok"
          f"   ({sum(r[3] for r in rows):.0f}s total)")
    for name, marker in skipped_required:
        print(f"\nREQUIRED SUITE DID NOT RUN: {name}\n"
              f"  it printed {marker!r} and exited 0, which is a skip, not a pass.\n"
              f"  Its subject was unavailable — check the toolchain on this runner.")
    if failed:
        print("\nfailed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
