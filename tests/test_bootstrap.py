#!/usr/bin/env python3
# ============================================================
#  Burnout — Phase 9.4: self-hosting fixed-point bootstrap
#
#  Proves the Cryo-in-Cryo compiler (cryo/selfhost/{lexer,codegen}.cryo)
#  is a fixed point: a program compiles to BYTE-IDENTICAL bytecode whether
#  the compiler was built by the reference (Python) compiler or by ITSELF.
#
#  - SRC        = lexer.cryo + codegen.cryo (the `import` inlined by concat)
#  - stage0     = the reference Python compiler (burnout/cryoc.py)
#  - self-host  = SRC's compile(); running it compiles a source string to .pyro
#
#  P_A = program T compiled by  self-host-built-by-Python
#  P_B = program T compiled by  self-host-built-by-self-host
#  Fixed point  <=>  P_A == P_B  (byte for byte)   and both run correctly.
#
#  Also checks that the self-host can compile its OWN full source into a
#  valid .pyro (single-level; the literal double-compile of the whole source
#  is bounded by the .pyro u16 string-length limit, a format constraint).
# ============================================================
import os
import sys
import subprocess
import tempfile
import hashlib

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(_here))
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
SELF = os.path.join(_root, "Cryo", "selfhost")
VM_BIN = os.path.join(_root, "build", "pyrovm.exe" if sys.platform == "win32" else "pyrovm")
TMP = tempfile.gettempdir()

_passed = 0
_failed = 0
def check(desc, cond):
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  ok   {desc}")
    else:
        _failed += 1; print(f"  FAIL {desc}")

def _read(f):
    with open(os.path.join(SELF, f), encoding="utf-8") as fh:
        return fh.read()

# SRC: the compiler's own source, with `import "lexer.cryo"` inlined by concat
_codegen = "\n".join(l for l in _read("codegen.cryo").splitlines()
                     if l.strip() != 'import "lexer.cryo"')
SRC = _read("lexer.cryo") + "\n" + _codegen

def _embed(s):
    # embed `s` as a Cryo string literal in a driver; split `${` so the
    # reference compiler does not interpolate the driver's own literal.
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("${", '$" + "{')

def _ref_compile(src_text, out):
    p = os.path.join(TMP, "boot_driver.cryo")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(src_text)
    r = subprocess.run([sys.executable, CRYOC, p, "--backend", "pyro", "-o", out, "--no-banner"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    try: os.remove(p)
    except OSError: pass
    return r

def _run(pyro, out=None):
    if out and os.path.exists(out):
        os.remove(out)
    return subprocess.run([VM_BIN, pyro], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)

def _sha(f):
    with open(f, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()

def selfhost_via_python(prog, tag):
    """Compile `prog` with the self-host codegen as built by the Python compiler."""
    out = os.path.join(TMP, tag + "_A.pyro").replace("\\", "/")
    st = os.path.join(TMP, tag + "_stA.pyro")
    if _ref_compile(SRC + '\ncompile("' + _embed(prog) + '", "' + out + '");\n', st).returncode != 0:
        return None
    _run(st, out)
    return out if os.path.exists(out) else None

def selfhost_via_selfhost(prog, tag):
    """Compile `prog` with the self-host codegen as built by the self-host itself."""
    out = os.path.join(TMP, tag + "_B.pyro").replace("\\", "/")
    driver_b = SRC + '\ncompile("' + _embed(prog) + '", "' + out + '");\n'
    stage2 = os.path.join(TMP, tag + "_stage2.pyro").replace("\\", "/")
    stmid = os.path.join(TMP, tag + "_stmid.pyro")
    if _ref_compile(SRC + '\ncompile("' + _embed(driver_b) + '", "' + stage2 + '");\n', stmid).returncode != 0:
        return None
    _run(stmid, stage2)                 # stage2 = self-host-compile(driver_b)
    if not os.path.exists(stage2):
        return None
    _run(stage2, out)                   # run stage2 -> compiles prog -> out
    return out if os.path.exists(out) else None

# ── feature-rich program exercising the full self-hosted subset ──────────
T = ('fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n-1) + fib(n-2); } '
     'enum Res { Ok(int), Err(string) } '
     'fn classify(int x) -> string ={ Res r = x > 0 ? Ok(x) : Err("neg"); '
     '  match r { Ok(v) => { return "ok:" + to_string(v); } Err(e) => { return e; } } return "?"; } '
     'int[] a = [3, 1, 2]; int s = 0; for (int i = 0; i < len(a); i++) { s += a[i]; } '
     'int acc = 0; int k = 0; while (k < 6) { k = k + 1; if (k == 3) { continue; } '
     '  if (k == 5) { break; } acc += k; } '
     'print(fib(12)); print(classify(7)); print(classify(-1)); print(s); print(acc); '
     'print("n=${s} fib=${fib(6)}"); print(240 & 15); print(1 << 8); '
     'map<string,int> m = {"x": 10}; m["y"] = 20; print(m["x"] + m["y"]);')

EXPECTED = ["144", "ok:7", "neg", "6", "7", "n=6 fib=8", "0", "256", "30"]

print("[9.4] self-hosting fixed-point bootstrap")

pA = selfhost_via_python(T, "boot")
pB = selfhost_via_selfhost(T, "boot")

check("self-host (built by Python) compiles the program", pA is not None)
check("self-host (built by itself) compiles the program", pB is not None)

if pA and pB:
    outA = _run(pA).stdout.replace("\r\n", "\n").strip().split("\n")
    outB = _run(pB).stdout.replace("\r\n", "\n").strip().split("\n")
    check("program output is correct (Python-built compiler)", outA == EXPECTED)
    check("program output is correct (self-built compiler)", outB == EXPECTED)
    if _sha(pA) != _sha(pB):
        print(f"    P_A sha={_sha(pA)}\n    P_B sha={_sha(pB)}")
    check("FIXED POINT: byte-identical bytecode from both compilers", _sha(pA) == _sha(pB))

# ── the self-host can compile its OWN full source into a valid .pyro ─────
own = os.path.join(TMP, "selfsrc.pyro").replace("\\", "/")
st = os.path.join(TMP, "selfsrc_st.pyro")
ok_compile = _ref_compile(SRC + '\ncompile("' + _embed(SRC) + '", "' + own + '");\n', st).returncode == 0
if ok_compile:
    _run(st, own)
check("self-host compiles its own full source (lexer+codegen) to a valid .pyro",
      ok_compile and os.path.exists(own) and os.path.getsize(own) > 4 and open(own, "rb").read(4) == b"PYRO")

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
