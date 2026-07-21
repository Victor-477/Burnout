#!/usr/bin/env python3
# ============================================================
#  LLM gateway for Cryo — translates the Cryo agent contract
#  to the Chat Completions API (OpenAI-compatible) and back.
#
#  Serve: OpenAI, Azure OpenAI, Anthropic (endpoint /v1 compat),
#  Groq, Mistral, e LLMs locais (Ollama, LM Studio, vLLM...).
#
#  Usage:
#    set OPENAI_API_KEY=sk-...           (the provider key)
#    set OPENAI_BASE_URL=https://api.openai.com/v1   (optional)
#    python burnout/scripts/llm_gateway.py 8801
#
#  Then run the Cryo agent pointing at the gateway:
#    set CRYO_LLM_URL=http://127.0.0.1:8801
#    python burnout/cryoc.py cryo/examples/example_agent.cryo --backend go --run
#
#  Cryo contract (what cryoAgent sends/expects):
#    POST {model, messages, tools}
#      messages: [{role:user,content}, {role:assistant,tool_call:{name,arguments}},
#                 {role:tool,name,content}]
#      tools:    [{name, parameters:<JSON Schema as string>}]
#    <- {"tool_call": {"name","arguments"}}   (the model requests a tool)
#    <- {"content": "final response"}
# ============================================================
import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_KEY  = os.environ.get("OPENAI_API_KEY", "")


def to_openai(req: dict) -> dict:
    """Contrato Cryo -> corpo da API OpenAI Chat Completions."""
    msgs = []
    call_id = 0
    last_id = None
    for m in req.get("messages", []):
        role = m.get("role")
        if role == "assistant" and m.get("tool_call"):
            call_id += 1
            last_id = f"call_{call_id}"
            tc = m["tool_call"]
            args = tc.get("arguments", {})
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": last_id, "type": "function",
                    "function": {"name": tc.get("name"),
                                 "arguments": json.dumps(args)},
                }],
            })
        elif role == "tool":
            msgs.append({"role": "tool", "tool_call_id": last_id or "call_1",
                         "name": m.get("name"), "content": str(m.get("content", ""))})
        else:
            msgs.append({"role": role or "user", "content": m.get("content", "")})

    tools = []
    for t in req.get("tools", []):
        try:
            params = json.loads(t.get("parameters", "{}"))
        except Exception:
            params = {"type": "object", "properties": {}}
        tools.append({"type": "function",
                      "function": {"name": t.get("name"), "parameters": params}})

    body = {"model": req.get("model", "gpt-4o-mini"), "messages": msgs}
    if tools:
        body["tools"] = tools
    return body


def from_openai(resp: dict) -> dict:
    """OpenAI response -> Cryo contract."""
    try:
        msg = resp["choices"][0]["message"]
    except Exception:
        return {"content": ""}
    calls = msg.get("tool_calls")
    if calls:
        fn = calls[0]["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        return {"tool_call": {"name": fn.get("name"), "arguments": args}}
    return {"content": msg.get("content") or ""}


def call_llm(body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + "/chat/completions", data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        cryo_req = json.loads(self.rfile.read(n) or b"{}")
        try:
            openai_resp = call_llm(to_openai(cryo_req))
            out = from_openai(openai_resp)
        except Exception as e:
            sys.stderr.write(f"[gateway] error: {e}\n")
            out = {"content": ""}
        body = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8801
    if not API_KEY:
        sys.stderr.write("[gateway] WARNING: OPENAI_API_KEY not defined.\n")
    sys.stderr.write(f"[gateway] escutando em http://127.0.0.1:{port}  ->  {BASE_URL}\n")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
