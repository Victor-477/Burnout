#!/usr/bin/env python3
# ============================================================
#  test_llm_resilience.py — retries, backoff, typed failure (11.19)
#
#      match llm_try("model", "prompt", { "retries": 3 }) {
#          LlmOk(text)          => print(text);
#          LlmFailed(k, why) if k == "rate_limited" => backOff();
#          LlmFailed(k, why)    => print("failed: ${k}");
#      }
#
#  Two things are being defended.
#
#  RETRY THE RIGHT THINGS. The old loop tried three times with no
#  pause and no discrimination — including 400 and 401, which
#  cannot improve by being asked again, so a wrong API key cost
#  three round trips before failing. Retrying is now limited to
#  transport errors, 408, 429 and 5xx, with exponential backoff.
#
#  FAILURE IS A VALUE. Every failure used to return "", which is
#  also what an empty completion returns; the program could not
#  tell the difference and could not react. llm_try yields a
#  match-able outcome instead, and never aborts.
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

status = [200]
body = [b'the answer']
hdrs = [{}]
delay = [0.0]
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
        if delay[0]:
            time.sleep(delay[0])
        try:
            self.send_response(status[0])
            for k, v in hdrs[0].items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(body[0])))
            self.end_headers()
            self.wfile.write(body[0])
        except Exception:
            pass

    def log_message(self, *a):
        pass

    def handle_error(self, *a):
        pass


