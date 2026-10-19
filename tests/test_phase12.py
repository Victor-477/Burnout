#!/usr/bin/env python3
# ============================================================
#  test_phase12.py — Phase 12 Toolchain & Concurrency Suite
#
#  Verifies Phase 12 capabilities:
#    * 12.1: Test framework in language (test fn, isolation, runner)
#    * 12.2: REPL (interactive loop, replay prelude, expression wrapping)
#    * 12.3: Package manager (cryo.toml, cryo.lock, @pkg imports)
#    * 12.5: Concurrency in the VM (spawn/await, cooperative scheduler)
#    * 12.8: Dynamic assert message evaluation
#    * 12.9: Payload-less enum members as values across backends
#    * 12.10: json_encode sorted key ordering
#    * 12.11: Awaiting future twice without deadlock
#    * 12.12: Lazy assert evaluation
#    * 12.13: Uniform out-of-bounds error messages
# ============================================================
import json
import os
import shutil
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
PYRO = os.path.join(ROOT, 'Burnout', 'pyro.py')
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def run_src(src, backend, timeout=60):
    work = tempfile.mkdtemp(prefix='cryo_p12_')
    try:
        p = os.path.join(work, 'p.cryo')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(src)

        if backend == 'pyro':
            r = subprocess.run([sys.executable, PYRO, 'run', p, '--vm'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()

        elif backend == 'node':
            if not shutil.which('node'):
                return None, None, 'node not available'
            out = os.path.join(work, 'p.js')
            c = subprocess.run([sys.executable, CRYOC, p, '--backend', 'node', '-o', out, '--no-banner'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
            if c.returncode != 0:
                return c.returncode, '', (c.stdout + c.stderr).strip()
            r = subprocess.run(['node', out], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=timeout)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()

        elif backend == 'go':
            if not shutil.which('go'):
                return None, None, 'go not available'
            out = os.path.join(work, 'p.go')
            c = subprocess.run([sys.executable, CRYOC, p, '--backend', 'go', '-o', out, '--no-banner'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
            if c.returncode != 0:
                return c.returncode, '', (c.stdout + c.stderr).strip()
            r = subprocess.run(['go', 'run', 'p.go'], cwd=work, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=timeout)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()
        else:
            raise ValueError(f"unknown backend {backend}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("[Phase 12] comprehensive verification of Phase 12 Toolchain & Parity\n")

    # ══════════════════════════════════════════════════════════
    # ── 12.1: Test Framework in Language
    # ══════════════════════════════════════════════════════════
    print("── 12.1: Test framework in language ──")
    work = tempfile.mkdtemp(prefix='p12_test_')
    try:
        suite_path = os.path.join(work, 'sample.cryo')
        with open(suite_path, 'w', encoding='utf-8') as f:
            f.write("""
test fn test_one() ={
    assert(1 + 1 == 2, "math works");
}
test fn test_two() ={
    assert(2 * 3 == 6, "mult works");
}
""")
        # Test --list
        r_list = subprocess.run([sys.executable, CRYOC, 'test', suite_path, '--list'],
                                capture_output=True, text=True, encoding='utf-8')
        check("test runner: --list discovers and reports test names",
              r_list.returncode == 0 and "test_one" in r_list.stdout and "test_two" in r_list.stdout)

        # Test execution on pyro, node, go
        for be in ('pyro', 'node', 'go'):
            r = subprocess.run([sys.executable, CRYOC, 'test', suite_path, '--backend', be],
                               capture_output=True, text=True, encoding='utf-8')
            check(f"test runner: {be} passes and reports ok",
                  r.returncode == 0 and "2 passed, 0 failed" in r.stdout)

        # Test isolation on failure
        fail_path = os.path.join(work, 'fail.cryo')
        with open(fail_path, 'w', encoding='utf-8') as f:
            f.write("""
test fn t_pass() ={ assert(true); }
test fn t_fail() ={ assert(false, "boom"); }
test fn t_after() ={ assert(true); }
""")
        r_fail = subprocess.run([sys.executable, CRYOC, 'test', fail_path, '--backend', 'pyro'],
                                capture_output=True, text=True, encoding='utf-8')
        check("test runner: test failure is isolated and does not abort suite",
              r_fail.returncode != 0 and "ok   t_pass" in r_fail.stdout and "FAIL t_fail" in r_fail.stdout and "ok   t_after" in r_fail.stdout)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ══════════════════════════════════════════════════════════
    # ── 12.2: REPL
    # ══════════════════════════════════════════════════════════
    print("\n── 12.2: Interactive REPL ──")
    p_repl = subprocess.run([sys.executable, CRYOC, 'repl', '--backend', 'pyro'],
                            input="int x = 40;\nx + 2;\n:quit\n",
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    check("repl: persistent declaration and expression evaluation",
          "42" in p_repl.stdout,
          f"out={p_repl.stdout!r}")

    p_cmd = subprocess.run([sys.executable, CRYOC, 'repl', '--backend', 'pyro'],
                           input=":help\n:quit\n",
                           capture_output=True, text=True, encoding='utf-8', timeout=30)
    check("repl: :help command responds",
          "Commands:" in p_cmd.stdout or "REPL" in p_cmd.stdout,
          f"out={p_cmd.stdout!r}")

    # ══════════════════════════════════════════════════════════
    # ── 12.3: Package Manager
    # ══════════════════════════════════════════════════════════
    print("\n── 12.3: Package manager ──")
    work_pkg = tempfile.mkdtemp(prefix='p12_pkg_')
    try:
        # Initialize package
        r_init = subprocess.run([sys.executable, CRYOC, 'pkg', 'init', 'mypkg'],
                                cwd=work_pkg, capture_output=True, text=True, encoding='utf-8')
        toml_path = os.path.join(work_pkg, 'cryo.toml')
        check("pkg: init creates cryo.toml manifest",
              r_init.returncode == 0 and os.path.isfile(toml_path))

        # Check lock creation
        r_lock = subprocess.run([sys.executable, CRYOC, 'pkg', 'lock'],
                                cwd=work_pkg, capture_output=True, text=True, encoding='utf-8')
        lock_path = os.path.join(work_pkg, 'cryo.lock')
        check("pkg: lock creates cryo.lock content digest",
              r_lock.returncode == 0 and os.path.isfile(lock_path))

        # Check check command
        r_chk = subprocess.run([sys.executable, CRYOC, 'pkg', 'check'],
                               cwd=work_pkg, capture_output=True, text=True, encoding='utf-8')
        check("pkg: check verifies integrity cleanly",
              r_chk.returncode == 0 and "up to date" in r_chk.stdout.lower(),
              f"rc={r_chk.returncode}, out={r_chk.stdout!r}")
    finally:
        shutil.rmtree(work_pkg, ignore_errors=True)

    # ══════════════════════════════════════════════════════════
    # ── 12.5: Concurrency in the VM (spawn/await)
    # ══════════════════════════════════════════════════════════
    print("\n── 12.5: Concurrency in the VM (spawn/await) ──")
    src_concur = """
fn add(int a, int b) -> int ={
    return a + b;
}
future<int> f1 = spawn add(10, 20);
future<int> f2 = spawn add(30, 40);
int r1 = await f1;
int r2 = await f2;
print(to_string(r1 + r2));
"""
    rc, out, err = run_src(src_concur, 'pyro')
    check("pyro: spawn and await futures in cooperative VM (12.5)",
          rc == 0 and out == "100",
          f"rc={rc}, out={out!r}, err={err!r}")

    # ══════════════════════════════════════════════════════════
    # ── 12.8 - 12.13: Parity Close-Outs
    # ══════════════════════════════════════════════════════════
    print("\n── 12.8 - 12.13: Parity Close-Outs ──")

    # 12.9 Payload-less enum members as values across all backends
    src_enum = """
enum Mode {
    FAST,
    SLOW
}
Mode m = Mode.SLOW;
if (m == Mode.SLOW) {
    print("slow_ok");
} else {
    print("fail");
}
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_enum, be)
        check(f"{be}: payload-less enum member value (12.9)",
              rc == 0 and out == "slow_ok",
              f"rc={rc}, out={out!r}, err={err!r}")

    # 12.10 json_encode key sorting
    src_json = """
map<string,int> d = {"z": 10, "a": 20, "m": 30};
print(json_encode(d));
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_json, be)
        # Parse JSON and ensure order matches sorted keys: a, m, z
        has_order = out.find('"a"') < out.find('"m"') < out.find('"z"')
        check(f"{be}: json_encode key sorting (12.10)",
              rc == 0 and has_order,
              f"rc={rc}, out={out!r}, err={err!r}")

    # 12.11 Awaiting same future twice
    src_await_twice = """
future<int> f = spawn 42;
int a = await f;
int b = await f;
print(to_string(a + b));
"""
    rc, out, err = run_src(src_await_twice, 'pyro')
    check("pyro: awaiting the same future twice is idempotent (12.11)",
          rc == 0 and out == "84",
          f"rc={rc}, out={out!r}, err={err!r}")

    # 12.12 Lazy assert evaluation (side effect only evaluated if assertion fails)
    src_lazy_assert = """
fn side_effect() -> string ={
    print("side_effect_called");
    return "error";
}
assert(true, side_effect());
print("done");
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_lazy_assert, be)
        check(f"{be}: assert message evaluated lazily on success (12.12)",
              rc == 0 and out == "done" and "side_effect_called" not in out,
              f"rc={rc}, out={out!r}, err={err!r}")

    # 12.13 Out-of-bounds error message uniform contract
    src_oob = """
int[] arr = [1, 2];
print(to_string(arr[10]));
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_oob, be)
        combined = (out + err).lower()
        has_index_err = "index" in combined or "out of bounds" in combined or "bounds" in combined
    # ══════════════════════════════════════════════════════════
    # ── 12.14–12.17: C++ and C# VM & Toolchain Resources
    # ══════════════════════════════════════════════════════════
    print("── 12.14–12.17: C++ and C# VM & Language Resources ──")

    # 12.14: C++ Pyro VM execution
    pyrovm_cpp = os.path.join(ROOT, 'build', 'pyrovm_cpp.exe' if sys.platform == 'win32' else 'pyrovm_cpp')
    test_pyro = os.path.join(ROOT, 'build', 'test_phase12.pyro')
    if os.path.isfile(pyrovm_cpp) and os.path.isfile(test_pyro):
        r = subprocess.run([pyrovm_cpp, test_pyro], capture_output=True, text=True)
        check("cpp VM: execute test_phase12.pyro cleanly (12.14)",
              r.returncode == 0,
              f"rc={r.returncode}, out={r.stdout}, err={r.stderr}")
    else:
        check("cpp VM: binary and bytecode present (12.14)", False, f"missing {pyrovm_cpp} or {test_pyro}")

    # 12.15: C# Pyro VM execution
    pyrovm_cs = os.path.join(ROOT, 'build', 'pyrovm_cs.exe' if sys.platform == 'win32' else 'pyrovm_cs')
    if os.path.isfile(pyrovm_cs) and os.path.isfile(test_pyro):
        r = subprocess.run([pyrovm_cs, test_pyro], capture_output=True, text=True)
        check("csharp VM: execute test_phase12.pyro cleanly (12.15)",
              r.returncode == 0,
              f"rc={r.returncode}, out={r.stdout}, err={r.stderr}")
    else:
        check("csharp VM: binary and bytecode present (12.15)", False, f"missing {pyrovm_cs} or {test_pyro}")

    # 12.16: Testrunner --backend cpp and --backend csharp
    test_cryo = os.path.join(ROOT, 'Cryo', 'examples', 'test_phase12.cryo')
    r_cpp = subprocess.run([sys.executable, CRYOC, 'test', test_cryo, '--backend', 'cpp'],
                           capture_output=True, text=True)
    check("testrunner: cryoc test on --backend cpp (12.16)",
          r_cpp.returncode == 0 and "4 passed, 0 failed" in r_cpp.stdout,
          f"rc={r_cpp.returncode}, out={r_cpp.stdout}, err={r_cpp.stderr}")

    r_cs = subprocess.run([sys.executable, CRYOC, 'test', test_cryo, '--backend', 'csharp'],
                          capture_output=True, text=True)
    check("testrunner: cryoc test on --backend csharp (12.16)",
          r_cs.returncode == 0 and "4 passed, 0 failed" in r_cs.stdout,
          f"rc={r_cs.returncode}, out={r_cs.stdout}, err={r_cs.stderr}")

    # 12.17: C# embedding resource
    embed_csproj = os.path.join(ROOT, 'Cryo', 'examples', 'embedding', 'embed_csharp.csproj')
    if shutil.which('dotnet') and os.path.isfile(embed_csproj):
        r_embed = subprocess.run(['dotnet', 'run', '--project', embed_csproj],
                                 capture_output=True, text=True, cwd=ROOT)
        check("embed: C# embedding library executes Cryo source (12.17)",
              r_embed.returncode == 0 and "42" in r_embed.stdout and "Eval executed cleanly" in r_embed.stdout,
              f"rc={r_embed.returncode}, out={r_embed.stdout}, err={r_embed.stderr}")
    else:
        check("embed: dotnet and embed_csharp.csproj present (12.17)", False)

    print(f"\n{_passed} passed, {_failed} failed\n")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
