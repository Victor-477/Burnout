#!/usr/bin/env python3
# ============================================================
#  Burnout — Go VM vs C VM Parity Test (Pyro)
# ============================================================
import os
import sys
import subprocess
import tempfile

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_here = os.path.dirname(os.path.abspath(__file__))   # Burnout/tests/
_root = os.path.dirname(os.path.dirname(_here))       # project root

CRYO_EXAMPLES = os.path.join(_root, "Cryo", "examples")
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
GO_VM = os.path.join(_root, "Pyro", "vm", "pyrovm_go.exe")

C_VM_BIN = os.path.join(_root, "Pyro", "vm", "pyrovm.exe")
_SRC = [os.path.join(_root, "Pyro", "vm", "main.c"),
        os.path.join(_root, "Pyro", "vm", "pyro_runtime.c")]
# pyro_runtime.c uses sockets for http_serve(), so Windows links winsock too.
_SYSLIBS = ["-lm"] + (["-lws2_32"] if sys.platform == "win32" else [])

def _c_vm_build_cmd():
    """Pick a C toolchain: gcc/clang if on PATH, else MSVC via vcvars.

    Both compilers are told to read the sources as UTF-8, so the accented
    error messages stay byte-identical to the Go VM's — essential for the
    stderr parity checks below. Returns (cmd, use_shell) or None.
    """
    import shutil
    for cc in ("gcc", "clang", "cc"):
        if shutil.which(cc):
            return ([cc, "-O2", "-std=c11", "-finput-charset=UTF-8",
                     "-fexec-charset=UTF-8", "-o", C_VM_BIN] + _SRC + _SYSLIBS, False)
    for vc in (r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat",
               r"C:\Program Files (x86)\Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat"):
        if os.path.exists(vc):
            return (f'call "{vc}" && cl /O2 /utf-8 /Fe:{C_VM_BIN} ' + " ".join(_SRC) + ' ws2_32.lib', True)
    return None

def compile_c_vm():
    print("Compiling Go VM...")
    go_build = ["go", "build", "-o", GO_VM, os.path.join(_root, "Pyro", "vm", "main.go")]
    res_go = subprocess.run(go_build, capture_output=True, text=True)
    if res_go.returncode != 0:
        print("Go VM compilation error:")
        print(res_go.stderr)
        sys.exit(1)

    built = _c_vm_build_cmd()
    if built is None:
        print("No C toolchain (gcc/clang/cl) found — C VM parity test skipped.")
        sys.exit(0)
    cmd, use_shell = built
    print(f"Compiling C VM ({cmd if use_shell else cmd[0]})...")
    res = subprocess.run(cmd, shell=use_shell, capture_output=True, text=True)
    if res.returncode != 0:
        print("C VM compilation error:")
        print(res.stdout)
        print(res.stderr)
        sys.exit(1)
    print("VMs compiled successfully!")

def run_command(args, stdin=""):
    try:
        res = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=5)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -999, "", "Timeout"

