#!/usr/bin/env python3
# ============================================================
#  test_agent_loop.py — agent loop upgrades (roadmap 11.20)
#
#  Four things, and a fifth that turned up on the way.
#
#  1. Several tools asked for in one step run TOGETHER. They are
#     independent by construction — the model asked without seeing
#     any of their results — so serialising them only adds latency.
#     Proven by timing, not by reading the generated code.
#  2. A failing tool is a RESULT, not the end of the run: an
#     unknown name, unreadable arguments, or a tool that aborts all
#     come back as {"error": …} and the loop carries on. A division
#     by zero inside a tool used to kill the whole program.
#  3. Exhausting the step budget has a defined outcome. It used to
#     `return ""`, which is also what an empty answer looks like.
#  4. The conversation is kept under a byte budget by dropping the
#     oldest tool exchanges, never the task itself.
#  5. Found here: tool arguments sent as a JSON-ENCODED STRING (the
#     OpenAI convention) never parsed, and the error was discarded
#     — so every such tool ran on zero arguments and answered
#     confidently about nothing.
# ============================================================
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')

_passed = _failed = 0

script = [[]]
turns = []


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        try:
            turns.append(json.loads(self.rfile.read(n)))
        except Exception:
            turns.append({})
        i = min(len(turns) - 1, len(script[0]) - 1)
        b = json.dumps(script[0][i]).encode()
        try:
            self.send_response(200)
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except Exception:
            pass

    def log_message(self, *a):
        pass

    def handle_error(self, *a):
        pass


TOOLS = ('tool fn add(int a, int b) -> int ={ return a + b; }\n'
         'tool fn half(int n) -> int ={ return 100 / n; }\n'
         'tool fn slow(int ms) -> int ={ sleep(ms); return ms; }\n')

PROG = (TOOLS +
        'match agent_try("m", "go"%s) {\n'
        '    LlmOk(text) => print("ok:${text}");\n'
        '    LlmFailed(kind, why) => print("fail:${kind}:${why}");\n'
        '}\n'
        'print("continued");\n')


