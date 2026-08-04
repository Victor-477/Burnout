#!/usr/bin/env python3
# ============================================================
#  test_format.py — string format specs (roadmap 11.3)
#
#  Three things are being defended here, in order of how badly
#  they would hurt if they broke:
#
#  1. PARITY. The spec is desugared into ordinary Cryo, so every
#     backend runs the same algorithm — which is only worth
#     anything if it is checked. Every case runs on pyro, go and
#     node and the three outputs must be byte-identical.
#  2. NO SILENT WRONGNESS. A misspelled spec must be an error, not
#     a spec that quietly disappears; a colon that is really a
#     ternary must stay a ternary. Both of those failed silently
#     during development, which is why they are pinned here.
#  3. The exact digits. Rounding, sign placement and zero-padding
#     are the whole point of the feature.
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
PYRO = os.path.join(ROOT, 'Burnout', 'pyro.py')

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def compile_to(src, work, backend, out):
    cf = os.path.join(work, 'prog.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    p = subprocess.run([sys.executable, CRYOC, cf, '--backend', backend,
                        '-o', os.path.join(work, out), '--no-banner'],
                       capture_output=True, text=True, timeout=180,
                       errors='replace')
    return p.returncode, (p.stdout + p.stderr)


def run_pyro(src, work):
    cf = os.path.join(work, 'prog.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    p = subprocess.run([sys.executable, PYRO, 'run', cf],
                       capture_output=True, text=True, timeout=180,
                       errors='replace')
    return p.stdout.replace('\r\n', '\n').strip()


def run_go(src, work):
    rc, log = compile_to(src, work, 'go', 'prog.go')
    if rc != 0:
        return f"<compile failed> {log[-200:]}"
    p = subprocess.run(['go', 'run', 'prog.go'], cwd=work, capture_output=True,
                       text=True, timeout=300, errors='replace')
    return (p.stdout + p.stderr).replace('\r\n', '\n').strip()


def run_node(src, work):
    rc, log = compile_to(src, work, 'node', 'prog.js')
    if rc != 0:
        return f"<compile failed> {log[-200:]}"
    p = subprocess.run(['node', os.path.join(work, 'prog.js')],
                       capture_output=True, text=True, timeout=180,
                       errors='replace')
    return (p.stdout + p.stderr).replace('\r\n', '\n').strip()


