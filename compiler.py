#!/usr/bin/env python3
# ============================================================
#  Cryo Compiler — Entry Point / CLI  (v0.4)
#
#  Uso:
#    python compiler.py app.cryo                 # backend C (padrao)
#    python compiler.py app.cryo --backend asm   # backend x86-64 nativo
#    python compiler.py app.cryo --unsafe        # desliga instrumentacao
#    python compiler.py app.cryo --audit         # relatorio de seguranca
#    python compiler.py app.cryo --run -v
# ============================================================

import sys
import os
import io
import argparse
import subprocess

# ── Windows: garante saida UTF-8 (evita crash com cp1252) ──
for _stream in ('stdout', 'stderr'):
    _s = getattr(sys, _stream, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

# Burnout (motor) importa o front-end de CRYO e os backends de PYRO.
_here = os.path.dirname(os.path.abspath(__file__))   # burnout/
_root = os.path.dirname(_here)
for _p in ('cryo', 'pyro'):
    sys.path.insert(0, os.path.join(_root, _p))
sys.path.insert(0, _here)

from lexer      import Lexer,     LexerError        # CRYO
from parser     import Parser,    ParseError        # CRYO
from security   import audit_ast, format_audit      # CRYO
from codegen_c  import CodeGenC,  CodeGenError       # PYRO
from codegen_go import CodeGenGo, CodeGenGoError     # PYRO
from codegen_asm import CodeGenAsm, CodeGenAsmError  # PYRO


BANNER = r"""
  ____                    _____                      _ _
 / ___|_ __ _   _  ___   / ____|___ _ __ ___  _ __ (_) | ___ _ __
| |   | '__| | | |/ _ \ | |   / _ \ '_ ` _ \| '_ \| | |/ _ \ '__|
| |___| |  | |_| | (_) || |__|  __/ | | | | | |_) | | |  __/ |
 \____|_|   \__, |\___/  \_____\___|_| |_| |_| .__/|_|_|\___|_|
            |___/                            |_|   v0.4.0
   .cryo  ->  C nativo  |  x86-64 assembly   (modo seguro)
"""


def default_abi() -> str:
    """ABI padrao do backend asm conforme a plataforma."""
    return 'win64' if sys.platform == 'win32' else 'sysv'


def compile_source(source: str, backend: str, safe: bool,
                   abi: str = 'sysv') -> str:
    tokens = Lexer(source).tokenize()
    ast    = Parser(tokens).parse()
    if backend == 'asm':
        return CodeGenAsm(safe=safe, abi=abi).generate(ast)
    if backend == 'go':
        return CodeGenGo(safe=safe).generate(ast)
    return CodeGenC(safe=safe).generate(ast)


def parse_ast(source: str):
    return Parser(Lexer(source).tokenize()).parse()


def _gcc_c_flags(compiler_dir: str, output_path: str, runtime: str,
                 bin_path: str, safe: bool):
    flags = ['gcc', '-O2', '-std=c11', f'-I{compiler_dir}']
    if safe:
        # Endurecimento do binario gerado
        flags += [
            '-fstack-protector-strong',
            '-D_FORTIFY_SOURCE=2',
            '-Wall', '-Wformat', '-Wformat-security',
        ]
    flags += ['-x', 'c', output_path, runtime, '-lm', '-o', bin_path]
    return flags


def _gcc_asm_flags(output_path: str, runtime: str, bin_path: str, abi: str):
    if abi == 'win64':
        # MinGW: PE relocavel, libm embutida em libmingwex
        return ['gcc', '-O2', output_path, runtime, '-o', bin_path]
    # System V: -no-pie faz lea rip/rotulos absolutos funcionarem
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
                 emit_only: bool = False,
                 run: bool = False) -> str:

    if abi is None:
        abi = default_abi()

    if not os.path.isfile(input_path):
        print(f"[Erro] Arquivo não encontrado: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, 'r', encoding='utf-8') as f:
        source = f.read()

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

    # ── auditoria de seguranca ──
    if audit:
        findings = audit_ast(parse_ast(source))
        print(format_audit(findings, input_path))
        if any(f.level == 'ALTO' for f in findings):
            print("[Auditoria] Achados de nível ALTO encontrados.", file=sys.stderr)

    # ── geracao de codigo ──
    code = compile_source(source, backend, safe, abi)

    ext = {'asm': '.s', 'go': '.go'}.get(backend, '.pyro')
    if output_path is None:
        # separa fontes (.cryo) de artefatos gerados: saída vai para build/
        os.makedirs('build', exist_ok=True)
        base = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join('build', base + ext)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(code)

    modo = 'SEGURO' if safe else 'UNSAFE'
    if verbose:
        alvo = {'asm': f'x86-64 asm/{abi}', 'go': 'Go nativo'}.get(backend, 'C nativo')
        print(f"✓ [{alvo} / {modo}] gerado: {input_path}  →  {output_path}")

    # ── emit-only: gera fonte e para (o build script cuida do toolchain) ──
    if emit_only:
        return output_path

    # ── montar/compilar binário ──
    bin_path     = os.path.abspath(os.path.splitext(output_path)[0])
    if sys.platform == 'win32':
        bin_path += '.exe'
    compiler_dir = os.path.dirname(os.path.abspath(__file__))   # burnout/
    runtime_dir  = os.path.join(compiler_dir, '..', 'pyro', 'runtime')
    runtime      = os.path.join(runtime_dir, 'cryo_runtime.c')

    if backend == 'asm':
        cmd, tool = _gcc_asm_flags(output_path, runtime, bin_path, abi), 'gcc'
    elif backend == 'go':
        cmd, tool = ['go', 'build', '-o', bin_path, output_path], 'go'
    else:
        cmd, tool = _gcc_c_flags(runtime_dir, output_path, runtime, bin_path, safe), 'gcc'

    if verbose:
        print(f"→ Compilando: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if verbose:
                print(f"✓ Binário: {bin_path}")
                if result.stderr.strip():
                    print(f"[{tool} avisos]\n{result.stderr}", file=sys.stderr)
            if run:
                print(f"\n── Executando: {bin_path} ───────────────────")
                subprocess.run([bin_path])
        else:
            print(f"[{tool}] Erro:\n{result.stderr}", file=sys.stderr)
    except FileNotFoundError:
        if verbose:
            print(f"⚠  {tool} não encontrado — apenas {ext} gerado (não compilado)")

    return output_path


def main() -> None:
    ap = argparse.ArgumentParser(
        prog='cryo',
        description='Compilador Cryo v0.5 — .cryo → Go (base), C nativo ou x86-64 asm',
    )
    ap.add_argument('input',           help='Arquivo de entrada (.cryo)')
    ap.add_argument('-o', '--output',  help='Arquivo de saída (.go/.pyro/.s)')
    ap.add_argument('--backend', choices=('go', 'c', 'asm'), default='go',
                    help='Backend de geração de código (padrão: go)')
    ap.add_argument('--abi', choices=('sysv', 'win64'), default=None,
                    help='ABI do backend asm (padrão: win64 no Windows, senão sysv)')
    ap.add_argument('--unsafe', action='store_true',
                    help='Desliga a instrumentação de segurança')
    ap.add_argument('--audit', action='store_true',
                    help='Executa auditoria de segurança estática e sai')
    ap.add_argument('--emit-only', action='store_true',
                    help='Apenas gera o fonte (.pyro/.s); não invoca o gcc')
    ap.add_argument('--tokens', action='store_true', help='Imprime tokens')
    ap.add_argument('--ast',    action='store_true', help='Imprime AST')
    ap.add_argument('-v', '--verbose', action='store_true', help='Saída detalhada')
    ap.add_argument('--run',    action='store_true', help='Executa após compilar')
    ap.add_argument('--no-banner', action='store_true', help='Oculta o banner')
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
            emit_only   = args.emit_only,
            run         = args.run,
        )
    except LexerError     as e:
        print(f"\n[Erro Léxico]    {e}", file=sys.stderr); sys.exit(1)
    except ParseError     as e:
        print(f"\n[Erro Sintático] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenAsmError as e:
        print(f"\n[Erro CodeGen ASM] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenGoError as e:
        print(f"\n[Erro CodeGen Go] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenError   as e:
        print(f"\n[Erro CodeGen]   {e}", file=sys.stderr); sys.exit(1)
    except Exception      as e:
        print(f"\n[Erro Interno]   {e}", file=sys.stderr); raise


if __name__ == '__main__':
    main()
