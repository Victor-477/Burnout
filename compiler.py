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

# Burnout (compilador) importa o front-end de CRYO; os backends (codegens)
# vivem aqui mesmo (Burnout = o compilador). A VM Pyro fica em pyro/vm.
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
from codegen_c    import CodeGenC,    CodeGenError       # backend C
from codegen_go   import CodeGenGo,   CodeGenGoError     # backend Go
from codegen_asm  import CodeGenAsm,  CodeGenAsmError    # backend x86-64
from codegen_pyro import CodeGenPyro, CodeGenPyroError   # backend bytecode Pyro
from codegen_node import CodeGenNode, CodeGenNodeError   # backend Node.js / JS


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
                   abi: str = 'sysv', base_dir: str | None = None):
    """Retorna str (go/c/asm) ou bytes (pyro = bytecode)."""
    ast = load_ast(source, base_dir)
    verify_foreign(ast)   # blocos estrangeiros/libraries exigem `import >Lang<`
    if backend == 'asm':
        return CodeGenAsm(safe=safe, abi=abi).generate(ast)
    if backend == 'go':
        return CodeGenGo(safe=safe).generate(ast)
    if backend == 'node':
        return CodeGenNode(safe=safe).generate(ast)
    if backend == 'pyro':
        return CodeGenPyro(safe=safe).generate(ast)   # bytes
    return CodeGenC(safe=safe).generate(ast)


def parse_ast(source: str):
    return Parser(Lexer(source).tokenize()).parse()


def load_ast(source: str, base_dir: str | None = None):
    """Parse + resolução de módulos (import \"arquivo.cryo\")."""
    ast = parse_ast(source)
    return resolve_modules(ast, base_dir or os.getcwd())


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


