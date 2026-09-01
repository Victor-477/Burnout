#!/usr/bin/env python3
# ============================================================
#  test_bench_report.py — bench_vm.py --save rewrites in place
#
#  This exists because of one incident. `--save` built the whole
#  report and opened it with 'w', so every run TRUNCATED the file:
#  BENCHMARKS.md went from 452 lines to 34, and the entire 13.4
#  operand-stack write-up — the A/A control, both A/B orientations,
#  the GC finding that killed the first version — went with it.
#
#  It was recoverable that time only because the file had been
#  copied first, and it had been copied because the file itself
#  carried a paragraph warning about it. A warning that has to be
#  read before every run is not a safeguard, it is a rehearsal for
#  losing the file.
#
#  So the property under test is not "the table is correct". It is:
#
#    * everything OUTSIDE the markers survives, byte for byte;
#    * everything INSIDE them is replaced;
#    * a report with no markers is NOT rewritten — refusing is the
#      recoverable outcome, and guessing where the region "would
#      have been" is how you delete prose silently.
#
#  The generator is called directly rather than through the CLI:
#  running --save for real needs a Go toolchain and half a minute
#  of benchmarking, and none of that is what broke.
# ============================================================
import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
import bench_vm as B                                  # noqa: E402

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


# Rows are (name, size, go_seconds, c_seconds) — what run() collects.
ROWS_A = [('arith', 182, 0.261, None), ('calls', 199, 0.044, None)]
ROWS_B = [('arith', 182, 0.232, None), ('calls', 199, 0.040, None)]

PROSE_ABOVE = "# Pyro VM benchmarks\n\nIntro prose nobody generated.\n"
PROSE_BELOW = (
    "\n## Analysis written by hand\n\n"
    "The A/A control came back at +0.73%, so the -11.0% is a result.\n\n"
    "| orientation | min |\n|---|---:|\n| old in A | -11.0% |\n"
    "\n### A second table, to catch a generator that guesses\n\n"
    "| Program | .pyro | Go VM | C VM | go/c |\n|---|---:|---:|---:|---:|\n"
    "| `arith` | 182 B | 999 ms | — | — |\n")


def _report(body):
    return PROSE_ABOVE + body + PROSE_BELOW


def _marked(rows):
    return (B._BEGIN + "\n" + "\n".join(B._generated_block(rows)) + "\n"
            + B._END + "\n")


def with_report(text):
    """Point bench_vm at a throwaway report and give back its path."""
    d = tempfile.mkdtemp(prefix='cryo_bench_rep_')
    p = os.path.join(d, 'BENCHMARKS.md')
    if text is not None:
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
    B.REPORT = p
    return d, p


def main():
    _orig = B.REPORT
    try:
        print("\n── the prose survives a rewrite ──")
        d, p = with_report(_report(_marked(ROWS_A)))
        try:
            ok, msg = B._write_report(ROWS_B)
            out = open(p, encoding='utf-8').read()
            check("--save reports success", ok, msg)
            check("the prose ABOVE the markers is untouched",
                  out.startswith(PROSE_ABOVE), out[:120])
            check("the prose BELOW the markers is untouched",
                  PROSE_BELOW in out, out[-200:])
            check("the hand-written analysis is still there",
                  '-11.0%' in out and 'A/A control' in out, out[-300:])
            check("the new numbers are in", '232 ms' in out, out[:600])
            check("the old numbers are gone", '261 ms' not in out, out[:600])
            check("a second table elsewhere is not touched",
                  '999 ms' in out, out[-300:])
            check("the markers are still there for next time",
                  out.count(B._BEGIN_KEY) == 1 and out.count(B._END_KEY) == 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)

        print("\n── rewriting twice is stable ──")
        d, p = with_report(_report(_marked(ROWS_A)))
        try:
            B._write_report(ROWS_B)
            once = open(p, encoding='utf-8').read()
            B._write_report(ROWS_B)
            twice = open(p, encoding='utf-8').read()
            # A splice that drifts by a line each run eats the file slowly
            # instead of all at once, which is harder to notice, not easier.
            check("the same rows produce the same file", once == twice,
                  f"{len(once)} then {len(twice)} chars")
        finally:
            shutil.rmtree(d, ignore_errors=True)

        print("\n── the file's line endings survive ──")
        # Caught on the first real run: reading with universal newlines and
        # writing with the default translation turned an LF report into CRLF,
        # so all 493 lines showed as changed. A rewrite you cannot read the
        # diff of does not do the job the rewrite was for.
        for label, nl in (("LF", "\n"), ("CRLF", "\r\n")):
            text = _report(_marked(ROWS_A)).replace("\n", nl)
            d, p = with_report(None)
            try:
                with open(p, 'w', encoding='utf-8', newline='') as f:
                    f.write(text)
                B._write_report(ROWS_B)
                raw = open(p, 'rb').read()
                crlf = raw.count(b'\r\n')
                bare = raw.count(b'\n') - crlf
                check(f"{label} in, {label} out",
                      (crlf == 0 and bare > 0) if nl == "\n"
                      else (bare == 0 and crlf > 0),
                      f"crlf={crlf} bare-lf={bare}")
            finally:
                shutil.rmtree(d, ignore_errors=True)

        print("\n── an unmarked report is refused, not truncated ──")
        original = _report("- machine: something\n\n| Program |\n|---|\n")
        d, p = with_report(original)
        try:
            ok, msg = B._write_report(ROWS_B)
            after = open(p, encoding='utf-8').read()
            check("it refuses", not ok, msg)
            check("the file is byte-for-byte unchanged", after == original,
                  f"{len(original)} -> {len(after)} chars")
            check("and the message says how to fix it",
                  'marker' in msg and B._BEGIN in msg, msg)
        finally:
            shutil.rmtree(d, ignore_errors=True)

        print("\n── half a marker pair is refused too ──")
        for label, body in (("begin only", B._BEGIN + "\n- machine: x\n"),
                            ("end only", "- machine: x\n" + B._END + "\n")):
            original = _report(body)
            d, p = with_report(original)
            try:
                ok, _ = B._write_report(ROWS_B)
                check(f"{label}: refused and unchanged",
                      not ok and open(p, encoding='utf-8').read() == original)
            finally:
                shutil.rmtree(d, ignore_errors=True)

        print("\n── no report yet: write the whole scaffold ──")
        d, p = with_report(None)
        try:
            ok, _ = B._write_report(ROWS_B)
            out = open(p, encoding='utf-8').read()
            check("a fresh report is written", ok and os.path.isfile(p))
            check("with the numbers", '232 ms' in out, out[:400])
            # Without these the NEXT --save would refuse, which would make a
            # first run produce a file the tool cannot update.
            check("and with the markers, so the next run can rewrite it",
                  B._BEGIN_KEY in out and B._END_KEY in out, out[:400])
            check("the scaffold round-trips: rewriting it again works",
                  B._write_report(ROWS_A)[0]
                  and '261 ms' in open(p, encoding='utf-8').read())
        finally:
            shutil.rmtree(d, ignore_errors=True)
    finally:
        B.REPORT = _orig

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
