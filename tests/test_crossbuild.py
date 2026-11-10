#!/usr/bin/env python3
# ============================================================
#  test_crossbuild.py — `cryoc build` (14.6)
#
#  The claim being tested is "one machine produces executables for
#  Windows, Linux and macOS". Only one of those can be RUN here, so
#  the other five are checked the only way that means anything: by
#  reading the binary's own header.
#
#  WHY THE HEADER AND NOT `go build` EXITING 0. A build that quietly
#  ignored GOARCH would still exit 0, still write a file of about the
#  right size, and still look exactly like a success in a log — while
#  shipping an x86 binary to the people with ARM machines, who are the
#  only ones who would ever find out. So every target asserts the
#  machine field inside the PE/ELF/Mach-O header: 0x8664 is not
#  0xAA64, and no amount of exit-code green makes it so.
#
#  Headers are parsed here rather than shelled out to `file`: it is not
#  on a stock Windows box, and a check that silently stops running is
#  the failure mode run_all.py exists to catch.
#
#  AND THE TWO ROUTES ARE COMPARED. `--via native` and `--via bundle`
#  are the Go backend and the Pyro VM — two of the engines invariant 1
#  is about. A build that chooses between them must not change what
#  the program prints, so both are run and their output compared
#  against the expected text, not merely against each other: two
#  engines agreeing can mean both are wrong.
# ============================================================
import hashlib
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

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(_here))
sys.path.insert(0, os.path.join(_root, 'Burnout'))
sys.path.insert(0, os.path.join(_root, 'Cryo'))

CRYOC = os.path.join(_root, 'Burnout', 'cryoc.py')
HAS_GO = shutil.which('go') is not None

import crossbuild                                            # noqa: E402

_passed = _failed = 0


