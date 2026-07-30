#!/usr/bin/env python3
# ============================================================
#  test_llm_options.py — LLM generation controls (roadmap 11.16)
#
#      llm(model, prompt, { "temperature": 0.2, "seed": 7 })
#
#  The point of the feature is that the options reach the
#  provider, so the tests assert on the JSON a real HTTP server
#  actually received — not on the generated Go source. Checking
#  the emitted code would pass just as well if the payload were
#  assembled wrongly, which is the failure worth catching.
#
#  A stub provider runs on localhost, CRYO_LLM_URL points at it,
#  and each compiled program makes a real request.
#
#  The LLM layer is go-only (node lists `llm` as unsupported and
#  routes to --backend go; pyro has no such native), so go is the
#  whole surface here.
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

received = []          # payloads the stub provider saw
delay_s = [0.0]        # how long the stub should stall before replying
reply = ['{"content": "ok"}']


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
        body = self.rfile.read(n)
        try:
            received.append(json.loads(body))
        except Exception:
            received.append({'<unparsable>': body.decode('utf-8', 'replace')})
        if delay_s[0]:
            time.sleep(delay_s[0])
        out = reply[0].encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass

    def handle_error(self, *a):
        # The timeout test hangs up mid-reply on purpose; the resulting
        # ConnectionAborted traceback is expected, and printing it would look
        # like a failure in an otherwise passing run.
        pass


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


def run(exe, url, timeout=60):
    env = dict(os.environ)
    env['CRYO_LLM_URL'] = url
    p = subprocess.run([exe], capture_output=True, text=True, env=env,
                       timeout=timeout, errors='replace')
    return (p.stdout + p.stderr).strip()


def compile_only(src, work, name):
    rc, log, _ = build(src, work, name)
    return rc, log


def test_payload(work, url):
    print("\n── the options reach the provider ──")

    src = ('string r = llm("m", "hi", { "temperature": 0.25, "max_tokens": 400,\n'
           '                            "seed": 7, "top_p": 0.9 });\n'
           'print(r);\n')
    rc, log, exe = build(src, work, 'opts')
    check("a program with options compiles on go", rc == 0, log[-250:])
    if rc != 0:
        return
    received.clear()
    run(exe, url)
    check("the provider received exactly one request", len(received) == 1,
          str(received))
    if not received:
        return
    got = received[0]
    check("temperature is sent, as a number", got.get('temperature') == 0.25,
          str(got))
    check("max_tokens is sent, as an integer", got.get('max_tokens') == 400,
          str(got))
    check("seed is sent", got.get('seed') == 7, str(got))
    check("top_p is sent", got.get('top_p') == 0.9, str(got))
    check("model and prompt still ride in the same payload",
          got.get('model') == 'm' and got.get('prompt') == 'hi', str(got))
    check("timeout is NOT sent to the provider — it is ours",
          'timeout' not in got, str(got))

    # `stop` as a single string and as an array: both must survive as JSON.
    for label, literal, expect in (
            ('a string', '"END"', 'END'),
            ('an array', '["A", "B"]', ['A', 'B'])):
        src = 'string r = llm("m", "hi", { "stop": %s });\nprint(r);\n' % literal
        rc, log, exe = build(src, work, 'stop')
        if rc != 0:
            check(f"stop as {label} compiles", False, log[-250:])
            continue
        received.clear()
        run(exe, url)
        check(f"stop as {label} arrives unchanged",
              received and received[0].get('stop') == expect, str(received))

    # No options at all must keep the old payload exactly.
    src = 'string r = llm("m", "hi");\nprint(r);\n'
    rc, log, exe = build(src, work, 'noopts')
    check("a call with no options still compiles", rc == 0, log[-250:])
    if rc == 0:
        received.clear()
        run(exe, url)
        got = received[0] if received else {}
        check("no options means no extra payload keys",
              set(got) == {'model', 'prompt'}, str(got))

    # Values need not be literals — a seed held in a variable is the whole
    # point of seeding, so an expression has to work.
    src = ('int s = 3 + 4;\n'
           'string r = llm("m", "hi", { "seed": s });\nprint(r);\n')
    rc, log, exe = build(src, work, 'expr')
    check("an expression is allowed as an option value", rc == 0, log[-250:])
    if rc == 0:
        received.clear()
        run(exe, url)
        check("the evaluated expression is what gets sent",
              received and received[0].get('seed') == 7, str(received))


