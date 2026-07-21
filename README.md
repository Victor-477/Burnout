# Burnout — the system's compiler

**Burnout** is the **compiler program** (Go/Python base): it takes `.cryo` (from
**CRYO**) and generates the `.pyro` target language (**PYRO** bytecode) — or, as
alternative targets, Go/C/asm code. It contains the orchestration front-end and
**all the code generators** (backends).

```
  .cryo ──►  Burnout: [CRYO front-end] → AST → [backend] ──►  .pyro (the Pyro target) | .go | .c | .s
```

## Contents

| File | Role |
|---|---|
| `cryoc.py` | CLI entry point |
| `__init__.py` | **Library API** (`import burnout`) + `pyproject.toml` (pip) |
| `compiler.py` | Orchestration: source → AST (CRYO) → code (backend) → run/build |
| `codegen_pyro.py` | **Pyro bytecode backend** (`.pyro`) — the custom target language |
| `codegen_go.py` | Go backend (alternative target; full language + skills/machine) |
| `codegen_c.py` | Native C backend |
| `codegen_asm.py` | x86-64 backend (System V and Win64 ABIs) |
| `codegen_legacy.py` | Legacy Python backend |
| `runtime/cryo_runtime.c/.h` | C runtime (C/asm backends) |
| `scripts/build_win64.*` | MinGW build on Windows (asm backend) |
| `tests/test_smoke.py` | Smoke tests for the generators |

## Usage (from the project root)

```bash
# Pyro target (custom bytecode) — generates .pyro and runs it on the Pyro VM
python burnout/cryoc.py cryo/examples/example_bytecode.cryo --backend pyro --run

# Alternative targets
python burnout/cryoc.py cryo/examples/app.cryo --backend go --run    # Go
python burnout/cryoc.py cryo/examples/app.cryo --backend c            # C
python burnout/cryoc.py cryo/examples/app.cryo --backend asm          # x86-64

python burnout/tests/test_smoke.py
```

## Use as a library (import/call from projects)

Burnout is also an **importable Python package**. Install in editable mode from
this folder (inside the Cryo monorepo, with `../cryo` and `../pyro` alongside):

```bash
cd burnout
pip install -e .
```

Then, in any project:

```python
import burnout

# Compile a .cryo string -> target code
#   go/c/asm -> str   |   pyro -> bytes (bytecode)
go_src = burnout.compile_source('print("ola");', backend="go")
bc     = burnout.compile_source('print(1 + 2);', backend="pyro")

# Compile a file and (optionally) run it
burnout.compile_file("app.cryo", backend="pyro", run=True)
burnout.run("app.cryo", backend="go")          # shortcut for compile_file(run=True)

# Front-end: tokens and AST
toks = burnout.tokenize(open("app.cryo").read())
ast  = burnout.parse_ast(open("app.cryo").read())

# Disassemble an already-generated .pyro
print(burnout.disassemble(open("build/app.pyro", "rb").read()))
```

It also works as an executable module (same CLI as `cryoc.py`):

```bash
python -m burnout app.cryo --backend go --run
cryoc app.cryo --run           # console-script installed by pip
```

> **Public API:** `compile_source`, `compile_file`, `run`, `parse_ast`,
> `tokenize`, `disassemble`, `default_abi`, `BACKENDS`, `__version__`, and the
> exceptions `LexerError` / `ParseError` / `CodeGen*Error`.
>
> **Note (Windows):** if `pip install -e .` fails writing `Scripts\cryoc.exe`
> (file in use), the package is still importable — use `python -m burnout` as the
> CLI, or repeat the install with the terminal closed.

Generated artifacts go to `build/` at the root (git-ignored). `--backend pyro --run`
builds the Pyro VM (`pyro/vm`) once and runs the `.pyro`.

## Dependencies

Burnout depends on **CRYO** (front-end: lexer/parser/AST/analysis) and, for the
Pyro target, invokes the **Pyro VM** in `pyro/vm`. It will be distributed as its
own repository, consuming CRYO as a dependency.
