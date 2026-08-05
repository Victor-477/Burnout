#!/usr/bin/env python3
# ============================================================
#  test_llm_repair.py — validated structured output (roadmap 11.18)
#
#  `llm(...) as T` used to json.Unmarshal and throw the error
#  away, so every bad reply became a zero-valued T:
#
#      missing a field    -> name=Ada age=0
#      wrong type         -> name=Ada age=0
#      not JSON at all    -> name=    age=0
#      wrapped in a fence -> name=    age=0
#
#  age=0 is indistinguishable from a real zero, so the program
#  could not tell a good answer from a discarded one.
#
#  Now the reply is validated against the schema the compiler
#  already emits, and a failure re-asks with the problem stated.
#  The test that matters most is RECOVERY: a provider that answers
#  badly and then correctly must end up with the correct value —
#  otherwise this is just a more talkative way to fail.
# ============================================================
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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

# The stub replies with replies[i] for the i-th request, repeating the last.
replies = [['{"name": "Ada", "age": 36}']]
asks = []


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
            asks.append(json.loads(self.rfile.read(n)))
        except Exception:
            asks.append({})
        seq = replies[0]
        body = seq[min(len(asks) - 1, len(seq) - 1)].encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def handle_error(self, *a):
        pass


PERSON = 'struct Person { string name; int age; }\n'


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


def run(exe, url):
    env = dict(os.environ)
    env['CRYO_LLM_URL'] = url
    p = subprocess.run([exe], capture_output=True, text=True, env=env,
                       timeout=180, errors='replace')
    return p.stdout.strip(), p.stderr.strip()


ASK = (PERSON + 'Person p = llm("m", "who?"%s) as Person;\n'
                'print("name=[${p.name}] age=[${p.age}]");\n')


def test_recovery(work, url):
    """The point of the feature: a bad reply followed by a good one works."""
    print("\n── a repair actually recovers the answer ──")
    rc, log, exe = build(ASK % '', work, 'recover')
    check("the program compiles", rc == 0, log[-250:])
    if rc != 0:
        return

    for label, seq, want_reqs in (
            ('a missing field, then correct',
             ['{"name": "Ada"}', '{"name": "Ada", "age": 36}'], 2),
            ('a wrong type, then correct',
             ['{"name": "Ada", "age": "thirty-six"}',
              '{"name": "Ada", "age": 36}'], 2),
            ('prose, then correct',
             ['Sure! Ada is 36.', '{"name": "Ada", "age": 36}'], 2),
            ('two bad, then correct',
             ['{}', 'nope', '{"name": "Ada", "age": 36}'], 3)):
        replies[0] = seq
        asks.clear()
        out, err = run(exe, url)
        check(f"{label}: the right value comes back",
              out == 'name=[Ada] age=[36]', f"{out!r} / {err[:120]}")
        check(f"{label}: it took {want_reqs} request(s)",
              len(asks) == want_reqs, f"{len(asks)} requests")


def test_repair_prompt(work, url):
    print("\n── the re-ask says what was wrong ──")
    rc, log, exe = build(ASK % '', work, 'prompt')
    if rc != 0:
        check("build", False, log[-250:])
        return
    replies[0] = ['{"name": "Ada"}', '{"name": "Ada", "age": 36}']
    asks.clear()
    run(exe, url)
    check("a second request was made", len(asks) >= 2, str(len(asks)))
    if len(asks) < 2:
        return
    second = asks[1].get('prompt', '')
    check("the re-ask names the problem", 'age is missing' in second,
          second[:220])
    check("the re-ask still carries the original question",
          'who?' in second, second[:220])
    check("the re-ask restates the schema", '"properties"' in second,
          second[:220])
    check("the re-ask asks for JSON only",
          'JSON only' in second, second[:220])
    check("the schema still travels in its own field too",
          'schema' in asks[1], str(list(asks[1])))


