#!/usr/bin/env python3
# ============================================================
#  test_integrity.py — reproducible builds and signing (11.14)
#
#  Two halves that only mean something together.
#
#  REPRODUCIBLE BUILDS were already true when this was written —
#  the bootstrap fixed point depends on it, so the container was
#  built to be deterministic from the start. That makes these
#  checks a LOCK rather than a new feature, and worth having for
#  exactly that reason: the ways to break it are all accidental
#  (a set where a list was meant, a dict iterated unsorted, a
#  path or timestamp leaking in) and none of them fail loudly.
#
#  SIGNING is new. The tests that matter are not "a good file
#  verifies" — that is the easy half — but the four refusals:
#  a wrong key, a tampered body, a tampered PERMISSIONS section,
#  and an unsigned file offered while a key is configured. The
#  last one is the one a naive implementation gets wrong, and
#  getting it wrong makes the whole feature decorative: if
#  unsigned files were accepted under a key, deleting 32 bytes
#  and clearing a flag bit would be a complete bypass.
# ============================================================
import os
import shutil
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
EXE = '.exe' if sys.platform == 'win32' else ''
VM = os.path.join(ROOT, 'Pyro', 'vm', 'pyrovm_go' + EXE)

KEY = 'this-is-a-test-key-01234567890'
OTHER_KEY = 'another-test-key-0987654321zz'
FLAG_SIGNED = 0x20

PROG = 'string[] a = ["x", "y"];\nfor (int i, string s in enumerate(a)) { print(s); }\n'

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def build(work, src, out, extra=(), cwd=None):
    p = os.path.join(work, 'p.cryo')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(src)
    r = subprocess.run([sys.executable, CRYOC, p, '--backend', 'pyro',
                        '-o', out, '--no-banner', '--no-cache', *extra],
                       capture_output=True, text=True, timeout=900, cwd=cwd)
    return r


def run_vm(pyro, key=None, keyfile=None):
    env = dict(os.environ)
    env.pop('PYRO_KEY', None)
    env.pop('PYRO_KEY_FILE', None)
    if key:
        env['PYRO_KEY'] = key
    if keyfile:
        env['PYRO_KEY_FILE'] = keyfile
    r = subprocess.run([VM, pyro], capture_output=True, text=True,
                       timeout=300, env=env)
    return r.returncode, r.stdout.replace('\r\n', '\n'), r.stderr


def flags_of(path):
    with open(path, 'rb') as f:
        return f.read(6)[5]


print("[11.14] integrity — reproducible builds and signing")
if not os.path.isfile(VM):
    print("Go VM not built — skipping.")
    sys.exit(0)

work = tempfile.mkdtemp(prefix='cryo_int_')
keyfile = os.path.join(work, 'key.txt')
with open(keyfile, 'w', encoding='utf-8') as f:
    f.write(KEY)

# ── 1. reproducible builds ─────────────────────────────────
print("\n── reproducible builds ──")
outs = []
for i in range(3):
    o = os.path.join(work, f'r{i}.pyro')
    build(work, PROG, o)
    outs.append(open(o, 'rb').read())
check("three builds of the same source are byte-identical",
      len(set(outs)) == 1, f"{len(set(outs))} distinct outputs")

# The compiler must not bake in where it was RUN. A path or a working directory
# in the container would be invisible until two machines disagreed.
alt = tempfile.mkdtemp(prefix='cryo_int_alt_')
o_alt = os.path.join(alt, 'r.pyro')
build(alt, PROG, o_alt, cwd=alt)
check("the build does not depend on the working directory",
      open(o_alt, 'rb').read() == outs[0])

# Assets and permissions are the sections with an ordering choice in them, so
# they are where non-determinism would actually come from.
assets = os.path.join(work, 'assets')
os.makedirs(assets, exist_ok=True)
for n in ('c', 'a', 'b'):
    with open(os.path.join(assets, f'{n}.txt'), 'w', encoding='utf-8') as f:
        f.write(f'content-{n}')
rich = ('permissions { read = "./data"; net = "example.com"; }\n'
        'print(asset("a.txt"));\n')
a_outs = []
for i in range(2):
    o = os.path.join(work, f'a{i}.pyro')
    build(work, rich, o, extra=('--assets', assets))
    a_outs.append(open(o, 'rb').read())
check("with embedded assets and declared permissions too",
      len(set(a_outs)) == 1 and len(a_outs[0]) > 0)

