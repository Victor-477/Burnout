#!/usr/bin/env python3
# ==========================================================================
# Cryo Compiler — Entry Point / CLI (v1.1.0)
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
import glob
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
import modules                                                  # CRYO (11.23 cache hooks)
from semantic     import check as semantic_check, SemanticError  # CRYO
from generics     import monomorphize                             # CRYO
from traits       import lower_traits                             # CRYO
from optimize     import optimize as optimize_ast              # CRYO (11.21)
from codegen_c    import CodeGenC,    CodeGenError       # C backend
from codegen_go   import CodeGenGo,   CodeGenGoError     # Go backend
from codegen_asm  import CodeGenAsm,  CodeGenAsmError    # x86-64 backend
from codegen_pyro import CodeGenPyro, CodeGenPyroError   # Pyro bytecode backend
from codegen_node import CodeGenNode, CodeGenNodeError   # Node.js/JS backend
from codegen_wasm import CodeGenWasm, CodeGenWasmError   # WebAssembly backend
import frontend                                          # front-end pages (10.11/10.13)


BANNER = r"""
  ____                    _____                      _ _
 / ___|_ __ _   _  ___   / ____|___ _ __ ___  _ __ (_) | ___ _ __
| |   | '__| | | |/ _ \ | |   / _ \ '_ ` _ \| '_ \| | |/ _ \ '__|
| |___| |  | |_| | (_) || |__|  __/ | | | | | |_) | | |  __/ |
 \____|_|   \__, |\___/  \_____\___|_| |_| |_| .__/|_|_|\___|_|
            |___/                            |_|   v1.1.0
   .cryo  ->  native C  |  x86-64 assembly   (safe mode)
"""


def collect_assets(directory: str) -> dict:
    """Roadmap 11.9 — every file under `directory`, keyed by its path
    RELATIVE to it, with forward slashes.

    Relative and slash-normalised so the same tree embeds identically on
    Windows and POSIX; the container has to be reproducible or the bootstrap
    fixed point stops holding.
    """
    out = {}
    if not directory:
        return out
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"--assets: not a directory: {directory}")
    for root, _dirs, files in os.walk(directory):
        for f in sorted(files):
            full = os.path.join(root, f)
            rel = os.path.relpath(full, directory).replace(os.sep, '/')
            with open(full, 'rb') as fh:
                out[rel] = fh.read()
    return out


def load_sign_key(spec: str) -> bytes:
    """Read the signing key named by --sign.

    `env:NAME` reads it from the environment; anything else is a file path.
    A key cannot be passed literally on the command line ON PURPOSE: argv is
    readable by every process on the machine and lands in shell history, so
    offering `--sign <secret>` would make the convenient way the unsafe one.

    Whitespace is stripped so a key file written by `echo` or an editor that
    adds a trailing newline still produces the same key — otherwise signing and
    verifying with "the same" key silently disagree.
    """
    if spec.startswith('env:'):
        name = spec[4:]
        val = os.environ.get(name)
        if not val:
            raise FileNotFoundError(
                f"--sign env:{name}: the environment variable is not set or is empty")
        key = val.strip().encode('utf-8')
    else:
        if not os.path.isfile(spec):
            raise FileNotFoundError(
                f"--sign: no such key file: {spec} "
                f"(use 'env:NAME' to read the key from the environment)")
        with open(spec, 'rb') as f:
            key = f.read().strip()
    if len(key) < 16:
        # Not a policy about entropy — a guard against the common accident of
        # pointing --sign at the wrong file and signing with three bytes.
        raise ValueError("--sign: the key is shorter than 16 bytes; "
                         "that is almost certainly the wrong file")
    return key


def default_abi() -> str:
    """Default ABI of the asm backend depending on the platform."""
    return 'win64' if sys.platform == 'win32' else 'sysv'


