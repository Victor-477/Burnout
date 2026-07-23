#!/usr/bin/env python3
# ============================================================
#  Burnout — Phase 9.6: `pyro` single-entry CLI verification
#
#  Exercises the unified CLI (build / run / vm / c) on both a `.cryo` source
#  and a precompiled `.pyro`. `run`/`vm`/`c` are checked everywhere; `build`
#  (and native `run`) build+run+compare against the VM only where a C
#  toolchain exists, and are otherwise expected to skip/fall back cleanly.
# ============================================================
import os
import sys
import shutil
import subprocess
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(_here))
PYRO = os.path.join(_root, "Burnout", "pyro.py")
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
TMP = tempfile.gettempdir()
HAS_CC = any(shutil.which(c) for c in ("gcc", "clang", "cc", "zig", "cl"))

_passed = _failed = 0
def check(desc, cond):
    global _passed, _failed
    if cond: _passed += 1; print(f"  ok   {desc}")
    else: _failed += 1; print(f"  FAIL {desc}")

def run(args):
    return subprocess.run([sys.executable, PYRO] + args, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)

SRC = ('fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n-1) + fib(n-2); } '
       'int s = 0; for (int i = 0; i < 5; i++) { s += i; } '
       'print(fib(12)); print(s); print("hi " + to_string(s));')
EXPECT = "144\n10\nhi 10"

cryo = os.path.join(TMP, "cli_demo.cryo")
open(cryo, "w", encoding="utf-8").write(SRC)
pyro = os.path.join(TMP, "cli_demo.pyro")
subprocess.run([sys.executable, CRYOC, cryo, "--backend", "pyro", "-o", pyro, "--no-banner"],
               capture_output=True)

print(f"[9.6] pyro CLI   C toolchain: {'yes' if HAS_CC else 'none (native build skipped)'}")

def norm(s): return s.replace("\r\n", "\n").strip()

# pyro vm  (cryo + pyro)
check("vm <cryo> output", norm(run(["vm", cryo]).stdout) == EXPECT)
check("vm <pyro> output", norm(run(["vm", pyro]).stdout) == EXPECT)
# pyro run --vm
check("run --vm <cryo> output", norm(run(["run", cryo, "--vm"]).stdout) == EXPECT)
# pyro run (native if toolchain, else falls back to VM) — must produce correct output either way
check("run <cryo> output", norm(run(["run", cryo]).stdout) == EXPECT)
# pyro c  (emits C with a main)
r = run(["c", cryo]); check("c <cryo> emits C main", "int main(void)" in r.stdout)
r = run(["c", pyro]); check("c <pyro> emits C main", "int main(void)" in r.stdout)

# pyro build
out = os.path.join(TMP, "cli_demo_bin" + (".exe" if sys.platform == "win32" else ""))
if os.path.exists(out): os.remove(out)
rb = run(["build", cryo, "-o", out])
if HAS_CC:
    check("build produces a native binary", rb.returncode == 0 and os.path.exists(out))
    if os.path.exists(out):
        rr = subprocess.run([out], capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("native binary output == expected", norm(rr.stdout) == EXPECT)
else:
    # no toolchain: build must fail cleanly (non-zero + helpful message), not crash
    check("build fails cleanly without a C toolchain",
          rb.returncode != 0 and "toolchain" in (rb.stderr + rb.stdout))

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
