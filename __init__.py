"""Burnout — the compiler of the Cryo system, as an importable library.

Burnout compiles the **Cryo** source language (`.cryo`) to the target language
**Pyro** (bytecode) and for alternative targets (Go, C, x86-64). This module exposes
the programmatic API to use the compiler within other Python projects — without
depending on the CLI.

Uso rápido::

    import burnout

    # compiles a .cryo string and returns the target code (str for go/c/asm,
    # bytes for the pyro bytecode)
    go_src = burnout.compile_source('print("ola");', backend="go")

    # compiles a file and (optionally) executes
    burnout.compile_file("app.cryo", backend="pyro", run=True)

    # front-end: tokens e AST
    toks = burnout.tokenize(open("app.cryo").read())
    ast  = burnout.parse_ast(open("app.cryo").read())

    # disassembles an already generated .pyro
    print(burnout.disassemble(open("build/app.pyro", "rb").read()))

Backends: ``"go"`` (default, most complete), ``"pyro"`` (own bytecode),
``"c"`` and ``"asm"``. See Burnout README for coverage matrix.
"""

import os as _os
import sys as _sys

# ------------------------------------------------------------------
#  Locates the CRYO front-end and the PYRO VM (imports are "flat": the
#  modules lexer/parser/codegen_* live in sibling folders). Adding them
#  to sys.path keeps the separate repository architecture intact and
#  works both in the monorepo and in a `pip install -e .`.
# ------------------------------------------------------------------
_here = _os.path.dirname(_os.path.abspath(__file__))          # .../burnout
_root = _os.path.dirname(_here)                               # project root

for _p in ("cryo", "pyro"):
    _d = _os.path.join(_root, _p)
    if _os.path.isdir(_d) and _d not in _sys.path:
        _sys.path.insert(0, _d)
if _here not in _sys.path:
    _sys.path.insert(0, _here)

# ------------------------------------------------------------------
#  Public API, re-exported from the compiler's internal modules.
# ------------------------------------------------------------------
from compiler import (          # noqa: E402
    compile_source as _compile_source,
    compile_file,
    parse_ast,
    default_abi,
    main,
)
from lexer import Lexer, LexerError          # noqa: E402
from parser import Parser, ParseError        # noqa: E402
from foreign import verify as verify_foreign, ForeignError, collect_imports  # noqa: E402
from backends import select_backend, missing_capabilities                    # noqa: E402
from modules import resolve_modules, ModuleError                             # noqa: E402
from semantic import check as semantic_check, SemanticError                  # noqa: E402
from compiler import load_ast                                                # noqa: E402
from codegen_c import CodeGenError           # noqa: E402
from codegen_go import CodeGenGoError        # noqa: E402
from codegen_asm import CodeGenAsmError      # noqa: E402
from codegen_pyro import CodeGenPyroError    # noqa: E402
from codegen_node import CodeGenNodeError    # noqa: E402

try:                                          # Pyro bytecode disassembler
    from disasm_pyro import disassemble       # noqa: E402
except Exception:                             # pragma: no cover
    disassemble = None

__version__ = "0.9.0"

BACKENDS = ("go", "pyro", "c", "asm", "node")


def tokenize(source):
    """Tokenizes a `.cryo` source and returns the token list (CRYO front-end)."""
    return Lexer(source).tokenize()


def compile_source(source, backend="go", safe=True, abi=None, base_dir=None,
                   optimize=True):
    """Compiles a `.cryo` source and returns the target code.

    Returns ``str`` for ``go``/``c``/``asm`` targets and ``bytes`` for target
    ``pyro`` (bytecode). ``safe`` turns on security instrumentation (default);
    ``abi`` only affects the ``asm`` backend; ``base_dir`` is the folder used for
    resolving ``import "file.cryo"`` (default: current directory).
    """
    if abi is None:
        abi = default_abi()
    if backend == "auto":
        backend = select_backend(load_ast(source, base_dir))[0]
    if backend not in BACKENDS:
        raise ValueError(f"invalid backend: {backend!r} (use one of {BACKENDS})")
    return _compile_source(source, backend=backend, safe=safe, abi=abi,
                           base_dir=base_dir, optimize=optimize)


# Historical alias
compile_string = compile_source


def run(path, backend="go", **kwargs):
    """Compiles and executes a `.cryo` file (shortcut for ``compile_file(..., run=True)``)."""
    kwargs.setdefault("run", True)
    return compile_file(path, backend=backend, **kwargs)


__all__ = [
    "__version__", "BACKENDS",
    "compile_source", "compile_string", "compile_file", "run",
    "parse_ast", "tokenize", "disassemble", "default_abi", "main",
    "verify_foreign", "collect_imports", "select_backend", "missing_capabilities",
    "resolve_modules", "load_ast", "ModuleError",
    "semantic_check", "SemanticError",
    "Lexer", "Parser",
    "LexerError", "ParseError", "ForeignError",
    "CodeGenError", "CodeGenGoError", "CodeGenAsmError", "CodeGenPyroError",
    "CodeGenNodeError",
]
