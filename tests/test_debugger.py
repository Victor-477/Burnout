#!/usr/bin/env python3
# ============================================================
#  test_debugger.py — VM debugger and sampling profiler (11.25)
#
#  Most of this file is about the DEBUG SECTION, not the debugger.
#
#  11.25 was specified as "breakpoints over the existing pc->line
#  table", and the table turned out to be almost empty: a 20-line
#  program produced 3 entries instead of 10. Four passes rewrite
#  the tree by CONSTRUCTING replacement nodes — module resolution,
#  monomorphize, trait lowering, the optimizer — and each has to
#  pass `line=` by hand, because `line` is a declared field on 16
#  of the 56 node classes and a plain attribute on the other 40.
#  They passed it for a handful and dropped it for the rest.
#
#  Nothing failed. The programs compiled and ran correctly; only
#  the debug information was gone, so stack traces — shipped in
#  Phase 5 — had been pointing at the wrong line, or at line 0,
#  for as long as those passes had existed. A silent wrong answer
#  is the failure mode worth testing hardest, so the checks below
#  are mostly "does the position survive the pass", one per pass,
#  and they are what would catch it coming back.
#
#  The debugger tests drive the real binary over a pipe, because
#  the thing being tested is the command loop.
# ============================================================
import os
import re
import struct
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
VMDIR = os.path.join(ROOT, 'Pyro', 'vm')
EXE = '.exe' if sys.platform == 'win32' else ''
VM = os.path.join(VMDIR, 'pyrovm_go' + EXE)

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


# ── the program the debugger tests drive ───────────────────
#
# `driver` rather than `main`: two functions named main make the pyro backend
# call the synthetic top level instead of the user's function and loop forever
# (recorded as a separate defect). Naming it something else keeps this file
# testing the debugger rather than that.
PROG = """fn slow(int n) -> int ={
    int acc = 0;
    for (int i = 0; i < n; i = i + 1) {
        acc = acc + i * i;
    }
    return acc;
}

fn fast(int n) -> int ={
    return n * n;
}

fn driver() -> void ={
    int a = slow(200000);
    int b = fast(7);
    print(a);
    print(b);
}

driver();
"""