def build(src, work, name):
    cf = os.path.join(work, name + '.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    exe = os.path.join(work, name + ('.exe' if sys.platform == 'win32' else ''))
    p = subprocess.run([sys.executable, CRYOC, cf, '--backend', 'go',
                        '-o', os.path.join(work, name + '.go'), '--no-banner'],
                       capture_output=True, text=True, timeout=300,
                       errors='replace')
    return p.returncode, (p.stdout + p.stderr), exe


def run(exe, url, replies, timeout=180):
    script[0] = replies
    turns.clear()
    env = dict(os.environ)
    env['CRYO_LLM_URL'] = url
    t0 = time.time()
    p = subprocess.run([exe], capture_output=True, text=True, env=env,
                       timeout=timeout, errors='replace')
    return (p.stdout.strip().split('\n'), p.stderr.strip(), time.time() - t0)


def tool_results():
    out = []
    for t in turns:
        for m in t.get('messages', []):
            if m.get('role') == 'tool' and m.get('content') not in out:
                out.append(m.get('content'))
    return out


def call(name, args_obj, as_string=True):
    """A tool request. Providers differ: `arguments` is a JSON-encoded string
    for some and an object for others — both have to work."""
    args = json.dumps(args_obj) if as_string else args_obj
    return {"tool_call": {"name": name, "arguments": args}}


def test_arguments(work, url):
    print("\n── tool arguments, in both shapes providers send ──")
    rc, log, exe = build(PROG % '', work, 'args')
    check("an agent program compiles", rc == 0, log[-300:])
    if rc != 0:
        return None
    out, _, _ = run(exe, url, [call('add', {'a': 2, 'b': 3}),
                               {"content": "the sum is 5"}])
    check("arguments as a JSON-encoded string are read (they never were)",
          tool_results() == ['5'], str(tool_results()))
    check("and the run finishes normally", out[0] == 'ok:the sum is 5', str(out))

    run(exe, url, [call('add', {'a': 4, 'b': 5}, as_string=False),
                   {"content": "done"}])
    check("arguments as a plain object still work",
          tool_results() == ['9'], str(tool_results()))
    return exe


def test_parallel(work, url):
    print("\n── several tools in one step run together ──")
    rc, log, exe = build(PROG % ', { "steps": 3 }', work, 'par')
    if rc != 0:
        check("build", False, log[-300:])
        return
    two = {"tool_calls": [{"name": "slow", "arguments": '{"ms":500}'},
                          {"name": "slow", "arguments": '{"ms":500}'}]}
    one = call('slow', {'ms': 500})
    # A freshly built binary pays a one-off start-up cost on its first run
    # (virus scanning on Windows), which swamps the difference being measured.
    run(exe, url, [one, {"content": "warm"}])

    _, _, both = run(exe, url, [two, {"content": "done"}])
    check("both results come back",
          sorted(tool_results()) == ['500'], str(tool_results()))

    _, _, single = run(exe, url, [one, {"content": "done"}])
    # Two 500ms tools together should cost about what one costs. Run serially
    # they would cost ~500ms more, which is the whole point.
    check("two 500ms tools cost about as much as one, not twice",
          both - single < 0.35,
          f"two together {both:.2f}s vs one {single:.2f}s")

    check("the results are paired with the right calls, in order",
          all(m.get('name') == 'slow'
              for t in turns for m in t.get('messages', [])
              if m.get('role') == 'tool'),
          str(turns[-1].get('messages')))


def test_tool_errors(work, url):
    print("\n── a failing tool is a result, not the end of the run ──")
    rc, log, exe = build(PROG % '', work, 'err')
    if rc != 0:
        check("build", False, log[-300:])
        return

    out, _, _ = run(exe, url, [call('nope', {}), {"content": "recovered"}])
    check("an unknown tool comes back as an error the model can read",
          tool_results() and 'unknown tool: nope' in tool_results()[0],
          str(tool_results()))
    check("and the loop continues to an answer", out[0] == 'ok:recovered',
          str(out))

    out, _, _ = run(exe, url, [call('add', {'a': 'twelve', 'b': 3}),
                               {"content": "recovered"}])
    check("unreadable arguments come back as an error, not as zeros",
          tool_results() and 'could not read the arguments' in tool_results()[0],
          str(tool_results()))
    check("the loop continues", out[0] == 'ok:recovered', str(out))

    # 100/0 aborts in safe mode. Without recovery it takes the program with it.
    out, _, _ = run(exe, url, [call('half', {'n': 0}), {"content": "recovered"}])
    check("a tool that aborts is caught and reported",
          tool_results() and 'DivByZero' in tool_results()[0],
          str(tool_results()))
    check("the run survives a tool that would have killed the program",
          out[0] == 'ok:recovered' and out[-1] == 'continued', str(out))


def test_budget(work, url):
    print("\n── the step budget has a defined outcome ──")
    rc, log, exe = build(PROG % ', { "steps": 3 }', work, 'budget')
    if rc != 0:
        check("build", False, log[-300:])
        return
    out, _, _ = run(exe, url, [call('add', {'a': 1, 'b': 1})])   # never answers
    check("exactly the budgeted number of turns", len(turns) == 3, str(len(turns)))
    check("the outcome names the budget, not an empty answer",
          out[0].startswith('fail:step_budget'), str(out))
    check("it says how many steps were used", '3 steps' in out[0], str(out))
    check("the program continues afterwards", out[-1] == 'continued', str(out))

    # A provider failure inside the loop is surfaced too, not swallowed.
    rc, log, exe2 = build(PROG % '', work, 'nourl')
    if rc == 0:
        env = dict(os.environ)
        env.pop('CRYO_LLM_URL', None)
        p = subprocess.run([exe2], capture_output=True, text=True, env=env,
                           timeout=120, errors='replace')
        line = p.stdout.strip().split('\n')[0]
        check("a transport failure inside the loop surfaces as itself",
              line.startswith('fail:no_endpoint'), line)


def test_context(work, url):
    print("\n── the conversation is kept under a budget ──")
    rc, log, exe = build(PROG % ', { "steps": 6, "max_context": 400 }',
                         work, 'ctx')
    check("max_context is accepted", rc == 0, log[-300:])
    if rc != 0:
        return
    out, err, _ = run(exe, url, [call('add', {'a': 1, 'b': 1})])
    check("it reports what it dropped", 'dropped' in err, err[:200])
    check("the task itself is never dropped",
          all(t['messages'][0].get('content') == 'go'
              for t in turns if t.get('messages')),
          str([t['messages'][0] for t in turns if t.get('messages')][:2]))
    sizes = [len(json.dumps(t.get('messages', []))) for t in turns]
    check("the conversation stops growing",
          max(sizes) < 4 * 400, f"sizes {sizes}")


def test_plain_agent(work, url):
    print("\n── plain agent() is unchanged ──")
    src = TOOLS + 'string r = agent("m", "go", ["add"], 4);\nprint("[" + r + "]");\n'
    rc, log, exe = build(src, work, 'plain')
    check("agent() still compiles", rc == 0, log[-300:])
    if rc != 0:
        return
    out, _, _ = run(exe, url, [call('add', {'a': 2, 'b': 2}),
                               {"content": "four"}])
    check("it returns the answer", out[0] == '[four]', str(out))
    out, _, _ = run(exe, url, [call('add', {'a': 1, 'b': 1})])
    check("and still yields empty when the budget runs out",
          out[0] == '[]', str(out))


def main():
    work = tempfile.mkdtemp(prefix='cryo_agent_')
    srv = HTTPServer(('127.0.0.1', 0), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_arguments(work, url)
        test_parallel(work, url)
        test_tool_errors(work, url)
        test_budget(work, url)
        test_context(work, url)
        test_plain_agent(work, url)
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
