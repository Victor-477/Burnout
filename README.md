# ⚡ Burnout — The Multi-Backend Cryo Compiler Engine

[![Version](https://img.shields.io/badge/Version-1.0.0-blue.svg)](__init__.py)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-brightgreen.svg)](pyproject.toml)

**Burnout** is the core compiler engine and command-line orchestration tool for the Cryo language system. It accepts Cryo source files (`.cryo`), performs parsing via the Cryo frontend, executes semantic checks, safety instrumentation, and optimization passes, and emits code for six distinct targets: **Pyro Bytecode (`.pyro`)**, **Native Go**, **Node.js / JavaScript**, **Native C**, **x86-64 Assembly**, and **WebAssembly**. A `.pyro` can be lowered further, ahead of time, into a standalone native binary that needs no VM at runtime.

---

## 📐 Compilation Pipeline

```text
  [ .cryo Source ]
          │
          ▼
   ┌──────────────┐
   │ CRYO Frontend│  ──► Lexer → Parser → AST → Semantic Analysis → Security Taint Check
   └──────────────┘
          │
          ▼
   ┌──────────────┐
   │ Burnout Engine│ ──► Optimizations (Constant folding, Peephole, Dead-code elimination)
   └──────────────┘
          │
  ┌───────┼───────────┬───────────┬───────────┬──────────┐
  ▼       ▼           ▼           ▼           ▼          ▼
[Pyro]  [Go]       [Node.js]     [C]       [ASM]     [WASM]
(Bytecode) (Native) (CommonJS) (Native)  (x86-64)  (browser)
  │
  └─► AOT (aot_pyro.py) ─► C ─► cc ─► standalone native binary
```

---

## 📁 Repository & Component Structure

| Component | Responsibility |
| :--- | :--- |
| 📄 [`cryoc.py`](cryoc.py) | **CLI Entry Point:** Command-line driver for compilation, LSP server launch (`--lsp`), and formatting (`fmt`). |
| 📄 [`compiler.py`](compiler.py) | **Orchestration Driver:** Manages source reading, AST loading, backend dispatching, and binary execution. |
| 📄 [`codegen_pyro.py`](codegen_pyro.py) | **Pyro Bytecode Generator:** Emits v2 binary bytecode (`.pyro`) for execution on the Go VM or C VM. |
| 📄 [`codegen_go.py`](codegen_go.py) | **Go Generator:** Emits native Go source code, providing full SaaS, HTTP, and LLM features. |
| 📄 [`codegen_node.py`](codegen_node.py) | **Node.js Generator:** Emits CommonJS JavaScript for Node.js environments. |
| 📄 [`codegen_c.py`](codegen_c.py) | **Native C Generator:** Emits safe, high-performance C source files. |
| 📄 [`codegen_asm.py`](codegen_asm.py) | **x86-64 Assembly Generator:** Emits native assembly for System V AMD64 and Windows x64 ABIs. |
| 📄 [`codegen_wasm.py`](codegen_wasm.py) | **WebAssembly Generator:** Emits a `.wasm` binary module directly (no `wat2wasm`), for the browser. |
| 📄 [`aot_pyro.py`](aot_pyro.py) | **AOT Translator:** Lowers `.pyro` to standalone C against the Pyro runtime — a native binary with no VM at runtime. |
| 📄 [`pyro.py`](pyro.py) | **Unified CLI:** `pyro build \| run \| vm \| c` over `.cryo` or `.pyro`; auto-detects the C toolchain and VM. |
| 📄 [`lsp.py`](lsp.py) | **Language Server:** Provides JSON-RPC Language Server Protocol (LSP) diagnostics, hover, and definitions. |
| 📄 [`disasm_pyro.py`](disasm_pyro.py) | **Disassembler:** Decodes `.pyro` binary files into human-readable assembly listings. |
| 📁 [`runtime/`](runtime/) | **C Runtime:** Native runtime headers (`cryo_runtime.h`) and implementation (`cryo_runtime.c`). |
| 📁 [`tests/`](tests/) | **Test Suites:** Integration, parity, bootstrap, AOT, WASM and full-stack test scripts. |

---

## 🛠️ CLI Usage Guide

Execute Burnout directly using Python:

```bash
# 1. Compile and run via the Pyro VM (custom binary bytecode)
python Burnout/cryoc.py Cryo/examples/example_bytecode.cryo --backend pyro --run --no-banner

# 2. Compile and run via the Go backend (supports LLM & concurrency features)
python Burnout/cryoc.py Cryo/examples/example_go.cryo --backend go --run --no-banner

# 3. Perform static security audit and vulnerability analysis
python Burnout/cryoc.py Cryo/examples/example_saas.cryo --audit

# 4. Format a Cryo source file in place
python Burnout/cryoc.py fmt Cryo/examples/example_calc.cryo --write

# 5. Launch the Language Server Protocol (LSP) daemon
python Burnout/cryoc.py --lsp

# 6. Compile to WebAssembly for the browser
python Burnout/cryoc.py Cryo/examples/fullstack/client.cryo --backend wasm -o app.wasm
```

### The unified `pyro` command

```bash
python Burnout/pyro.py build app.cryo -o app.exe
```

`build` produces a standalone native binary, `run` runs natively (falling back to
the VM when no C toolchain is present), `vm` interprets the bytecode, and `c`
emits the AOT C source. Each accepts `.cryo` or `.pyro`.

Because `pyro_runtime.c` uses sockets for `http_serve`, native links on Windows
also need `-lws2_32`; the CLI adds it automatically.

---

## 🐍 Using Burnout as a Python Library

Burnout can be installed as an importable Python library (`pip install -e .`):

```python
import burnout

# Compile a Cryo string directly to Go or Pyro Bytecode
go_code = burnout.compile_source('print("Hello from Cryo 1.0!");', backend="go")
pyro_bytes = burnout.compile_source('print(42);', backend="pyro")

# Tokenize and parse source code programmatically
tokens = burnout.tokenize('int x = 10;')
ast = burnout.parse_ast('int x = 10;')

# Disassemble compiled Pyro bytecode bytes
disassembly_listing = burnout.disassemble(pyro_bytes)
print(disassembly_listing)
```

---

## 🧪 Testing and Quality Assurance

Run the test suite from the repository root:

```bash
# Run 410+ frontend, backend, and security assertions
python Burnout/tests/test_smoke.py

# Verify byte-level execution parity between the C VM and Go VM
python Burnout/tests/test_c_vm.py

# Verify compiler self-hosting stages, and the bootstrap fixed point
python Burnout/tests/test_selfhost.py
python Burnout/tests/test_bootstrap.py

# Verify the AOT native route, the unified CLI, WASM, and the full-stack demo
python Burnout/tests/test_aot.py
python Burnout/tests/test_pyro_cli.py
python Burnout/tests/test_wasm.py
python Burnout/tests/test_fullstack.py
```

Suites skip any leg whose toolchain is missing (C compiler, Node, Go) instead of
failing, so a bare checkout still runs green. Installing a C compiler is what
enables `test_aot`, `test_c_vm` and `pyro build` — and those are the suites that
exercise the runtime's native path, so run them before trusting a runtime change.