# ── 2. signing ─────────────────────────────────────────────
print("\n── signing ──")
signed = os.path.join(work, 'signed.pyro')
unsigned = os.path.join(work, 'unsigned.pyro')
r = build(work, PROG, signed, extra=('--sign', keyfile))
check("--sign compiles", r.returncode == 0, r.stderr[-300:])
build(work, PROG, unsigned)
sb, ub = open(signed, 'rb').read(), open(unsigned, 'rb').read()
check("the signature adds exactly 32 bytes", len(sb) - len(ub) == 32,
      f"{len(sb)} vs {len(ub)}")
# Everything after the header is identical; the ONLY difference in the header
# is the signed flag. Stated this precisely because the flag byte is inside
# what gets signed — a signature that covered the body but not the flag could
# be replayed onto an unsigned container.
check("the body after the header is untouched", sb[6:-32] == ub[6:])
check("and the header differs only by the signed flag",
      bytes([sb[5] & ~FLAG_SIGNED]) == bytes([ub[5]]) and sb[:5] == ub[:5],
      f"{sb[:6]!r} vs {ub[:6]!r}")
check("the signed flag is set", flags_of(signed) & FLAG_SIGNED != 0)
check("and clear on an unsigned build", flags_of(unsigned) & FLAG_SIGNED == 0)

sig2 = os.path.join(work, 'signed2.pyro')
build(work, PROG, sig2, extra=('--sign', keyfile))
check("signing is reproducible", open(sig2, 'rb').read() == sb)

other = os.path.join(work, 'other.pyro')
with open(os.path.join(work, 'k2.txt'), 'w', encoding='utf-8') as f:
    f.write(OTHER_KEY)
build(work, PROG, other, extra=('--sign', os.path.join(work, 'k2.txt')))
ob = open(other, 'rb').read()
check("a different key gives a different signature", ob[-32:] != sb[-32:])
check("...over an identical body", ob[:-32] == sb[:-32])

# ── 3. verification ────────────────────────────────────────
print("\n── verification ──")
code, out, err = run_vm(signed, key=KEY)
check("the right key runs the program", code == 0 and out.strip() == "x\ny",
      f"{code} {out!r} {err[:200]}")

code, out, err = run_vm(signed)
check("no key configured runs it without checking", code == 0 and out.strip() == "x\ny",
      f"{code} {out!r} {err[:200]}")

code, out, err = run_vm(signed, key=OTHER_KEY)
check("a wrong key is refused", code != 0, f"{code} {err[:200]}")
check("and the message says the signature failed",
      'signature check FAILED' in err, err[:200])

# THE one that makes the feature real. If unsigned files were accepted while a
# key is configured, an attacker would not need to forge anything — just strip.
code, out, err = run_vm(unsigned, key=KEY)
check("an UNSIGNED file is refused when a key is configured", code != 0,
      f"{code} {out!r}")
check("and the message explains why that is deliberate",
      'not signed' in err and 'bypass' in err, err[:300])

# ── 4. tampering ───────────────────────────────────────────
print("\n── tampering ──")
tam = os.path.join(work, 'tampered.pyro')
d = bytearray(sb)
d[d.index(b'x')] = ord('Z')          # a string constant the program prints
with open(tam, 'wb') as f:
    f.write(bytes(d))
code, out, err = run_vm(tam, key=KEY)
check("a modified body is refused", code != 0, f"{code} {out!r}")
code, out, err = run_vm(tam)
# Not a bug: verification is opt-in on the verifier's side. Worth asserting so
# the opt-in nature is a stated property rather than an assumption.
check("...and runs modified when no key is configured (opt-in, by design)",
      code == 0 and 'Z' in out, f"{code} {out!r}")

# The permissions section is the first thing worth editing on someone else's
# .pyro — widening `net` costs one byte. It is why the signature is written
# last and covers everything.
psigned = os.path.join(work, 'perm.pyro')
build(work, 'permissions { net = "a.example"; }\nprint(1);\n', psigned,
      extra=('--sign', keyfile))
pb = bytearray(open(psigned, 'rb').read())
i = pb.find(b'a.example')
check("the permissions really are inside the container", i > 0)
if i > 0:
    pb[i] = ord('b')
    ptam = os.path.join(work, 'perm_tampered.pyro')
    with open(ptam, 'wb') as f:
        f.write(bytes(pb))
    code, out, err = run_vm(ptam, key=KEY)
    check("editing the declared permissions is refused", code != 0,
          f"{code} {out!r} {err[:150]}")