def check(desc, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {desc}")
    else:
        _failed += 1
        print(f"  FAIL {desc}" + (f"\n         {detail}" if detail else ""))


def run(args, cwd=None):
    return subprocess.run([sys.executable, CRYOC, 'build'] + args,
                          capture_output=True, text=True, cwd=cwd,
                          encoding='utf-8', errors='replace')


HELLO = (
    'string who = "world";\n'
    'print("hello, " + who + "!");\n'
    'int[] xs = [3, 1, 2];\n'
    'print(sort(xs));\n'
    'fn fib(int n) -> int ={\n'
    '    if (n < 2) { return n; }\n'
    '    return fib(n - 1) + fib(n - 2);\n'
    '}\n'
    'print("fib(20) = ${fib(20)}");\n'
)
HELLO_OUT = ['hello, world!', '[1, 2, 3]', 'fib(20) = 6765']


# ── binary headers ──────────────────────────────────────────
#
# (format, machine) from the file itself. Enough of each format to
# identify the target and nothing more.
ELF_MACHINES = {0x3E: 'x64', 0xB7: 'arm64'}
PE_MACHINES = {0x8664: 'x64', 0xAA64: 'arm64'}
MACHO_CPUS = {0x01000007: 'x64', 0x0100000C: 'arm64'}


def identify(path):
    with open(path, 'rb') as f:
        head = f.read(4096)
    if head[:4] == b'\x7fELF':
        if head[4] != 2:
            return 'elf', '32-bit'
        return 'elf', ELF_MACHINES.get(
            struct.unpack_from('<H', head, 18)[0], 'other')
    if head[:4] == b'\xcf\xfa\xed\xfe':                 # MH_MAGIC_64, LE
        return 'macho', MACHO_CPUS.get(
            struct.unpack_from('<I', head, 4)[0], 'other')
    if head[:2] == b'MZ':
        off = struct.unpack_from('<I', head, 0x3C)[0]
        if head[off:off + 4] != b'PE\x00\x00':
            return 'pe', 'no-pe-header'
        return 'pe', PE_MACHINES.get(
            struct.unpack_from('<H', head, off + 4)[0], 'other')
    return 'unknown', head[:4].hex()


EXPECTED = {
    'windows-x64':   ('pe', 'x64'),
    'windows-arm64': ('pe', 'arm64'),
    'linux-x64':     ('elf', 'x64'),
    'linux-arm64':   ('elf', 'arm64'),
    'macos-x64':     ('macho', 'x64'),
    'macos-arm64':   ('macho', 'arm64'),
}


# ── 1. target resolution (no toolchain needed) ──────────────
print("\n── target resolution ───────────────────────────────")


def names(specs):
    return [t.name for t in crossbuild.resolve_targets(specs)]


check("every target has an expected header", set(EXPECTED) == set(crossbuild.TARGETS))
check("--target all is the six targets", len(names(['all'])) == 6)
check("'macos' means both macOS architectures",
      names(['macos']) == ['macos-x64', 'macos-arm64'], names(['macos']))
check("'darwin' is an alias of macos", names(['darwin']) == names(['macos']))
check("comma-separated lists resolve",
      names(['windows-x64,linux-arm64']) == ['windows-x64', 'linux-arm64'])
check("repeated --target accumulates",
      names(['linux-x64', 'macos-arm64']) == ['linux-x64', 'macos-arm64'])
check("Go spellings resolve (linux-amd64, darwin-arm64)",
      names(['linux-amd64,darwin-arm64']) == ['linux-x64', 'macos-arm64'])
check("aarch64 resolves", names(['linux-aarch64']) == ['linux-arm64'])
check("order is the order asked for",
      names(['macos-arm64,windows-x64']) == ['macos-arm64', 'windows-x64'])
# A --target windows,all must not build Windows twice: the second pass would
# overwrite the first binary while the table reported two.
check("duplicates are dropped", len(names(['windows,all,windows-x64'])) == 6)
check("'host' is this machine",
      names(['host']) == [crossbuild.host_target().name])
try:
    crossbuild.resolve_targets(['solaris'])
    check("an unknown target raises", False, "no exception")
except crossbuild.BuildError as e:
    check("an unknown target raises BuildError, listing the real ones",
          'linux-arm64' in str(e), str(e))

env = crossbuild._go_env(crossbuild.TARGETS['macos-arm64'])
check("GOOS/GOARCH are set per target",
      (env['GOOS'], env['GOARCH']) == ('darwin', 'arm64'))
# With cgo on, a build for another system goes looking for that system's C
# compiler and fails — this one variable is the difference between six
# binaries and one.
check("CGO_ENABLED=0 (or no cross build happens at all)",
      env['CGO_ENABLED'] == '0')
check("an inherited GOFLAGS is dropped", 'GOFLAGS' not in env)

r = run(['--list-targets'])
check("--list-targets exits 0 and names every target",
      r.returncode == 0 and all(n in r.stdout for n in crossbuild.TARGETS),
      r.stdout + r.stderr)

r = run(['--target', 'solaris', 'nothing.cryo'])
check("an unknown target exits non-zero", r.returncode != 0)
r = run(['definitely-not-here.cryo'])
check("a missing input exits non-zero and says so",
      r.returncode != 0 and 'not found' in (r.stdout + r.stderr))


# ── 2. the cross build itself ───────────────────────────────
if not HAS_GO:
    print("\n  SKIP cross build: the Go toolchain is not on PATH")
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)

tmp = tempfile.mkdtemp(prefix='cryo_xbuild_')
src = os.path.join(tmp, 'hello.cryo')
with open(src, 'w', encoding='utf-8') as f:
    f.write(HELLO)

print("\n── native route: six targets, headers checked ──────")
dist = os.path.join(tmp, 'dist')
r = run([src, '--target', 'all', '-o', dist, '--via', 'native'])
check("`--target all --via native` exits 0", r.returncode == 0,
      r.stdout + r.stderr)
for name, want in EXPECTED.items():
    t = crossbuild.TARGETS[name]
    path = os.path.join(dist, f'hello-{name}{t.exe}')
    if not os.path.isfile(path):
        check(f"{name}: binary produced", False, f"missing {path}")
        continue
    got = identify(path)
    check(f"{name}: a {want[0].upper()} binary for {want[1]}", got == want,
          f"header says {got}")

