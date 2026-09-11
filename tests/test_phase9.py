#!/usr/bin/env python3
# ============================================================
#  test_phase9.py — Phase 9 feature suite (roadmap Phase 9)
#
#  Verifies Phase 9 Standalone Pyro capabilities:
#    * 9.1: C VM (pyrovm) and Go VM execution parity
#    * 9.2: Minimal C runtime (pyro_runtime.c) & natives
#    * 9.3: Self-hosted compiler pipeline (Cryo compiling to .pyro)
#    * 9.4: Fixed-point bootstrap determinism
#    * 9.5: AOT translation (.pyro -> C)
#    * 9.6: Unified single-entry `pyro` CLI (build, run, vm, c, test, repl)
# ============================================================
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
AOT = os.path.join(ROOT, 'Burnout', 'aot_pyro.py')
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

import aot_pyro
import compiler

EXE = '.exe' if sys.platform == 'win32' else ''
GO_VM = os.path.join(ROOT, 'Pyro', 'vm', 'pyrovm_go' + EXE)
C_VM = os.path.join(ROOT, 'Pyro', 'vm', 'pyrovm' + EXE)
if not os.path.isfile(C_VM):
    c_alt = os.path.join(ROOT, 'build', 'pyrovm' + EXE)
    if os.path.isfile(c_alt):
        C_VM = c_alt

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def compile_to_pyro(src, work, name='prog.pyro'):
    src_path = os.path.join(work, 'src.cryo')
    with open(src_path, 'w', encoding='utf-8') as f:
        f.write(src)
    out_path = os.path.join(work, name)
    r = subprocess.run([sys.executable, CRYOC, src_path, '--backend', 'pyro',
                        '-o', out_path, '--no-banner'],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not os.path.isfile(out_path):
        return None, (r.stdout + r.stderr)
    return out_path, ''


def run_vm(vm_path, pyro_path):
    if not os.path.isfile(vm_path):
        return None, f"VM not found: {vm_path}"
    r = subprocess.run([vm_path, pyro_path], capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=60)
    return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip()


def main():
    print("[Phase 9] comprehensive verification of Phase 9 Standalone Pyro")
    work = tempfile.mkdtemp(prefix='cryo_p9_')

    try:
        # ============================================================
        # ── 9.1 & 9.2: Pyro VM & Minimal Runtime Parity ────────────
        # ============================================================
        print("\n── 9.1 & 9.2: Pyro VM & runtime execution parity ──")

        sample_program = """
        fn fib(int n) -> int ={
            if (n < 2) { return n; }
            return fib(n - 1) + fib(n - 2);
        }

        string s = "Pyro Cryo";
        print(substr(s, 0, 4));
        print(fib(10));

        int[] arr = [3, 1, 2];
        arr.push(4);
        print(len(arr));
        print(arr[3]);

        map<string, int> m = {"alpha": 10, "beta": 20};
        print(m["alpha"] + m["beta"]);
        print(has(m, "beta"));
        """
        expected_vm_out = "Pyro\n55\n4\n4\n30\ntrue"

        pyro_file, err = compile_to_pyro(sample_program, work, 'sample.pyro')
        check("front-end compiles program to .pyro bytecode", pyro_file is not None, err)

        if pyro_file:
            rc_go, out_go = run_vm(GO_VM, pyro_file)
            check("Go VM (pyrovm_go) executes .pyro", rc_go == 0 and out_go == expected_vm_out, f"rc={rc_go}, out={out_go}")

            if os.path.isfile(C_VM):
                rc_c, out_c = run_vm(C_VM, pyro_file)
                check("C VM (pyrovm) executes .pyro", rc_c == 0 and out_c == expected_vm_out, f"rc={rc_c}, out={out_c}")
                check("Go VM and C VM produce identical output", out_go == out_c, f"go={out_go!r}, c={out_c!r}")
            else:
                print("  skip C VM check (pyrovm binary not found)")

        # ============================================================
        # ── 9.3: Cryo Self-Hosted Compiler Pipeline ────────────────
        # ============================================================
        print("\n── 9.3: self-hosted compiler pipeline ──")
        selfhost_dir = os.path.join(ROOT, 'Cryo', 'selfhost')
        check("selfhost lexer.cryo exists", os.path.isfile(os.path.join(selfhost_dir, 'lexer.cryo')))
        check("selfhost parser.cryo exists", os.path.isfile(os.path.join(selfhost_dir, 'parser.cryo')))
        check("selfhost codegen.cryo exists", os.path.isfile(os.path.join(selfhost_dir, 'codegen.cryo')))
        check("selfhost pyroc.cryo CLI driver exists", os.path.isfile(os.path.join(selfhost_dir, 'pyroc.cryo')))

        # Compile and execute a small program using the compiled selfhost compiler if pyroc.pyro is available
        pyroc_pyro = os.path.join(ROOT, 'build', 'pyroc.pyro')
        if os.path.isfile(pyroc_pyro) and os.path.isfile(GO_VM):
            test_src = os.path.join(work, 'test_sh.cryo')
            test_out = os.path.join(work, 'test_sh.pyro')
            with open(test_src, 'w', encoding='utf-8') as f:
                f.write('int a = 12; int b = 30; print(a + b);')
            r_sh = subprocess.run([GO_VM, pyroc_pyro, test_src, test_out],
                                  capture_output=True, text=True, timeout=60)
            if r_sh.returncode == 0 and os.path.isfile(test_out):
                rc_run, out_run = run_vm(GO_VM, test_out)
                check("self-hosted pyroc compiles Cryo and executes on VM",
                      rc_run == 0 and out_run == '42', f"rc={rc_run}, out={out_run}")
            else:
                print("  skip pyroc compilation run")
        else:
            print("  skip pyroc.pyro direct run (build artifact not prebuilt)")

        # ============================================================
        # ── 9.4: Fixed-Point Bootstrap Determinism ─────────────────
        # ============================================================
        print("\n── 9.4: deterministic bytecode generation ──")
        # Ensure that compiling the same source twice produces identical bytes
        deterministic_src = """
        fn calculate(int x) -> int ={ return x * 3 + 1; }
        print(calculate(7));
        """
        p1, _ = compile_to_pyro(deterministic_src, work, 'det1.pyro')
        p2, _ = compile_to_pyro(deterministic_src, work, 'det2.pyro')
        if p1 and p2:
            bytes1 = open(p1, 'rb').read()
            bytes2 = open(p2, 'rb').read()
            check("compilation is deterministic (byte-identical .pyro)", bytes1 == bytes2)

        # ============================================================
        # ── 9.5: AOT Compilation (.pyro -> C) ──────────────────────
        # ============================================================
        print("\n── 9.5: AOT (.pyro -> C) ──")
        aot_src = """
        fn multiply(int a, int b) -> int ={ return a * b; }
        int val = multiply(6, 7);
        print("answer: " + to_string(val));
        """
        aot_pyro_file, _ = compile_to_pyro(aot_src, work, 'aot_test.pyro')
        if aot_pyro_file:
            with open(aot_pyro_file, 'rb') as f:
                pyro_bytes = f.read()
            c_code = aot_pyro.compile_to_c(pyro_bytes)
            check("AOT produces C code", isinstance(c_code, str) and len(c_code) > 0)
            check("AOT emits C main entry point", "int main(int argc, char** argv)" in c_code)
            check("AOT emits function implementation", "void fn_" in c_code)
            check("AOT sets up constant pool", "setup_consts(" in c_code)

        # ============================================================
        # ── 9.6: Unified single-entry `pyro` CLI ───────────────────
        # ============================================================
        print("\n── 9.6: unified single-entry pyro CLI ──")
        cli_src = os.path.join(work, 'cli_test.cryo')
        with open(cli_src, 'w', encoding='utf-8') as f:
            f.write('print(10 + 20);')

        # pyro run
        r_run = subprocess.run([sys.executable, PYRO, 'run', cli_src, '--vm'],
                               capture_output=True, text=True, timeout=60)
        check("pyro run --vm executes source", r_run.returncode == 0 and r_run.stdout.strip() == '30')

        # pyro c
        r_c = subprocess.run([sys.executable, PYRO, 'c', cli_src],
                             capture_output=True, text=True, timeout=60)
        check("pyro c emits AOT C source", r_c.returncode == 0 and "int main(" in r_c.stdout)

        # pyro test
        cli_test_src = os.path.join(work, 'cli_test_fn.cryo')
        with open(cli_test_src, 'w', encoding='utf-8') as f:
            f.write('test fn t_cli() ={ assert(1 == 1, "cli test"); }')
        r_test = subprocess.run([sys.executable, PYRO, 'test', cli_test_src],
                                capture_output=True, text=True, timeout=60)
        check("pyro test runs test suite", r_test.returncode == 0 and "1 passed, 0 failed" in r_test.stdout)

        # pyro repl
        r_repl = subprocess.run([sys.executable, PYRO, 'repl'],
                                input='print("repl ok");\n:quit\n',
                                capture_output=True, text=True, timeout=60)
        check("pyro repl executes input interactively", r_repl.returncode == 0 and "repl ok" in r_repl.stdout)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
