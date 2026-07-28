#!/usr/bin/env python3
# ============================================================
#  Burnout — Full-stack demo verification
#
#  Proves the Cryo full-stack story end to end:
#    * CLIENT  — client.cryo compiled with the wasm backend, instantiated in
#                Node, whose *exported* functions (fib/square/factorial/sum_to)
#                are called directly and checked against known values.
#    * SERVER  — server.cryo compiled by the self-hosted pyroc and run on the
#                Pyro VM; the built-in http_serve native must serve index.html,
#                glue.js and app.wasm (the latter as application/wasm).
#
#  Generation is always checked. The Node run and the live server run are each
#  skipped cleanly when their toolchain (node / built VM + pyroc.pyro) is absent.
# ============================================================
import os
import sys
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(_here))
sys.path.insert(0, os.path.join(_root, "Burnout"))
sys.path.insert(0, os.path.join(_root, "Cryo"))
import compiler

DEMO = os.path.join(_root, "Cryo", "examples", "fullstack")
PUBLIC = os.path.join(DEMO, "public")
CLIENT = os.path.join(DEMO, "client.cryo")
SERVER = os.path.join(DEMO, "server.cryo")
VM_BIN = os.path.join(_root, "build", "pyrovm.exe" if sys.platform == "win32" else "pyrovm")
PYROC = os.path.join(_root, "build", "pyroc.pyro")
TMP = tempfile.gettempdir()
NODE = shutil.which("node")

# Instantiate app.wasm, call each export over a range of n, print JSON results.
_HARNESS = r"""
const fs = require('fs');
const buf = fs.readFileSync(process.argv[2]);
WebAssembly.instantiate(buf, { env: { log: () => {} } })
  .then(({instance}) => {
    const e = instance.exports, out = {};
    for (const n of [0, 1, 10, 15, 20]) {
      out[n] = {
        fib: e.fib(BigInt(n)).toString(),
        square: e.square(BigInt(n)).toString(),
        factorial: e.factorial(BigInt(n)).toString(),
        sum_to: e.sum_to(BigInt(n)).toString(),
      };
    }
    process.stdout.write(JSON.stringify(out));
  })
  .catch(err => { console.error('WASM error:', err.message); process.exit(1); });
"""

def _fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

def _fact(n):
    f = 1
    for i in range(1, n + 1):
        f *= i
    return f

_passed = _failed = 0
def check(desc, cond):
    global _passed, _failed
    if cond: _passed += 1; print(f"  ok   {desc}")
    else: _failed += 1; print(f"  FAIL {desc}")

print(f"[demo] full-stack   Node: {'yes' if NODE else 'none'}   "
      f"VM: {'yes' if os.path.exists(VM_BIN) else 'none'}   "
      f"pyroc: {'yes' if os.path.exists(PYROC) else 'none'}")

# ---- CLIENT: Cryo -> wasm, exports called from JS -----------------------------
src = open(CLIENT, encoding="utf-8").read()
data = compiler.compile_source(src, "wasm", safe=True)
ok_gen = isinstance(data, (bytes, bytearray)) and data[:4] == b"\x00asm"
check("client.cryo compiles to a wasm module", ok_gen)

if ok_gen and NODE:
    wpath = os.path.join(TMP, "fullstack_client.wasm")
    with open(wpath, "wb") as f:
        f.write(data)
    hpath = os.path.join(TMP, "fullstack_harness.js")
    open(hpath, "w", encoding="utf-8").write(_HARNESS)
    r = subprocess.run([NODE, hpath, wpath], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    try:
        got = json.loads(r.stdout)
    except json.JSONDecodeError:
        got = None
        print(f"    node stderr: {r.stderr.strip()[:160]}")
    check("wasm exports are callable from JS", got is not None)
    if got is not None:
        allok = True
        for n in (0, 1, 10, 15, 20):
            g = got[str(n)]
            allok &= (g["fib"] == str(_fib(n)) and g["square"] == str(n * n)
                      and g["factorial"] == str(_fact(n)) and g["sum_to"] == str(n * (n + 1) // 2))
        check("exported fib/square/factorial/sum_to match expected values", allok)

# ---- SERVER: Cryo http_serve serves the demo assets ---------------------------
if os.path.exists(VM_BIN) and os.path.exists(PYROC):
    spyro = os.path.join(TMP, "fullstack_server.pyro")
    rc = subprocess.run([VM_BIN, PYROC, SERVER, spyro], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    check("server.cryo compiles via self-hosted pyroc", rc.returncode == 0 and os.path.exists(spyro))

    if rc.returncode == 0 and os.path.exists(spyro):
        # pick a free port
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        proc = subprocess.Popen([VM_BIN, spyro, PUBLIC, str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            up = False
            for _ in range(50):
                try:
                    urllib.request.urlopen(base + "/", timeout=1).read(); up = True; break
                except Exception:
                    time.sleep(0.1)
            check("http_serve accepts connections", up)
            if up:
                idx = urllib.request.urlopen(base + "/", timeout=2)
                body = idx.read().decode("utf-8", "replace")
                check("serves index.html", "Cryo" in body and "app.wasm" not in body.split("<title>")[0])
                w = urllib.request.urlopen(base + "/app.wasm", timeout=2)
                ct = w.headers.get("Content-Type", "")
                wbytes = w.read()
                check("serves app.wasm as application/wasm", ct == "application/wasm")
                check("served app.wasm is a valid module", wbytes[:4] == b"\x00asm")
                g = urllib.request.urlopen(base + "/glue.js", timeout=2)
                check("serves glue.js", g.status == 200 and b"WebAssembly" in g.read())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