# The host's own binary is the one that can be RUN, and running it is the only
# check here that says the program still works rather than merely that a file
# of the right shape exists.
host = crossbuild.host_target()
host_bin = os.path.join(dist, f'hello-{host.name}{host.exe}')
if os.path.isfile(host_bin):
    p = subprocess.run([host_bin], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    out = [l.strip() for l in p.stdout.splitlines() if l.strip()]
    check("the host binary runs and prints the right thing",
          p.returncode == 0 and out == HELLO_OUT, repr(p.stdout) + p.stderr)

sums = os.path.join(dist, 'SHA256SUMS')
if os.path.isfile(sums):
    listed = {}
    with open(sums, encoding='utf-8') as f:
        for line in f.read().splitlines():
            if line.strip():
                digest, base = line.split('  ', 1)
                listed[base.strip()] = digest
    ok = len(listed) == 6
    for base, digest in listed.items():
        p = os.path.join(dist, base)
        h = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.isfile(p) else None
        ok = ok and h == digest
    check("SHA256SUMS lists all six and every digest matches", ok, str(listed))
else:
    check("SHA256SUMS is written", False, "no SHA256SUMS in dist")

print("\n── bundle route: the VM carrying the bytecode ──────")
bdist = os.path.join(tmp, 'bundled')
r = run([src, '--target', 'host', '-o', bdist, '--via', 'bundle'])
check("`--via bundle` exits 0", r.returncode == 0, r.stdout + r.stderr)
bundled = os.path.join(bdist, f'hello-{host.name}{host.exe}')
if os.path.isfile(bundled):
    check(f"the bundle is still a {EXPECTED[host.name][0].upper()} binary",
          identify(bundled) == EXPECTED[host.name], str(identify(bundled)))
    p = subprocess.run([bundled], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    out = [l.strip() for l in p.stdout.splitlines() if l.strip()]
    # Compared against the expected text, not against the native route's
    # output: two engines agreeing is not evidence that either is right.
    check("the bundled binary prints the same, correct output",
          p.returncode == 0 and out == HELLO_OUT, repr(p.stdout) + p.stderr)
else:
    check("the bundle route produces a binary", False, bundled)

print("\n── route selection ────────────────────────────────")
# json_decode without a target type: the Pyro backend takes it, the Go backend
# refuses it. `auto` must notice and switch rather than fail — and must say so,
# because the two routes produce visibly different binaries.
jd = os.path.join(tmp, 'jd.cryo')
with open(jd, 'w', encoding='utf-8') as f:
    f.write('print(json_decode("{\\"a\\": 1}"));\n')
jdist = os.path.join(tmp, 'auto')
r = run([jd, '--target', 'host', '-o', jdist])
check("auto falls back to bundle for a program Go does not cover",
      r.returncode == 0 and 'bundle' in (r.stdout + r.stderr),
      r.stdout + r.stderr)
jd_bin = os.path.join(jdist, f'jd-{host.name}{host.exe}')
if os.path.isfile(jd_bin):
    p = subprocess.run([jd_bin], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    check("the fallback binary runs", p.stdout.strip() == '{a: 1}',
          repr(p.stdout) + p.stderr)
else:
    check("the fallback produced a binary", False, jd_bin)

# A broken program is not an unsupported one. Falling back would run the whole
# front end again and print the same error with a line about Go on top of it.
bad = os.path.join(tmp, 'bad.cryo')
with open(bad, 'w', encoding='utf-8') as f:
    f.write('print(nope_undeclared);\n')
r = run([bad, '--target', 'host', '-o', os.path.join(tmp, 'bad')])
check("a program with a semantic error fails without a fallback notice",
      r.returncode != 0 and 'falling back' not in (r.stdout + r.stderr),
      r.stdout + r.stderr)

# Assets live in the Pyro container, which only the VM reads. Asking for them
# on the Go route has to be refused, not silently dropped — a binary missing
# its assets fails on the machine it was shipped to and nowhere else.
res = os.path.join(tmp, 'res')
os.makedirs(res, exist_ok=True)
with open(os.path.join(res, 'note.txt'), 'w', encoding='utf-8') as f:
    f.write('embedded')
ap_ = os.path.join(tmp, 'withassets.cryo')
with open(ap_, 'w', encoding='utf-8') as f:
    f.write('print(asset("note.txt"));\n')
r = run([ap_, '--assets', res, '--via', 'native', '-o', os.path.join(tmp, 'na')])
check("--assets --via native is refused, naming the route that works",
      r.returncode != 0 and 'bundle' in (r.stdout + r.stderr),
      r.stdout + r.stderr)
r = run([ap_, '--assets', res, '--target', 'host', '-o', os.path.join(tmp, 'wa')])
asset_bin = os.path.join(tmp, 'wa', f'withassets-{host.name}{host.exe}')
if r.returncode == 0 and os.path.isfile(asset_bin):
    p = subprocess.run([asset_bin], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    check("--assets with no --via picks bundle, and the asset is in the binary",
          p.stdout.strip() == 'embedded', repr(p.stdout) + p.stderr)
else:
    check("--assets builds a binary", False, r.stdout + r.stderr)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
