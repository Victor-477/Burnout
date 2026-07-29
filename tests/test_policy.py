#!/usr/bin/env python3
# ============================================================
#  test_policy.py — capability sandbox (roadmap 11.11)
#
#  Three modes, and the tests exist to keep them distinct:
#
#    nothing set     everything allowed        (unchanged default)
#    PYRO_SANDBOX=1  everything gated refused  (unchanged)
#    PYRO_POLICY=... deny by default, grant exactly what is listed
#
#  The case that matters most is the ESCAPE: granting `fs.read=./data`
#  must not let a program read `./data/../secret`. That is checked
#  from both sides — denied under one grant, allowed under the grant
#  that actually covers it — so a test cannot pass by refusing
#  everything.
#
#  Both VMs are exercised, because a policy that differs between
#  engines is worse than no policy at all.
# ============================================================
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
BUILD = os.path.join(ROOT, 'build')
GOVM = os.path.join(BUILD, 'pyrovm.exe' if sys.platform == 'win32' else 'pyrovm')
CVM = os.path.join(BUILD, 'pyroc.exe' if sys.platform == 'win32' else 'pyroc')

PROGRAMS = {
    'read':   'print(read_file("data/ok.txt"));',
    'write':  'print(write_file("data/new.txt", "x"));',
    'escape': 'print(read_file("data/../secret/no.txt"));',
    'env':    'print(len(env("PATH")) > 0);',
    'exec':   'print(len(exec("echo hi")) > 0);',
}

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def run(engine, pyro, workdir, policy=None, sandbox=False):
    env = dict(os.environ)
    env.pop('PYRO_POLICY', None)
    env.pop('PYRO_SANDBOX', None)
    if policy:
        env['PYRO_POLICY'] = policy
    if sandbox:
        env['PYRO_SANDBOX'] = '1'
    p = subprocess.run([engine, pyro], capture_output=True, text=True,
                       cwd=workdir, env=env, timeout=30, errors='replace')
    return (p.stdout + p.stderr).strip()


def main():
    print("-- capability sandbox (11.11) --")
    engines = [(e, n) for e, n in ((GOVM, 'Go VM'), (CVM, 'C VM')) if os.path.isfile(e)]
    if not engines:
        print("  SKIP  no VM built")
        sys.exit(0)

    work = tempfile.mkdtemp(prefix='policy_')
    try:
        os.makedirs(os.path.join(work, 'data'))
        os.makedirs(os.path.join(work, 'secret'))
        open(os.path.join(work, 'data', 'ok.txt'), 'w').write('public')
        open(os.path.join(work, 'secret', 'no.txt'), 'w').write('classified')

        pyro = {}
        for tag, src in PROGRAMS.items():
            cf = os.path.join(work, tag + '.cryo')
            pf = os.path.join(work, tag + '.pyro')
            open(cf, 'w', encoding='utf-8').write(src + '\n')
            r = subprocess.run([sys.executable, CRYOC, cf, '--backend', 'pyro',
                                '-o', pf, '--no-banner'],
                               capture_output=True, text=True, cwd=ROOT)
            if r.returncode != 0:
                check(f"{tag} compiles", False, (r.stdout + r.stderr)[-200:])
                return
            pyro[tag] = pf
        check("the sample programs compile", True)

        for engine, name in engines:
            def out(tag, **kw):
                return run(engine, pyro[tag], work, **kw)

            # 1. default: unchanged
            check(f"{name}: no sandbox -> allowed", out('read') == 'public', out('read'))

            # 2. deny-all: unchanged
            o = out('read', sandbox=True)
            check(f"{name}: PYRO_SANDBOX=1 -> refused",
                  'blocked by sandbox policy' in o, o)

            # 3. a policy grants exactly what it names
            check(f"{name}: fs.read grants the read",
                  out('read', policy='fs.read=./data') == 'public',
                  out('read', policy='fs.read=./data'))
            o = out('write', policy='fs.read=./data')
            check(f"{name}: fs.read does NOT grant a write", 'denied for' in o, o)
            check(f"{name}: fs.write grants the write",
                  out('write', policy='fs.write=./data') == 'true',
                  out('write', policy='fs.write=./data'))

            # 4. the escape, checked from BOTH sides
            o = out('escape', policy='fs.read=./data')
            check(f"{name}: ../ cannot leave a granted root", 'denied for' in o, o)
            check(f"{name}: the same path IS allowed when granted",
                  out('escape', policy='fs.read=./secret') == 'classified',
                  out('escape', policy='fs.read=./secret'))

            # 5. env and exec are separate capabilities
            check(f"{name}: env=PATH grants it",
                  out('env', policy='env=PATH') == 'true', out('env', policy='env=PATH'))
            o = out('env', policy='env=HOME')
            check(f"{name}: a different env var is refused", 'denied for' in o, o)
            check(f"{name}: exec=echo grants it",
                  out('exec', policy='exec=echo') == 'true',
                  out('exec', policy='exec=echo'))
            o = out('exec', policy='fs.read=*')
            check(f"{name}: exec is not implied by another capability",
                  'denied for' in o, o)

            # 6. the refusal must say what to grant
            o = out('write', policy='fs.read=./data')
            check(f"{name}: the message names the capability to grant",
                  'PYRO_POLICY' in o and 'fs.write' in o, o)

            # 7. a typo is rejected, not ignored
            o = out('read', policy='bogus=1')
            check(f"{name}: an unknown capability is an error",
                  'unknown capability' in o, o)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
