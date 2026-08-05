#!/usr/bin/env python3
# ============================================================
#  test_difftest.py — the differential generator (roadmap 13.1)
#
#  Two different jobs, and it is worth being clear which is which:
#
#  1. A REGRESSION run over a FIXED set of seeds. Deterministic, so
#     it belongs in the suite: the same programs every time, and any
#     new disagreement is a change someone just made. This is not
#     "fuzzing" — nothing here is random at run time.
#
#  2. Tests of the harness ITSELF. A generator that silently emits
#     nothing, or a reducer that silently never reduces, would report
#     a clean run forever. That failure mode is not hypothetical: the
#     reducer shipped broken the first time, rebuilding the unit name
#     as `uu0` so every candidate called a function that no longer
#     existed — both backends refused it, which reads as "no
#     disagreement", so it appeared to work while shrinking nothing.
#     Hence the assertions below that it CAN find and CAN shrink a
#     disagreement that is planted on purpose.
#
#  Exploratory runs are a separate, longer job:
#      python burnout/difftest.py --runs 200 --seed 9000
# ============================================================
import os
import random
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

import difftest as D          # noqa: E402

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def main():
    print("[13.1] differential testing")

    # ── the generator produces real, varied programs ───────
    print("\n── the generator ──")
    progs = [D.Gen(random.Random(s)).program(3) for s in range(1, 12)]
    check("every program has units and calls",
          all(any(l.startswith('fn u') for l in p) and any(l.endswith('();') for l in p)
              for p in progs))
    check("the same seed gives the same program",
          D.Gen(random.Random(7)).program(3) == D.Gen(random.Random(7)).program(3))
    check("different seeds give different programs",
          len({'\n'.join(p) for p in progs}) == len(progs))

    text = '\n'.join('\n'.join(p) for p in progs)
    # The output must be reproducible, or a disagreement means nothing. These
    # are the natives that would make it not be.
    for bad in ('now_ms', 'monotonic_ms', 'random', 'seed', 'input',
                'http_get', 'http_post', 'read_file', 'write_file', 'exec',
                'spawn', 'await', 'llm', 'agent'):
        check(f"never generates {bad}(", f"{bad}(" not in text)
    check("always prints something", text.count('print(') >= len(progs))

    # ── the harness can see a disagreement, and reduce it ──
    #
    # The BACKEND RUNNER is stubbed here, deliberately. A planted disagreement
    # cannot be written in Cryo — if it could, it would be a real defect and
    # the point is to test the machinery, not the language. Stubbing also means
    # these run with no compiler installed at all, and in a second rather than
    # a minute.
    #
    # The stub disagrees only on programs containing a marker unit, so the
    # reducer has to actually find it among the noise.
    print("\n── the harness itself (backend runner stubbed) ──")
    real_run = D.run_one

    def fake_run(work, src, backend):
        # Keyed on the CALL, not the definition: output can only differ if the
        # unit actually runs. That is what forces the reducer to keep the
        # definition AND its call — keying on the definition would let it
        # delete the call and still "disagree", which no real backend does.
        if 'u1();' not in src:
            return 'ok', 'SAME'
        # A definition-less call would not compile; model that as a refusal so
        # the reducer cannot cheat by deleting the function and keeping the call.
        if 'fn u1(' not in src:
            return 'refused', 'undefined: u1'
        return 'ok', ('DIFFERENT' if backend == 'node' else 'SAME')

    try:
        D.run_one = fake_run
        stub_backends = ('pyro', 'node', 'go')
        work = tempfile.mkdtemp(prefix='cryo_dt_')
        lines = D.Gen(random.Random(3)).program(4)
        rep = D.disagreement(work, lines, stub_backends)
        check("a planted disagreement is detected", rep is not None, repr(rep)[:200])
        check("and the report names each backend's output",
              rep is not None and 'node' in rep and 'DIFFERENT' in rep, repr(rep)[:200])

        small = D.shrink(work, lines, stub_backends)
        check("it is reduced", len(small) < len(lines),
              f"{len(lines)} -> {len(small)} elements")
        check("down to the offending unit and its call",
              any(l.startswith('fn u1(') for l in small) and 'u1();' in small,
              repr(small))
        check("the reduction still disagrees",
              D.disagreement(work, small, stub_backends) is not None)
        # The reducer must not leave units that have nothing to do with it.
        check("unrelated units are gone",
              not any(l.startswith('fn u0(') or l.startswith('fn u2(')
                      for l in small), repr(small))

        # A program every backend agrees on must NOT be reported. A harness
        # that cries wolf is worse than none.
        clean = [l for l in lines if not l.startswith('fn u1(') and l != 'u1();']
        check("an agreeing program is not reported",
              D.disagreement(work, clean, stub_backends) is None)
    finally:
        D.run_one = real_run

    backends = D.available(['pyro', 'node', 'go'])

    # ── the regression corpus ──────────────────────────────
    #
    # Fixed seeds, so this is deterministic. Seeds 106 and 109 are kept
    # deliberately: both found 13.1's first real defect — `abs()` was typed
    # 'number' on go while the emitter produced the int64 helper, so
    # `int b = abs(a) + 1;` did not compile.
    print("\n── regression corpus ──")
    if len(backends) < 2:
        print("  --   skipped (need two backends)")
    else:
        work = tempfile.mkdtemp(prefix='cryo_dt2_')
        for seed in (1, 2, 3, 106, 109, 500, 501):
            lines = D.Gen(random.Random(seed)).program(3)
            rep = D.disagreement(work, lines, backends)
            check(f"seed {seed} agrees on {'/'.join(backends)}",
                  rep is None, (rep or '')[:220])

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