def compile_source(source: str, backend: str, safe: bool,
                   abi: str = 'sysv', base_dir: str | None = None,
                   optimize: bool = True, sandbox: bool = False,
                   emit: str = 'html', assets: dict | None = None,
                   use_cache: bool = True, path: str | None = None,
                   sign_key: bytes | None = None):
    """Returns str (go/c/asm/frontend) or bytes (pyro/wasm = binary)."""
    # ── 11.23: incremental compilation ──────────────────────
    # The artifact key needs every input, and the imports are only known after
    # module resolution — so the sources are collected while parsing and the
    # cache is consulted once they are all in hand. The parse cache pays for
    # itself on that first pass; the artifact cache skips everything after it.
    import cache as _cache
    settings = {'backend': backend, 'safe': safe, 'abi': abi,
                'optimize': optimize, 'sandbox': sandbox, 'emit': emit,
                'assets': sorted(assets) if assets else None,
                # 11.14 — a DIGEST of the signing key, never the key. It has to
                # be in the cache key or a signed build and an unsigned one
                # would share an entry and the signature would come and go
                # depending on what was compiled first; but the cache filename
                # is derived from these settings, so the key itself must not
                # appear in it.
                'sign': (__import__('hashlib').sha256(sign_key).hexdigest()[:16]
                         if sign_key else None)}
    art = _cache.ArtifactCache(enabled=use_cache)
    read: list = []
    if use_cache:
        modules.PARSE_CACHE = _cache.ParseCache(enabled=True)
        modules.READ_SOURCES = read
    else:
        modules.PARSE_CACHE = None
        modules.READ_SOURCES = None
    read.append(('<entry>', source))

    ast = load_ast(source, base_dir)
    modules.PARSE_CACHE = None
    modules.READ_SOURCES = None

    key = art.key(read, settings) if use_cache else None
    if key is not None:
        hit = art.get(key)
        if hit is not None:
            data, warnings = hit
            # Replay whatever the original compilation printed. Compiling is
            # not a pure function, and a build that is fast but silent about a
            # problem the first one reported is a worse build.
            if warnings:
                sys.stderr.write(warnings)
            # Text backends are stored as bytes and handed back as text, so a
            # cached build is indistinguishable from a fresh one.
            return data if backend in ('pyro', 'wasm') else data.decode('utf-8')

    # Capture diagnostics so they can be replayed on a later hit.
    if key is not None:
        buf = io.StringIO()
        real_err = sys.stderr
        sys.stderr = _Tee(real_err, buf)
        try:
            out = _compile_resolved(ast, backend, safe, abi, optimize, sandbox,
                                    emit, assets, source, path or base_dir,
                                    sign_key)
        finally:
            sys.stderr = real_err
        art.put(key, out if isinstance(out, bytes) else out.encode('utf-8'),
                buf.getvalue())
        return out

    return _compile_resolved(ast, backend, safe, abi, optimize, sandbox,
                             emit, assets, source, path or base_dir, sign_key)


