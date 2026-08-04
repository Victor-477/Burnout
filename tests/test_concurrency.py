#!/usr/bin/env python3
# ============================================================
#  test_concurrency.py — spawn/await in the VM (roadmap 12.5)
#
#  Three separate claims, and they need different evidence:
#
#  1. THE VALUES ARE RIGHT, and agree with the go backend. Plain
#     comparison of output.
#  2. THE WORK ACTUALLY OVERLAPS. This one cannot be shown by
#     output at all — a scheduler that ran each task to
#     completion at its await would print exactly the same
#     numbers. So it is measured: N tasks that sleep must cost
#     about ONE sleep, not N. Without this test "concurrency"
#     would be indistinguishable from deferred evaluation, and
#     calling that concurrency would be a lie.
#  3. FAILURE IS CLEAN. A deadlock must be reported and named,
#     not hung on — a hang is the worst possible outcome, and
#     the one a scheduler makes newly reachable.
#
#  Where pyro and go DISAGREE is recorded here too rather than
#  hidden, because the disagreements are the interesting part.
#  Awaiting twice WAS one of them and is now fixed on the go side
#  (12.11); the ordering of interleaved output remains, and is
#  not fixable — go's order is a race, so there is nothing stable
#  there to agree with.
# ============================================================
import os
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
WORK = tempfile.mkdtemp(prefix='cryo_conc_')

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def build(src, backend, name):
    """Compile to an artifact, returning (rc, log, artifact_path)."""
    cf = os.path.join(WORK, name + '.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    ext = {'pyro': '.pyro', 'go': '.go', 'node': '.js', 'c': '.c'}[backend]
    out = os.path.join(WORK, name + ext)
    p = subprocess.run([sys.executable, CRYOC, cf, '--backend', backend,
                        '-o', out, '--no-banner'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=300)
    return p.returncode, (p.stdout or '') + (p.stderr or ''), out


def run(src, backend, name):
    """Compile and run, returning (rc, stdout-lines, combined-log)."""
    cf = os.path.join(WORK, name + '.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    p = subprocess.run([sys.executable, CRYOC, cf, '--backend', backend,
                        '--run', '--no-banner'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=300)
    lines = [l.strip() for l in (p.stdout or '').splitlines()
             if l.strip() and not l.lstrip().startswith(('✓', '→', '─'))]
    return p.returncode, lines, (p.stdout or '') + (p.stderr or '')


# ── the programs ────────────────────────────────────────────
TWO = ('fn t(int n) -> int ={ return n * n; }\n'
       'future<int> a = spawn t(6);\n'
       'future<int> b = spawn t(7);\n'
       'print(await a + await b);\n')

FANOUT = ('fn t(int n) -> int ={ sleep(SLEEPMS); return n * n; }\n'
          'int[] ids = [1, 2, 3, 4, 5];\n'
          'future<int>[] pend = [];\n'
          'for (int id in ids) { pend.push(spawn t(id)); }\n'
          'int sum = 0;\n'
          'for (future<int> f in pend) { sum += await f; }\n'
          'print(sum);\n')

SEQ = ('fn t(int n) -> int ={ sleep(SLEEPMS); return n * n; }\n'
       'int[] ids = [1, 2, 3, 4, 5];\n'
       'int sum = 0;\n'
       'for (int id in ids) { sum += t(id); }\n'
       'print(sum);\n')


def main():
    print("[12.5] concurrency in the VM")

    # ── values, and agreement with go ──────────────────────
    print("\n── the values are right, and go agrees ──")
    rc, out, log = run(TWO, 'pyro', 'two')
    check("two spawned tasks give 36 + 49", rc == 0 and '85' in out, log[-400:])
    rc_g, out_g, log_g = run(TWO, 'go', 'two_go')
    check("go gives the same", rc_g == 0 and out_g[:1] == out[:1],
          f"pyro={out[:1]} go={out_g[:1]}")

    rc, out, log = run(FANOUT.replace('SLEEPMS', '1'), 'pyro', 'fan')
    check("fan-out of 5 gives 55", rc == 0 and '55' in out, log[-400:])
    rc_g, out_g, _ = run(FANOUT.replace('SLEEPMS', '1'), 'go', 'fan_go')
    check("go gives the same", out_g[:1] == out[:1], f"pyro={out[:1]} go={out_g[:1]}")

    # spawn over an arbitrary expression, with capture. `spawn` binds a UNARY,
    # so `spawn base * 2` is `(spawn base) * 2` on every backend — the
    # parenthesised form is the one that means what it looks like.
    rc, out, log = run('int base = 10;\nfuture<int> f = spawn (base * 2);\n'
                       'print(await f);\n', 'pyro', 'cap')
    check("spawn over an expression captures the enclosing local",
          rc == 0 and '20' in out, log[-400:])

    rc, out, log = run('fn dbl(int n) -> int ={ return n * 2; }\nint base = 10;\n'
                       'future<int> f = spawn dbl(base);\nprint(await f);\n',
                       'pyro', 'cap2')
    check("and an argument is evaluated in the spawner",
          rc == 0 and '20' in out, log[-400:])

    rc, out, log = run('fn r(future<int> f) -> int ={ return await f; }\n'
                       'future<int> a = spawn (7);\nprint(r(a));\n', 'pyro', 'passfut')
    check("a future can be passed to a function and awaited there",
          rc == 0 and '7' in out, log[-400:])

    # ── 12.11: a future holds its value ────────────────────
    #
    # This was the first divergence 12.5 turned up, and it is now fixed on the
    # go side rather than papered over. `await f` twice used to kill a go binary
    # with "all goroutines are asleep" — the future WAS a buffered channel and
    # the single value had already been taken — while pyro printed it twice. A
    # crash on one backend and a result on the other is invariant 1 broken, and
    # pyro had the defensible reading: a future is a value you can look at, not
    # a queue you drain.
    print("\n── a future can be awaited more than once ──")
    TWICE = ('fn t() -> int ={ return 5; }\n'
             'future<int> f = spawn t();\nprint(await f);\nprint(await f);\n')
    for b in ('pyro', 'go'):
        rc, out, log = run(TWICE, b, 'twice_' + b)
        check(f"{b}: awaiting the same future twice gives the value twice",
              rc == 0 and out[:2] == ['5', '5'], f"rc={rc} out={out[:3]} {log[-300:]}")
        check(f"{b}: and does not deadlock",
              'deadlock' not in log and 'asleep' not in log, log[-300:])

    # Every future in a fan-out awaited twice: 55 collected twice.
    DOUBLE_FAN = ('fn t(int n) -> int ={ return n * n; }\n'
                  'int[] ids = [1, 2, 3, 4, 5];\n'
                  'future<int>[] pend = [];\n'
                  'for (int id in ids) { pend.push(spawn t(id)); }\n'
                  'int sum = 0;\n'
                  'for (future<int> f in pend) { sum += await f; }\n'
                  'for (future<int> f in pend) { sum += await f; }\n'
                  'print(sum);\n')
    outs = {}
    for b in ('pyro', 'go'):
        rc, out, log = run(DOUBLE_FAN, b, 'dblfan_' + b)
        outs[b] = out[:1]
        check(f"{b}: a whole fan-out collected twice gives 110",
              rc == 0 and '110' in out, f"rc={rc} out={out[:2]} {log[-300:]}")
    check("and the two backends agree on it", outs['pyro'] == outs['go'], repr(outs))

    # ── the part output cannot prove ───────────────────────
    #
    # A scheduler that simply ran each task to completion at its await would
    # print 55 too. The difference is only visible in TIME.
    print("\n── the work actually overlaps ──")
    ms = 200
    _rc, _l, seq_art = build(SEQ.replace('SLEEPMS', str(ms)), 'pyro', 'seqp')
    _rc2, _l2, con_art = build(FANOUT.replace('SLEEPMS', str(ms)), 'pyro', 'conp')
    vm = os.path.join(ROOT, 'build', 'pyrovm.exe' if sys.platform == 'win32' else 'pyrovm')
    if not os.path.isfile(vm):
        print("  --   VM binary not built; timing skipped")
    else:
        def best_of(art, n=3):
            b = 9e9
            for _ in range(n):
                t0 = time.perf_counter()
                p = subprocess.run([vm, art], capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=300)
                b = min(b, time.perf_counter() - t0)
            return b * 1000, (p.stdout or '').strip()

        seq_ms, seq_out = best_of(seq_art)
        con_ms, con_out = best_of(con_art)
        check("both forms give the same answer", seq_out == con_out == '55',
              f"seq={seq_out!r} conc={con_out!r}")
        # 5 x 200ms. Sequential must be near 1000ms, concurrent near 200ms.
        # The thresholds are loose because this is wall-clock on a shared
        # machine; the CLAIM is an order-of-magnitude gap, not a precise number.
        check(f"sequential costs about 5 sleeps ({seq_ms:.0f}ms)",
              seq_ms > 3.5 * ms, f"{seq_ms:.0f}ms")
        check(f"concurrent costs about ONE sleep ({con_ms:.0f}ms)",
              con_ms < 2.0 * ms, f"{con_ms:.0f}ms")
        check("so the tasks genuinely overlap, not merely defer",
              con_ms < seq_ms / 2, f"seq={seq_ms:.0f}ms conc={con_ms:.0f}ms")

    # With only one task alive there is nobody to yield to, so sleep must stay
    # an ordinary sleep rather than becoming a no-op.
    _rc, _l, solo = build('sleep(300);\nprint("done");\n', 'pyro', 'solo')
    if os.path.isfile(vm):
        t0 = time.perf_counter()
        p = subprocess.run([vm, solo], capture_output=True, text=True, timeout=120)
        el = (time.perf_counter() - t0) * 1000
        check("a lone sleep still sleeps", el > 250 and 'done' in (p.stdout or ''),
              f"{el:.0f}ms")

    # ── ordering ───────────────────────────────────────────
    print("\n── the schedule is deterministic ──")
    ORDER = ('fn t(int n) -> int ={ print(n); sleep(10); print(n * 10); return n; }\n'
             'future<int> a = spawn t(1);\n'
             'future<int> b = spawn t(2);\n'
             'print(await a + await b);\n')
    firsts = set()
    for i in range(3):
        rc, out, _ = run(ORDER, 'pyro', f'ord{i}')
        firsts.add(tuple(out))
    check("the same program prints the same thing every run",
          len(firsts) == 1, repr(firsts))
    seq = list(firsts)[0]
    check("tasks start in spawn order", seq[:2] == ('1', '2'), repr(seq))
    check("and both resume before either finishes — they interleave",
          seq[2:4] == ('10', '20'), repr(seq))

    # A spawned task that nobody awaits and nobody yields to never runs here.
    #
    # This is where pyro and go genuinely disagree, and it is not pyro's
    # doing: on go the program is a race between the goroutine and
    # main returning, so it prints "main" on one run and "main"/"ran" on the
    # next — both were observed while writing this. So the assertion is that
    # pyro is CONSISTENT, not that the two agree: there is nothing stable on
    # the go side to agree with.
    NEVER = ('fn t() -> int ={ print("ran"); return 1; }\n'
             'future<int> f = spawn t();\nprint("main");\n')
    runs = {tuple(run(NEVER, 'pyro', f'never{i}')[1]) for i in range(3)}
    check("an un-awaited task does not run, on every run",
          runs == {('main',)}, repr(runs))

    # ── failing cleanly ────────────────────────────────────
    print("\n── failure is reported, never hung on ──")
    CYCLE = ('future<int> g1 = spawn (0);\n'
             'future<int> g2 = spawn (0);\n'
             'fn one() -> int ={ return await g2; }\n'
             'fn two() -> int ={ return await g1; }\n'
             'g1 = spawn one();\n'
             'g2 = spawn two();\n'
             'print(await g1);\n')
    rc, out, log = run(CYCLE, 'pyro', 'cycle')
    check("an await cycle is detected instead of hanging",
          'deadlock' in log, log[-400:])
    check("and the message names the tasks in the cycle",
          'is awaiting task' in log, log[-400:])

    rc, out, log = run('fn t(int z) -> int ={ return 10 / z; }\n'
                       'future<int> f = spawn t(0);\nprint(await f);\n', 'pyro', 'abort')
    check("an abort inside a task ends the program",
          rc != 0 or 'DivByZero' in log, log[-300:])

    # ── the other backends ─────────────────────────────────
    print("\n── the backends that refuse, refuse clearly ──")
    for b, needle in (('node', 'not supported in the node backend'),
                      ('c', 'not supported in the C backend')):
        rc, log, _ = build(TWO, b, 'refuse_' + b)
        check(f"{b} refuses spawn/await", rc != 0 and needle in log, log[-300:])
        # A suggestion pointing at a backend that also refuses is worse than none.
        check(f"{b} suggests go or pyro, and not itself",
              'go or pyro' in log, log[-300:])

    # ── the selector ───────────────────────────────────────
    print("\n── --backend auto ──")
    sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
    from lexer import Lexer          # noqa: E402
    from parser import Parser        # noqa: E402
    from backends import select_backend, missing_capabilities   # noqa: E402
    ast = Parser(Lexer(TWO).tokenize()).parse()
    picked, _why = select_backend(ast)
    check("auto picks pyro for a concurrent program now that it runs there",
          picked == 'pyro', repr(picked))
    miss, _f = missing_capabilities(ast, 'pyro')
    check("and pyro reports no missing capability for it", not miss, repr(miss))
    miss_n, _f = missing_capabilities(ast, 'node')
    check("while node still reports concurrency as missing",
          'concurrency' in miss_n, repr(miss_n))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
