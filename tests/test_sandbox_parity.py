#!/usr/bin/env python3
# ============================================================
#  test_sandbox_parity.py — the runtime sandbox, on every
#  primary backend (Phase 7, "Runtime Sandbox")
#
#  The item shipped marked complete while covering pyro and go
#  only, and node FAILED OPEN: the same --sandbox program that
#  pyro and go refused would write the file on node. Golden
#  rule 1 is parity across pyro/go/node, and a security control
#  is the worst possible place to be the odd one out.
#
#  Two holes, both found by RUNNING the program rather than
#  reading the guard list:
#
#    * node ignored --sandbox entirely (wrote the file)
#    * go did not gate read_file at all — under --sandbox it
#      read the file and returned its contents while pyro and
#      node refused. Information disclosure, failing open.
#
#  A third defect was cosmetic but the same shape: go reported
#  `exec(...)` as "pyro_exec() blocked", because both share one
#  helper that hardcoded the name — the right refusal under a
#  name no other backend prints for that program.
#
#  So these tests assert three things, and the third is the one
#  a guard-list review would miss:
#    1. the operation is BLOCKED,
#    2. nothing sensitive reached the output,
#    3. the message text is IDENTICAL across backends.
# ============================================================
import os
import re
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
BACKENDS = ('pyro', 'go', 'node')
_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def run(work, src, backend, sandbox=True, env=None):
    cf = os.path.join(work, 't.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    cmd = [sys.executable, CRYOC, cf, '--backend', backend, '--run', '--no-banner']
    if sandbox:
        cmd.append('--sandbox')
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=900,
                       env=dict(os.environ, **(env or {})))
    return (r.stdout or '') + '\n' + (r.stderr or '')


def msg_of(text):
    m = re.search(r'\[Cryo Security\] Sandbox: [^\r\n]*', text)
    return m.group(0).strip() if m else '<NOT BLOCKED>'


def main():
    print("[Phase 7] runtime sandbox parity")
    work = tempfile.mkdtemp(prefix='cryo_sbx_')
    d = work.replace(chr(92), '/')
    with open(os.path.join(work, 'secret.txt'), 'w') as f:
        f.write('TOPSECRET')

    # `exec` echoes a marker; the rest touch the filesystem or the environment.
    cases = [
        ('write_file', f'bool o = write_file("{d}/w.txt", "x");\nprint(o);\n'),
        ('read_file',  f'string s = read_file("{d}/secret.txt");\nprint(s);\n'),
        ('make_dir',   f'bool b = make_dir("{d}/sub");\nprint(b);\n'),
        ('delete_file', f'bool b = delete_file("{d}/secret.txt");\nprint(b);\n'),
        ('env',        'string v = env("PATH");\nprint(len(v) >= 0);\n'),
        ('exec',       'string o = exec("echo pwned");\nprint(len(o) >= 0);\n'),
    ]

    print("\n── blocked on every backend, with the same message ──")
    for name, src in cases:
        got = {b: run(work, src, b) for b in BACKENDS}
        texts = {b: msg_of(t) for b, t in got.items()}
        check(f"{name} is blocked on all three",
              all(v != '<NOT BLOCKED>' for v in texts.values()), repr(texts))
        # The leak check is separate on purpose: "it printed an error" and
        # "it did not do the thing" are different claims, and go's read_file
        # hole satisfied neither while looking like it satisfied the first.
        check(f"{name} leaks nothing",
              not any(('TOPSECRET' in t or 'pwned' in t) for t in got.values()),
              repr({b: t[-120:] for b, t in got.items()}))
        check(f"{name} reports identical text",
              len(set(texts.values())) == 1, repr(texts))

    # Without the flag the same programs must still work — a sandbox that is
    # always on is not a sandbox, it is a missing feature.
    print("\n── and they still work without --sandbox ──")
    target = os.path.join(work, 'w2.txt')
    src = f'bool o = write_file("{d}/w2.txt", "x");\nprint(o);\n'
    for b in BACKENDS:
        if os.path.exists(target):
            os.remove(target)
        out = run(work, src, b, sandbox=False)
        check(f"{b} writes when not sandboxed",
              os.path.exists(target), out[-160:])

    # PYRO_SANDBOX=1 tightens without the flag, and never loosens.
    print("\n── PYRO_SANDBOX=1 tightens without the flag ──")
    for b in BACKENDS:
        out = run(work, src, b, sandbox=False, env={'PYRO_SANDBOX': '1'})
        check(f"{b} honours PYRO_SANDBOX=1",
              msg_of(out) != '<NOT BLOCKED>', out[-160:])

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