def test_structured(work, url):
    """`llm(...) as T` is a separate code path in codegen_go, so options have
    to be threaded there too — easy to change one call site and miss this."""
    print("\n── options work with structured output (`as T`) ──")
    src = ('struct Person { string name; int age; }\n'
           'Person p = llm("m", "who?", { "temperature": 0.0, "seed": 42 }) as Person;\n'
           'print("${p.name} ${p.age}");\n')
    rc, log, exe = build(src, work, 'structured')
    check("`llm(...) as T` with options compiles", rc == 0, log[-250:])
    if rc != 0:
        return
    received.clear()
    reply[0] = '{"name": "Ada", "age": 36}'
    out = run(exe, url)
    reply[0] = '{"content": "ok"}'
    got = received[0] if received else {}
    check("the options are sent on the structured path too",
          got.get('temperature') == 0.0 and got.get('seed') == 42, str(got))
    check("the schema still rides alongside them", 'schema' in got, str(got))
    check("the reply is still parsed into the struct", out == 'Ada 36',
          f"output {out!r}")


def test_timeout(work, url):
    print("\n── timeout bounds the request ──")
    # retries: 0 keeps this about the timeout alone. 11.19 added retries with
    # backoff, and a wall-clock bound over three attempts measures those plus
    # process start-up plus the stub's own serialised sleeps — none of which
    # is what this test is for.
    src = ('string r = llm("m", "hi", { "timeout": 300, "retries": 0 });\n'
           'print("[" + r + "]");\n')
    rc, log, exe = build(src, work, 'to')
    slow = ('string r = llm("m", "hi", { "retries": 0 });\n'
            'print("[" + r + "]");\n')
    rc2, log2, exe2 = build(slow, work, 'noto')
    check("a program with a timeout compiles", rc == 0, log[-250:])
    if rc != 0 or rc2 != 0:
        return
    received.clear()
    delay_s[0] = 1.5                     # provider stalls past the 300ms budget
    started = time.time()
    out = run(exe, url, timeout=120)
    bounded = time.time() - started
    started = time.time()
    run(exe2, url, timeout=120)
    unbounded = time.time() - started
    delay_s[0] = 0.0
    check("the call gives up rather than waiting for the stalled provider",
          unbounded - bounded > 0.8,
          f"bounded {bounded:.1f}s vs unbounded {unbounded:.1f}s")
    check("a timed-out call yields the empty string, not a crash",
          out == '[]', f"output {out!r}")


def test_validation(work):
    print("\n── mistakes are caught at compile time ──")
    bad = [
        ('an unknown option name',
         '{ "temprature": 0.2 }', "unknown llm() option 'temprature'"),
        ('a bare (unquoted) option name',
         '{ temperature: 0.2 }', 'must be quoted'),
        ('the wrong literal kind',
         '{ "temperature": "hot" }', "'temperature' takes a number"),
        ('a float where an int is required',
         '{ "max_tokens": 1.5 }', "'max_tokens' takes an int"),
        ('the same option twice',
         '{ "seed": 1, "seed": 2 }', "'seed' is set twice"),
        ('a number for stop',
         '{ "stop": 3 }', "'stop' takes a string"),
        ('options that are not a map',
         '"nope"', 'must be written as a map literal'),
    ]
    for label, opts, needle in bad:
        src = 'string r = llm("m", "hi", %s);\nprint(r);\n' % opts
        rc, log = compile_only(src, work, 'bad')
        check(f"{label} is refused", rc != 0 and needle in log,
              f"rc={rc} log={log[-220:]}")

    # And the valid set must all be accepted.
    src = ('string r = llm("m", "hi", { "temperature": 0.2, "top_p": 0.9,\n'
           '   "max_tokens": 10, "stop": "X", "seed": 1, "timeout": 500 });\n'
           'print(r);\n')
    rc, log = compile_only(src, work, 'allok')
    check("every documented option is accepted together", rc == 0, log[-250:])


def main():
    work = tempfile.mkdtemp(prefix='cryo_llm_')
    srv = HTTPServer(('127.0.0.1', 0), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        test_payload(work, url)
        test_structured(work, url)
        test_timeout(work, url)
        test_validation(work)
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
