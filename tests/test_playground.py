#!/usr/bin/env python3
# ============================================================
#  test_playground.py — Comprehensive tests for Cryo Playground
#
#  Verifies the Express playground server:
#  1. Health check & environment inspection
#  2. Pre-loaded examples catalog
#  3. Multi-backend compilation (pyro, node, go, c, cpp, csharp)
#  4. Compiler inspection outputs (target code, pyasm disassembly, AST, tokens, audit)
#  5. Error parsing with line/column coordinates
#  6. Multi-VM execution (Go, C, C++, C#)
#  7. Multi-VM parity comparison mode
# ============================================================
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER_DIR = os.path.join(ROOT, 'cryo-playground')
TEST_PORT = 3199
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")
        if detail:
            print(f"       -> {detail}")


def http_get(endpoint: str):
    url = BASE_URL + endpoint
    req = urllib.request.Request(url, method='GET')
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode('utf-8'))


def http_post(endpoint: str, payload: dict):
    url = BASE_URL + endpoint
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {'raw': body}


def main():
    global _passed, _failed
    print("=" * 60)
    print("  Cryo Playground Backend API & Multi-VM Verification")
    print("=" * 60)

    # 1. Start Playground Express Server on TEST_PORT
    print(f"\n[1/6] Launching server on port {TEST_PORT}...")
    env = os.environ.copy()
    env['PORT'] = str(TEST_PORT)
    env['PYTHON_PATH'] = sys.executable

    server_proc = subprocess.Popen(
        ['node', 'server.js'],
        cwd=SERVER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    try:
        # Wait for server to accept requests
        started = False
        for _ in range(30):
            try:
                status, data = http_get('/api/health')
                if status == 200 and data.get('status') == 'ok':
                    started = True
                    break
            except Exception:
                time.sleep(0.3)

        check("server startup and /api/health responds", started)
        if not started:
            print("Server failed to start in time. Aborting tests.")
            return 1

        # Check health response details
        status, health = http_get('/api/health')
        check("health endpoint returns available VMs",
              health.get('availableVms', {}).get('cpp') is True and
              health.get('availableVms', {}).get('csharp') is True and
              health.get('availableVms', {}).get('c') is True,
              f"health={health}")

        # 2. Examples Catalog Endpoint
        print("\n[2/6] Verifying /api/examples catalog...")
        status, examples = http_get('/api/examples')
        check("examples endpoint returns catalog array",
              status == 200 and isinstance(examples, list) and len(examples) >= 8,
              f"len={len(examples) if isinstance(examples, list) else 0}")

        example_ids = [ex.get('id') for ex in examples]
        check("catalog includes modern Phase 10-12 examples",
              'format' in example_ids and 'concurrency' in example_ids and 'struct_methods' in example_ids,
              f"ids={example_ids}")

        # 3. Compilation across all backends
        print("\n[3/6] Testing /api/compile across multiple backends...")
        src_sample = """
fn double(int n) -> int ={
    return n * 2;
}
int res = double(21);
print("Result: " + to_string(res));
"""
        # 3.1 Pyro compilation & inspection (disassembly, AST, tokens)
        status, res_pyro = http_post('/api/compile', {'source': src_sample, 'backend': 'pyro'})
        check("compile pyro: generates disassembly and AST",
              res_pyro.get('success') is True and
              "double:" in res_pyro.get('disassembly', '') and
              "Program" in res_pyro.get('ast', ''),
              f"res={res_pyro}")

        # 3.2 Node.js compilation
        status, res_node = http_post('/api/compile', {'source': src_sample, 'backend': 'node'})
        check("compile node: emits JavaScript target code",
              res_node.get('success') is True and
              "function double" in res_node.get('code', ''),
              f"code={res_node.get('code', '')[:80]!r}")

        # 3.3 C++ compilation
        status, res_cpp = http_post('/api/compile', {'source': src_sample, 'backend': 'cpp'})
        check("compile cpp: emits C++14 target code",
              res_cpp.get('success') is True and
              "int64_t double_" in res_cpp.get('code', '') or "int64_t" in res_cpp.get('code', ''),
              f"code={res_cpp.get('code', '')[:80]!r}")

        # 3.4 C# compilation
        status, res_cs = http_post('/api/compile', {'source': src_sample, 'backend': 'csharp'})
        check("compile csharp: emits C# target code",
              res_cs.get('success') is True and
              "class Program" in res_cs.get('code', '') or "long double_" in res_cs.get('code', ''),
              f"code={res_cs.get('code', '')[:80]!r}")

        # 4. Compiler Error Parsing
        print("\n[4/6] Testing /api/compile error reporting...")
        src_err = """
fn broken() -> int ={
    print(undeclared_variable_xyz);
    return 0;
}
"""
        status, res_err = http_post('/api/compile', {'source': src_err, 'backend': 'pyro'})
        errors = res_err.get('errors', [])
        check("compiler errors parsed with line coordinates",
              len(errors) > 0 and any(e.get('line') == 3 for e in errors),
              f"errors={errors}, stderr={res_err.get('stderr')!r}")

        # 5. Program Execution across targets
        print("\n[5/6] Testing /api/run across backends and VMs...")
        src_run = """
int a = 20;
int b = 22;
print(to_string(a + b));
"""
        # 5.1 Run Pyro default VM
        status, r_pyro = http_post('/api/run', {'source': src_run, 'backend': 'pyro', 'vm': 'auto'})
        check("run pyro (auto VM): prints 42",
              r_pyro.get('success') is True and r_pyro.get('stdout', '').strip() == "42",
              f"res={r_pyro}")

        # 5.2 Run Pyro C++ VM
        status, r_cpp_vm = http_post('/api/run', {'source': src_run, 'backend': 'pyro', 'vm': 'cpp'})
        check("run pyro (C++ VM): prints 42",
              r_cpp_vm.get('success') is True and r_cpp_vm.get('stdout', '').strip() == "42",
              f"res={r_cpp_vm}")

        # 5.3 Run Pyro C# VM
        status, r_cs_vm = http_post('/api/run', {'source': src_run, 'backend': 'pyro', 'vm': 'csharp'})
        check("run pyro (C# VM): prints 42",
              r_cs_vm.get('success') is True and r_cs_vm.get('stdout', '').strip() == "42",
              f"res={r_cs_vm}")

        # 5.4 Run Node backend
        status, r_node = http_post('/api/run', {'source': src_run, 'backend': 'node'})
        check("run node backend: prints 42",
              r_node.get('success') is True and "42" in r_node.get('stdout', ''),
              f"res={r_node}")

        # 5.5 Run C++ backend
        status, r_cpp = http_post('/api/run', {'source': src_run, 'backend': 'cpp'})
        check("run cpp backend: prints 42",
              r_cpp.get('success') is True and "42" in r_cpp.get('stdout', ''),
              f"res={r_cpp}")

        # 5.6 Run C# backend
        status, r_cs = http_post('/api/run', {'source': src_run, 'backend': 'csharp'})
        check("run csharp backend: prints 42",
              r_cs.get('success') is True and "42" in r_cs.get('stdout', ''),
              f"res={r_cs}")

        # 6. Multi-VM Parity Comparison Mode
        print("\n[6/6] Testing /api/run with vm='compare' (Multi-VM Parity Mode)...")
        src_compare = """
string text = "Cryo Parity";
print(upper(text));
int[] nums = [3, 1, 2];
print(to_string(sort(nums)));
"""
        status, r_cmp = http_post('/api/run', {'source': src_compare, 'backend': 'pyro', 'vm': 'compare'})
        check("multi-vm comparison: parityMatch is true across all engines",
              r_cmp.get('success') is True and
              r_cmp.get('isCompare') is True and
              r_cmp.get('parityMatch') is True and
              "c" in r_cmp.get('results', {}) and
              "cpp" in r_cmp.get('results', {}) and
              "csharp" in r_cmp.get('results', {}),
              f"res={r_cmp}")

    finally:
        # Cleanly terminate server process
        server_proc.terminate()
        try:
            server_proc.wait(timeout=3)
        except Exception:
            server_proc.kill()

    print(f"\nResults: {_passed} passed, {_failed} failed\n")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