TRY = ('match llm_try("m", "hello"%s) {\n'
       '    LlmOk(text) => print("ok:${text}");\n'
       '    LlmFailed(kind, why) if kind == "rate_limited" => print("rl:${why}");\n'
       '    LlmFailed(kind, why) => print("fail:${kind}");\n'
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


def run(exe, url, timeout=180):
    env = dict(os.environ)
    if url:
        env['CRYO_LLM_URL'] = url
    else:
        env.pop('CRYO_LLM_URL', None)
    t0 = time.time()
    p = subprocess.run([exe], capture_output=True, text=True, env=env,
                       timeout=timeout, errors='replace')
    return p.stdout.strip().split('\n'), time.time() - t0


def scenario(exe, url, st, bd=b'x', hd=None, retries_hdr=None):
    status[0], body[0], hdrs[0] = st, bd, (hd or {})
    asks.clear()
    out, el = run(exe, url)
    return out, len(asks), el


def test_classification(work, url):
    print("\n── only what can succeed is retried ──")
    rc, log, exe = build(TRY % ', { "retries": 2 }', work, 'cls')
    check("llm_try compiles and matches on the outcome", rc == 0, log[-300:])
    if rc != 0:
        return None

    out, n, _ = scenario(exe, url, 200, b'the answer')
    check("200: one attempt, LlmOk", out[0] == 'ok:the answer' and n == 1,
          f"{out} in {n}")

    for st, label in ((500, 'server_error'), (503, 'server_error'),
                      (408, 'timeout')):
        out, n, _ = scenario(exe, url, st, b'boom')
        check(f"{st}: retried to the budget (3 attempts)", n == 3, f"{n}")
        if st != 408:
            check(f"{st}: reported as {label}", out[0] == f'fail:{label}',
                  str(out))

    out, n, _ = scenario(exe, url, 429, b'slow')
    check("429: retried, and reported as rate_limited",
          n == 3 and out[0].startswith('rl:'), f"{out} in {n}")

    # The point of classification: these cannot improve, so they must not be
    # retried. Each pointless retry costs the caller a real round trip.
    for st in (400, 401, 403, 404):
        out, n, _ = scenario(exe, url, st, b'nope')
        check(f"{st}: NOT retried — one attempt only", n == 1, f"{n} attempts")
        check(f"{st}: reported as refused", out[0] == 'fail:refused', str(out))

    check("the program continues after a failure",
          out[-1] == 'continued', str(out))
    return exe


def test_backoff(work, url):
    print("\n── the pause between attempts grows ──")
    rc, log, exe = build(TRY % ', { "retries": 3 }', work, 'backoff')
    if rc != 0:
        check("build", False, log[-300:])
        return
    out, n, el = scenario(exe, url, 500, b'boom')
    check("four attempts were made", n == 4, str(n))
    # 200 + 400 + 800 = 1.4s of waiting; without backoff this is ~0.
    check("it waited between them, increasingly", el > 1.2,
          f"{el:.2f}s for {n} attempts")
    check("but not absurdly long", el < 6.0, f"{el:.2f}s")

    # Retry-After on a 429 replaces the computed pause.
    status[0], body[0], hdrs[0] = 429, b'slow', {'Retry-After': '1'}
    asks.clear()
    _, el2 = run(exe, url)
    check("a 429 Retry-After header is honoured", el2 > 1.0,
          f"{el2:.2f}s over {len(asks)} attempts")
    hdrs[0] = {}


def test_budget(work, url):
    print("\n── the retry budget is settable ──")
    for flags, want in ((', { "retries": 0 }', 1),
                        (', { "retries": 1 }', 2),
                        ('', 3)):                     # default is 2 retries
        rc, log, exe = build(TRY % flags, work, 'b%d' % want)
        if rc != 0:
            check(f"build {flags or '(default)'}", False, log[-250:])
            continue
        out, n, _ = scenario(exe, url, 500, b'boom')
        check(f"{flags or '(default)'} -> {want} attempt(s)", n == want,
              f"{n}")
    check("retries is never sent to the provider",
          all('retries' not in a for a in asks), str(asks[:1]))


def test_transport(work, url):
    print("\n── transport failures, and no endpoint at all ──")
    rc, log, exe = build(TRY % ', { "retries": 0 }', work, 'tr')
    if rc != 0:
        check("build", False, log[-250:])
        return

    # Nothing listening: connection refused is a transport failure.
    out, _ = run(exe, 'http://127.0.0.1:9/v1')
    check("a dead endpoint is reported as transport, not as an empty answer",
          out[0] == 'fail:transport', str(out))
    check("and the program still continues", out[-1] == 'continued', str(out))

    out, _ = run(exe, None)
    check("no CRYO_LLM_URL is its own kind", out[0] == 'fail:no_endpoint',
          str(out))

    # A per-call timeout must surface as `timeout`, not as a generic failure.
    rc, log, exe = build(TRY % ', { "retries": 0, "timeout": 200 }', work, 'to')
    rc2, log2, exe2 = build(TRY % ', { "retries": 0 }', work, 'noto')
    if rc == 0 and rc2 == 0:
        delay[0] = 1.5
        status[0], body[0] = 200, b'too late'
        asks.clear()
        out, bounded = run(exe, url)
        _, unbounded = run(exe2, url)
        delay[0] = 0.0
        check("a timed-out call is reported as timeout",
              out[0] == 'fail:timeout', str(out))
        # Absolute times are useless here — process start-up dominates and
        # varies. What must hold is that the bounded call gave up well before
        # the stalling provider answered.
        check("the bounded call returns clearly sooner than the unbounded one",
              unbounded - bounded > 0.8,
              f"bounded {bounded:.2f}s vs unbounded {unbounded:.2f}s")
    else:
        check("build (timeout)", False, (log + log2)[-250:])


def test_plain_llm_unaffected(work, url):
    print("\n── plain llm() is unchanged ──")
    src = 'string r = llm("m", "hello");\nprint("[" + r + "]");\n'
    rc, log, exe = build(src, work, 'plain')
    check("a plain llm() call still compiles", rc == 0, log[-250:])
    if rc != 0:
        return
    status[0], body[0], hdrs[0] = 200, b'hi there', {}
    asks.clear()
    out, _ = run(exe, url)
    check("it returns the completion", out[0] == '[hi there]', str(out))
    status[0], body[0] = 500, b'boom'
    asks.clear()
    out, _ = run(exe, url)
    check("and still yields empty on failure (llm_try is the typed door)",
          out[0] == '[]', str(out))
    check("retrying a 5xx still happens for it", len(asks) == 3, str(len(asks)))


def test_other_backends(work):
    print("\n── the other backends refuse clearly ──")
    for backend, needle in (('node', 'not supported in the node backend'),
                            ('pyro', 'unknown in pyro backend')):
        cf = os.path.join(work, 'x.cryo')
        with open(cf, 'w', encoding='utf-8') as f:
            f.write(TRY % '')
        p = subprocess.run([sys.executable, CRYOC, cf, '--backend', backend,
                            '-o', os.path.join(work, 'x.out'), '--no-banner'],
                           capture_output=True, text=True, timeout=180,
                           errors='replace')
        check(f"{backend} says so", p.returncode != 0 and needle in
              (p.stdout + p.stderr), (p.stdout + p.stderr)[-200:])


def main():
    work = tempfile.mkdtemp(prefix='cryo_res_')
    srv = HTTPServer(('127.0.0.1', 0), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        test_classification(work, url)
        test_backoff(work, url)
        test_budget(work, url)
        test_transport(work, url)
        test_plain_llm_unaffected(work, url)
        test_other_backends(work)
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