def test_bounded(work, url):
    print("\n── the loop is bounded, and the budget is settable ──")
    bad = ['{"name": "Ada"}']

    rc, log, exe = build(ASK % '', work, 'default')
    if rc == 0:
        replies[0] = bad
        asks.clear()
        out, err = run(exe, url)
        check("by default it gives up after 3 attempts (1 + 2 repairs)",
              len(asks) == 3, f"{len(asks)} requests")
        check("and says so, naming the problem",
              'still invalid after 3' in err and 'age is missing' in err,
              err[:200])
    else:
        check("build (default)", False, log[-250:])

    rc, log, exe = build(ASK % ', { "repair": 0 }', work, 'norepair')
    if rc == 0:
        replies[0] = bad
        asks.clear()
        run(exe, url)
        check('"repair": 0 asks exactly once', len(asks) == 1,
              f"{len(asks)} requests")
    else:
        check('build ("repair": 0)', False, log[-250:])

    rc, log, exe = build(ASK % ', { "repair": 4 }', work, 'more')
    if rc == 0:
        replies[0] = bad
        asks.clear()
        run(exe, url)
        check('"repair": 4 asks five times', len(asks) == 5,
              f"{len(asks)} requests")
        check("repair is never sent to the provider",
              all('repair' not in a for a in asks), str(asks[0]))
    else:
        check('build ("repair": 4)', False, log[-250:])


def test_clean(work, url):
    """Fenced or prose-wrapped JSON is the commonest 'bad' reply and is not
    really bad at all — recovering it costs nothing and avoids a round trip."""
    print("\n── fenced and prose-wrapped replies need no repair ──")
    rc, log, exe = build(ASK % '', work, 'clean')
    if rc != 0:
        check("build", False, log[-250:])
        return
    for label, body in (
            ('a ```json fence', '```json\n{"name": "Ada", "age": 36}\n```'),
            ('a bare ``` fence', '```\n{"name": "Ada", "age": 36}\n```'),
            ('prose around it', 'Here you go:\n{"name": "Ada", "age": 36}\nHope that helps!')):
        replies[0] = [body]
        asks.clear()
        out, _ = run(exe, url)
        check(f"{label} is read on the first try",
              out == 'name=[Ada] age=[36]' and len(asks) == 1,
              f"{out!r} in {len(asks)} request(s)")


def test_valid_unchanged(work, url):
    print("\n── a good reply is untouched ──")
    rc, log, exe = build(ASK % '', work, 'good')
    if rc != 0:
        check("build", False, log[-250:])
        return
    replies[0] = ['{"name": "Ada", "age": 36}']
    asks.clear()
    out, err = run(exe, url)
    check("one request, right answer, nothing on stderr",
          out == 'name=[Ada] age=[36]' and len(asks) == 1 and err == '',
          f"{out!r} / {len(asks)} req / {err[:120]}")


def test_nested(work, url):
    print("\n── nested objects and arrays are validated too ──")
    src = ('struct Item { string sku; int qty; }\n'
           'struct Order { string id; Item[] items; }\n'
           'Order o = llm("m", "order?") as Order;\n'
           'print("${o.id}/${len(o.items)}");\n')
    rc, log, exe = build(src, work, 'nested')
    check("a nested schema compiles", rc == 0, log[-250:])
    if rc != 0:
        return
    replies[0] = [
        '{"id": "A1", "items": [{"sku": "x", "qty": "two"}]}',   # bad element
        '{"id": "A1", "items": [{"sku": "x", "qty": 2}]}',
    ]
    asks.clear()
    out, err = run(exe, url)
    check("a bad array element is caught and repaired",
          out == 'A1/1' and len(asks) == 2, f"{out!r} in {len(asks)} req")
    if len(asks) >= 2:
        check("the problem points at the element that was wrong",
              'items[0].qty' in asks[1].get('prompt', ''),
              asks[1].get('prompt', '')[:200])


def main():
    work = tempfile.mkdtemp(prefix='cryo_repair_')
    srv = HTTPServer(('127.0.0.1', 0), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_recovery(work, url)
        test_repair_prompt(work, url)
        test_bounded(work, url)
        test_clean(work, url)
        test_valid_unchanged(work, url)
        test_nested(work, url)
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
