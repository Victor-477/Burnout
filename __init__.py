"""Burnout — o compilador do sistema Cryo, como biblioteca importável.

O Burnout compila a linguagem-fonte **Cryo** (`.cryo`) para a linguagem-alvo
**Pyro** (bytecode) e para alvos alternativos (Go, C, x86-64). Este módulo expõe
a API programática para usar o compilador dentro de outros projetos Python — sem
depender da CLI.

Uso rápido::

    import burnout

    # compila uma string .cryo e devolve o código-alvo (str para go/c/asm,
    # bytes para o bytecode pyro)
    go_src = burnout.compile_source('print("ola");', backend="go")

    # compila um arquivo e (opcionalmente) executa
    burnout.compile_file("app.cryo", backend="pyro", run=True)

    # front-end: tokens e AST
    toks = burnout.tokenize(open("app.cryo").read())
    ast  = burnout.parse_ast(open("app.cryo").read())

    # desmonta um .pyro já gerado
    print(burnout.disassemble(open("build/app.pyro", "rb").read()))

Backends: ``"go"`` (padrão, mais completo), ``"pyro"`` (bytecode próprio),
``"c"`` e ``"asm"``. Veja o README do Burnout para a matriz de cobertura.
"""

import os as _os
import sys as _sys

# ------------------------------------------------------------------
#  Localiza o front-end CRYO e a VM PYRO (imports são "flat": os
#  módulos lexer/parser/codegen_* vivem em pastas irmãs). Adicioná-los
#  ao sys.path mantém a arquitetura de repositórios separados intacta e
#  funciona tanto no monorepo quanto num `pip install -e .`.
# ------------------------------------------------------------------
_here = _os.path.dirname(_os.path.abspath(__file__))          # .../burnout
_root = _os.path.dirname(_here)                               # raiz do projeto

for _p in ("cryo", "pyro"):
    _d = _os.path.join(_root, _p)
    if _os.path.isdir(_d) and _d not in _sys.path:
        _sys.path.insert(0, _d)
if _here not in _sys.path:
    _sys.path.insert(0, _here)

# ------------------------------------------------------------------
#  API pública, re-exportada dos módulos internos do compilador.
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
from backends import select_backend                                          # noqa: E402
from codegen_c import CodeGenError           # noqa: E402
from codegen_go import CodeGenGoError        # noqa: E402
from codegen_asm import CodeGenAsmError      # noqa: E402
from codegen_pyro import CodeGenPyroError    # noqa: E402
from codegen_node import CodeGenNodeError    # noqa: E402

try:                                          # desassembler do bytecode Pyro
    from disasm_pyro import disassemble       # noqa: E402
except Exception:                             # pragma: no cover
    disassemble = None

__version__ = "0.9.0"

BACKENDS = ("go", "pyro", "c", "asm", "node")


def tokenize(source):
    """Tokeniza um fonte `.cryo` e devolve a lista de tokens (front-end CRYO)."""
    return Lexer(source).tokenize()


def compile_source(source, backend="go", safe=True, abi=None):
    """Compila um fonte `.cryo` e devolve o código-alvo.

    Devolve ``str`` para os alvos ``go``/``c``/``asm`` e ``bytes`` para o alvo
    ``pyro`` (bytecode). ``safe`` liga a instrumentação de segurança (padrão);
    ``abi`` só afeta o backend ``asm`` (padrão: da plataforma).
    """
    if abi is None:
        abi = default_abi()
    if backend == "auto":
        backend = select_backend(parse_ast(source))[0]
    if backend not in BACKENDS:
        raise ValueError(f"backend inválido: {backend!r} (use um de {BACKENDS})")
    return _compile_source(source, backend=backend, safe=safe, abi=abi)


# Alias histórico
compile_string = compile_source


def run(path, backend="go", **kwargs):
    """Compila e executa um arquivo `.cryo` (atalho de ``compile_file(..., run=True)``)."""
    kwargs.setdefault("run", True)
    return compile_file(path, backend=backend, **kwargs)


__all__ = [
    "__version__", "BACKENDS",
    "compile_source", "compile_string", "compile_file", "run",
    "parse_ast", "tokenize", "disassemble", "default_abi", "main",
    "verify_foreign", "collect_imports", "select_backend",
    "Lexer", "Parser",
    "LexerError", "ParseError", "ForeignError",
    "CodeGenError", "CodeGenGoError", "CodeGenAsmError", "CodeGenPyroError",
    "CodeGenNodeError",
]
