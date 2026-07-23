#!/usr/bin/env python3
# ============================================================
#  Pyro — single-entry CLI for the Cryo/Pyro toolchain (Phase 9.6)
#
#  Ties the whole pipeline together behind one command:
#      .cryo  --(front-end)-->  .pyro  --(AOT)-->  C  --(cc)-->  native exe
#
#  Subcommands:
#    pyro build <in> [-o out]     compile to a native binary (zero-dependency:
#                                 no VM, no .pyro, no Python/Go at runtime)
#    pyro run   <in> [--vm]       compile and run (native if a C toolchain is
#                                 present; otherwise falls back to the Pyro VM)
#    pyro vm    <in>              run on the Pyro VM (bytecode interpreter)
#    pyro c     <in> [-o out.c]   emit the AOT C source only
#
#  <in> may be a `.cryo` source or an already-compiled `.pyro`.
#  The produced native binary needs nothing at runtime — the goal of Phase 9.
# ============================================================
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
for _p in (_here, os.path.join(_root, "Cryo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import compiler          # front-end (.cryo -> .pyro)
import aot_pyro          # AOT (.pyro -> C)

VMDIR = os.path.join(_root, "Pyro", "vm")
RUNTIME = os.path.join(VMDIR, "pyro_runtime.c")
EXE = ".exe" if sys.platform == "win32" else ""


def _err(msg):
    print(f"[pyro] {msg}", file=sys.stderr)
    sys.exit(1)


def find_cc():
    """(name, build(cfile, exe)->returncode) for a usable C compiler, or (None, None)."""
    def runner(argv):
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace").returncode
    for cc in ("gcc", "clang", "cc"):
        if shutil.which(cc):
            return cc, (lambda c, e, _cc=cc: runner([_cc, "-O2", "-o", e, c, RUNTIME, "-I", VMDIR, "-lm"]))
    if shutil.which("zig"):
        return "zig cc", (lambda c, e: runner(["zig", "cc", "-O2", "-o", e, c, RUNTIME, "-I", VMDIR, "-lm"]))
    if shutil.which("cl"):
        return "cl", (lambda c, e: runner(["cl", "/O2", "/utf-8", "/I", VMDIR, "/Fe:" + e, c, RUNTIME]))
    return None, None


def find_vm():
    for c in (os.path.join(_root, "build", "pyrovm" + EXE),
              os.path.join(VMDIR, "pyrovm" + EXE)):
        if os.path.isfile(c):
            return c
    return None


def to_pyro(inp, safe=True, sandbox=False):
    """Return a path to a .pyro for `inp` (front-end compiles .cryo; passes .pyro through)."""
    if inp.endswith(".pyro"):
        return inp, None
    with open(inp, encoding="utf-8") as f:
        src = f.read()
    data = compiler.compile_source(src, "pyro", safe=safe,
                                   base_dir=os.path.dirname(os.path.abspath(inp)),
                                   sandbox=sandbox)
    if not isinstance(data, (bytes, bytearray)):
        _err("front-end did not produce bytecode")
    tmp = tempfile.NamedTemporaryFile(suffix=".pyro", delete=False)
    tmp.write(data); tmp.close()
    return tmp.name, tmp.name          # (path, temp-to-clean)


def emit_c(pyro_path):
    with open(pyro_path, "rb") as f:
        return aot_pyro.compile_to_c(f.read())


def build_native(inp, out, safe=True, sandbox=False, verbose=False):
    cc_name, build = find_cc()
    if build is None:
        _err("no C toolchain found (need gcc/clang/cc/zig/cl) — "
             "use `pyro vm` or `pyro c` instead")
    pyro_path, tmp = to_pyro(inp, safe, sandbox)
    try:
        c_src = emit_c(pyro_path)
    finally:
        if tmp and tmp != inp:
            try: os.remove(tmp)
            except OSError: pass
    with tempfile.NamedTemporaryFile(suffix=".c", delete=False, mode="w", encoding="utf-8") as cf:
        cf.write(c_src); cfile = cf.name
    try:
        if verbose:
            print(f"[pyro] {cc_name}: building {out}")
        rc = build(cfile, out)
    finally:
        try: os.remove(cfile)
        except OSError: pass
    if rc != 0 or not os.path.exists(out):
        _err(f"C toolchain ({cc_name}) failed to build {out}")
    return out


def cmd_build(a):
    base = os.path.splitext(os.path.basename(a.input))[0]
    out = a.output or (base + EXE)
    build_native(a.input, os.path.abspath(out), safe=not a.unsafe,
                 sandbox=a.sandbox, verbose=True)
    print(f"[pyro] built {out}")


def cmd_c(a):
    pyro_path, tmp = to_pyro(a.input, safe=not a.unsafe, sandbox=a.sandbox)
    try:
        c_src = emit_c(pyro_path)
    finally:
        if tmp and tmp != a.input:
            try: os.remove(tmp)
            except OSError: pass
    if a.output:
        with open(a.output, "w", encoding="utf-8") as f:
            f.write(c_src)
        print(f"[pyro] wrote {a.output}")
    else:
        sys.stdout.write(c_src)


def cmd_vm(a):
    vm = find_vm()
    if vm is None:
        _err("no Pyro VM binary found (build pyro/vm) — try `pyro build`")
    pyro_path, tmp = to_pyro(a.input, safe=not a.unsafe, sandbox=a.sandbox)
    try:
        sys.exit(subprocess.run([vm, pyro_path] + a.args).returncode)
    finally:
        if tmp and tmp != a.input:
            try: os.remove(tmp)
            except OSError: pass


def cmd_run(a):
    cc_name, _ = find_cc()
    if a.vm or cc_name is None:
        if not a.vm:
            print("[pyro] no C toolchain — running on the Pyro VM", file=sys.stderr)
        return cmd_vm(a)
    exe = os.path.join(tempfile.gettempdir(),
                       "pyro_run_" + os.path.splitext(os.path.basename(a.input))[0] + EXE)
    build_native(a.input, exe, safe=not a.unsafe, sandbox=a.sandbox)
    try:
        sys.exit(subprocess.run([exe] + a.args).returncode)
    finally:
        try: os.remove(exe)
        except OSError: pass


def main():
    ap = argparse.ArgumentParser(prog="pyro", description="Cryo/Pyro toolchain — one command")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, with_args=False):
        p.add_argument("input", help="input .cryo or .pyro")
        p.add_argument("--unsafe", action="store_true", help="turn off safety instrumentation")
        p.add_argument("--sandbox", action="store_true", help="refuse network/machine natives")
        if with_args:
            p.add_argument("args", nargs="*", help="arguments passed to the program")

    pb = sub.add_parser("build", help="compile to a native binary")
    common(pb); pb.add_argument("-o", "--output", help="output binary path")
    pb.set_defaults(fn=cmd_build)

    pr = sub.add_parser("run", help="compile and run (native, else VM)")
    common(pr, with_args=True); pr.add_argument("--vm", action="store_true", help="force VM execution")
    pr.set_defaults(fn=cmd_run)

    pv = sub.add_parser("vm", help="run on the Pyro VM")
    common(pv, with_args=True); pv.set_defaults(fn=cmd_vm)

    pc = sub.add_parser("c", help="emit AOT C source")
    common(pc); pc.add_argument("-o", "--output", help="output .c path")
    pc.set_defaults(fn=cmd_c)

    a = ap.parse_args()
    if not hasattr(a, "args"):
        a.args = []
    a.fn(a)


if __name__ == "__main__":
    main()
