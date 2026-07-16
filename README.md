# Burnout — o motor (driver/CLI)

O **Burnout** é o motor que orquestra tudo: liga o front-end (**CRYO**) aos
backends (**PYRO**), monta/executa binários e roda os testes. É o ponto de entrada
do sistema.

## Conteúdo

| Arquivo | Papel |
|---|---|
| `cryoc.py` | Ponto de entrada da CLI |
| `compiler.py` | Orquestração: fonte → AST (CRYO) → código (PYRO) → binário |
| `scripts/build_win64.*` | Build MinGW no Windows (backend asm) |
| `tests/test_smoke.py` | Testes de fumaça dos geradores |

## Uso (a partir da raiz do projeto)

```bash
python burnout/cryoc.py cryo/examples/example_pyro.cryo --run -v
python burnout/cryoc.py cryo/examples/app.cryo --backend c
python burnout/cryoc.py cryo/examples/app.cryo --audit
python burnout/tests/test_smoke.py
```

Artefatos gerados vão para `build/` na raiz (git-ignored).

## Dependências

Burnout depende de **CRYO** (lexer/parser/AST/análise) e **PYRO** (backends/runtime).
Adiciona ambos ao `sys.path` em `cryoc.py`/`compiler.py`. Será distribuído como
repositório próprio, consumindo CRYO e PYRO como dependências.
