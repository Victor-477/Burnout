#!/usr/bin/env python3
# ============================================================
#  test_llm_stream.py — LLM streaming (roadmap 11.17)
#
#      for (string token in llm_stream(model, prompt, opts)) { ... }
#
#  The assertion that actually matters is TIMING. Collecting the
#  right tokens in the right order proves nothing about streaming:
#  an implementation that read the whole body and then handed the
#  tokens over one by one would pass that. So the stub provider
#  emits a chunk every 300ms and the test checks the program
#  printed them as they landed, not in one burst at the end.
#
#  Streaming cannot use 11.5's iter() protocol — that returns a
#  materialised collection, which is exactly the wait streaming
#  exists to avoid — so the loop is driven by llm_next/llm_token
#  and the parser lowers the for-each form onto it.
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

# stub-provider knobs
tokens = [["Silent ", "code ", "compiles"]]
gap_s = [0.0]
mode = ['sse']            # 'sse' | 'plain' | 'nodone'
received = []


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _chunk(self, data: bytes):
        self.wfile.write(hex(len(data))[2:].encode() + b'\r\n' + data + b'\r\n')
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        try:
            received.append(json.loads(self.rfile.read(n)))
        except Exception:
            received.append({})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        try:
            for t in tokens[0]:
                if mode[0] == 'plain':
                    self._chunk((t + '\n').encode())
                else:
                    self._chunk(('data: ' + json.dumps({"content": t})
                                 + '\n\n').encode())
                if gap_s[0]:
                    time.sleep(gap_s[0])
            if mode[0] == 'sse':
                self._chunk(b'data: [DONE]\n\n')
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, *a):
        pass

    def handle_error(self, *a):
        pass          # clients hang up on purpose in the timeout test


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


def run_timed(exe, url, timeout=120):
    """Run and record when each output line appeared."""
    env = dict(os.environ)
    if url:
        env['CRYO_LLM_URL'] = url
    else:
        env.pop('CRYO_LLM_URL', None)
    start = time.time()
    p = subprocess.Popen([exe], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env, bufsize=1)
    stamped = []
    for line in p.stdout:
        stamped.append((time.time() - start, line.rstrip('\r\n')))
    p.wait(timeout=timeout)
    return stamped


LOOP = ('for (string token in llm_stream("m", "p", { "temperature": 0.7 })) {\n'
        '    print(token);\n'
        '}\n'
        'print("--end--");\n')


def test_tokens(work, url):
    print("\n── the tokens arrive, in order ──")
    rc, log, exe = build(LOOP, work, 'loop')
    check("a streaming for-loop compiles on go", rc == 0, log[-250:])
    if rc != 0:
        return None
    received.clear()
    gap_s[0] = 0.0
    out = [t for _, t in run_timed(exe, url)]
    check("every token is delivered, in order",
          out == ["Silent ", "code ", "compiles", "--end--"], str(out))
    check("the loop ends when the stream does — the program continues",
          out and out[-1] == "--end--", str(out))
    got = received[0] if received else {}
    check("the request asks for streaming", got.get('stream') is True, str(got))
    check("generation options ride along (11.16)",
          got.get('temperature') == 0.7, str(got))
    return exe


def test_incremental(work, url, exe):
    """The distinguishing test: tokens must appear as they land."""
    print("\n── the tokens arrive INCREMENTALLY, not in one burst ──")
    if exe is None:
        check("incremental delivery", False, "no binary")
        return
    received.clear()
    gap_s[0] = 0.3                       # provider pauses between chunks
    stamped = run_timed(exe, url)
    gap_s[0] = 0.0
    toks = [(t, s) for t, s in stamped if s != "--end--"]
    check("three tokens were printed", len(toks) == 3, str(stamped))
    if len(toks) != 3:
        return
    spread = toks[-1][0] - toks[0][0]
    # 3 chunks at 300ms apart => ~0.6s between first and last. A buffered
    # implementation prints them together, so the spread collapses to ~0.
    check("first and last token are separated in time (not buffered)",
          spread > 0.35, f"spread {spread:.2f}s over {[f'{t:.2f}' for t, _ in toks]}")
    check("each token appears roughly when the provider sent it",
          all(toks[i + 1][0] - toks[i][0] > 0.15 for i in range(len(toks) - 1)),
          str([f'{t:.2f}' for t, _ in toks]))


def test_raw_form(work, url):
    print("\n── the underlying calls work directly too ──")
    src = ('int h = llm_stream("m", "p");\n'
           'int n = 0;\n'
           'while (llm_next(h)) {\n'
           '    n = n + 1;\n'
           '    print("${n}:${llm_token(h)}");\n'
           '}\n'
           'print("total ${n}");\n')
    rc, log, exe = build(src, work, 'raw')
    check("llm_stream/llm_next/llm_token compile as ordinary calls",
          rc == 0, log[-250:])
    if rc != 0:
        return
    received.clear()
    out = [t for _, t in run_timed(exe, url)]
    check("the manual loop sees the same tokens",
          out == ["1:Silent ", "2:code ", "3:compiles", "total 3"], str(out))


