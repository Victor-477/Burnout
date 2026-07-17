# Burnout — o compilador do sistema

O **Burnout** é o **programa compilador** (base Go/Python): recebe `.cryo` (do
**CRYO**) e gera a linguagem-alvo `.pyro` (bytecode do **PYRO**) — ou, como alvos
alternativos, código Go/C/asm. Contém o front-end de orquestração e **todos os
geradores de código** (backends).

```
  .cryo ──►  Burnout: [front-end CRYO] → AST → [backend] ──►  .pyro (padrão do Pyro) | .go | .c | .s
```

## Conteúdo

| Arquivo | Papel |
|---|---|
| `cryoc.py` | Ponto de entrada da CLI |
| `__init__.py` | **API de biblioteca** (`import burnout`) + `pyproject.toml` (pip) |
| `compiler.py` | Orquestração: fonte → AST (CRYO) → código (backend) → executa/monta |
| `codegen_pyro.py` | **Backend bytecode Pyro** (`.pyro`) — a linguagem-alvo própria |
| `codegen_go.py` | Backend Go (alvo alternativo; linguagem completa + skills/máquina) |
| `codegen_c.py` | Backend C nativo |
| `codegen_asm.py` | Backend x86-64 (ABIs System V e Win64) |
| `codegen_legacy.py` | Backend Python legado |
| `runtime/cryo_runtime.c/.h` | Runtime C (backends C/asm) |
| `scripts/build_win64.*` | Build MinGW no Windows (backend asm) |
| `tests/test_smoke.py` | Testes de fumaça dos geradores |

## Uso (a partir da raiz do projeto)

```bash
# Alvo Pyro (bytecode próprio) — gera .pyro e executa na VM Pyro
python burnout/cryoc.py cryo/examples/example_bytecode.cryo --backend pyro --run

# Alvos alternativos
python burnout/cryoc.py cryo/examples/app.cryo --backend go --run    # Go
python burnout/cryoc.py cryo/examples/app.cryo --backend c            # C
python burnout/cryoc.py cryo/examples/app.cryo --backend asm          # x86-64

python burnout/tests/test_smoke.py
```

## Usar como biblioteca (importar/chamar em projetos)

O Burnout também é um **pacote Python importável**. Instale em modo editável a
partir desta pasta (dentro do monorepo Cryo, com `../cryo` e `../pyro` ao lado):

```bash
cd burnout
pip install -e .
```

Depois, em qualquer projeto:

```python
import burnout

# Compila uma string .cryo -> código-alvo
#   go/c/asm -> str   |   pyro -> bytes (bytecode)
go_src = burnout.compile_source('print("ola");', backend="go")
bc     = burnout.compile_source('print(1 + 2);', backend="pyro")

# Compila um arquivo e (opcionalmente) executa
burnout.compile_file("app.cryo", backend="pyro", run=True)
burnout.run("app.cryo", backend="go")          # atalho de compile_file(run=True)

# Front-end: tokens e AST
toks = burnout.tokenize(open("app.cryo").read())
ast  = burnout.parse_ast(open("app.cryo").read())

# Desmonta um .pyro já gerado
print(burnout.disassemble(open("build/app.pyro", "rb").read()))
```

Também funciona como módulo executável (mesma CLI do `cryoc.py`):

```bash
python -m burnout app.cryo --backend go --run
cryoc app.cryo --run           # console-script instalado pelo pip
```

> **API pública:** `compile_source`, `compile_file`, `run`, `parse_ast`,
> `tokenize`, `disassemble`, `default_abi`, `BACKENDS`, `__version__`, e as
> exceções `LexerError` / `ParseError` / `CodeGen*Error`.
>
> **Nota (Windows):** se o `pip install -e .` falhar ao escrever
> `Scripts\cryoc.exe` (arquivo em uso), o pacote ainda fica importável — use
> `python -m burnout` como CLI, ou repita a instalação com o terminal fechado.

Artefatos gerados vão para `build/` na raiz (git-ignored). O `--backend pyro --run`
compila a VM Pyro (`pyro/vm`) uma vez e executa o `.pyro`.

## Dependências

Burnout depende de **CRYO** (front-end: lexer/parser/AST/análise) e, para o alvo
Pyro, aciona a **VM Pyro** de `pyro/vm`. Será distribuído como repositório próprio,
consumindo CRYO como dependência.
