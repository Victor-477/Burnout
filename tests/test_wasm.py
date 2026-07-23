#!/usr/bin/env python3
# ============================================================
#  Burnout — Phase 9.6: WebAssembly backend verification
#
#  Compiles Cryo (numeric subset) to a .wasm module and, where Node is
#  present, instantiates it and asserts the module's output (via the host
#  `env.log` import) matches the Pyro VM's output for the same program.
#  Generation is always checked; the Node run is skipped cleanly otherwise.
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
sys.path.insert(0, os.path.join(_root, "Burnout"))
sys.path.insert(0, os.path.join(_root, "Cryo"))
import compiler

CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
VM_BIN = os.path.join(_root, "build", "pyrovm.exe" if sys.platform == "win32" else "pyrovm")
TMP = tempfile.gettempdir()
NODE = shutil.which("node")

_HARNESS = r"""
const fs = require('fs');
const buf = fs.readFileSync(process.argv[2]);
const out = [];
WebAssembly.instantiate(buf, { env: { log: (x) => out.push(x.toString()) } })
  .then(({instance}) => { instance.exports.main(); process.stdout.write(out.join('\n')); })
  .catch(e => { console.error('WASM error:', e.message); process.exit(1); });
"""

_passed = _failed = 0
def check(desc, cond):
    global _passed, _failed
    if cond: _passed += 1; print(f"  ok   {desc}")
    else: _failed += 1; print(f"  FAIL {desc}")

PROGRAMS = [
    ("arith",  'print(2 + 3 * 4); print((2 + 3) * 4); print(20 - 6 / 2); print(17 % 5);'),
    ("funcs",  'fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n-1) + fib(n-2); } '
               'fn add(int a, int b) -> int ={ return a + b; } print(fib(15)); print(add(20, 22));'),
    ("flow",   'int s = 0; for (int i = 0; i < 6; i++) { if (i == 3) { continue; } '
               'if (i == 5) { break; } s += i; } print(s); '
               'int f = 1; int k = 1; while (k <= 5) { f = f * k; k = k + 1; } print(f);'),
    # bare booleans print as true/false in the VM but 0/1 in the string-less
    # wasm subset, so gate them through `if` to compare numeric output.
    ("cmpbool",'int x = 7; int r = 0; if (x > 5 && x < 10) { r = r + 1; } '
               'if (x < 0 || x == 7) { r = r + 10; } if (x == 8) { r = r + 100; } '
               'print(r); print(240 & 15); print(1 << 8); print(255 >> 4);'),
    ("nested", 'fn ack(int m, int n) -> int ={ if (m == 0) { return n + 1; } '
               'if (n == 0) { return ack(m - 1, 1); } return ack(m - 1, ack(m, n - 1)); } '
               'print(ack(2, 3));'),
]

def vm_output(src, tag):
    cf = os.path.join(TMP, "wasm_" + tag + ".cryo"); open(cf, "w", encoding="utf-8").write(src)
    pyro = os.path.join(TMP, "wasm_" + tag + ".pyro")
    subprocess.run([sys.executable, CRYOC, cf, "--backend", "pyro", "-o", pyro, "--no-banner"],
                   capture_output=True)
    r = subprocess.run([VM_BIN, pyro], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.replace("\r\n", "\n").strip()

print(f"[9.6] WebAssembly backend   Node: {'yes' if NODE else 'none (wasm run skipped)'}")
hpath = os.path.join(TMP, "wasm_harness.js")
if NODE:
    open(hpath, "w", encoding="utf-8").write(_HARNESS)

for tag, src in PROGRAMS:
    data = compiler.compile_source(src, "wasm", safe=True)
    ok_gen = isinstance(data, (bytes, bytearray)) and data[:4] == b"\x00asm"
    check(f"[{tag}] compiles to a wasm module", ok_gen)
    if not ok_gen:
        continue
    wpath = os.path.join(TMP, "wasm_" + tag + ".wasm")
    with open(wpath, "wb") as f:
        f.write(data)
    if NODE:
        r = subprocess.run([NODE, hpath, wpath], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        wout = r.stdout.replace("\r\n", "\n").strip()
        expect = vm_output(src, tag)
        if wout != expect:
            print(f"    wasm: {wout!r}\n    vm  : {expect!r}\n    err : {r.stderr.strip()[:120]}")
        check(f"[{tag}] wasm output == Pyro VM output", wout == expect)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
