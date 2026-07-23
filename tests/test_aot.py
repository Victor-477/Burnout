#!/usr/bin/env python3
# ============================================================
#  Burnout — Phase 9.5: AOT (.pyro -> native C) verification
#
#  For each program:
#    1. compile .cryo -> .pyro (reference), run it on the Pyro VM  (baseline)
#    2. AOT-translate .pyro -> C  (burnout/aot_pyro.py)  [always checked]
#    3. if a C toolchain is present: build (C + pyro_runtime.c) -> native exe,
#       run it, and assert its stdout == the VM's stdout  (semantic parity)
#
#  When no C compiler is found the native build/run is skipped (the AOT
#  generation is still verified); this keeps the suite green on machines
#  without a C toolchain while proving the full pipeline where one exists.
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
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
AOT = os.path.join(_root, "Burnout", "aot_pyro.py")
VMDIR = os.path.join(_root, "Pyro", "vm")
RUNTIME = os.path.join(VMDIR, "pyro_runtime.c")
VM_BIN = os.path.join(_root, "build", "pyrovm.exe" if sys.platform == "win32" else "pyrovm")
TMP = tempfile.gettempdir()
# pyro_runtime.c uses sockets for http_serve(), so Windows links winsock too.
SYSLIBS = ["-lm"] + (["-lws2_32"] if sys.platform == "win32" else [])

_passed = 0
_failed = 0
def check(desc, cond):
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  ok   {desc}")
    else:
        _failed += 1; print(f"  FAIL {desc}")

def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, **kw)

def find_c_compiler():
    """Return (name, build_fn) for a usable C compiler, or (None, None).
    build_fn(cfile, exe) -> subprocess result."""
    for cc in ("gcc", "clang", "cc"):
        if shutil.which(cc):
            def build(cfile, exe, _cc=cc):
                return _run([_cc, "-O2", "-o", exe, cfile, RUNTIME, "-I", VMDIR] + SYSLIBS)
            return cc, build
    if shutil.which("zig"):
        def build(cfile, exe):
            return _run(["zig", "cc", "-O2", "-o", exe, cfile, RUNTIME, "-I", VMDIR] + SYSLIBS)
        return "zig cc", build
    if shutil.which("cl"):   # only if already in a developer environment
        def build(cfile, exe):
            return _run(["cl", "/O2", "/utf-8", "/I", VMDIR, "/Fe:" + exe, cfile, RUNTIME,
                         "ws2_32.lib"])
        return "cl", build
    return None, None

# (label, source)  — feature-diverse programs
PROGRAMS = [
    ("arith",   'int a = 2; int b = 3; print(a + b * 2); print((a + b) * 2); print(10 - 4 / 2);'),
    ("flow",    'int s = 0; int i = 0; while (i < 5) { s = s + i; i = i + 1; } print(s); '
                'for (int k = 1; k <= 4; k++) { if (k == 2) { continue; } print(k); }'),
    ("funcs",   'fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n-1) + fib(n-2); } '
                'print(fib(15));'),
    ("strings", 'string s = "Cryo"; print(s); print("hi " + s); print(upper(s)); '
                'print(len("hello")); print(substr("hello world", 0, 5));'),
    ("arrmap",  'int[] a = [3, 1, 2]; a.push(4); int t = 0; for (int v in a) { t += v; } '
                'print(t); print(a[3]); map<string,int> m = {"x": 10}; m["y"] = 20; '
                'print(m["x"] + m["y"]); print(has(m, "x"));'),
    ("enum",    'enum Res { Ok(int), Err(string) } '
                'fn f(int x) -> string ={ Res r = x > 0 ? Ok(x) : Err("neg"); '
                '  match r { Ok(v) => { return "ok:" + to_string(v); } Err(e) => { return e; } } return "?"; } '
                'print(f(7)); print(f(-1));'),
    ("bitfloat",'print(240 & 15); print(1 << 8); print(255 >> 4); number x = 3.5; print(x * 2.0); '
                'print(sqrt(16.0));'),
    ("trycatch", 'fn risky(int n) -> int ={ if (n < 0) { throw("neg"); } return n * 2; } '
                 'int a = 0; try { a = risky(5); } catch (string e) { a = -1; } print(a); '
                 'try { a = risky(-3); } catch (string e) { print("caught:" + e); a = -99; } print(a); '
                 'try { throw("boom"); } catch (string e) { print(e); } finally { print("fin"); } '
                 'try { assert(false, "nope"); } catch (string e) { print(e); }'),
]

def compile_pyro(src, tag):
    cf = os.path.join(TMP, "aot_" + tag + ".cryo")
    with open(cf, "w", encoding="utf-8") as f:
        f.write(src)
    pyro = os.path.join(TMP, "aot_" + tag + ".pyro")
    r = _run([sys.executable, CRYOC, cf, "--backend", "pyro", "-o", pyro, "--no-banner"])
    return pyro if (r.returncode == 0 and os.path.exists(pyro)) else None

cc_name, build = find_c_compiler()
print(f"[9.5] AOT (.pyro -> native C)   C toolchain: {cc_name or 'none (native build/run skipped)'}")

for tag, src in PROGRAMS:
    pyro = compile_pyro(src, tag)
    if pyro is None:
        check(f"[{tag}] compiles to .pyro", False); continue
    vm = _run([VM_BIN, pyro])
    vm_out = vm.stdout.replace("\r\n", "\n").strip()

    cfile = os.path.join(TMP, "aot_" + tag + ".c")
    gen = _run([sys.executable, AOT, pyro, "-o", cfile])
    ok_gen = (gen.returncode == 0 and os.path.exists(cfile)
              and "int main(int argc, char** argv)" in open(cfile, encoding="utf-8").read())
    check(f"[{tag}] AOT generates C", ok_gen)

    if ok_gen and build is not None:
        exe = os.path.join(TMP, "aot_" + tag + (".exe" if sys.platform == "win32" else ""))
        b = build(cfile, exe)
        if b.returncode != 0 or not os.path.exists(exe):
            check(f"[{tag}] native builds", False)
            print("    " + (b.stderr or b.stdout).strip()[:200])
        else:
            r = _run([exe])
            nat_out = r.stdout.replace("\r\n", "\n").strip()
            if nat_out != vm_out:
                print(f"    VM : {vm_out!r}\n    AOT: {nat_out!r}")
            check(f"[{tag}] native output == VM output", nat_out == vm_out)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