# Every case is (label, cryo expression source, expected output). Brackets are
# in the source so trailing padding is visible in a diff.
CASES = [
    # precision
    ('two decimals',      'number x = 3.14159; print("${x:.2f}");',      '3.14'),
    ('zero decimals',     'number x = 3.14159; print("${x:.0f}");',      '3'),
    ('pads the fraction', 'number x = 0.5;     print("${x:.3f}");',      '0.500'),
    ('rounds up',         'number x = 2.345;   print("${x:.2f}");',      '2.35'),
    ('rounds half away',  'number x = 0.125;   print("${x:.2f}");',      '0.13'),
    ('negative rounds',   'number x = 0.0-0.125; print("${x:.2f}");',    '-0.13'),
    ('int through f',     'int n = 7;          print("${n:.2f}");',      '7.00'),
    # grouping
    ('groups thousands',  'number t = 1234567.891; print("${t:,.2f}");', '1,234,567.89'),
    ('groups an int',     'print("${1000000:,d}");',                     '1,000,000'),
    ('no group under 1k', 'print("${999:,d}");',                         '999'),
    ('group boundary',    'print("${1000:,d}");',                        '1,000'),
    ('groups a negative', 'print("${0-1234567:,d}");',                   '-1,234,567'),
    # width and alignment
    ('left aligns text',  'string s = "cryo"; print("[${s:<8}]");',      '[cryo    ]'),
    ('right aligns text', 'string s = "cryo"; print("[${s:>8}]");',      '[    cryo]'),
    ('centres text',      'string s = "cryo"; print("[${s:^8}]");',      '[  cryo  ]'),
    # 3 spare columns split 1 left / 2 right, as Python does
    ('centre extra right', 'string s = "abc"; print("[${s:^6}]");',      '[ abc  ]'),
    ('custom fill',       'string s = "cryo"; print("[${s:.>8}]");',     '[....cryo]'),
    ('numbers right by default',
     'print("[${42:6d}]");',                                             '[    42]'),
    ('width narrower does not cut',
     'print("[${123456:3d}]");',                                         '[123456]'),
    ('width with precision',
     'number x = 3.14159; print("[${x:>10.3f}]");',                      '[     3.142]'),
    # zero padding
    ('zero pads',         'print("${42:05d}");',                         '00042'),
    ('zero pad keeps sign in front',
     'print("${0-42:05d}");',                                            '-0042'),
    ('zero pad a float',  'number x = 1.5; print("${x:07.2f}");',        '0001.50'),
    # percent and truncation
    ('percent',           'print("${0.4567:.1%}");',                     '45.7%'),
    ('percent no decimals', 'print("${0.5:.0%}");',                      '50%'),
    ('truncates a string', 'string s = "cryogenic"; print("${s:.4s}");', 'cryo'),
    ('truncation is a no-op when short',
     'string s = "ab"; print("${s:.4s}");',                              'ab'),
    # composition
    ('two specs in one literal',
     'number x = 2.5; int n = 5; print("${x:.1f}|${n:03d}");',           '2.5|005'),
    ('spec on a call',    'int n = 5; print("${to_number(n):.1f}");',    '5.0'),
    ('spec on arithmetic', 'number v = 2.5; print("${v * 4.0:.2f}");',   '10.00'),
    ('spec on an index',
     'map<string,int> m = {"n": 7}; print("${m[\\"n\\"]:03d}");',        '007'),
    # the colon is not always a spec
    ('a ternary is left alone',
     'int n = 5; print("${n > 3 ? 10 : 2}");',                           '10'),
    ('a colon inside a string key is left alone',
     'map<string,int> m = {"a:b": 1}; print("${m[\\"a:b\\"]}");',        '1'),
    ('plain interpolation is unchanged',
     'number x = 3.14159; print("${x}");',                               '3.14159'),
    # A range inside interpolation needs the sub-parser's synthetic helper to
    # be adopted by the outer parser; it used to be dropped, failing with
    # "unknown function '__cryo_range'". Wrapped in len() on purpose — the
    # three backends render an array three different ways (pyro "[0, 1, 2]",
    # go "[0 1 2]", node "0,1,2"), which is a real parity gap but not this
    # feature's, and asserting on it here would test the wrong thing.
    ('a range inside interpolation still compiles',
     'print("${len(0..3)}");',                                           '3'),
]


def test_values_and_parity(work):
    print("\n── values, and identical output on pyro / go / node ──")
    for label, src, expected in CASES:
        got = run_pyro(src, work)
        check(f"{label}: {expected!r}", got == expected, f"pyro gave {got!r}")
    print("\n  (go and node run the same cases — parity is the invariant)")
    for label, src, expected in CASES:
        g, n = run_go(src, work), run_node(src, work)
        check(f"{label}: go == node == pyro", g == expected and n == expected,
              f"go={g!r} node={n!r} expected={expected!r}")


# ── a bad spec is an error, never a spec that vanishes ────────
def test_errors(work):
    print("\n── malformed specs are refused ──")
    bad = [
        ('x:>4q',  'unsupported format spec',
         "an unknown type used to compile silently, printing x unformatted"),
        ('x:99z',  'unsupported format spec', "unknown type after a width"),
        ('x:.2',   'needs a type',
         "'.2' is ambiguous without a type: 2 decimals or 2 characters"),
        ('x:,s',   'does not apply to a string', "',' groups digits"),
        ('x:.3d',  'does not apply to an integer', "'d' takes no precision"),
        ('x y',    'leftover input',
         "a fragment that is not one expression used to compile as just 'x'"),
    ]
    for frag, needle, why in bad:
        src = 'number x = 1.5;\nprint("${%s}");\n' % frag
        rc, log = compile_to(src, work, 'pyro', 'bad.pyro')
        check(f"'${{{frag}}}' is refused ({why})",
              rc != 0 and needle in log, f"rc={rc} log={log[-180:]}")


