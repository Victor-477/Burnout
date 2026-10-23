# ============================================================
#  Cryo Compiler — LLM / AI Target Code Generator  (v1.1.0)
#  .cryo  ->  .llm  (AI-readable command & execution manifest)
# ============================================================
"""
Translates Cryo source AST into an AI-optimized command specification.

LLMs read structured manifests with explicit schemas, tool calling definitions,
system instructions, and sequential execution directives far more reliably than
raw imperative code.

Output includes:
1. System Role & Directive
2. Native LLM / Prompt Blocks (`>llm(...)`)
3. Data Schemas (Full JSON Schema specifications for structured output)
4. Registered Tools & Functions (`tool fn`)
5. Skills & Policies (`skill`)
6. Step-by-Step Execution Directives & Commands
7. Output Validation Contract
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from ast_nodes import (
    Node, Program, StructDecl, StructField, FunctionDecl, SkillDecl,
    VarDecl, ConstDecl, Assignment, IndexAssignment, CompoundAssignment,
    Increment, Block, Return, If, While, For, DoWhile, ForEach,
    TryCatch, Break, Continue, Switch, SwitchCase, Assert,
    ForeignBlock, BinaryExpr, TernaryExpr, CastExpr, UnwrapExpr,
    TryExpr, SpawnExpr, AwaitExpr, MapLiteral, ArrayLiteral,
    UnaryExpr, CallExpr, CallValueExpr, MethodCallExpr, FieldAccess,
    IndexAccess, StructInit, Lambda, Identifier, Literal,
)


class CodeGenLLMError(Exception):
    pass


class CodeGenLLM:
    """Generates an AI-optimized command execution manifest (.llm) from Cryo AST."""

    _PRIMITIVES: Dict[str, str] = {
        'int': 'integer',
        'number': 'number',
        'string': 'string',
        'bool': 'boolean',
        'void': 'null',
        'null': 'null',
    }

    def __init__(self, emit_format: str = "markdown"):
        self.emit_format = emit_format  # "markdown" or "json"
        self._schemas: Dict[str, StructDecl] = {}
        self._tools: List[FunctionDecl] = []
        self._skills: List[SkillDecl] = []
        self._functions: List[FunctionDecl] = []
        self._prompts: List[str] = []
        self._commands: List[str] = []
        self._variables: Dict[str, str] = {}

    def generate(self, program: Program) -> str:
        """Entry point: converts Program AST into .llm command manifest."""
        self._scan(program)

        if self.emit_format == "json":
            return self._generate_json()
        return self._generate_markdown()

    def _scan(self, program: Program):
        """Scans top-level declarations and collects execution commands."""
        for stmt in program.statements:
            if isinstance(stmt, StructDecl):
                self._schemas[stmt.name] = stmt
            elif isinstance(stmt, SkillDecl):
                self._skills.append(stmt)
            elif isinstance(stmt, FunctionDecl):
                if stmt.is_tool:
                    self._tools.append(stmt)
                else:
                    self._functions.append(stmt)
            elif isinstance(stmt, ForeignBlock):
                if stmt.lang.lower() in ('llm', 'prompt', 'ai'):
                    self._prompts.append(stmt.code.strip())
            else:
                cmd = self._stmt_to_command(stmt)
                if cmd:
                    self._commands.append(cmd)

    def _json_schema_obj(self, typ: str) -> Dict[str, Any]:
        """Recursively generates JSON Schema dict for a Cryo type."""
        base = typ.rstrip('?')
        if base in self._PRIMITIVES:
            return {"type": self._PRIMITIVES[base]}
        if base.endswith('[]'):
            item_type = base[:-2]
            return {
                "type": "array",
                "items": self._json_schema_obj(item_type)
            }
        if base.startswith('map<') and base.endswith('>'):
            return {"type": "object"}
        if base in self._schemas:
            struct = self._schemas[base]
            props = {f.name: self._json_schema_obj(f.field_type) for f in struct.fields}
            reqs = [f.name for f in struct.fields if not f.field_type.endswith('?')]
            return {
                "type": "object",
                "description": f"Schema definition for {base}",
                "properties": props,
                "required": reqs
            }
        return {"type": "string"}

    def _expr(self, node: Optional[Node]) -> str:
        """Converts an expression AST node into human/AI-readable expression string."""
        if node is None:
            return "null"
        if isinstance(node, Literal):
            if node.kind == 'string':
                return json.dumps(node.value, ensure_ascii=False)
            if node.kind == 'null':
                return "null"
            if node.kind == 'bool':
                return "true" if node.value else "false"
            return str(node.value)
        if isinstance(node, Identifier):
            return node.name
        if isinstance(node, BinaryExpr):
            return f"({self._expr(node.left)} {node.op} {self._expr(node.right)})"
        if isinstance(node, UnaryExpr):
            return f"{node.op}{self._expr(node.operand)}"
        if isinstance(node, TernaryExpr):
            return f"(if {self._expr(node.condition)} then {self._expr(node.then_expr)} else {self._expr(node.else_expr)})"
        if isinstance(node, CastExpr):
            # Special formatting for `llm(...) as Schema`
            if isinstance(node.expr, CallExpr) and node.expr.callee == 'llm':
                model = self._expr(node.expr.args[0]) if node.expr.args else '"default"'
                prompt = self._expr(node.expr.args[1]) if len(node.expr.args) > 1 else '""'
                return f"LLM_QUERY(model={model}, prompt={prompt}, response_schema={node.target_type})"
            return f"({self._expr(node.expr)} as {node.target_type})"
        if isinstance(node, UnwrapExpr):
            inner = getattr(node, 'operand', None) or getattr(node, 'expr', None)
            return f"{self._expr(inner)}!"
        if isinstance(node, FieldAccess):
            return f"{self._expr(node.obj)}.{node.field}"
        if isinstance(node, IndexAccess):
            return f"{self._expr(node.obj)}[{self._expr(node.index)}]"
        if isinstance(node, ArrayLiteral):
            elems = ", ".join(self._expr(e) for e in node.elements)
            return f"[{elems}]"
        if isinstance(node, MapLiteral):
            pairs = []
            for k, v in node.pairs:
                k_str = self._expr(k)
                v_str = self._expr(v)
                pairs.append(f"{k_str}: {v_str}")
            return "{" + ", ".join(pairs) + "}"
        if isinstance(node, CallExpr):
            if node.callee == 'llm':
                model = self._expr(node.args[0]) if node.args else '"default"'
                prompt = self._expr(node.args[1]) if len(node.args) > 1 else '""'
                opts = f", options={self._expr(node.args[2])}" if len(node.args) > 2 else ""
                return f"LLM_COMPLETE(model={model}, prompt={prompt}{opts})"
            if node.callee in ('agent', 'agent_call'):
                model = self._expr(node.args[0]) if node.args else '"default"'
                prompt = self._expr(node.args[1]) if len(node.args) > 1 else '""'
                return f"AGENT_TASK(model={model}, instruction={prompt})"
            if node.callee == 'schema_of':
                return f"SCHEMA_OF({self._expr(node.args[0])})"
            args = ", ".join(self._expr(a) for a in node.args)
            return f"{node.callee}({args})"
        if isinstance(node, MethodCallExpr):
            args = ", ".join(self._expr(a) for a in node.args)
            return f"{self._expr(node.obj)}.{node.method}({args})"
        return str(node)

    def _stmt_to_command(self, stmt: Node, indent: int = 0) -> str:
        """Converts an executable statement into an unambiguous command directive."""
        ind = "  " * indent
        if isinstance(stmt, VarDecl):
            self._variables[stmt.name] = stmt.var_type
            val_str = self._expr(stmt.value) if stmt.value is not None else "null"
            # Check if this is an LLM structured query assignment
            if isinstance(stmt.value, CastExpr) and isinstance(stmt.value.expr, CallExpr) and stmt.value.expr.callee == 'llm':
                model = self._expr(stmt.value.expr.args[0])
                prompt = self._expr(stmt.value.expr.args[1])
                target = stmt.value.target_type
                return (f"{ind}• EXECUTE LLM GENERATION: Query model {model} with prompt {prompt}\n"
                        f"{ind}  ↳ Constrain output strictly to Schema `{target}` and store result into variable `{stmt.name}`")
            return f"{ind}• DECLARE `{stmt.name}` ({stmt.var_type}) = {val_str}"

        if isinstance(stmt, ConstDecl):
            return f"{ind}• CONSTANT `{stmt.name}` ({stmt.var_type}) = {self._expr(stmt.value)}"

        if isinstance(stmt, Assignment):
            target = getattr(stmt.target, 'name', self._expr(stmt.target))
            return f"{ind}• ASSIGN `{target}` = {self._expr(stmt.value)}"

        if isinstance(stmt, IndexAssignment):
            return f"{ind}• UPDATE `{self._expr(stmt.array)}[{self._expr(stmt.index)}]` = {self._expr(stmt.value)}"

        if isinstance(stmt, CompoundAssignment):
            return f"{ind}• MUTATE `{stmt.name}` {stmt.op} {self._expr(stmt.value)}"

        if isinstance(stmt, Increment):
            return f"{ind}• INCREMENT `{stmt.name}` by {stmt.op}"

        if isinstance(stmt, If):
            cond = self._expr(stmt.condition)
            lines = [f"{ind}• IF ({cond}):"]
            for s in stmt.then_body:
                lines.append(self._stmt_to_command(s, indent + 1))
            if stmt.else_body:
                lines.append(f"{ind}  ELSE:")
                for s in stmt.else_body:
                    lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, While):
            lines = [f"{ind}• WHILE ({self._expr(stmt.condition)}):"]
            for s in stmt.body:
                lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, For):
            init = f"`{stmt.var_name}` in [{self._expr(stmt.range_start)}..{self._expr(stmt.range_end)}]" if stmt.is_range else "loop"
            lines = [f"{ind}• FOR ({init}):"]
            for s in stmt.body:
                lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, ForEach):
            lines = [f"{ind}• FOR EACH `{stmt.var_name}` IN {self._expr(stmt.iterable)}:"]
            for s in stmt.body:
                lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, Return):
            return f"{ind}• RETURN {self._expr(stmt.value)}"

        if isinstance(stmt, Assert):
            msg = f" (Error: {self._expr(stmt.message)})" if stmt.message else ""
            return f"{ind}• ASSERT ({self._expr(stmt.condition)}){msg}"

        if isinstance(stmt, ForeignBlock):
            if stmt.lang.lower() in ('llm', 'prompt', 'ai'):
                return f"{ind}• PROMPT INSTRUCTION:\n{ind}  \"\"\"\n{stmt.code.strip()}\n{ind}  \"\"\""
            return f"{ind}• FOREIGN BLOCK [{stmt.lang}]: {stmt.code.strip()}"

        if isinstance(stmt, CallExpr):
            if stmt.callee == 'print':
                args_str = ", ".join(self._expr(a) for a in stmt.args)
                return f"{ind}• OUTPUT / PRINT: {args_str}"
            if stmt.callee == 'llm':
                model = self._expr(stmt.args[0])
                prompt = self._expr(stmt.args[1])
                return f"{ind}• CALL LLM: Model {model} with prompt {prompt}"
            if stmt.callee in ('agent', 'agent_call'):
                model = self._expr(stmt.args[0])
                prompt = self._expr(stmt.args[1])
                return f"{ind}• DISPATCH AGENT: Model {model} with goal {prompt}"
            args_str = ", ".join(self._expr(a) for a in stmt.args)
            return f"{ind}• INVOKE `{stmt.callee}({args_str})`"

        return f"{ind}• EXECUTE {type(stmt).__name__}"

    def _generate_markdown(self) -> str:
        """Emits the full AI-oriented Markdown command execution manifest."""
        out = [
            "# 🤖 LLM Execution Command & Manifest",
            "> **Generated by Burnout Compiler (`--backend llm`)**",
            "> This document is an optimized instruction file formatted for direct AI comprehension, reasoning, and autonomous execution.",
            "",
            "## 📋 1. System Directive & Execution Role",
            "You are an autonomous AI runtime engine executing the logic specified in this manifest.",
            "- Adhere strictly to the defined data schemas when generating structured JSON payloads.",
            "- When tools are invoked or required, refer to the tool signatures in Section 3.",
            "- Carry out the sequential instructions in Section 5 step-by-step and produce the final specified output.",
            "",
        ]

        # 2. Prompts & LLM foreign blocks
        if self._prompts:
            out.append("## 🧠 2. Global System Prompts & Foreign LLM Blocks (`>llm<`)")
            for i, p in enumerate(self._prompts, 1):
                out.append(f"### Prompt Block #{i}")
                out.append("```text")
                out.append(p)
                out.append("```")
                out.append("")

        # 3. Schemas
        if self._schemas:
            out.append("## 🧬 3. Structured Data Schemas (JSON Schema Specification)")
            out.append("When any LLM call or tool specifies structured output of these types, enforce the corresponding schema:")
            out.append("")
            for name, s in self._schemas.items():
                schema_json = json.dumps(self._json_schema_obj(name), indent=2, ensure_ascii=False)
                out.append(f"### Schema `{name}`")
                fields_summary = ", ".join(f"{f.name}: {f.field_type}" for f in s.fields)
                out.append(f"- **Cryo Signature**: `schema {name} {{ {fields_summary} }}`")
                out.append("- **JSON Schema Validation Definition**:")
                out.append("```json")
                out.append(schema_json)
                out.append("```")
                out.append("")

        # 4. Tools & Functions
        if self._tools or self._functions:
            out.append("## 🛠️ 4. Tools & Callable Functions")
            if self._tools:
                out.append("### Registered AI Tools (`tool fn`)")
                for fn in self._tools:
                    params_summary = ", ".join(f"{pn}: {pt}" for pt, pn in fn.params) or "void"
                    ret = fn.return_type or "void"
                    params_schema = {pn: self._json_schema_obj(pt) for pt, pn in fn.params}
                    tool_call_spec = {
                        "name": fn.name,
                        "description": f"Tool function '{fn.name}' compiled from Cryo source.",
                        "parameters": {
                            "type": "object",
                            "properties": params_schema,
                            "required": [pn for pt, pn in fn.params if not pt.endswith('?')]
                        }
                    }
                    out.append(f"#### Tool: `{fn.name}({params_summary}) -> {ret}`")
                    out.append("```json")
                    out.append(json.dumps(tool_call_spec, indent=2, ensure_ascii=False))
                    out.append("```")
                    out.append("")
            if self._functions:
                out.append("### Auxiliary Functions")
                for fn in self._functions:
                    params_summary = ", ".join(f"{pn}: {pt}" for pt, pn in fn.params) or "void"
                    ret = fn.return_type or "void"
                    out.append(f"- `{fn.name}({params_summary}) -> {ret}`")
                out.append("")

        # 5. Skills
        if self._skills:
            out.append("## 🎯 5. Declared Skills & Execution Policies")
            for sk in self._skills:
                out.append(f"### Skill: `{sk.name}`")
                cfg = {}
                for k, v in sk.fields:
                    cfg[k] = self._expr(v)
                out.append("```json")
                out.append(json.dumps(cfg, indent=2, ensure_ascii=False))
                out.append("```")
                out.append("")

        # 6. Workflow & Commands
        out.append("## ⚡ 6. Execution Directives & Sequential Workflow")
        out.append("Follow and execute the following sequential commands in exact order:")
        out.append("")
        if self._commands:
            for cmd in self._commands:
                out.append(cmd)
        else:
            out.append("*(No top-level commands found; call registered tools or functions as needed)*")
        out.append("")

        # 7. Output Contract
        out.append("## 📤 7. Expected Output Contract")
        out.append("- Produce the computed result matching the execution workflow.")
        out.append("- If structured schemas were queried (`llm(...) as Schema`), return valid JSON adhering strictly to the schema.")
        out.append("- Return execution stdout/prints in the final summary.")
        out.append("")

        return "\n".join(out)

    def _generate_json(self) -> str:
        """Emits a structured JSON representation of the LLM command manifest."""
        manifest = {
            "version": "1.1.0",
            "target": "llm",
            "prompts": self._prompts,
            "schemas": {name: self._json_schema_obj(name) for name in self._schemas},
            "tools": [
                {
                    "name": fn.name,
                    "return_type": fn.return_type or "void",
                    "parameters": {
                        "type": "object",
                        "properties": {pn: self._json_schema_obj(pt) for pt, pn in fn.params},
                        "required": [pn for pt, pn in fn.params if not pt.endswith('?')]
                    }
                } for fn in self._tools
            ],
            "skills": [
                {
                    "name": sk.name,
                    "config": {k: self._expr(v) for k, v in sk.fields}
                } for sk in self._skills
            ],
            "commands": self._commands
        }
        return json.dumps(manifest, indent=2, ensure_ascii=False)