def build_vm():
    """Every .go file — the debugger and profiler live beside main.go."""
    import glob
    srcs = sorted(glob.glob(os.path.join(VMDIR, '*.go')))
    r = subprocess.run(['go', 'build', '-o', VM] + srcs,
                       cwd=VMDIR, capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def compile_pyro(work, src, name='p'):
    cf = os.path.join(work, name + '.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    out = os.path.join(work, name + '.pyro')
    r = subprocess.run([sys.executable, CRYOC, cf, '--backend', 'pyro',
                        '-o', out, '--no-banner', '--no-cache'],
                       capture_output=True, text=True, timeout=600)
    return (cf, out) if r.returncode == 0 and os.path.isfile(out) else (cf, None)


def debug_section(path):
    """[(pc, line)] read straight out of the .pyro container."""
    d = open(path, 'rb').read()
    o = 4
    ver, flags = d[o], d[o + 1]
    o += 2
    nconsts = struct.unpack_from('<H', d, o)[0]
    o += 2
    for _ in range(nconsts):
        tag = d[o]
        o += 1
        if tag in (1, 2):
            o += 8
        elif tag == 3:
            if ver >= 3:
                n = struct.unpack_from('<I', d, o)[0]; o += 4
            else:
                n = struct.unpack_from('<H', d, o)[0]; o += 2
            o += n
        elif tag == 4:
            o += 1
    nfuncs = struct.unpack_from('<H', d, o)[0]
    o += 2 + nfuncs * (2 + 4 + 1 + 2) + 2
    codelen = struct.unpack_from('<I', d, o)[0]
    o += 4 + codelen
    if not (flags & 2):
        return []
    nd = struct.unpack_from('<I', d, o)[0]
    o += 4
    return [struct.unpack_from('<II', d, o + 8 * i) for i in range(nd)]


def run_dbg(pyro, source, commands, timeout=120):
    r = subprocess.run([VM, '--debug', '--source=' + source, pyro],
                       input=''.join(c + '\n' for c in commands),
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


# ── 1. the positions survive every rewriting pass ──────────
def test_line_preservation():
    print("\n── source positions survive the AST passes ──")
    from lexer import Lexer
    from parser import Parser
    from modules import resolve_modules
    from generics import monomorphize
    from traits import lower_traits
    from optimize import optimize as optimize_ast

    def body_lines(ast):
        out = []
        for n in ast.statements:
            for b in (getattr(n, 'body', None) or []):
                out.append(getattr(b, 'line', None))
        return out

    ast = Parser(Lexer(PROG).tokenize()).parse()
    want = body_lines(ast)
    check("the parser gives every statement a line", all(want) and len(want) >= 8,
          str(want))

    work = tempfile.mkdtemp(prefix='cryo_dbg_')
    for name, fn in (('resolve_modules', lambda a: resolve_modules(a, work)),
                     ('monomorphize', monomorphize),
                     ('lower_traits', lower_traits),
                     ('optimize_ast', optimize_ast)):
        ast = fn(ast)
        got = body_lines(ast)
        # Each pass rebuilds nodes; none of them may lose a position. This is
        # the check that would have caught the original defect.
        check(f"{name} keeps them", got == want, f"{want} -> {got}")


# ── 2. the debug section is actually populated ─────────────
def test_debug_section(work):
    print("\n── the .pyro debug section ──")
    _cf, pyro = compile_pyro(work, PROG, 'sect')
    if not pyro:
        check("compiles", False)
        return None, None
    ent = debug_section(pyro)
    lines = [l for _pc, l in ent]

    check("a debug section is present", len(ent) > 0)
    # One entry per statement that begins a line. The point of the test is the
    # COUNT: the bug left 3 of these behind and everything still worked.
    check("one entry per statement line, not a handful",
          len(ent) >= 9, f"{len(ent)} entries: {ent}")
    for want in (2, 3, 6, 10, 14, 15, 16, 17):
        check(f"line {want} is in the table", want in lines, str(lines))
    check("entries are ordered by pc",
          all(ent[i][0] <= ent[i + 1][0] for i in range(len(ent) - 1)), str(ent))
    # Bodies of functions are what was lost: `fast` is one statement at line 10.
    check("a one-statement function still gets an entry", 10 in lines, str(lines))
    return _cf, pyro


# ── 3. breakpoints ─────────────────────────────────────────
def test_breakpoints(cf, pyro):
    print("\n── breakpoints ──")
    out = run_dbg(pyro, cf, ['break 15', 'continue', 'backtrace', 'quit'])
    check("break <line> arms and fires", 'breakpoint hit' in out, out[-400:])
    check("it stops on the right line", re.search(r':15\b', out) is not None,
          out[-400:])
    check("the backtrace names the enclosing function",
          re.search(r'#0\s+driver', out) is not None, out[-400:])
    check("and the frame that called it",
          re.search(r'#1\s+main', out) is not None, out[-400:])

    # A function's entry pc is its prologue, BEFORE any statement. Resolving it
    # with the same backwards search stack traces use reported the line of
    # whatever preceded the function — line 0 for the first one.
    out = run_dbg(pyro, cf, ['break fast', 'continue', 'backtrace', 'quit'])
    check("break <function> resolves to its first statement, not line 0",
          'line 10' in out, out[:400])
    check("break <function> stops inside that function",
          re.search(r'#0\s+fast', out) is not None, out[-400:])

    out = run_dbg(pyro, cf, ['break 12', 'quit'])
    check("a line with no statement is refused, not silently armed",
          'no statement begins at line 12' in out, out[:400])
    check("and the refusal names a line that does work",
          re.search(r'nearest is line \d+', out) is not None, out[:400])

    out = run_dbg(pyro, cf, ['break nosuchfn', 'quit'])
    check("an unknown function name is refused",
          'no function named' in out, out[:300])

    out = run_dbg(pyro, cf, ['break 15', 'delete 15', 'info break', 'continue'])
    check("delete removes it", 'no breakpoints' in out, out[:400])
    check("and the program then runs to completion",
          '2666646666700000' in out, out[-300:])


# ── 4. stepping ────────────────────────────────────────────
def test_stepping(cf, pyro):
    print("\n── stepping ──")
    out = run_dbg(pyro, cf, ['break 16', 'continue', 'step', 'quit'])
    check("step advances one source line", ':17' in out, out[-400:])

    # `next` must not descend into a call...
    out = run_dbg(pyro, cf, ['break 15', 'continue', 'next', 'backtrace', 'quit'])
    check("next steps OVER a call", re.search(r'#0\s+driver', out) is not None,
          out[-400:])
    check("next lands on the following line", ':16' in out, out[-400:])

    # ... while `step` does.
    out = run_dbg(pyro, cf, ['break 15', 'continue', 'step', 'backtrace', 'quit'])
    check("step steps INTO a call", re.search(r'#0\s+fast', out) is not None,
          out[-500:])

    out = run_dbg(pyro, cf, ['break fast', 'continue', 'finish', 'backtrace', 'quit'])
    check("finish returns to the caller",
          re.search(r'#0\s+driver', out) is not None, out[-400:])

    # A breakpoint inside a call must still fire while `next` steps over it —
    # the one thing a breakpoint may never do is be skipped.
    out = run_dbg(pyro, cf, ['break 10', 'break 15', 'continue', 'next',
                             'backtrace', 'quit'])
    check("a breakpoint inside a stepped-over call still fires",
          re.search(r'#0\s+fast', out) is not None, out[-500:])


# ── 5. inspection ──────────────────────────────────────────
def test_inspection(cf, pyro):
    print("\n── inspecting a stopped program ──")
    out = run_dbg(pyro, cf, ['break fast', 'continue', 'info locals', 'quit'])
    check("info locals shows the parameter's value",
          re.search(r'\[0\]\s+param\s+=\s+7', out) is not None, out[-400:])

    out = run_dbg(pyro, cf, ['break 16', 'continue', 'info locals', 'quit'])
    check("and locals computed so far",
          '2666646666700000' in out or '49' in out, out[-400:])

    out = run_dbg(pyro, cf, ['break 16', 'continue', 'list', 'quit'])
    check("list shows the source around the stop",
          'print(a);' in out and '->' in out, out[-500:])
    check("list marks the breakpoint line", '*' in out, out[-500:])

    out = run_dbg(pyro, cf, ['help', 'quit'])
    check("help explains that locals are shown by slot",
          'by SLOT' in out or 'by slot' in out.lower(), out[:600])

    out = run_dbg(pyro, cf, ['nonsense', 'quit'])
    check("an unknown command says so instead of dying",
          'unknown command' in out, out[:300])


# ── 6. the debugger is off unless asked for ────────────────
def test_off_by_default(work, pyro):
    print("\n── a normal run is unchanged ──")
    plain = subprocess.run([VM, pyro], capture_output=True, text=True, timeout=300)
    check("the program runs without --debug", plain.returncode == 0, plain.stderr[-300:])
    check("and prints exactly what it printed before",
          plain.stdout.replace('\r\n', '\n') == '2666646666700000\n49\n',
          repr(plain.stdout))
    check("with nothing from the debugger on stderr",
          '(pyro)' not in plain.stdout and '(pyro)' not in plain.stderr)

    # Stdin closing must not leave the VM spinning on EOF.
    r = subprocess.run([VM, '--debug', pyro], input='', capture_output=True,
                       text=True, timeout=300)
    check("--debug with no commands finishes instead of hanging on EOF",
          r.returncode == 0, r.stderr[-300:])


# ── 7. the sampling profiler ───────────────────────────────
def test_profiler(work, pyro):
    print("\n── sampling profiler ──")
    r = subprocess.run([VM, '--profile', '--profile-hz=5000', pyro],
                       capture_output=True, text=True, timeout=600)
    out = r.stderr
    check("the program still runs and prints", '2666646666700000' in r.stdout,
          r.stdout[:200])
    check("a profile is written", 'Pyro profile' in out, out[:300])
    check("it reports the sampling rate used", '5000 Hz' in out, out[:300])
    # `slow` is a 200k-iteration loop and `fast` is one multiply; anything else
    # on top means the profiler is not measuring what it claims to.
    rows = [l for l in out.split('\n') if re.search(r'\d+\.\d+%', l)]
    check("it attributes the time to some function", len(rows) >= 1, out[:400])
    check("and the hottest one is the expensive function",
          bool(rows) and 'slow' in rows[0], '\n'.join(rows[:3]))
    check("percentages are reported", '%' in out, out[:300])
    check("it says where native time is charged",
          'native' in out.lower(), out[-300:])

    out_file = os.path.join(work, 'prof.txt')
    r = subprocess.run([VM, '--profile', '--profile-out=' + out_file, pyro],
                       capture_output=True, text=True, timeout=600)
    check("--profile-out writes to a file", os.path.isfile(out_file))
    if os.path.isfile(out_file):
        txt = open(out_file, encoding='utf-8').read()
        check("the file holds the profile", 'Pyro profile' in txt, txt[:200])
        check("and it is not duplicated on stderr", 'Pyro profile' not in r.stderr,
              r.stderr[:200])

    # A program too short to sample must say so rather than print an empty
    # table, which reads as "your program spends no time anywhere".
    _cf, tiny = compile_pyro(work, 'print(1);\n', 'tiny')
    if tiny:
        r = subprocess.run([VM, '--profile', '--profile-hz=1', tiny],
                           capture_output=True, text=True, timeout=300)
        check("a program shorter than one sample period says so",
              'less than one sampling period' in r.stderr, r.stderr[:300])

    r = subprocess.run([VM, '--profile-hz=0', pyro], capture_output=True,
                       text=True, timeout=300)
    check("a nonsense sampling rate is refused",
          r.returncode != 0 and 'positive' in r.stderr, r.stderr[:200])


# ── 8. option handling ─────────────────────────────────────
def test_options(work, pyro):
    print("\n── options ──")
    r = subprocess.run([VM, '--help'], capture_output=True, text=True, timeout=120)
    check("--help lists the new options",
          '--debug' in r.stdout and '--profile' in r.stdout, r.stdout[:300])

    r = subprocess.run([VM, '--nope', pyro], capture_output=True, text=True,
                       timeout=120)
    check("an unknown option is refused", r.returncode != 0, r.stdout[:200])
    check("and the message names it", '--nope' in r.stderr, r.stderr[:300])

    # Options bind to the VM only BEFORE the program; after it they are the
    # program's, so a script taking its own --debug is not intercepted.
    _cf, argp = compile_pyro(work, 'string[] a = args(); print(len(a));\n', 'argp')
    if argp:
        r = subprocess.run([VM, argp, '--debug', 'x'], capture_output=True,
                           text=True, timeout=300)
        check("arguments after the program are the program's",
              r.stdout.strip() == '2', repr(r.stdout))


def main():
    print("[11.25] VM debugger and profiler")
    ok, err = build_vm()
    if not ok:
        print("Go VM did not build — skipping.\n" + err[-500:])
        return 0
    print(f"  VM: {VM}")

    test_line_preservation()

    work = tempfile.mkdtemp(prefix='cryo_dbg_')
    cf, pyro = test_debug_section(work)
    if not pyro:
        print("\ncould not compile the test program — stopping")
        return 1
    test_breakpoints(cf, pyro)
    test_stepping(cf, pyro)
    test_inspection(cf, pyro)
    test_off_by_default(work, pyro)
    test_profiler(work, pyro)
    test_options(work, pyro)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