class _Tee:
    """Writes to both streams: the user still sees a warning as it happens,
    and the cache keeps a copy to replay next time."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._streams[0], name)


def _compile_resolved(ast, backend: str, safe: bool, abi: str,
                      optimize: bool, sandbox: bool, emit: str, assets,
                      source: str = None, path: str = None,
                      sign_key: bytes | None = None):
    """Everything after module resolution. Split out so the artifact cache can
    skip all of it — that is the 72% the profile showed."""
    ast = monomorphize(ast)
    ast = lower_traits(ast)
    semantic_check(ast, source, path)   # variable/function/arity/break —
                          # so an error names what was written
    verify_foreign(ast)   # foreign blocks/libraries require `import >Lang<`
    if optimize:
        # 11.21 — folding, propagation, pruning and inlining on the AST, so
        # every backend benefits and not just the pyro peephole.
        ast = optimize_ast(ast)
    if backend == 'frontend':
        # 10.11/10.13 — assembles html/javascript/CSS blocks into a page.
        # Not a code generator: it emits a document, not a program.
        return frontend.render(ast, emit)
    if backend == 'asm':
        return CodeGenAsm(safe=safe, abi=abi).generate(ast)
    if backend == 'go':
        return CodeGenGo(safe=safe, sandbox=sandbox).generate(ast)
    if backend == 'node':
        return CodeGenNode(safe=safe).generate(ast)
    if backend == 'wasm':
        return CodeGenWasm(safe=safe).generate(ast)   # bytes (.wasm)
    if backend == 'pyro':
        return CodeGenPyro(safe=safe, optimize=optimize, sandbox=sandbox,
                           assets=assets, sign_key=sign_key).generate(ast)
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

    # (re)compiles the VM if it does not yet exist or if any source is newer.
    # Every .go file, not just main.go: 11.25 split the debugger and the
    # profiler into their own files, and a staleness check that watches one
    # file silently keeps running the old VM when the others change.
    srcs = sorted(glob.glob(os.path.join(vm_dir, '*.go')))
    need = not os.path.isfile(pyrovm)
    if not need and srcs:
        need = max(os.path.getmtime(s) for s in srcs) > os.path.getmtime(pyrovm)
    try:
        if need:
            if verbose:
                print(f"→ Compiling Pyro VM: go build -o {pyrovm}  (in {vm_dir})")
            # Name the .go files, NOT the package ('.'): pyro/vm also holds the
            # C VM (main.c, pyro_runtime.c) and `go build .` has refused with
            # "C source files not allowed when not using cgo or SWIG".
            r = subprocess.run(['go', 'build', '-o', pyrovm]
                               + [os.path.basename(s) for s in srcs],
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
                 use_cache: bool = True,
                 emit_only: bool = False,
                 emit: str = 'html',
                 assets: dict | None = None,
                 sign_key: bytes | None = None,
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
    # --strict implies the audit: passing it alone used to run no audit at
    # all, so the gate silently passed everything.
    if audit or audit_only or strict:
        audit_ast_obj = load_ast(source, base_dir)
        findings = audit_ast(audit_ast_obj)
        print(format_audit(findings, input_path))
        # 11.15 — this compared against 'ALTO'. Finding.level has been
        # 'HIGH' since the levels were renamed to English, so the CI gate
        # never fired: --strict reported findings and exited 0.
        has_high = any(f.level == 'HIGH' for f in findings)
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
                              optimize=optimize, sandbox=sandbox, emit=emit,
                              assets=assets, use_cache=use_cache,
                              path=input_path, sign_key=sign_key)
    except (CodeGenError, CodeGenGoError, CodeGenAsmError,
            CodeGenPyroError, CodeGenNodeError) as e:
        # safety net: if auto chose a backend that failed,
        # recompiles in go (superset) instead of aborting.
        if auto and backend != 'go':
            print(f"⚠ backend {backend} did not support the program ({e});"
                  f"recompiling with go", file=sys.stderr)
            backend = 'go'
            code = compile_source(source, backend, safe, abi, base_dir=base_dir,
                                  optimize=optimize, sandbox=sandbox, emit=emit,
                                  assets=assets, use_cache=use_cache,
                                  path=input_path, sign_key=sign_key)
        else:
            raise

    ext = {'asm': '.s', 'go': '.go', 'pyro': '.pyro',
           'node': '.js', 'c': '.c', 'wasm': '.wasm',
           'frontend': '.html'}.get(backend, '.c')
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
                'wasm': 'WebAssembly', 'c': 'native C',
                'frontend': f'front-end page ({emit})'}.get(backend, 'native C')
        tam = f"  ({len(code)} bytes)" if isinstance(code, (bytes, bytearray)) else ""
        print(f"✓ [{alvo} / {modo}] generated: {input_path} → {output_path}{tam}")

    # ── 10.13 `--emit pyro`: the .html shell is only half the artifact ──
    # It fetches a binary, so that binary has to exist next to it or the page
    # is dead on arrival. The front-end declarations are stripped first: they
    # describe the document, and handing them to a code generator fails.
    if backend == 'frontend' and emit == 'pyro':
        ast = frontend.strip_frontend(parse_ast(source))
        if not ast.statements:
            raise frontend.FrontendError(
                "--emit pyro compiles this program's Cryo functions to a binary "
                "for the browser, but the file has none — it is only a page. "
                "Use --emit html for a page with no Cryo logic.")
        bin_path = os.path.join(os.path.dirname(output_path) or '.', 'app.wasm')
        try:
            blob = CodeGenWasm(safe=safe).generate(ast)
        except CodeGenWasmError as e:
            raise frontend.FrontendError(
                f"--emit pyro could not compile this program's logic to a "
                f"browser binary: {e}\nThe wasm backend covers the numeric "
                f"subset. Use --emit html and load your own script instead."
            ) from e
        with open(bin_path, 'wb') as f:
            f.write(blob)
        if verbose:
            print(f"✓ [browser binary] generated: {bin_path}  ({len(blob)} bytes)")

    # ── emit-only: generates source and stops (the build script takes care of the toolchain) ──
    if emit_only or backend in ('wasm', 'frontend'):   # final artifacts (loaded by a host/browser)
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


def _point_at(path, message, prefix):
    """Render a front-end error against its source line (roadmap 11.24).

    Lexer and parser errors carry their position inside the message text — the
    raise sites format "Line N" into it — so the number is recovered here
    rather than threaded through forty call sites. Falls back to the plain
    message when there is no line, or the file cannot be read.
    """
    import re as _re
    import diagnostics as _dx
    msg = str(message).strip()
    # The raise sites already include the tag, and printing it again produced
    # "[Syntax Error] [Syntax Error] Line 2: ...".
    if msg.startswith(prefix):
        msg = msg[len(prefix):].strip()
    m = _re.search(r"Line (\d+)", msg)
    if not m or not path:
        return prefix + " " + msg
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return prefix + " " + msg
    line = int(m.group(1))
    body = msg[m.end():].lstrip(": ").strip() or msg
    # Underline the token when the message names one, e.g. SEMICOLON (';')
    tok = _re.search(r"\((['\"])(.+?)\1\)", body)
    needle = tok.group(2) if tok else None
    return _dx.render(src, line, prefix + " " + body, path, needle)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog='cryo',
        description='Cryo compiler v1.1.0 — .cryo → Go (base), native C, x86-64 asm, Pyro bytecode/native, front-end pages',
    )
    # nargs='?' so `--clear-cache` alone is a valid command line; the
    # requirement is enforced below, where it can say why.
    ap.add_argument('input', nargs='?', help='Input file (.cryo)')
    ap.add_argument('-o', '--output',  help='Output file (.go/.pyro/.s)')
    ap.add_argument('--backend',
                    choices=('auto', 'go', 'c', 'asm', 'pyro', 'node', 'wasm',
                             'frontend'),
                    default='go',
                    help='Backend: go (default), c, asm, pyro, node, wasm, '
                         'frontend (assemble html/javascript/CSS blocks into a '
                         'page), or auto (choose the best for the program)')
    ap.add_argument('--assets', metavar='DIR', default=None,
                    help='embed every file under DIR into the .pyro, readable '
                         'at runtime with asset("name") (roadmap 11.9)')
    ap.add_argument('--emit', choices=('html', 'pyro'), default='html',
                    help="frontend backend only: 'html' writes one self-contained "
                         "vanilla file; 'pyro' writes an .html shell plus the "
                         "program's logic as a binary the browser loads")
    ap.add_argument('--abi', choices=('sysv', 'win64'), default=None,
                    help='asm backend ABI (default: win64 on Windows, else sysv)')
    ap.add_argument('--unsafe', action='store_true',
                    help='Turn off safety instrumentation')
    ap.add_argument('--audit', action='store_true',
                    help='Run the static security audit and continue compiling')
    ap.add_argument('--audit-only', action='store_true',
                    help='Runs the audit, prints the report and exits (does not compile)')
    ap.add_argument('--no-cache', action='store_true',
                    help='Do not read or write the incremental cache (11.23)')
    ap.add_argument('--clear-cache', action='store_true',
                    help='Empty .cryocache and exit')
    ap.add_argument('--strict', action='store_true',
                    help='With --audit/--audit-only: exit with code 2 if there are HIGH findings (CI gate)')
    ap.add_argument('--sandbox', action='store_true',
                    help='Pyro/go backends: refuse network/machine natives by policy (VM: flag in .pyro; go: gate in generated code). Runtime: PYRO_SANDBOX=1')
    ap.add_argument('--emit-only', action='store_true',
                    help='It only generates the source (.pyro/.s); does not invoke the toolchain')
    ap.add_argument('--no-opt', action='store_true',
                    help='Turn off bytecode optimizer (pyro backend)')
    ap.add_argument('--sign', metavar='KEY',
                    help='pyro backend: sign the .pyro so the VM can refuse a '
                         'tampered one. KEY is a path to a key file, or "env:NAME" '
                         'to read it from the environment (preferred: a key on the '
                         'command line is visible to every process on the machine)')
    ap.add_argument('--dis', action='store_true',
                    help='Disassembles the generated Pyro bytecode (pyro backend)')
    ap.add_argument('--tokens', action='store_true', help='Print tokens')
    ap.add_argument('--ast',    action='store_true', help='Print AST')
    ap.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    ap.add_argument('--run',    action='store_true', help='Run after compiling')
    ap.add_argument('--no-banner', action='store_true', help='Hide the banner')
    args = ap.parse_args()

    if args.clear_cache:
        import cache as _cache
        n, size = _cache.clear()
        print(f"cache cleared: {n} entr(ies), {size} bytes")
        if not args.input:
            return
    if not args.input:
        ap.error("an input file is required")

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
            use_cache   = not args.no_cache,
            emit_only   = args.emit_only,
            emit        = args.emit,
            assets      = collect_assets(args.assets) if args.assets else None,
            sign_key    = load_sign_key(args.sign) if args.sign else None,
            dis         = args.dis,
            run         = args.run,
        )
    except LexerError     as e:
        print("\n" + _point_at(args.input, str(e), '[Lexical Error]'),
              file=sys.stderr); sys.exit(1)
    except ParseError     as e:
        # 11.24 — these messages already carry "Line N"; showing that line with
        # a caret turns a coordinate into the mistake itself.
        print("\n" + _point_at(args.input, str(e), '[Syntax Error]'),
              file=sys.stderr); sys.exit(1)
    except ForeignError   as e:
        print(f"\n[Foreign Error] {e}", file=sys.stderr); sys.exit(1)
    except ModuleError    as e:
        # Every raise site in modules.py already writes the tag into the
        # message, so prefixing unconditionally printed it twice:
        # "[Module Error] [Module Error] '_hidden' is not pub in module 'm'".
        _m = str(e)
        print("\n" + (_m if _m.startswith('[Module Error]') else f"[Module Error] {_m}"),
              file=sys.stderr); sys.exit(1)
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
    except CodeGenWasmError as e:
        print(f"\n[CodeGen Wasm Error] {e}", file=sys.stderr); sys.exit(1)
    except CodeGenError   as e:
        print(f"\n[CodeGen Error]   {e}", file=sys.stderr); sys.exit(1)
    except Exception      as e:
        print(f"\n[Internal Error]   {e}", file=sys.stderr); raise


if __name__ == '__main__':
    main()
