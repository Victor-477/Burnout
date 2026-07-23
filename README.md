# ⚡ Burnout — The Multi-Backend Cryo Compiler Engine

[![Version](https://img.shields.io/badge/Version-1.0.0-blue.svg)](__init__.py)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-brightgreen.svg)](pyproject.toml)

**Burnout** is the core compiler engine and command-line orchestration tool for the Cryo language system. It accepts Cryo source files (`.cryo`), performs parsing via the Cryo frontend, executes semantic checks, safety instrumentation, and optimization passes, and emits code for five distinct targets: **Pyro Bytecode (`.pyro`)**, **Native Go**, **Node.js / JavaScript**, **Native C**, and **x86-64 Assembly**.

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
  ┌───────┼───────────┬───────────┬───────────┐
  ▼       ▼           ▼           ▼           ▼
[Pyro]  [Go]       [Node.js]     [C]       [ASM]
(Bytecode) (Native) (CommonJS) (Native)  (x86-64)
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
| 📄 [`lsp.py`](lsp.py) | **Language Server:** Provides JSON-RPC Language Server Protocol (LSP) diagnostics, hover, and definitions. |
| 📄 [`disasm_pyro.py`](disasm_pyro.py) | **Disassembler:** Decodes `.pyro` binary files into human-readable assembly listings. |
| 📁 [`runtime/`](runtime/) | **C Runtime:** Native runtime headers (`cryo_runtime.h`) and implementation (`cryo_runtime.c`). |
| 📁 [`tests/`](tests/) | **Test Suites:** Integration and parity test scripts (`test_smoke.py`, `test_c_vm.py`, `test_selfhost.py`). |

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
```

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

# Verify compiler self-hosting stages
python Burnout/tests/test_selfhost.py
```