def test_parity():
    compile_c_vm()
    
    examples = sorted([f for f in os.listdir(CRYO_EXAMPLES) if f.startswith("example_") and f.endswith(".cryo")])
    
    passed = 0
    failed = 0
    skipped = 0
    
    # Additionally, we could temporarily disable the network if we didn't want to test real HTTP,
    # but both the Go VM and the C VM must make an identical request.
    
    for filename in examples:
        filepath = os.path.join(CRYO_EXAMPLES, filename)
        
        # We test whether the file is compilable with the pyro backend
        with tempfile.NamedTemporaryFile(suffix=".pyro", delete=False) as tmp:
            tmp_pyro = tmp.name
        
        # Compile using cryoc.py
        comp_args = [sys.executable, CRYOC, filepath, "--backend", "pyro", "-o", tmp_pyro, "--no-banner"]
        c_code, c_out, c_err = run_command(comp_args)
        
        if c_code != 0:
            # if it does not compile with the pyro backend, we just skip it (e.g. asm, node, etc.)
            skipped += 1
            os.remove(tmp_pyro)
            continue
            
        print(f"Testing {filename}...")
        
        # Run on the Go VM
        go_args = [GO_VM, tmp_pyro]
        go_code, go_out, go_err = run_command(go_args)
        
        # Run on the C VM
        C_VM = os.path.join(_root, "Pyro", "vm", "pyrovm.exe")
        c_args = [C_VM, tmp_pyro]
        cvm_code, cvm_out, cvm_err = run_command(c_args)
        
        # Clean up temp file
        try:
            os.remove(tmp_pyro)
        except OSError:
            pass
            
        # Compare outputs
        # The 'go run' output may include some warnings or go-compiling messages, so
        # we must filter them or ensure stderr is clean.
        # In the Go VM case, if run directly from main.go, the output is clean.
        
        # Normalize line breaks
        go_out_norm = go_out.replace("\r\n", "\n")
        cvm_out_norm = cvm_out.replace("\r\n", "\n")
        
        go_err_norm = go_err.replace("\r\n", "\n").strip()
        cvm_err_norm = cvm_err.replace("\r\n", "\n").strip()
        
        # for stderr-comparison purposes, the Go compiler may put warnings or messages on stderr if go run recompiles.
        # but the stderr of the running program itself must be compared.

        # Let's check whether the standard outputs are identical
        if go_code != cvm_code or go_out_norm != cvm_out_norm:
            print(f"[FAIL] Parity failure in {filename}")
            print(f"Exit Codes: Go={go_code}, C={cvm_code}")
            if go_out_norm != cvm_out_norm:
                print("--- Output Go ---")
                print(repr(go_out_norm))
                print("--- Output C ---")
                print(repr(cvm_out_norm))
            if go_err_norm != cvm_err_norm:
                print("--- Stderr Go ---")
                print(repr(go_err_norm))
                print("--- Stderr C ---")
                print(repr(cvm_err_norm))
            failed += 1
        else:
            print(f"[OK] {filename} ok")
            passed += 1
            
    # ── ABORT parity (stdout + stderr + exit code) ─────────
    # Programs that abort must produce identical messages/stack traces
    # in both VMs — the loop above only covers clean executions.
    C_VM = os.path.join(_root, "Pyro", "vm", "pyrovm.exe")
    aborts = [
        ("div-zero",     'int x = 10; int y = 0; print(x / y);', []),
        ("array-oob",    'int[] a = [1, 2]; print(a[5]);', []),
        ("array-set-oob",'int[] a = [1, 2]; a[5] = 9;', []),
        ("string-oob",   'string s = "hi"; print(s[9]);', []),
        ("uncaught",     'throw("boom");', []),
        ("unwrap-null",  'int? x = null; int y = x!; print(y);', []),
        ("assert-fail",  'assert(1 == 2, "nope");', []),
        ("sandbox-http", 'string b = http_get("http://127.0.0.1:9/x"); print(b);', ["--sandbox"]),
        # try/catch: the captured value must be identical in both VMs
        ("catch-throw",  'try { throw("x"); } catch (string e) { print("cap: " + e); }', []),
        ("catch-assert", 'try { assert(false, "boom"); } catch (string e) { print(e); }', []),
        ("catch-unwrap", 'try { int? z = null; int y = z!; print(y); } catch (string e) { print(e); }', []),
    ]
    print("\n-- abort parity (stdout+stderr+exit) --")
    for name, src, extra in aborts:
        with tempfile.NamedTemporaryFile(suffix=".cryo", delete=False, mode="w", encoding="utf-8") as tc:
            tc.write(src); src_path = tc.name
        with tempfile.NamedTemporaryFile(suffix=".pyro", delete=False) as tp:
            pyro_path = tp.name
        comp = [sys.executable, CRYOC, src_path, "--backend", "pyro", "-o", pyro_path, "--no-banner"] + extra
        rc, _o, _e = run_command(comp)
        if rc != 0:
            print(f"[FAIL] abort '{name}': did not compile to pyro: {_e.strip()[:120]}")
            failed += 1
            for p in (src_path, pyro_path):
                try: os.remove(p)
                except OSError: pass
            continue
        gc, go_o, go_e = run_command([GO_VM, pyro_path])
        cc, c_o, c_e = run_command([C_VM, pyro_path])
        for p in (src_path, pyro_path):
            try: os.remove(p)
            except OSError: pass
        go_o, c_o = go_o.replace("\r\n","\n"), c_o.replace("\r\n","\n")
        go_e, c_e = go_e.replace("\r\n","\n").strip(), c_e.replace("\r\n","\n").strip()
        if gc == cc and go_o == c_o and go_e == c_e:
            print(f"[OK] abort '{name}' (exit={cc})")
            passed += 1
        else:
            print(f"[FAIL] abort '{name}'")
            print(f"  exit: Go={gc} C={cc}")
            if go_o != c_o: print(f"  stdout Go={go_o!r} C={c_o!r}")
            if go_e != c_e: print(f"  stderr Go={go_e!r}\n         C={c_e!r}")
            failed += 1

    # ── write_bytes primitive parity (binary I/O) ─────────
    print("\n-- write_bytes parity (identical bytes written) --")
    wb_out = os.path.join(tempfile.gettempdir(), "pyro_wb_parity.bin").replace("\\", "/")
    wb_vals = [80, 89, 82, 79, 1, 2, 3, 255, 0, 10]
    wb_expected = bytes(wb_vals)
    wb_src = ('int[] b = [' + ",".join(str(v) for v in wb_vals) + ']; '
              'bool ok = write_bytes("' + wb_out + '", b); print(ok);')
    with tempfile.NamedTemporaryFile(suffix=".cryo", delete=False, mode="w", encoding="utf-8") as tc:
        tc.write(wb_src); wb_cryo = tc.name
    with tempfile.NamedTemporaryFile(suffix=".pyro", delete=False) as tp:
        wb_pyro = tp.name
    rc, _o, _e = run_command([sys.executable, CRYOC, wb_cryo, "--backend", "pyro",
                              "-o", wb_pyro, "--no-banner"])
    def _run_and_read(vm):
        try: os.remove(wb_out)
        except OSError: pass
        code, out, _ = run_command([vm, wb_pyro])
        data = None
        try:
            with open(wb_out, "rb") as fh: data = fh.read()
        except OSError: pass
        return code, out.replace("\r\n", "\n").strip(), data
    if rc != 0:
        print("[FAIL] write_bytes: did not compile to pyro"); failed += 1
    else:
        gcode, gout, gdata = _run_and_read(GO_VM)
        ccode, cout, cdata = _run_and_read(C_VM)
        ok = (gout == cout == "true" and gdata == cdata == wb_expected)
        if ok:
            print(f"[OK] write_bytes writes {len(wb_expected)} identical bytes (Go == C == expected)")
            passed += 1
        else:
            print("[FAIL] write_bytes divergiu")
            print(f"  stdout Go={gout!r} C={cout!r}")
            print(f"  bytes  Go={gdata!r} C={cdata!r} expected={wb_expected!r}")
            failed += 1
    for p in (wb_cryo, wb_pyro):
        try: os.remove(p)
        except OSError: pass

    # ── args() / read_file() parity ───────────────────────
    print("\n-- args/read_file parity --")
    rf_target = os.path.join(_root, "Cryo", "examples", "fullstack", "client.cryo").replace("\\", "/")
    nat_src = ('string[] a = args(); print("argc=" + to_string(len(a))); '
               'for (int i = 0; i < len(a); i++) { print(a[i]); } '
               'print("len=" + to_string(len(read_file("' + rf_target + '")))); '
               'print("missing=[" + read_file("no/such/file.txt") + "]");')
    with tempfile.NamedTemporaryFile(suffix=".cryo", delete=False, mode="w", encoding="utf-8") as tc:
        tc.write(nat_src); nat_cryo = tc.name
    with tempfile.NamedTemporaryFile(suffix=".pyro", delete=False) as tp:
        nat_pyro = tp.name
    rc, _o, _e = run_command([sys.executable, CRYOC, nat_cryo, "--backend", "pyro",
                              "-o", nat_pyro, "--no-banner"])
    if rc != 0:
        print("[FAIL] args/read_file: did not compile to pyro"); failed += 1
    else:
        gc_, go_o, _ = run_command([GO_VM, nat_pyro, "alpha", "beta"])
        cc_, c_o, _ = run_command([C_VM, nat_pyro, "alpha", "beta"])
        go_o, c_o = go_o.replace("\r\n", "\n").strip(), c_o.replace("\r\n", "\n").strip()
        # must agree with each other AND actually see the args / read the file
        want = "argc=2\nalpha\nbeta"
        if gc_ == cc_ and go_o == c_o and go_o.startswith(want) and "missing=[]" in go_o:
            print(f"[OK] args/read_file identical (Go == C)")
            passed += 1
        else:
            print("[FAIL] args/read_file diverged")
            print(f"  exit Go={gc_} C={cc_}")
            print(f"  stdout Go={go_o!r}\n         C={c_o!r}")
            failed += 1
    for p in (nat_cryo, nat_pyro):
        try: os.remove(p)
        except OSError: pass

    # ── http_serve() parity ───────────────────────────────
    # Both VMs serve the same directory on their own port; every response
    # (status, content-type, body) must be identical.
    print("\n-- http_serve parity --")
    import socket, time, urllib.request, urllib.error
    serve_dir = os.path.join(_root, "Cryo", "examples", "fullstack", "public")
    srv_src = ('string[] a = args(); http_serve(to_int(a[1]), a[0]);')
    with tempfile.NamedTemporaryFile(suffix=".cryo", delete=False, mode="w", encoding="utf-8") as tc:
        tc.write(srv_src); srv_cryo = tc.name
    with tempfile.NamedTemporaryFile(suffix=".pyro", delete=False) as tp:
        srv_pyro = tp.name
    rc, _o, _e = run_command([sys.executable, CRYOC, srv_cryo, "--backend", "pyro",
                              "-o", srv_pyro, "--no-banner"])
    if rc != 0 or not os.path.isdir(serve_dir):
        print("[SKIP] http_serve: no compiled server or demo dir absent")
    else:
        def _free_port():
            s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
        def _probe(vm):
            port = _free_port()
            proc = subprocess.Popen([vm, srv_pyro, serve_dir, str(port)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = f"http://127.0.0.1:{port}"
            res = {}
            try:
                for _ in range(60):
                    try:
                        urllib.request.urlopen(base + "/", timeout=1).read(); break
                    except urllib.error.HTTPError:
                        break
                    except Exception:
                        time.sleep(0.1)
                for name, path in (("index", "/"), ("wasm", "/app.wasm"),
                                   ("missing", "/nope.txt")):
                    try:
                        r = urllib.request.urlopen(base + path, timeout=2)
                        res[name] = (r.status, r.headers.get("Content-Type", ""), r.read())
                    except urllib.error.HTTPError as e:
                        res[name] = (e.code, "", b"")
                    except Exception as e:
                        res[name] = ("ERR", str(e), b"")
            finally:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill()
            return res
        gres, cres = _probe(GO_VM), _probe(C_VM)
        for name in ("index", "wasm", "missing"):
            g, c = gres.get(name), cres.get(name)
            if g == c and g is not None and g[0] in (200, 404):
                print(f"[OK] http_serve {name}: Go == C (status={g[0]}, type={g[1] or 'n/a'})")
                passed += 1
            else:
                print(f"[FAIL] http_serve {name} diverged")
                print(f"  Go={None if g is None else (g[0], g[1], len(g[2]))}")
                print(f"  C ={None if c is None else (c[0], c[1], len(c[2]))}")
                failed += 1
    for p in (srv_cryo, srv_pyro):
        try: os.remove(p)
        except OSError: pass

    print(f"\nSummary: {passed} passed, {failed} failed, {skipped} skipped.")
    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    test_parity()
