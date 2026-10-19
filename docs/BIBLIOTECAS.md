# Bibliotecas para Cryo: o que existe, o que custa e uma proposta

Este documento investiga como incorporar bibliotecas à linguagem Cryo. Ele parte de uma experiência concreta: a escrita da [Cryo-Math](https://github.com/Victor-477/Cryo-Math), uma biblioteca matemática em Cryo puro (cerca de 60 funções: trigonometria, logaritmos, combinatória, estatística), e de uma [calculadora](https://github.com/Victor-477/Cryo_Calculator) que a usa.

## Resumo

- **Já é possível** criar bibliotecas em Cryo. Um módulo `.cryo` com `pub` é resolvido pelo front-end **antes** da geração de código, então a mesma biblioteca roda em todos os backends sem mudar nada no Burnout. A Cryo-Math passa os mesmos testes em `pyro`, `node` e `go`.
- **O caminho tinha buracos**, e eles apareciam justamente ao escrever uma biblioteca. Por exemplo: `m::f()` não funcionava dentro de `try`, `switch` ou `assert`; `cryoc test` não resolvia `import`; o backend Go recusava `[]`/`{}` em atribuições; a VM dava a volta silenciosamente em overflow. As correções estão nos PRs que acompanham este documento.
- **O que falta é distribuição.** Hoje uma biblioteca só é alcançada por caminho relativo ou por uma dependência `path` no `cryo.toml`. Não existe biblioteca padrão. Prova disso: o repositório Cryo tem **duas cópias divergentes** de `store.cryo` (`lib/store.cryo` com 121 linhas e `examples/taskapp/store.cryo` com 49), porque não há como uma importar a outra.
- **Proposta:** uma biblioteca padrão endereçada por `@std/...`, distribuída junto com o front-end. A implementação é pequena (algumas dezenas de linhas em `packages.py`), não toca em nenhum backend e reaproveita a resolução de pacotes que já existe.

## Onde uma biblioteca entra no pipeline

```text
 .cryo ──► Lexer ──► Parser ──► resolve_modules ──► semântica ──► otimizador ──► codegen ──► .pyro / Go / JS / C / asm / wasm
                        │              │
                        │              └─ import "x.cryo" [as ns], import "@dep/x.cryo"
                        └─ lowerings: lines(), chars(), title_case() … viram funções Cryo
```

Tudo o que acontece à esquerda do `codegen` é **independente de backend**. Uma biblioteca que entra por ali vale para os seis alvos de uma vez. Já uma biblioteca que entra à direita (um builtin nativo, um bloco estrangeiro) precisa ser feita para cada alvo.

## As cinco formas de estender a linguagem hoje

| Forma | Como se usa | Onde mexe | Backends | Custo de uma função nova |
|---|---|---|---|---|
| **1. Módulo Cryo** | `import "math.cryo" as m;` → `m::sqrt(2.0)` | só no `.cryo` | todos | zero no compilador |
| **2. Pacote** | `cryo.toml` + `import "@cryo_math/math.cryo" as m;` | só no `.cryo` + manifesto | todos | zero no compilador |
| **3. Lowering no front-end** | `lines(s)` vira uma função Cryo gerada pelo parser | `cryo/parser.py` | todos | 1 lugar (o parser) |
| **4. Builtin nativo** | `sqrt(x)` vira a instrução `NATIVE` | 9 arquivos em 3 repositórios | os que implementarem | alto, e precisa de paridade |
| **5. Bloco estrangeiro** | `import >go<` + `library >go net/http<` + `>go( … )` | só no `.cryo` | **um** | zero, mas prende a um backend |

### 1 e 2 — Módulos e pacotes (o caminho natural para bibliotecas)

É o que a Cryo-Math usa. Pontos fortes:

- **Visibilidade real:** só o que é `pub` é alcançável por `ns::`; estado de módulo (`int _count = 0;`) é privado por construção.
- **Sem colisão de nomes:** com `as ns`, a função `sqrt` da biblioteca vira `m__sqrt` e não sombreia o builtin `sqrt` do programa.
- **Reprodutível:** o `cryo.lock` guarda um hash do **conteúdo** dos fontes da dependência, e não uma versão.
- **Portável:** como a resolução acontece no front-end, nenhum gerador de código sabe que a biblioteca existe.

Limitações encontradas:

| Limitação | Situação |
|---|---|
| `m::f()` dentro de `try`/`switch`/`assert`/`unsafe`/`match`/`x!`/`expr?` dava "unknown function" | corrigido na `dev` (Cryo `4bb524c`) |
| `cryoc test` ignorava `import`, então não dava para testar uma biblioteca com o runner da linguagem | corrigido na `dev` (Burnout `9d0a2f1`) |
| backend Go recusava `xs = []` e `f([1.0, 2.5])` com parâmetro tipado | corrigido (PR no Burnout: `codegen_go.py`) |
| overflow de `int` dava a volta na Pyro VM e abortava no Go/C | corrigido (PR no Pyro) |
| sem literal com expoente (`1e-12`), o que é incômodo numa biblioteca numérica | corrigido (PR no Cryo: os dois lexers) |
| dependência só por **caminho local**: o usuário precisa clonar os repositórios lado a lado | aberto, veja a proposta B |
| não há **biblioteca padrão**: `cryo/lib/store.cryo` não é importável de fora do repositório | aberto, veja a proposta A |
| `to_number("abc")` aborta sem possibilidade de `catch`, então uma biblioteca de parsing precisa validar tudo antes | aberto, é decisão de design (está documentado como fail-fast) |
| `print` de `number` difere entre backends: `1e10 + 1.0` mostra `1.0000000001e+10` (pyro/go), `10000000001` (node) e `1e+10` (c) | aberto, afeta qualquer biblioteca numérica que imprime resultados |

### 3 — Lowerings no front-end

O parser reescreve `lines(s)`, `chars(s)`, `title_case(s)` etc. em funções Cryo comuns. Isso só acontece quando o programa não define uma função de mesmo nome. A documentação de builtins explica por que esse mecanismo existe: um nativo novo precisa ser adicionado em vários lugares que não podem divergir. É a forma certa de crescer a biblioteca **embutida** sem custo nos backends. O ponto fraco é que o código Cryo fica dentro de strings/AST em Python, longe de onde um usuário da linguagem o leria ou testaria.

### 4 — Builtins nativos

Para adicionar um nativo como `random_int`, hoje é preciso mexer em:

| Repositório | Arquivos |
|---|---|
| Burnout | `codegen_pyro.py` (tabela `NATIVES`: nome → id, aridade), `codegen_go.py`, `codegen_node.py` |
| Cryo | `semantic.py`, `selfhost/semantic.cryo`, `parser.py` |
| Pyro | `vm/main.go`, `vm/pyro_runtime.c` (VM C, C++ e AOT), `vm/csharp/PyroVM.cs` |

São 9 arquivos, e uma divergência de id entre a tabela do gerador e a VM **não falha de forma visível**: o programa simplesmente chama outro nativo. Esse caminho só se justifica para o que Cryo não consegue expressar sozinho: E/S, sistema operacional, relógio, rede e aleatoriedade. Funções matemáticas como seno e logaritmo **não** precisam dele, e a Cryo-Math mostra isso.

### 5 — Blocos estrangeiros

`library >go net/http<` dá acesso direto ao ecossistema Go, Node ou C. É poderoso para aplicações (o exemplo da API usa isso), mas uma biblioteca construída assim só funciona no backend daquela linguagem. Serve para **adaptadores**, não para a biblioteca de uso geral.

## Proposta A: biblioteca padrão `@std`

```cryo
import "@std/math.cryo" as math;     // sem cryo.toml, em qualquer projeto
import "@std/store.cryo" as store;

print(math::sin(math::PI / 6.0));
```

**Regras:**

1. `std` é um nome de pacote **reservado**. `import "@std/x.cryo"` resolve para o diretório `std/` distribuído junto com o front-end (`cryo/std/`), com ou sem `cryo.toml` no projeto.
2. Declarar `std` em `[dependencies]` é erro, para que ninguém possa substituir a biblioteca padrão sem perceber.
3. As regras de segurança que `@dep` já aplica continuam valendo: `@std/../x` é recusado e um arquivo inexistente gera erro.
4. `cryo.lock` passa a registrar o hash de conteúdo da `std` usada. Assim, uma atualização do compilador que muda a `std` aparece no `cryoc pkg check`, como qualquer outra dependência.

**Conteúdo inicial sugerido:**

| Módulo | Origem |
|---|---|
| `@std/math.cryo` | Cryo-Math (já testada em pyro/node/go) |
| `@std/store.cryo` | unificar `cryo/lib/store.cryo` e `examples/taskapp/store.cryo` |
| `@std/testing.cryo` | asserts com mensagem (`assert_eq`, `assert_close`), úteis com `cryoc test` |

**Onde implementar:** em `cryo/packages.py` → `resolve_import`, um ramo para `dep == "std"` antes da consulta ao manifesto, e em `modules.py` para não exigir manifesto nesse caso. Os testes ficariam em `burnout/tests/test_packages.py`, ao lado dos testes de recusa que já existem. Nenhum backend muda.

**Por que não empacotar a Cryo-Math como builtins nativos:** daria cerca de 60 funções × 9 arquivos, com risco de divergência entre as VMs, e sem ganho de portabilidade, porque a versão em Cryo já roda em todos os backends. O ganho seria só de velocidade (uma instrução `NATIVE` contra um laço de série de Taylor na VM). Se a velocidade importar, o caminho mais barato é tornar nativas só as 3 ou 4 funções quentes (`sin`, `cos`, `exp`, `ln`) e manter a API em `@std/math.cryo`, que passaria a chamá-las.

## Proposta B: dependências por git, sem registro

```toml
[dependencies]
cryo_math = { git = "https://github.com/Victor-477/Cryo-Math", rev = "<sha do commit>" }
```

- `cryoc pkg fetch` clona para `.cryo/deps/<nome>-<sha>/`. Exigir o `rev` torna o download determinístico.
- O resto não muda: `@cryo_math/...` resolve para o diretório baixado, e o `cryo.lock` continua usando hash de conteúdo.
- Isso tira a exigência de "clonar os repositórios lado a lado", que hoje é o passo mais frágil de usar uma biblioteca de terceiros.

Há uma tensão com a filosofia atual do `pkg`, que hoje não acessa a rede de propósito. Uma forma de conciliar é deixar o download como um comando **explícito** (`pkg fetch`) e manter a compilação sempre offline, lendo só o que já está em disco.

## Proposta C: uma tabela única de nativos

Em vez de repetir o mapa nome → id em cada gerador e em cada VM, ter um arquivo `natives.json` (nome, id, aridade, backends) lido pelos geradores Python. Um teste em `burnout/tests` conferiria que as VMs Go, C e C# implementam exatamente esses ids. Isso não reduz o trabalho de implementar o nativo em cada VM, mas elimina a classe de bug em que ids divergentes fazem um nativo chamar outro.

## Ordem recomendada

1. **Fazer o merge dos PRs de correção**: sem eles, escrever uma biblioteca exige contornos (helpers para `try`, testes sem `cryoc test`, variáveis tipadas para o Go).
2. **Proposta A (`@std`)**: é pequena, não toca em backends e resolve a duplicação do `store.cryo`.
3. **Unificar a formatação de `number` no `print`** entre backends: qualquer biblioteca numérica depende disso para ter saída reprodutível.
4. **Proposta B (git)**, quando houver bibliotecas de terceiros.
5. **Proposta C**, no próximo nativo que for adicionado.