# ── the C backend ────────────────────────────────────────────
# C has no repeat()/pad_start()/starts_with(), so a spec with a WIDTH cannot
# work there — but it must REFUSE, not miscompile. Precision and grouping avoid
# those builtins deliberately, so they do work.
def test_c_backend(work):
    print("\n── C backend: precision works, width refuses ──")
    src = ('number x = 3.14159;\nnumber t = 1234567.891;\n'
           'print("${x:.2f}");\nprint("${t:,.2f}");\n')
    # The work directory is shared by every case in this file, so a binary from
    # an EARLIER case survives here. Without removing it first, this check runs
    # that stale program and reports its output as this one's — which is exactly
    # what happened on a machine with no gcc: no exe was produced, a leftover one
    # was executed, and C was blamed for printing '3'.
    exe = os.path.join(work, 'prog.exe' if sys.platform == 'win32' else 'prog')
    if os.path.isfile(exe):
        os.remove(exe)

    rc, log = compile_to(src, work, 'c', 'prog.c')
    check("precision and grouping compile on C", rc == 0, log[-200:])
    if rc == 0:
        if os.path.isfile(exe):
            p = subprocess.run([exe], capture_output=True, text=True,
                               timeout=120, errors='replace')
            out = p.stdout.replace('\r\n', '\n').strip()
            check("C agrees with pyro on the digits",
                  out == '3.14\n1,234,567.89', f"C gave {out!r}")

    rc, log = compile_to('print("${42:>8d}");\n', work, 'c', 'w.c')
    check("a width is refused with a backend suggestion",
          rc != 0 and 'not supported in the C backend' in log
          and '--backend go, node or pyro' in log, log[-200:])


# ── to_number/to_int on the C backend ────────────────────────
# cryo_to_num takes int64_t and cryo_to_int takes double, so an argument of the
# other kind converted silently through the parameter type: to_number(3.75)
# came back 3.0. The format helper takes a `number`, which is how this surfaced.
def test_c_conversions(work):
    print("\n── C: to_number/to_int no longer truncate (regression) ──")
    src = ('number x = 3.75;\nprint(to_number(x));\n'
           'int i = 7;\nprint(to_number(i) / 2.0);\n'
           'print(to_int(2.9));\nprint(to_int(5));\n')
    want = run_pyro(src, work)
    check("pyro baseline", want == '3.75\n3.5\n2\n5', f"got {want!r}")
    rc, log = compile_to(src, work, 'c', 'conv.c')
    check("compiles on C", rc == 0, log[-200:])
    exe = os.path.join(work, 'conv.exe' if sys.platform == 'win32' else 'conv')
    if rc == 0 and os.path.isfile(exe):
        p = subprocess.run([exe], capture_output=True, text=True, timeout=120,
                           errors='replace')
        got = p.stdout.replace('\r\n', '\n').strip()
        check("C matches pyro (to_number of a float keeps its fraction)",
              got == want, f"C gave {got!r}, pyro gave {want!r}")

    # A string argument has no parse helper in the C runtime; it used to read
    # the pointer as a number. Refusing is the honest answer.
    rc, log = compile_to('print(to_int("42"));\n', work, 'c', 's.c')
    check("to_int(string) is refused rather than emitted wrong",
          rc != 0 and 'not supported in the C backend' in log, log[-200:])


