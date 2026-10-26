# ============================================================
#  Cryo Compiler — LLM / AI Target Code Generator  (v1.2.0)
#  .cryo  ->  .llm  (Ultra-Optimized AI Agent Manifest & Directives)
# ============================================================
"""
Translates Cryo source AST into an AI-optimized command specification.

LLMs read structured manifests with explicit schemas, tool calling definitions,
system instructions, and sequential execution directives far more reliably than
raw imperative code.

Output includes:
1. Executive Protocol & Operating Directives (Few-shot guidance, strict formatting)
2. Prompts & LLM Foreign Blocks (`>llm(...)` / `import >llm<`)
3. Data Models: Full JSON Schema Specifications for `schema`, `struct`, and `enum`
4. Tool Calling Registry: OpenAI & Anthropic compliant Tool/Function specifications
5. Skills & Behavioral Policies
6. Step-by-Step Execution Plan (Variables, LLM Generations, Agent Tasks, Control Flow)
7. Final Response & Output Contract
"""

import json
from typing import Any, Dict, List, Optional, Tuple, Set

from ast_nodes import (
    Node, Program, StructDecl, StructField, FunctionDecl, SkillDecl,
    EnumDecl, MatchStatement, MatchCase,
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
        'float': 'number',
        'double': 'number',
        'string': 'string',
        'bool': 'boolean',
        'void': 'null',
        'null': 'null',
    }

    def __init__(self, emit_format: str = "markdown"):
        self.emit_format = emit_format  # "markdown" or "json"
        self._schemas: Dict[str, StructDecl] = {}
        self._enums: Dict[str, EnumDecl] = {}
        self._tools: List[FunctionDecl] = []
        self._skills: List[SkillDecl] = []
        self._functions: List[FunctionDecl] = []
        self._prompts: List[str] = []
        self._commands: List[str] = []
        self._variables: Dict[str, str] = {}
        self._declared_models: Set[str] = set()

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
            elif isinstance(stmt, EnumDecl):
                self._enums[stmt.name] = stmt
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
        is_optional = typ.endswith('?')

        if base in self._PRIMITIVES:
            schema = {"type": self._PRIMITIVES[base]}
            return schema

        if base.endswith('[]'):
            item_type = base[:-2]
            return {
                "type": "array",
                "items": self._json_schema_obj(item_type)
            }

        if base.startswith('map<') and base.endswith('>'):
            return {"type": "object"}

        if base in self._enums:
            enum_decl = self._enums[base]
            variants = [m.name for m in enum_decl.members]
            return {
                "type": "string",
                "enum": variants,
                "description": f"Enumeration {base} (allowed variants: {', '.join(variants)})"
            }

        if base in self._schemas:
            struct = self._schemas[base]
            props = {f.name: self._json_schema_obj(f.field_type) for f in struct.fields}
            reqs = [f.name for f in struct.fields if not f.field_type.endswith('?')]
            return {
                "type": "object",
                "description": f"Schema definition for {base}",
                "properties": props,
                "required": reqs,
                "additionalProperties": False
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
                return f"LLM_STRUCTURED_QUERY(model={model}, prompt={prompt}, schema={node.target_type})"
            return f"({self._expr(node.expr)} as {node.target_type})"
        if isinstance(node, UnwrapExpr):
            inner = getattr(node, 'operand', None) or getattr(node, 'expr', None)
            return f"{self._expr(inner)}!"
        if isinstance(node, TryExpr):
            return f"{self._expr(node.operand)}?"
        if isinstance(node, SpawnExpr):
            return f"SPAWN_ASYNC({self._expr(node.expr)})"
        if isinstance(node, AwaitExpr):
            return f"AWAIT({self._expr(node.expr)})"
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
                return f"AGENT_EXECUTE(model={model}, task={prompt})"
            if node.callee == 'schema_of':
                return f"SCHEMA_OF({self._expr(node.args[0])})"
            args = ", ".join(self._expr(a) for a in node.args)
            return f"{node.callee}({args})"
        if isinstance(node, MethodCallExpr):
            args = ", ".join(self._expr(a) for a in node.args)
            return f"{self._expr(node.obj)}.{node.method}({args})"
        if isinstance(node, StructInit):
            field_inits = ", ".join(f"{f}: {self._expr(v)}" for f, v in node.fields)
            return f"{node.name} {{ {field_inits} }}"
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
                return (f"{ind}• STEP [LLM_QUERY] -> Variable `{stmt.name}` ({target}):\n"
                        f"{ind}  - Engine: {model}\n"
                        f"{ind}  - Input Prompt: {prompt}\n"
                        f"{ind}  - Target Schema: `{target}`\n"
                        f"{ind}  - Directive: Generate structured JSON strictly conforming to Schema `{target}` and assign to `{stmt.name}`.")
            # Check if this is an Agent dispatch assignment
            if isinstance(stmt.value, CallExpr) and stmt.value.callee in ('agent', 'agent_call'):
                model = self._expr(stmt.value.args[0]) if stmt.value.args else '"default"'
                task = self._expr(stmt.value.args[1]) if len(stmt.value.args) > 1 else '""'
                return (f"{ind}• STEP [AGENT_DISPATCH] -> Variable `{stmt.name}` ({stmt.var_type}):\n"
                        f"{ind}  - Agent Model: {model}\n"
                        f"{ind}  - Goal / Task: {task}\n"
                        f"{ind}  - Directive: Delegate task to autonomous agent with access to registered tools, then store output in `{stmt.name}`.")
            return f"{ind}• STEP [DECLARE] -> Variable `{stmt.name}` ({stmt.var_type}) = {val_str}"

        if isinstance(stmt, ConstDecl):
            return f"{ind}• STEP [CONSTANT] -> Constant `{stmt.name}` ({stmt.var_type}) = {self._expr(stmt.value)}"

        if isinstance(stmt, Assignment):
            target = getattr(stmt, 'name', None) or getattr(stmt, 'target', 'var')
            if not isinstance(target, str):
                target = self._expr(target)
            return f"{ind}• STEP [ASSIGN] -> Set `{target}` = {self._expr(stmt.value)}"

        if isinstance(stmt, IndexAssignment):
            obj = getattr(stmt, 'obj', None) or getattr(stmt, 'array', 'arr')
            return f"{ind}• STEP [UPDATE] -> Set element `{self._expr(obj)}[{self._expr(stmt.index)}]` = {self._expr(stmt.value)}"

        if isinstance(stmt, CompoundAssignment):
            return f"{ind}• STEP [MUTATE] -> Modify `{stmt.name}` with operator {stmt.op}: {self._expr(stmt.value)}"

        if isinstance(stmt, Increment):
            return f"{ind}• STEP [INCREMENT] -> Update `{stmt.name}` ({stmt.op})"

        if isinstance(stmt, If):
            cond = self._expr(stmt.condition)
            lines = [f"{ind}• STEP [BRANCH] -> IF condition evaluates to true: ({cond})"]
            for s in stmt.then_body:
                lines.append(self._stmt_to_command(s, indent + 1))
            if stmt.else_body:
                lines.append(f"{ind}  ELSE:")
                for s in stmt.else_body:
                    lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, While):
            lines = [f"{ind}• STEP [LOOP_WHILE] -> Repeat while condition is true: ({self._expr(stmt.condition)})"]
            for s in stmt.body:
                lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, For):
            init = f"`{stmt.var_name}` in range [{self._expr(stmt.range_start)} .. {self._expr(stmt.range_end)}]" if stmt.is_range else "iterations"
            lines = [f"{ind}• STEP [LOOP_FOR] -> Iterate over {init}:"]
            for s in stmt.body:
                lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, ForEach):
            lines = [f"{ind}• STEP [LOOP_FOR_EACH] -> For each item `{stmt.var_name}` in collection {self._expr(stmt.iterable)}:"]
            for s in stmt.body:
                lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, MatchStatement):
            lines = [f"{ind}• STEP [PATTERN_MATCH] -> Evaluate subject: {self._expr(stmt.subject)}"]
            for case in stmt.cases:
                vars_str = f"({', '.join(case.pattern_vars)})" if case.pattern_vars else ""
                lines.append(f"{ind}  CASE `{case.pattern_name}{vars_str}`:")
                for s in case.body:
                    lines.append(self._stmt_to_command(s, indent + 2))
            return "\n".join(lines)

        if isinstance(stmt, Switch):
            lines = [f"{ind}• STEP [SWITCH] -> Evaluate switch expression: {self._expr(stmt.subject)}"]
            for c in stmt.cases:
                c_vals = ", ".join(self._expr(v) for v in c.values) if c.values else "default"
                lines.append(f"{ind}  CASE {c_vals}:")
                for s in c.body:
                    lines.append(self._stmt_to_command(s, indent + 2))
            if stmt.default_body:
                lines.append(f"{ind}  DEFAULT:")
                for s in stmt.default_body:
                    lines.append(self._stmt_to_command(s, indent + 2))
            return "\n".join(lines)

        if isinstance(stmt, TryCatch):
            lines = [f"{ind}• STEP [TRY_BLOCK]:"]
            for s in stmt.try_body:
                lines.append(self._stmt_to_command(s, indent + 1))
            if stmt.catch_body:
                c_name = f" as `{stmt.catch_name}`" if stmt.catch_name else ""
                lines.append(f"{ind}  CATCH Error{c_name}:")
                for s in stmt.catch_body:
                    lines.append(self._stmt_to_command(s, indent + 1))
            if stmt.finally_body:
                lines.append(f"{ind}  FINALLY:")
                for s in stmt.finally_body:
                    lines.append(self._stmt_to_command(s, indent + 1))
            return "\n".join(lines)

        if isinstance(stmt, Return):
            return f"{ind}• STEP [RETURN] -> Complete execution and return value: {self._expr(stmt.value)}"

        if isinstance(stmt, Assert):
            msg = f" (Failure message: {self._expr(stmt.message)})" if stmt.message else ""
            return f"{ind}• STEP [ASSERT] -> Verify condition holds: ({self._expr(stmt.condition)}){msg}"

        if isinstance(stmt, ForeignBlock):
            if stmt.lang.lower() in ('llm', 'prompt', 'ai'):
                return f"{ind}• STEP [PROMPT_DIRECTIVE]:\n{ind}  \"\"\"\n{stmt.code.strip()}\n{ind}  \"\"\""
            return f"{ind}• STEP [FOREIGN_BLOCK:{stmt.lang}]: {stmt.code.strip()}"

        if isinstance(stmt, CallExpr):
            if stmt.callee == 'print':
                args_str = ", ".join(self._expr(a) for a in stmt.args)
                return f"{ind}• STEP [OUTPUT_RESULT] -> Render/Print: {args_str}"
            if stmt.callee == 'llm':
                model = self._expr(stmt.args[0])
                prompt = self._expr(stmt.args[1])
                return f"{ind}• STEP [LLM_QUERY] -> Query model {model} with prompt {prompt}"
            if stmt.callee in ('agent', 'agent_call'):
                model = self._expr(stmt.args[0])
                prompt = self._expr(stmt.args[1])
                return f"{ind}• STEP [AGENT_DISPATCH] -> Dispatch autonomous agent with model {model} for goal: {prompt}"
            args_str = ", ".join(self._expr(a) for a in stmt.args)
            return f"{ind}• STEP [CALL_FUNCTION] -> Execute `{stmt.callee}({args_str})`"

        return f"{ind}• STEP [EXECUTE] -> {type(stmt).__name__}"

    def _generate_markdown(self) -> str:
        """Emits an ultra-optimized AI-oriented Markdown command execution manifest."""
        out = [
            "# 🤖 Autonomous AI Agent Execution Protocol & Manifest",
            "> **Generated by Burnout Compiler (`--backend llm`)**",
            "> **Target Engine**: Universal LLM / Agent Runtime (Claude 3.5 / GPT-4o / Gemini 1.5 Pro / Llama 3)",
            "",
            "## 📋 1. Executive Directive & Operating Principles",
            "You are an autonomous AI runtime engine executing the logic specified in this manifest.",
            "Operate strictly according to the following directives:",
            "1. **Deterministic Execution**: Execute every step under Section 6 in exact sequential order.",
            "2. **Strict Schema Adherence**: When a step specifies a target Schema (e.g. `Schema X`), the output must be valid, parseable JSON conforming 100% to the corresponding JSON Schema in Section 3 (no markdown code blocks inside JSON fields, no hallucinated keys).",
            "3. **Tool Invocations**: When actions require external calculation or retrieval, invoke the exact tool signatures declared in Section 4.",
            "4. **Agent Autonomy**: For `AGENT_DISPATCH` tasks, reason through intermediate steps, call necessary tools, and produce concise, validated answers.",
            "5. **Output Contract**: Provide the final program results as specified in Section 7.",
            "",
        ]

        # 2. Prompts & LLM foreign blocks
        if self._prompts:
            out.append("## 🧠 2. Global System Instructions & Context Prompts (`>llm<`)")
            for i, p in enumerate(self._prompts, 1):
                out.append(f"### 📌 System Instruction #{i}")
                out.append("```text")
                out.append(p)
                out.append("```")
                out.append("")

        # 3. Schemas & Enums
        if self._schemas or self._enums:
            out.append("## 🧬 3. Structured Data Models & Schemas (JSON Schema Specification)")
            out.append("Enforce these exact JSON Schema constraints whenever structured generation or tool interaction is requested:")
            out.append("")

            # Enums
            for name, e in self._enums.items():
                variants = [m.name for m in e.members]
                out.append(f"### Enum `{name}`")
                out.append(f"- **Valid Values**: `{json.dumps(variants)}`")
                out.append("")

            # Structs / Schemas
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
            out.append("## 🛠️ 4. Tool Registry & Functional Capabilities")
            if self._tools:
                out.append("### ⚡ Registered Callable AI Tools (`tool fn`)")
                out.append("These tools can be autonomously called by the agent or LLM execution runtime:")
                out.append("")
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
                out.append("### 📦 Auxiliary Deterministic Functions")
                for fn in self._functions:
                    params_summary = ", ".join(f"{pn}: {pt}" for pt, pn in fn.params) or "void"
                    ret = fn.return_type or "void"
                    out.append(f"- `{fn.name}({params_summary}) -> {ret}`")
                out.append("")

        # 5. Skills
        if self._skills:
            out.append("## 🎯 5. Declared Skills & Execution Policies (`skill`)")
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
        out.append("## ⚡ 6. Sequential Step-by-Step Execution Plan")
        out.append("Execute the following planned operations in order. For each step, apply the appropriate model, schema, or tool:")
        out.append("")
        if self._commands:
            for i, cmd in enumerate(self._commands, 1):
                out.append(f"{i}. {cmd.lstrip()}")
        else:
            out.append("*(No sequential top-level steps; the agent should stand ready to invoke tools or validate schemas as queried)*")
        out.append("")

        # 7. Output Contract
        out.append("## 📤 7. Expected Final Output Contract")
        out.append("When execution finishes:")
        out.append("1. **Outputs / Prints**: Report all values produced by `[OUTPUT_RESULT]` steps.")
        out.append("2. **Structured Objects**: If variables of a Schema type were parsed or extracted, output their final JSON representations matching the schema.")
        out.append("3. **Agent Status**: If any step failed an assertion or schema validation, return a concise diagnostic report.")
        out.append("")

        return "\n".join(out)

    def _generate_json(self) -> str:
        """Emits an ultra-structured JSON representation of the LLM command manifest."""
        manifest = {
            "version": "1.2.0",
            "format": "cryo-llm-manifest",
            "protocol": {
                "role": "autonomous_ai_runtime",
                "rules": [
                    "Execute sequential steps in order",
                    "Enforce JSON Schema on structured queries",
                    "Invoke registered tools when requested",
                    "Produce final results per output contract"
                ]
            },
            "system_prompts": self._prompts,
            "enums": {name: [m.name for m in e.members] for name, e in self._enums.items()},
            "schemas": {name: self._json_schema_obj(name) for name in self._schemas},
            "tools": [
                {
                    "name": fn.name,
                    "description": f"Tool function '{fn.name}' compiled from Cryo source.",
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
            "execution_plan": [cmd.strip() for cmd in self._commands]
        }
        return json.dumps(manifest, indent=2, ensure_ascii=False)