def test_early_exit(work, url):
    """Breaking out mid-stream must not wedge the program.

    The producer runs in a goroutine writing to a buffered channel; without
    llm_close it stays blocked once the buffer fills — harmless in a script,
    a leak per request in a long-running server."""
    print("\n── stopping early ──")
    src = ('int h = llm_stream("m", "p");\n'
           'int n = 0;\n'
           'while (llm_next(h)) {\n'
           '    n = n + 1;\n'
           '    print(llm_token(h));\n'
           '    if (n >= 2) { llm_close(h); break; }\n'
           '}\n'
           'print("stopped after ${n}");\n')
    rc, log, exe = build(src, work, 'early')
    check("llm_close compiles", rc == 0, log[-250:])
    if rc != 0:
        return
    tokens[0] = ["a", "b", "c", "d", "e", "f"]
    started = time.time()
    out = [t for _, t in run_timed(exe, url)]
    elapsed = time.time() - started
    tokens[0] = ["Silent ", "code ", "compiles"]
    check("the loop stops where it was told to",
          out == ["a", "b", "stopped after 2"], str(out))
    check("the program exits promptly after closing", elapsed < 15,
          f"took {elapsed:.1f}s")

    # Closing twice, or closing an unknown handle, must be harmless.
    src = ('int h = llm_stream("m", "p");\n'
           'print(llm_close(h));\n'
           'print(llm_close(h));\n'
           'print(llm_close(999));\n')
    rc, log, exe = build(src, work, 'twice')
    if rc == 0:
        out = [t for _, t in run_timed(exe, url)]
        check("closing twice is safe and reports honestly",
              out == ['true', 'false', 'false'], str(out))
    else:
        check("closing twice is safe and reports honestly", False, log[-250:])


def test_formats(work, url):
    print("\n── provider shapes ──")
    rc, log, exe = build(LOOP, work, 'fmt')
    if rc != 0:
        check("build for format tests", False, log[-250:])
        return

    # A provider that never sends [DONE] must still end when the body does.
    mode[0] = 'nodone'
    out = [t for _, t in run_timed(exe, url)]
    mode[0] = 'sse'
    check("a stream with no [DONE] still terminates at end of body",
          out == ["Silent ", "code ", "compiles", "--end--"], str(out))

    # Not every provider speaks SSE; a plain line-per-token body is read too.
    mode[0] = 'plain'
    out = [t for _, t in run_timed(exe, url)]
    mode[0] = 'sse'
    check("a non-SSE body is read one line per token",
          out == ["Silent ", "code ", "compiles", "--end--"], str(out))

    # An empty completion is a valid answer, not a hang.
    tokens[0] = []
    out = [t for _, t in run_timed(exe, url)]
    tokens[0] = ["Silent ", "code ", "compiles"]
    check("an empty stream runs the body zero times and continues",
          out == ["--end--"], str(out))


def test_no_endpoint(work):
    print("\n── with no provider configured ──")
    rc, log, exe = build(LOOP, work, 'nourl')
    if rc != 0:
        check("build", False, log[-250:])
        return
    out = [t for _, t in run_timed(exe, None)]
    check("CRYO_LLM_URL unset: the loop is skipped and the program finishes",
          out == ["--end--"], str(out))


def test_other_backends(work):
    print("\n── the other backends refuse clearly ──")
    for backend, needle in (('node', 'not supported in the node backend'),
                            ('pyro', "unknown in pyro backend")):
        cf = os.path.join(work, 'x.cryo')
        with open(cf, 'w', encoding='utf-8') as f:
            f.write(LOOP)
        p = subprocess.run([sys.executable, CRYOC, cf, '--backend', backend,
                            '-o', os.path.join(work, 'x.out'), '--no-banner'],
                           capture_output=True, text=True, timeout=180,
                           errors='replace')
        log = p.stdout + p.stderr
        check(f"{backend} reports it and names the backend to use",
              p.returncode != 0 and needle in log, log[-200:])


def main():
    work = tempfile.mkdtemp(prefix='cryo_stream_')
    srv = HTTPServer(('127.0.0.1', 0), Handler)
    url = f"http://127.0.0.1:{srv.server_port}/v1"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        exe = test_tokens(work, url)
        test_incremental(work, url, exe)
        test_raw_form(work, url)
        test_early_exit(work, url)
        test_formats(work, url)
        test_no_endpoint(work)
        test_other_backends(work)
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
