#!/usr/bin/env python3
# ============================================================
#  test_llm_backend.py — Dedicated Test Suite for LLM Language Target
#
#  Verifies:
#  1. Cryo AST -> .llm Markdown manifest compilation
#  2. JSON Schema generation for schemas, nested structs, and enums
#  3. OpenAI / Anthropic compliant Tool calling JSON generation
#  4. Structured LLM query decomposition (llm(...) as Schema)
#  5. Agent task delegation steps (agent(...) / agent_call(...))
#  6. Foreign LLM blocks (>llm(...) / >prompt(...) / >ai(...))
#  7. Node and Go backends handling foreign prompt blocks gracefully
#  8. Direct CLI invocation with `cryoc.py --backend llm` and `-o`
#  9. Structured JSON output format (`CodeGenLLM(emit_format="json")`)
# ============================================================
import json
import os
import subprocess
import sys
import tempfile
import unittest

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

from Burnout.compiler import compile_source
from Burnout.codegen_llm import CodeGenLLM
from Cryo.parser import Parser
from Cryo.lexer import Lexer

CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')


class TestLLMBackend(unittest.TestCase):

    def test_basic_prompt_and_directives(self):
        src = """
        import >llm<

        >llm(
          You are an automated research analyst.
          Always prioritize primary sources and summarize concisely.
        )

        string query = "Quantum computing breakthroughs";
        print(query);
        """
        out = compile_source(src, backend="llm", safe=True)
        self.assertIn("Autonomous AI Agent Execution Protocol & Manifest", out)
        self.assertIn("You are an automated research analyst.", out)
        self.assertIn("STEP [DECLARE] -> Variable `query`", out)
        self.assertIn("STEP [OUTPUT_RESULT] -> Render/Print: query", out)

    def test_json_schema_generation(self):
        src = """
        import >llm<

        enum Priority {
          Low,
          Medium,
          High
        }

        schema SubTask {
          string title;
          bool completed;
        }

        schema TaskReport {
          string id;
          Priority priority;
          SubTask[] subtasks;
          number score;
        }

        TaskReport r = llm("gpt-4o", "Analyze task") as TaskReport;
        """
        out = compile_source(src, backend="llm", safe=True)
        self.assertIn("Enum `Priority`", out)
        self.assertIn('["Low", "Medium", "High"]', out)
        self.assertIn("Schema `SubTask`", out)
        self.assertIn("Schema `TaskReport`", out)
        self.assertIn('"additionalProperties": false', out)
        self.assertIn("STEP [LLM_QUERY] -> Variable `r` (TaskReport)", out)
        self.assertIn("Target Schema: `TaskReport`", out)

    def test_tool_registry_and_call_spec(self):
        src = """
        import >llm<

        tool fn fetch_stock_price(string ticker, bool include_extended_hours) -> number ={
          return 145.50;
        }

        tool fn calculate_ratio(number a, number b) -> number ={
          return a / b;
        }

        string result = agent("claude-3-5-sonnet", "Evaluate APPL valuation");
        print(result);
        """
        out = compile_source(src, backend="llm", safe=True)
        self.assertIn("Tool Registry & Functional Capabilities", out)
        self.assertIn("fetch_stock_price", out)
        self.assertIn("calculate_ratio", out)
        self.assertIn('"ticker"', out)
        self.assertIn('"include_extended_hours"', out)
        self.assertIn("STEP [AGENT_DISPATCH] -> Variable `result`", out)
        self.assertIn("Agent Model: \"claude-3-5-sonnet\"", out)

    def test_skills_and_policies(self):
        src = """
        import >llm<

        skill CustomerSupportPolicy {
          model: "gpt-4o",
          temperature: 0.2,
          max_tokens: 500
        }

        string response = agent("gpt-4o", "Help with refund request");
        """
        out = compile_source(src, backend="llm", safe=True)
        self.assertIn("Skill: `CustomerSupportPolicy`", out)
        self.assertIn('"temperature": "0.2"', out)
        self.assertIn('"max_tokens": "500"', out)

    def test_control_flow_directives(self):
        src = """
        import >llm<

        int count = 5;
        if (count > 0) {
          print("Positive");
        } else {
          print("Non-positive");
        }

        while (count > 0) {
          count = count - 1;
        }
        """
        out = compile_source(src, backend="llm", safe=True)
        self.assertIn("STEP [BRANCH] -> IF condition evaluates to true:", out)
        self.assertIn("count > 0", out)
        self.assertIn("STEP [LOOP_WHILE] -> Repeat while condition is true:", out)
        self.assertIn("count > 0", out)

    def test_structured_json_manifest_output(self):
        src = """
        import >llm<

        schema User {
          string name;
          int age;
        }

        tool fn get_user(string id) -> string ={
          return "Alice";
        }

        User u = llm("gpt-4o", "Parse user") as User;
        """
        json_str = compile_source(src, backend="llm", safe=True, emit="json")
        data = json.loads(json_str)

        self.assertEqual(data["version"], "1.2.0")
        self.assertEqual(data["format"], "cryo-llm-manifest")
        self.assertIn("User", data["schemas"])
        self.assertEqual(data["schemas"]["User"]["properties"]["name"]["type"], "string")
        self.assertEqual(data["schemas"]["User"]["properties"]["age"]["type"], "integer")
        self.assertEqual(len(data["tools"]), 1)
        self.assertEqual(data["tools"][0]["name"], "get_user")
        self.assertTrue(len(data["execution_plan"]) > 0)

    def test_foreign_prompt_in_node_backend(self):
        src = """
        import >llm<

        fn get_prompt() -> string ={
          >llm(
            System prompt from function.
          )
        }

        print(get_prompt());
        """
        node_out = compile_source(src, backend="node", safe=True)
        self.assertIn("return `System prompt from function.`;", node_out)

    def test_foreign_prompt_in_go_backend(self):
        src = """
        import >llm<

        fn get_prompt() -> string ={
          >llm(
            System prompt from function.
          )
        }

        print(get_prompt());
        """
        go_out = compile_source(src, backend="go", safe=True)
        self.assertIn("Prompt / LLM foreign block: llm", go_out)
        self.assertIn("System prompt from function.", go_out)

    def test_cli_invocation_llm_backend(self):
        with tempfile.TemporaryDirectory() as td:
            cryo_file = os.path.join(td, "test_input.cryo")
            out_file = os.path.join(td, "test_input.llm")
            with open(cryo_file, "w", encoding="utf-8") as f:
                f.write("""
                import >llm<

                schema Product {
                  string sku;
                  number price;
                }

                Product p = llm("gpt-4o", "Parse product") as Product;
                print(p.sku);
                """)

            cmd = [sys.executable, CRYOC, cryo_file, "--backend", "llm", "-o", out_file, "--no-banner"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(res.returncode, 0, f"CLI compilation failed: {res.stderr}")
            self.assertTrue(os.path.exists(out_file), "Output .llm file was not created")

            with open(out_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("Autonomous AI Agent Execution Protocol & Manifest", content)
            self.assertIn("Schema `Product`", content)
            self.assertIn("STEP [LLM_QUERY] -> Variable `p` (Product)", content)


if __name__ == "__main__":
    unittest.main()