# ── the self-hosted compiler ─────────────────────────────────
# It does not implement format specs. It used to DROP them: "${x:.2f}" compiled
# to plain to_string(x), so the two compilers disagreed and neither said so.
def test_selfhost(work):
    print("\n── selfhost refuses a spec instead of dropping it ──")
    vm = os.path.join(ROOT, 'build',
                      'pyrovm.exe' if sys.platform == 'win32' else 'pyrovm')
    pyroc_src = os.path.join(ROOT, 'Cryo', 'selfhost', 'pyroc.cryo')
    if not (os.path.isfile(vm) and os.path.isfile(pyroc_src)):
        print("  skip (no VM or selfhost source)")
        return
    pyroc = os.path.join(work, 'pyroc.pyro')
    rc, log = 0, ''
    p = subprocess.run([sys.executable, CRYOC, pyroc_src, '--backend', 'pyro',
                        '-o', pyroc, '--no-banner'], capture_output=True,
                       text=True, timeout=300, errors='replace')
    if p.returncode != 0 or not os.path.isfile(pyroc):
        check("selfhost compiler builds", False, (p.stdout + p.stderr)[-250:])
        return
    check("selfhost compiler builds", True)

    spec = os.path.join(work, 'spec.cryo')
    with open(spec, 'w', encoding='utf-8') as f:
        f.write('number x = 1.5;\nprint("v=${x:.2f}");\n')
    out = os.path.join(work, 'spec.pyro')
    if os.path.exists(out):
        os.remove(out)
    r = subprocess.run([vm, pyroc, spec, out], capture_output=True, text=True,
                       timeout=180, errors='replace')
    log = r.stdout + r.stderr
    check("a spec is refused with a message naming the reference compiler",
          'Format specs are not implemented' in log and 'cryoc.py' in log,
          log[-250:])
    check("and no artifact is produced", not os.path.isfile(out))

    # Plain interpolation must be untouched by the new guard.
    plain = os.path.join(work, 'plain.cryo')
    with open(plain, 'w', encoding='utf-8') as f:
        f.write('number x = 1.5;\nprint("v=${x}");\n')
    pout = os.path.join(work, 'plain.pyro')
    subprocess.run([vm, pyroc, plain, pout], capture_output=True, text=True,
                   timeout=180, errors='replace')
    if os.path.isfile(pout):
        r = subprocess.run([vm, pout], capture_output=True, text=True,
                           timeout=120, errors='replace')
        check("plain interpolation still compiles and runs",
              r.stdout.strip() == 'v=1.5', r.stdout.strip())
    else:
        check("plain interpolation still compiles and runs", False,
              "no artifact")


def test_escaped_interp(work):
    r"""11.39 - a backslash before $ escapes an interpolation.

    There was no way to write a literal `${...}` in a Cryo string. The lexer
    honoured the escape and turned `\$` into a bare `$`; the parser then
    re-scanned that value for interpolations and found one. Neither pass was
    wrong on its own — the fact that it had been escaped was dropped between
    them, which is why it survived so long.

    Checked on the RUN OUTPUT rather than the generated code: what matters is
    the text the program prints.
    """
    print("\n-- 11.39: escaping an interpolation --")
    BS1 = chr(92)          # one backslash, kept out of the literals
    BSBS = BS1 + BS1       # two, i.e. an escaped backslash in Cryo
    cases = [
        ("a literal interpolation is left alone",
         'print("literal: ' + BS1 + '${notavar}");\n',
         'literal: ${notavar}'),
        ("a real interpolation still works",
         'int x = 7;\nprint("real: ${x}");\n', "real: 7"),
        ("both in one string",
         'int x = 7;\nprint("${x} and ' + BS1 + '${y}");\n',
         '7 and ${y}'),
        ("a bare dollar, no braces",
         'print("money: ' + BS1 + '$5.00");\n', 'money: $5.00'),
        # An escaped BACKSLASH followed by a real interpolation. This is the
        # case a backslash-based marker cannot tell apart from the literal
        # above — both would unescape to a backslash then "${". The sentinel
        # stands in for the "$" itself, so they stay distinct.
        ("an escaped backslash still leaves a live interpolation",
         'int x = 7;\nprint("esc: ' + BSBS + '${x}");\n',
         'esc: ' + BS1 + '7'),
    ]
    for label, src, want in cases:
        out = run_pyro(src, work)
        check(label, out.strip() == want, f"{out.strip()!r} != {want!r}")


def main():
    work = tempfile.mkdtemp(prefix='cryo_fmt_')
    try:
        test_values_and_parity(work)
        test_errors(work)
        test_c_backend(work)
        test_c_conversions(work)
        test_selfhost(work)
        test_escaped_interp(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
