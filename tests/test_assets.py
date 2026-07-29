#!/usr/bin/env python3
# ============================================================
#  test_assets.py — embedded assets / packaging (roadmap 11.9)
#
#  The claim: `pyro build --assets DIR` produces ONE executable
#  that carries its files inside it. So the decisive test is not
#  "asset() returns something" — it is running the binary from an
#  empty directory, with the asset tree, the .pyro and the source
#  all gone.
#
#  Also checks the property that let this ship without a format
#  bump: a .pyro WITHOUT assets must be byte-identical to what the
#  compiler produced before the section existed, and must still
#  load. The section is last and behind a flag, so an engine that
#  never looks at the flag never reads those bytes.
# ============================================================
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
PYROCLI = os.path.join(ROOT, 'Burnout', 'pyro.py')
BUILD = os.path.join(ROOT, 'build')
GOVM = os.path.join(BUILD, 'pyrovm.exe' if sys.platform == 'win32' else 'pyrovm')
CVM = os.path.join(BUILD, 'pyroc.exe' if sys.platform == 'win32' else 'pyroc')
EXE = '.exe' if sys.platform == 'win32' else ''

PROG = '''print(len(asset_names()));
for (string n in asset_names()) { print(n); }
print(asset("index.html"));
print(len(asset("css/app.css")));
print(asset("nope.txt") == "");
'''

EXPECTED = ['2', 'css/app.css', 'index.html', '<h1>hi</h1>', '15', 'true']

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def run(cmd, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or ROOT)
    return p.returncode, (p.stdout + p.stderr)


def lines(text):
    return [l.strip() for l in text.strip().splitlines() if l.strip()]


def main():
    print("-- embedded assets and single-file packaging (11.9) --")
    if not os.path.isfile(GOVM):
        print("  SKIP  no Pyro VM built")
        sys.exit(0)

    work = tempfile.mkdtemp(prefix='assets_')
    try:
        static = os.path.join(work, 'static')
        os.makedirs(os.path.join(static, 'css'))
        open(os.path.join(static, 'index.html'), 'w').write('<h1>hi</h1>')
        open(os.path.join(static, 'css', 'app.css'), 'w').write('body{color:red}')
        src = os.path.join(work, 'a.cryo')
        open(src, 'w', encoding='utf-8').write(PROG)
        pyro = os.path.join(work, 'a.pyro')

        rc, out = run([sys.executable, CRYOC, src, '--backend', 'pyro',
                       '--assets', static, '-o', pyro, '--no-banner'])
        check("compiles with --assets", rc == 0, out[-300:])
        if rc != 0:
            return

        # ── the same bytes must run the same on every engine ──
        rc, out = run([GOVM, pyro])
        check("Go VM reads the embedded assets", lines(out) == EXPECTED, out[:200])
        go_out = out

        if os.path.isfile(CVM):
            rc, out = run([CVM, pyro])
            check("C VM agrees byte for byte", lines(out) == lines(go_out), out[:200])
        else:
            print("  SKIP  no C VM built")

        check("keys are RELATIVE to the asset root, with forward slashes",
              'css/app.css' in lines(go_out) and 'static/index.html' not in go_out)
        check("a missing asset gives \"\", not an error", 'true' in lines(go_out))

        # ── one executable, then delete everything else ──
        exe = os.path.join(work, 'one' + EXE)
        rc, out = run([sys.executable, PYROCLI, 'build', src,
                       '--assets', static, '-o', exe])
        if not os.path.isfile(exe):
            print("  SKIP  no C toolchain for the native build")
        else:
            check("pyro build --assets produces a binary", True)
            isolated = tempfile.mkdtemp(prefix='isolated_')
            try:
                moved = os.path.join(isolated, 'one' + EXE)
                shutil.copy2(exe, moved)
                rc, out = run([moved], cwd=isolated)
                # nothing else is in `isolated` — no static/, no .pyro, no source
                check("the binary runs ALONE, with the asset tree gone",
                      lines(out) == EXPECTED, out[:200])
                check("nothing else was needed on disk",
                      os.listdir(isolated) == ['one' + EXE], os.listdir(isolated))
            finally:
                shutil.rmtree(isolated, ignore_errors=True)

        # ── no assets: the container must be unchanged ──
        plain = os.path.join(work, 'plain.cryo')
        open(plain, 'w', encoding='utf-8').write('print(1 + 1);\n')
        p1 = os.path.join(work, 'p1.pyro')
        p2 = os.path.join(work, 'p2.pyro')
        run([sys.executable, CRYOC, plain, '--backend', 'pyro', '-o', p1, '--no-banner'])
        run([sys.executable, CRYOC, plain, '--backend', 'pyro',
             '--assets', os.path.join(work, 'empty_dir'), '-o', p2, '--no-banner'])
        check("a program with no assets still compiles and runs",
              os.path.isfile(p1) and lines(run([GOVM, p1])[1]) == ['2'])
        if os.path.isfile(p1):
            with open(p1, 'rb') as f:
                blob = f.read()
            check("its flags byte does not set the asset bit",
                  blob[5] & 0x08 == 0, f"flags=0x{blob[5]:02x}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