# Truncating the signature and clearing the flag is the cheapest forgery
# attempt there is, so it gets its own check rather than being assumed covered.
strip = bytearray(sb[:-32])
strip[5] &= ~FLAG_SIGNED
sp = os.path.join(work, 'stripped.pyro')
with open(sp, 'wb') as f:
    f.write(bytes(strip))
code, out, err = run_vm(sp, key=KEY)
check("stripping the signature and the flag is refused", code != 0,
      f"{code} {out!r}")

# ── 5. keys ────────────────────────────────────────────────
print("\n── key handling ──")
code, out, err = run_vm(signed, keyfile=keyfile)
check("PYRO_KEY_FILE works as well as PYRO_KEY", code == 0 and out.strip() == "x\ny",
      f"{code} {err[:200]}")

# A key file written by an editor or `echo` gains a trailing newline. If one
# side trimmed and the other did not, signing and verifying with "the same" key
# would silently disagree.
nl = os.path.join(work, 'key_nl.txt')
with open(nl, 'w', encoding='utf-8') as f:
    f.write(KEY + '\n')
sig_nl = os.path.join(work, 'signed_nl.pyro')
build(work, PROG, sig_nl, extra=('--sign', nl))
check("a trailing newline in the key file changes nothing",
      open(sig_nl, 'rb').read() == sb)
code, out, err = run_vm(signed, keyfile=nl)
check("...on the verifying side too", code == 0, err[:200])

r = build(work, PROG, os.path.join(work, 'x.pyro'), extra=('--sign', 'env:PYRO_TEST_KEY_UNSET'))
check("--sign env:NAME with an unset variable is refused", r.returncode != 0)
check("and says the variable is not set",
      'not set' in (r.stdout + r.stderr), (r.stdout + r.stderr)[-200:])

short = os.path.join(work, 'short.txt')
with open(short, 'w', encoding='utf-8') as f:
    f.write('abc')
r = build(work, PROG, os.path.join(work, 'x.pyro'), extra=('--sign', short))
check("a suspiciously short key is refused rather than used",
      r.returncode != 0 and '16 bytes' in (r.stdout + r.stderr),
      (r.stdout + r.stderr)[-200:])

r = build(work, PROG, os.path.join(work, 'x.pyro'),
          extra=('--sign', os.path.join(work, 'nope.txt')))
check("a missing key file is refused with the path",
      r.returncode != 0 and 'nope.txt' in (r.stdout + r.stderr),
      (r.stdout + r.stderr)[-200:])

# ── 6. the cache must not confuse the two ──────────────────
# The artifact cache keys on the compile settings. Signing was added to those
# settings as a DIGEST of the key — without it, a signed and an unsigned build
# of the same source would share an entry and the signature would appear or
# vanish depending on which was compiled first.
print("\n── the incremental cache ──")
cwork = tempfile.mkdtemp(prefix='cryo_int_cache_')
p = os.path.join(cwork, 'p.cryo')
with open(p, 'w', encoding='utf-8') as f:
    f.write(PROG)


def cached_build(out, extra=()):
    subprocess.run([sys.executable, CRYOC, p, '--backend', 'pyro', '-o', out,
                    '--no-banner', *extra], capture_output=True, text=True,
                   timeout=900)
    return open(out, 'rb').read()


u1 = cached_build(os.path.join(cwork, 'u1.pyro'))
s1 = cached_build(os.path.join(cwork, 's1.pyro'), ('--sign', keyfile))
u2 = cached_build(os.path.join(cwork, 'u2.pyro'))
check("an unsigned build stays unsigned after a signed one", len(u2) == len(u1),
      f"{len(u1)} -> {len(u2)}")
check("the signed build is signed even though the unsigned one was cached",
      len(s1) == len(u1) + 32, f"{len(u1)} vs {len(s1)}")
s2 = cached_build(os.path.join(cwork, 's2.pyro'),
                  ('--sign', os.path.join(cwork, '..', 'k2.txt'))
                  if False else ('--sign', os.path.join(work, 'k2.txt')))
check("a different key is not served the first key's cached artifact",
      s2[-32:] != s1[-32:], "same signature from two different keys")

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