def _run_pyro(pyro_path: str, compiler_dir: str, run: bool, verbose: bool):
    """Compila a VM Pyro (Go) uma vez e executa o bytecode .pyro."""
    root    = os.path.dirname(compiler_dir)                 # raiz do projeto
    vm_dir  = os.path.join(root, 'pyro', 'vm')
    os.makedirs(os.path.join(root, 'build'), exist_ok=True)
    pyrovm  = os.path.join(root, 'build', 'pyrovm')
    if sys.platform == 'win32':
        pyrovm += '.exe'

    # (re)compila a VM se ainda não existe ou se o fonte é mais novo
    src = os.path.join(vm_dir, 'main.go')
    need = (not os.path.isfile(pyrovm) or
            (os.path.isfile(src) and os.path.getmtime(src) > os.path.getmtime(pyrovm)))
    try:
        if need:
            if verbose:
                print(f"→ Compilando VM Pyro: go build -o {pyrovm}  (em {vm_dir})")
            r = subprocess.run(['go', 'build', '-o', pyrovm, '.'],
                               cwd=vm_dir, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[go] Erro ao compilar a VM Pyro:\n{r.stderr}", file=sys.stderr)
                return
        if verbose:
            print(f"✓ VM Pyro: {pyrovm}")
        if run:
            print(f"\n── Executando (VM Pyro): {pyro_path} ───────────────────")
            subprocess.run([pyrovm, pyro_path])
    except FileNotFoundError:
        if verbose:
            print("⚠  go não encontrado — .pyro gerado, mas a VM não foi compilada/executada")


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
                 audit_only: bool = False,
                 emit_only: bool = False,
                 dis: bool = False,
                 run: bool = False) -> str:

    if abi is None:
        abi = default_abi()

    if not os.path.isfile(input_path):
        print(f"[Erro] Arquivo não encontrado: {input_path}", file=sys.stderr)
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

    # ── auditoria de seguranca ──
    if audit or audit_only:
        audit_ast_obj = load_ast(source, base_dir)
        findings = audit_ast(audit_ast_obj)
        print(format_audit(findings, input_path))
        if any(f.level == 'ALTO' for f in findings):
            print("[Auditoria] Achados de nível ALTO encontrados.", file=sys.stderr)
        # sugestão de backend: se o backend escolhido não cobre algum
        # recurso usado, aponta --backend auto (evita erro/omissão silenciosa).
        if backend != 'auto':
            miss_tags, miss_foreign = missing_capabilities(audit_ast_obj, backend)
            if miss_tags or miss_foreign:
                chosen, _motivo = select_backend(audit_ast_obj)
                partes = []
                if miss_tags:
                    partes.append("recursos [" + ", ".join(sorted(miss_tags)) + "]")
                if miss_foreign:
                    partes.append("blocos estrangeiros [" + ", ".join(sorted(miss_foreign)) + "]")
                print(f"\n[Auditoria] O backend '{backend}' não cobre "
                      f"{' e '.join(partes)}.")
                print(f"            Use --backend auto (escolheria '{chosen}') "
                      f"ou --backend {chosen}.")
        # --audit-only: relata e encerra; --audit segue para a compilação.
        if audit_only:
            return output_path

    # ── seleção automática de backend ──
    auto = (backend == 'auto')
    if auto:
        backend, motivo = select_backend(load_ast(source, base_dir))
        print(f"→ backend automático: {backend}  ({motivo})")

    # ── geracao de codigo ──
    try:
        code = compile_source(source, backend, safe, abi, base_dir=base_dir)
    except (CodeGenError, CodeGenGoError, CodeGenAsmError,
            CodeGenPyroError, CodeGenNodeError) as e:
        # rede de segurança: se o auto escolheu um backend que falhou,
        # recompila em go (superconjunto) em vez de abortar.
        if auto and backend != 'go':
            print(f"⚠  backend {backend} não suportou o programa ({e}); "
                  f"recompilando com go", file=sys.stderr)
            backend = 'go'
            code = compile_source(source, backend, safe, abi, base_dir=base_dir)
        else:
            raise

    ext = {'asm': '.s', 'go': '.go', 'pyro': '.pyro',
           'node': '.js', 'c': '.c'}.get(backend, '.c')
    if output_path is None:
        # separa fontes (.cryo) de artefatos gerados: saída vai para build/
        os.makedirs('build', exist_ok=True)
        base = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join('build', base + ext)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if isinstance(code, (bytes, bytearray)):        # .pyro = bytecode binário
        with open(output_path, 'wb') as f:
            f.write(code)
    else:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(code)

    modo = 'SEGURO' if safe else 'UNSAFE'
    if verbose:
        alvo = {'asm': f'x86-64 asm/{abi}', 'go': 'Go nativo',
                'pyro': 'bytecode Pyro', 'node': 'JavaScript (Node)',
                'c': 'C nativo'}.get(backend, 'C nativo')
        tam = f"  ({len(code)} bytes)" if isinstance(code, (bytes, bytearray)) else ""
        print(f"✓ [{alvo} / {modo}] gerado: {input_path}  →  {output_path}{tam}")

    # ── emit-only: gera fonte e para (o build script cuida do toolchain) ──
    if emit_only:
        return output_path

    # ── backend pyro: desassembla e/ou executa na VM Pyro ──
    if backend == 'pyro':
        if dis:
            import disasm_pyro
            print("\n── Desassembly (.pyro) ──────────────────────")
            print(disasm_pyro.disassemble(code))
        if run or not dis:
            _run_pyro(output_path,
                      compiler_dir=os.path.dirname(os.path.abspath(__file__)),
                      run=run, verbose=verbose)
        return output_path

    # ── backend node: executa o .js com o Node.js ──
    if backend == 'node':
        if run:
            try:
                print(f"\n── Executando (Node): {output_path} ───────────────────")
                subprocess.run(['node', output_path])
            except FileNotFoundError:
                print("⚠  node não encontrado — .js gerado, mas não executado",
                      file=sys.stderr)
        return output_path

    # ── montar/compilar binário ──
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
    ap.add_argument('--backend', choices=('auto', 'go', 'c', 'asm', 'pyro', 'node'),
                    default='go',
                    help='Backend: go (padrão), c, asm, pyro, node, ou auto '
                         '(escolhe o melhor pelo programa)')
    ap.add_argument('--abi', choices=('sysv', 'win64'), default=None,
                    help='ABI do backend asm (padrão: win64 no Windows, senão sysv)')
    ap.add_argument('--unsafe', action='store_true',
                    help='Desliga a instrumentação de segurança')
    ap.add_argument('--audit', action='store_true',
                    help='Executa a auditoria de segurança estática e segue compilando')
    ap.add_argument('--audit-only', action='store_true',
                    help='Executa a auditoria, imprime o relatório e sai (não compila)')
    ap.add_argument('--emit-only', action='store_true',
                    help='Apenas gera o fonte (.pyro/.s); não invoca o toolchain')
    ap.add_argument('--dis', action='store_true',
                    help='Desassembla o bytecode Pyro gerado (backend pyro)')
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
            audit_only  = args.audit_only,
            emit_only   = args.emit_only,
            dis         = args.dis,
            run         = args.run,
        )
    except LexerError     as e:
        print(f"\n[Erro Léxico]    {e}", file=sys.stderr); sys.exit(1)
    except ParseError     as e:
        print(f"\n[Erro Sintático] {e}", file=sys.stderr); sys.exit(1)
    except ForeignError   as e:
        print(f"\n[Erro Estrangeiro] {e}", file=sys.stderr); sys.exit(1)
    except ModuleError    as e:
        print(f"\n[Erro de Módulo] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenAsmError as e:
        print(f"\n[Erro CodeGen ASM] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenGoError as e:
        print(f"\n[Erro CodeGen Go] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenPyroError as e:
        print(f"\n[Erro CodeGen Pyro] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenNodeError as e:
        print(f"\n[Erro CodeGen Node] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenError   as e:
        print(f"\n[Erro CodeGen]   {e}", file=sys.stderr); sys.exit(1)
    except Exception      as e:
        print(f"\n[Erro Interno]   {e}", file=sys.stderr); raise


if __name__ == '__main__':
    main()
