#!/usr/bin/env python3
# ==========================================================================
# Cryo Compiler — Entry Point / CLI (v0.4)
# 
# Usage:
# python compiler.py app.cryo # C backend (default)
# python compiler.py app.cryo --backend asm # native x86-64 backend
# python compiler.py app.cryo --unsafe # turn off instrumentation
# python compiler.py app.cryo --audit # security report
# python compiler.py app.cryo --run -v
# ==========================================================================

import sys
import os
import io
import argparse
import subprocess

# ── Windows: guarantees UTF-8 output (avoids crash with cp1252) ──
for _stream in ('stdout', 'stderr'):
    _s = getattr(sys, _stream, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

# Burnout (compiler) imports the CRYO front-end; the backends (codes)
# live right here (Burnout = the compiler). The Pyro VM is located at pyro/vm.
_here = os.path.dirname(os.path.abspath(__file__))   # burnout/
_root = os.path.dirname(_here)
sys.path.insert(0, os.path.join(_root, 'cryo'))      # front-end (CRYO)
sys.path.insert(0, _here)                            # backends (Burnout)

from lexer       import Lexer,      LexerError        # CRYO
from parser      import Parser,     ParseError        # CRYO
from security    import audit_ast,  format_audit      # CRYO
from foreign     import verify as verify_foreign, ForeignError   # CRYO
from backends     import select_backend, missing_capabilities   # CRYO
from modules      import resolve_modules, ModuleError           # CRYO
from semantic     import check as semantic_check, SemanticError  # CRYO
from codegen_c    import CodeGenC,    CodeGenError       # C backend
from codegen_go   import CodeGenGo,   CodeGenGoError     # Go backend
from codegen_asm  import CodeGenAsm,  CodeGenAsmError    # x86-64 backend
from codegen_pyro import CodeGenPyro, CodeGenPyroError   # Pyro bytecode backend
from codegen_node import CodeGenNode, CodeGenNodeError   # Node.js/JS backend


BANNER = r"""
  ____                    _____                      _ _
 / ___|_ __ _   _  ___   / ____|___ _ __ ___  _ __ (_) | ___ _ __
| |   | '__| | | |/ _ \ | |   / _ \ '_ ` _ \| '_ \| | |/ _ \ '__|
| |___| |  | |_| | (_) || |__|  __/ | | | | | |_) | | |  __/ |
 \____|_|   \__, |\___/  \_____\___|_| |_| |_| .__/|_|_|\___|_|
            |___/                            |_|   v0.4.0
   .cryo  ->  native C  |  x86-64 assembly   (safe mode)
"""


def default_abi() -> str:
    """Default ABI of the asm backend depending on the platform."""
    return 'win64' if sys.platform == 'win32' else 'sysv'


def compile_source(source: str, backend: str, safe: bool,
                   abi: str = 'sysv', base_dir: str | None = None,
                   optimize: bool = True, sandbox: bool = False):
    """Returns str (go/c/asm) or bytes (pyro = bytecode)."""
    ast = load_ast(source, base_dir)
    semantic_check(ast)   # variable/function/aridade/break — errors early, with line
    verify_foreign(ast)   # foreign blocks/libraries require `import >Lang<`
    if backend == 'asm':
        return CodeGenAsm(safe=safe, abi=abi).generate(ast)
    if backend == 'go':
        return CodeGenGo(safe=safe, sandbox=sandbox).generate(ast)
    if backend == 'node':
        return CodeGenNode(safe=safe).generate(ast)
    if backend == 'pyro':
        return CodeGenPyro(safe=safe, optimize=optimize,
                           sandbox=sandbox).generate(ast)   # bytes
    return CodeGenC(safe=safe).generate(ast)


def parse_ast(source: str):
    return Parser(Lexer(source).tokenize()).parse()


def load_ast(source: str, base_dir: str | None = None):
    """Parse + module resolution (import \"file.cryo\")."""
    ast = parse_ast(source)
    return resolve_modules(ast, base_dir or os.getcwd())


def _gcc_c_flags(compiler_dir: str, output_path: str, runtime: str,
                 bin_path: str, safe: bool):
    flags = ['gcc', '-O2', '-std=c11', f'-I{compiler_dir}']
    if safe:
        # Hardening of the generated binary
        flags += [
            '-fstack-protector-strong',
            '-D_FORTIFY_SOURCE=2',
            '-Wall', '-Wformat', '-Wformat-security',
        ]
    flags += ['-x', 'c', output_path, runtime, '-lm', '-o', bin_path]
    return flags


def _run_pyro(pyro_path: str, compiler_dir: str, run: bool, verbose: bool):
    """Compiles the Pyro (Go) VM once and runs the .pyro bytecode."""
    root    = os.path.dirname(compiler_dir)                 # project root
    vm_dir  = os.path.join(root, 'pyro', 'vm')
    os.makedirs(os.path.join(root, 'build'), exist_ok=True)
    pyrovm  = os.path.join(root, 'build', 'pyrovm')
    if sys.platform == 'win32':
        pyrovm += '.exe'

    # (re)compiles the VM if it does not yet exist or if the source is newer
    src = os.path.join(vm_dir, 'main.go')
    need = (not os.path.isfile(pyrovm) or
            (os.path.isfile(src) and os.path.getmtime(src) > os.path.getmtime(pyrovm)))
    try:
        if need:
            if verbose:
                print(f"→ Compiling Pyro VM: go build -o {pyrovm}  (in {vm_dir})")
            r = subprocess.run(['go', 'build', '-o', pyrovm, '.'],
                               cwd=vm_dir, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[go] Error compiling Pyro VM:\n{r.stderr}", file=sys.stderr)
                return
        if verbose:
            print(f"✓ Pyro VM: {pyrovm}")
        if run:
            print(f"\n── Running (Pyro VM): {pyro_path} ───────────────────")
            subprocess.run([pyrovm, pyro_path])
    except FileNotFoundError:
        if verbose:
            print("⚠  go not found — .pyro generated, but VM was not compiled/executed")


def _gcc_asm_flags(output_path: str, runtime: str, bin_path: str, abi: str):
    if abi == 'win64':
        # MinGW: Relocatable PE, libm embedded in libmingwex
        return ['gcc', '-O2', output_path, runtime, '-o', bin_path]
    # System V: -no-pie makes lea rip/absolute labels work
    return ['gcc', '-no-pie', '-O2', output_path, runtime, '-lm', '-o', bin_path]


def compile_file(input_path: str,
                 output_path: str | None = None,
                 backend: str = 'c',
                 safe: bool = True,
                 abi: str | None = None,
                 verbose: bool = False,
                 show_tokens: bool = False,
                 show_ast: bool = False,
                 audit: bool = False,
                 audit_only: bool = False,
                 strict: bool = False,
                 sandbox: bool = False,
                 optimize: bool = True,
                 emit_only: bool = False,
                 dis: bool = False,
                 run: bool = False) -> str:

    if abi is None:
        abi = default_abi()

    if not os.path.isfile(input_path):
        print(f"[Error] File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, 'r', encoding='utf-8') as f:
        source = f.read()
    base_dir = os.path.dirname(os.path.abspath(input_path))

    if show_tokens:
        print("\n── Tokens ──────────────────────────────────")
        for t in Lexer(source).tokenize():
            print(f"  {t}")
        print()

    if show_ast:
        import pprint
        print("\n── AST ─────────────────────────────────────")
        pprint.pprint(parse_ast(source), width=80)
        print()

    # ── security audit ──
    if audit or audit_only:
        audit_ast_obj = load_ast(source, base_dir)
        findings = audit_ast(audit_ast_obj)
        print(format_audit(findings, input_path))
        has_high = any(f.level == 'ALTO' for f in findings)
        if has_high:
            print("[Audit] HIGH level findings found.", file=sys.stderr)
        # backend suggestion: if the chosen backend does not cover some
        # resource used, points --backend auto (prevents silent error/omission).
        if backend != 'auto':
            miss_tags, miss_foreign = missing_capabilities(audit_ast_obj, backend)
            if miss_tags or miss_foreign:
                chosen, _motivo = select_backend(audit_ast_obj)
                partes = []
                if miss_tags:
                    partes.append("recursos [" + ", ".join(sorted(miss_tags)) + "]")
                if miss_foreign:
                    partes.append("foreign blocks [" + ", ".join(sorted(miss_foreign)) + "]")
                print(f"\n[Audit] The backend '{backend}' does not cover"
                      f"{' e '.join(partes)}.")
                print(f"            Use --backend auto (escolheria '{chosen}') "
                      f"ou --backend {chosen}.")
        # --strict: fail (exit code 2) if there are HIGH findings —
        # useful as a CI gate, both with --audit and --audit-only.
        if strict and has_high:
            print("[Audit] --strict: aborting due to HIGH level findings.",
                  file=sys.stderr)
            sys.exit(2)
        # --audit-only: report and close; --audit proceeds to compilation.
        if audit_only:
            return output_path

    # ── automatic backend selection ──
    auto = (backend == 'auto')
    if auto:
        backend, motivo = select_backend(load_ast(source, base_dir))
        print(f"→ backend automático: {backend}  ({motivo})")

    # sandbox is applicable to both pyro (VM) and go (generated code) targets.
    if sandbox and backend not in ('pyro', 'go'):
        print(f"⚠ --sandbox only applies to pyro and go backends; ignored"
              f"para '{backend}'.", file=sys.stderr)

    # ── code generation ──
    try:
        code = compile_source(source, backend, safe, abi, base_dir=base_dir,
                              optimize=optimize, sandbox=sandbox)
    except (CodeGenError, CodeGenGoError, CodeGenAsmError,
            CodeGenPyroError, CodeGenNodeError) as e:
        # safety net: if auto chose a backend that failed,
        # recompiles in go (superset) instead of aborting.
        if auto and backend != 'go':
            print(f"⚠ backend {backend} did not support the program ({e});"
                  f"recompiling with go", file=sys.stderr)
            backend = 'go'
            code = compile_source(source, backend, safe, abi, base_dir=base_dir,
                                  optimize=optimize, sandbox=sandbox)
        else:
            raise

    ext = {'asm': '.s', 'go': '.go', 'pyro': '.pyro',
           'node': '.js', 'c': '.c'}.get(backend, '.c')
    if output_path is None:
        # separates sources (.cryo) from generated artifacts: output goes to build/
        os.makedirs('build', exist_ok=True)
        base = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join('build', base + ext)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if isinstance(code, (bytes, bytearray)):        # .pyro = binary bytecode
        with open(output_path, 'wb') as f:
            f.write(code)
    else:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(code)

    modo = 'SAFE' if safe else 'UNSAFE'
    if verbose:
        alvo = {'asm': f'x86-64 asm/{abi}', 'go': 'native Go',
                'pyro': 'Pyro bytecode', 'node': 'JavaScript (Node)',
                'c': 'native C'}.get(backend, 'native C')
        tam = f"  ({len(code)} bytes)" if isinstance(code, (bytes, bytearray)) else ""
        print(f"✓ [{alvo} / {modo}] generated: {input_path} → {output_path}{tam}")

    # ── emit-only: generates source and stops (the build script takes care of the toolchain) ──
    if emit_only:
        return output_path

    # ── pyro backend: disassembles and/or runs in the Pyro VM ──
    if backend == 'pyro':
        if dis:
            import disasm_pyro
            print("\n── Disassembly (.pyro) ──────────────────────")
            print(disasm_pyro.disassemble(code))
        if run or not dis:
            _run_pyro(output_path,
                      compiler_dir=os.path.dirname(os.path.abspath(__file__)),
                      run=run, verbose=verbose)
        return output_path

    # ── backend node: runs .js with Node.js ──
    if backend == 'node':
        if run:
            try:
                print(f"\n── Running (Node): {output_path} ───────────────────")
                subprocess.run(['node', output_path])
            except FileNotFoundError:
                print("⚠ node not found — .js generated but not executed",
                      file=sys.stderr)
        return output_path

    # ── assemble/compile binary ──
    bin_path     = os.path.abspath(os.path.splitext(output_path)[0])
    if sys.platform == 'win32':
        bin_path += '.exe'
    compiler_dir = os.path.dirname(os.path.abspath(__file__))   # burnout/
    runtime_dir  = os.path.join(compiler_dir, 'runtime')
    runtime      = os.path.join(runtime_dir, 'cryo_runtime.c')

    if backend == 'asm':
        cmd, tool = _gcc_asm_flags(output_path, runtime, bin_path, abi), 'gcc'
    elif backend == 'go':
        cmd, tool = ['go', 'build', '-o', bin_path, output_path], 'go'
    else:
        cmd, tool = _gcc_c_flags(runtime_dir, output_path, runtime, bin_path, safe), 'gcc'

    if verbose:
        print(f"→ Compiling: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if verbose:
                print(f"✓ Binary: {bin_path}")
                if result.stderr.strip():
                    print(f"[{tool} warnings]\n{result.stderr}", file=sys.stderr)
            if run:
                print(f"\n── Running: {bin_path} ───────────────────")
                subprocess.run([bin_path])
        else:
            print(f"[{tool}] Error:\n{result.stderr}", file=sys.stderr)
    except FileNotFoundError:
        if verbose:
            print(f"⚠ {tool} not found — just {ext} generated (not compiled)")

    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(
        prog='cryo',
        description='Cryo compiler v0.5 — .cryo → Go (base), native C or x86-64 asm',
    )
    ap.add_argument('input',           help='Input file (.cryo)')
    ap.add_argument('-o', '--output',  help='Output file (.go/.pyro/.s)')
    ap.add_argument('--backend', choices=('auto', 'go', 'c', 'asm', 'pyro', 'node'),
                    default='go',
                    help='Backend: go (default), c, asm, pyro, node, or auto'
                         '(choose the best according to the program)')
    ap.add_argument('--abi', choices=('sysv', 'win64'), default=None,
                    help='asm backend ABI (default: win64 on Windows, else sysv)')
    ap.add_argument('--unsafe', action='store_true',
                    help='Turn off safety instrumentation')
    ap.add_argument('--audit', action='store_true',
                    help='Run the static security audit and continue compiling')
    ap.add_argument('--audit-only', action='store_true',
                    help='Runs the audit, prints the report and exits (does not compile)')
    ap.add_argument('--strict', action='store_true',
                    help='With --audit/--audit-only: exit with code 2 if there are HIGH findings (CI gate)')
    ap.add_argument('--sandbox', action='store_true',
                    help='Pyro/go backends: refuse network/machine natives by policy (VM: flag in .pyro; go: gate in generated code). Runtime: PYRO_SANDBOX=1')
    ap.add_argument('--emit-only', action='store_true',
                    help='It only generates the source (.pyro/.s); does not invoke the toolchain')
    ap.add_argument('--no-opt', action='store_true',
                    help='Turn off bytecode optimizer (pyro backend)')
    ap.add_argument('--dis', action='store_true',
                    help='Disassembles the generated Pyro bytecode (pyro backend)')
    ap.add_argument('--tokens', action='store_true', help='Print tokens')
    ap.add_argument('--ast',    action='store_true', help='Print AST')
    ap.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    ap.add_argument('--run',    action='store_true', help='Run after compiling')
    ap.add_argument('--no-banner', action='store_true', help='Hide the banner')
    args = ap.parse_args()

    if not args.no_banner:
        print(BANNER)

    try:
        compile_file(
            args.input,
            output_path = args.output,
            backend     = args.backend,
            safe        = not args.unsafe,
            abi         = args.abi,
            verbose     = True,
            show_tokens = args.tokens,
            show_ast    = args.ast,
            audit       = args.audit,
            audit_only  = args.audit_only,
            strict      = args.strict,
            sandbox     = args.sandbox,
            optimize    = not args.no_opt,
            emit_only   = args.emit_only,
            dis         = args.dis,
            run         = args.run,
        )
    except LexerError     as e:
        print(f"\n[Lexical Error]    {e}", file=sys.stderr); sys.exit(1)
    except ParseError     as e:
        print(f"\n[Syntax Error] {e}", file=sys.stderr); sys.exit(1)
    except ForeignError   as e:
        print(f"\n[Foreign Error] {e}", file=sys.stderr); sys.exit(1)
    except ModuleError    as e:
        print(f"\n[Module Error] {e}", file=sys.stderr); sys.exit(1)
    except SemanticError  as e:
        print(f"\n[Semantic Error] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenAsmError as e:
        print(f"\n[CodeGen ASM Error] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenGoError as e:
        print(f"\n[CodeGen Go Error] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenPyroError as e:
        print(f"\n[CodeGen Pyro Error] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenNodeError as e:
        print(f"\n[CodeGen Node Error] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenError   as e:
        print(f"\n[CodeGen Error]   {e}", file=sys.stderr); sys.exit(1)
    except Exception      as e:
        print(f"\n[Internal Error]   {e}", file=sys.stderr); raise


if __name__ == '__main__':
    main()
