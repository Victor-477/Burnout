#!/usr/bin/env python3
# ============================================================
#  test_http_hardening.py — HTTP deadlines and the native client
#  (Forja_ERP/ESCOPO.md, gaps L-09 and L-18)
#
#  L-09  http_accept read a request with no timeout. The server is
#        sequential, so ONE client that connected and sent nothing
#        froze every other client for good.
#  L-18  http_get/http_post shelled out to curl through popen: the
#        URL was sprintf'd into a 1–2 KB buffer (overflow) and into
#        a shell command line (injection), the POST body went to a
#        temp file with -d (newlines stripped), and there was no
#        timeout, so a hung plugin froze the caller for good.
#
#  Runs every check on BOTH engines — the Go VM and the C VM (which
#  shares pyro_runtime.c with the AOT executables) — so the two stay
#  in step. Timeouts are shortened through PYRO_HTTP_TIMEOUT_MS and
#  PYRO_HTTP_CLIENT_TIMEOUT_MS so the suite stays fast.
# ============================================================
import glob
import http.server
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
VMDIR = os.path.join(ROOT, 'Pyro', 'vm')
EXE = '.exe' if sys.platform == 'win32' else ''

SERVER_TIMEOUT_MS = 1000
CLIENT_TIMEOUT_MS = 1500

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ── engines ───────────────────────────────────────────────

def build_engines(work):
    engines = []
    go = shutil.which('go')
    if go:
        out = os.path.join(work, 'pyrovm_go' + EXE)
        p = subprocess.run([go, 'build', '-o', out] + sorted(glob.glob(os.path.join(VMDIR, '*.go'))),
                           capture_output=True, text=True, cwd=VMDIR)
        check("Go VM builds", p.returncode == 0, p.stderr[-400:])
        if p.returncode == 0:
            engines.append(('go', out))
    else:
        print("  SKIP  no Go toolchain — Go VM not tested")
    cc = next((c for c in ('gcc', 'clang', 'cc') if shutil.which(c)), None)
    if cc:
        out = os.path.join(work, 'pyrovm_c' + EXE)
        libs = ['-lm'] + (['-lws2_32'] if sys.platform == 'win32' else [])
        p = subprocess.run([cc, '-O2', '-std=c11', '-finput-charset=UTF-8', '-fexec-charset=UTF-8',
                            '-o', out, os.path.join(VMDIR, 'main.c'),
                            os.path.join(VMDIR, 'pyro_runtime.c')] + libs,
                           capture_output=True, text=True)
        check("C VM builds", p.returncode == 0 and os.path.isfile(out), p.stderr[-400:])
        if p.returncode == 0 and os.path.isfile(out):
            engines.append(('c', out))
    else:
        print("  SKIP  no C toolchain — C VM not tested")
    return engines


def compile_cryo(src, work, name):
    path = os.path.join(work, name + '.cryo')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)
    out = os.path.join(work, name + '.pyro')
    p = subprocess.run([sys.executable, CRYOC, path, '--backend', 'pyro', '-o', out, '--no-banner'],
                       capture_output=True, text=True, cwd=ROOT)
    check(f"{name}.cryo compiles", p.returncode == 0, (p.stdout + p.stderr)[-400:])
    return out if p.returncode == 0 else None


def env_for(**extra):
    e = dict(os.environ)
    e.pop('PYRO_SANDBOX', None)
    e.pop('PYRO_POLICY', None)
    e['PYRO_HTTP_TIMEOUT_MS'] = str(SERVER_TIMEOUT_MS)
    e['PYRO_HTTP_CLIENT_TIMEOUT_MS'] = str(CLIENT_TIMEOUT_MS)
    e.update({k: str(v) for k, v in extra.items()})
    return e


# ── part 1: the server must survive idle and trickling clients ──

SERVER = r'''
int port = to_int(env("PORT"));
if (http_listen(port)) {
    while (true) {
        map<string,string> req = http_accept();
        if (len(req["method"]) > 0) {
            if (req["path"] == "/echo") {
                http_respond(200, "text/plain", req["body"]);
            } else {
                http_respond(200, "text/plain", "pong");
            }
        }
    }
}
'''


def get(url, data=None, timeout=8):
    req = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def wait_up(base, proc):
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            return get(base + '/ping', timeout=2) == b'pong'
        except Exception:
            time.sleep(0.2)
    return False


def test_server(engine, vm, pyro, work):
    port = free_port()
    base = f'http://127.0.0.1:{port}'
    proc = subprocess.Popen([vm, pyro], cwd=work, env=env_for(PORT=port),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_up(base, proc):
            check(f"[{engine}] server starts", False)
            return
        check(f"[{engine}] server starts", True)

        # 1. a client that connects and sends nothing at all
        idle = socket.create_connection(('127.0.0.1', port))
        t0 = time.time()
        try:
            ok = get(base + '/ping') == b'pong'
        except Exception as e:
            ok = False
        dt = time.time() - t0
        check(f"[{engine}] an idle connection does not freeze the server",
              ok and dt < 5, f"ok={ok} took {dt:.1f}s")
        idle.settimeout(5)
        try:
            dropped = idle.recv(1) == b''
        except (ConnectionResetError, ConnectionAbortedError):
            dropped = True
        except socket.timeout:
            dropped = False
        check(f"[{engine}] the idle connection is dropped at the deadline", dropped)
        idle.close()

        # 2. a client that trickles a byte at a time, which a per-recv
        #    SO_RCVTIMEO would never catch: the deadline is for the request
        stop = threading.Event()

        def trickle():
            try:
                s = socket.create_connection(('127.0.0.1', port))
                s.sendall(b'GET /slow HTTP/1.1\r\n')
                while not stop.is_set():
                    s.sendall(b'X')
                    time.sleep(0.2)
            except OSError:
                pass

        th = threading.Thread(target=trickle, daemon=True)
        th.start()
        time.sleep(0.3)
        t0 = time.time()
        try:
            ok = get(base + '/ping') == b'pong'
        except Exception:
            ok = False
        dt = time.time() - t0
        stop.set()
        check(f"[{engine}] a trickling client does not freeze the server",
              ok and dt < 5, f"ok={ok} took {dt:.1f}s")

        # 3. a body shorter than its Content-Length, then silence
        s = socket.create_connection(('127.0.0.1', port))
        s.sendall(b'POST /echo HTTP/1.1\r\nContent-Length: 100\r\n\r\n0123456789')
        try:
            ok = get(base + '/ping') == b'pong'
        except Exception:
            ok = False
        check(f"[{engine}] a truncated body does not freeze the server", ok)
        s.close()

        # 4. a multi-line body arrives intact
        body = 'linha 1\nlinha 2\r\n\nfim "aspas" & | ^ %PATH%\n'.encode()
        try:
            echoed = get(base + '/echo', data=body)
        except Exception as e:
            echoed = repr(e).encode()
        check(f"[{engine}] a multi-line request body arrives intact", echoed == body, echoed[:120])
    finally:
        proc.kill()
        proc.wait()


# ── part 2: the client ────────────────────────────────────

class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def reply(self, status, body=b'', headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path.startswith('/len/'):
            self.reply(200, str(len(self.path)).encode())
        elif self.path == '/redir':
            self.reply(302, headers=[('Location', '/pong')])
        elif self.path == '/pong':
            self.reply(200, b'pong')
        elif self.path == '/slow':
            time.sleep(6)
            self.reply(200, b'too late')
        elif self.path == '/chunked':
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Connection', 'close')
            self.end_headers()
            for part in (b'hello ', b'chunked'):
                self.wfile.write(b'%x\r\n%s\r\n' % (len(part), part))
            self.wfile.write(b'0\r\n\r\n')
            self.close_connection = True
        else:
            self.reply(200, b'path-ok')

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        data = self.rfile.read(n)
        if self.path == '/echo':
            ctype = self.headers.get('Content-Type', '')
            self.reply(200, data if ctype == 'application/json' else b'bad content-type ' + ctype.encode())
        elif self.path == '/r307':
            self.reply(307, headers=[('Location', '/echo')])
        else:
            self.reply(404)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


CLIENT = r'''
string up = env("UP");
print("echo=[" + http_post(up + "/echo", read_file(env("BODYFILE"))) + "]");
print("long=" + http_get(env("LONGURL")));
print("inject=" + http_get(up + "/x\"&echo INJECTED&\""));
print("redir=" + http_get(up + "/redir"));
print("r307=" + http_post(up + "/r307", "kept"));
print("chunked=" + http_get(up + "/chunked"));
print("refused=[" + http_get("http://127.0.0.1:1/") + "]");
print("badurl=[" + http_get("ftp://127.0.0.1/") + "]");
int t0 = now_ms();
string slow = http_get(up + "/slow");
print("slow=[" + slow + "] ms=" + to_string(now_ms() - t0));
print("done");
'''


def test_client(engine, vm, pyro, work, up, body_file, body):
    longpath = '/len/' + 'a' * 8000
    p = subprocess.run([vm, pyro], cwd=work, capture_output=True, timeout=60,
                       env=env_for(UP=up, BODYFILE=body_file, LONGURL=up + longpath))
    out = p.stdout.decode('utf-8', 'replace').replace('\r\n', '\n')
    lines = out.split('\n')

    def field(name):
        for ln in lines:
            if ln.startswith(name + '='):
                return ln[len(name) + 1:]
        return None

    check(f"[{engine}] client program runs to the end", p.returncode == 0 and 'done' in lines,
          (out + p.stderr.decode('utf-8', 'replace'))[-400:])
    check(f"[{engine}] POST body with newlines and quotes arrives intact (JSON content-type)",
          ('echo=[' + body + ']') in out, out[:300])
    check(f"[{engine}] an 8 KB URL is sent whole (no overflow)",
          field('long') == str(len(longpath)), field('long'))
    check(f"[{engine}] a URL cannot inject a shell command",
          'INJECTED' not in lines and field('inject') == 'path-ok', field('inject'))
    check(f"[{engine}] follows a 302", field('redir') == 'pong', field('redir'))
    check(f"[{engine}] a 307 resends the POST body", field('r307') == 'kept', field('r307'))
    check(f"[{engine}] decodes a chunked response", field('chunked') == 'hello chunked',
          field('chunked'))
    check(f"[{engine}] connection refused -> \"\"", field('refused') == '[]', field('refused'))
    check(f"[{engine}] unsupported scheme -> \"\"", field('badurl') == '[]', field('badurl'))
    slow = field('slow') or ''
    try:
        ms = int(slow.split('ms=')[1])
    except (IndexError, ValueError):
        ms = -1
    check(f"[{engine}] a hung upstream times out with \"\"",
          slow.startswith('[] ') and 0 <= ms < CLIENT_TIMEOUT_MS + 2500, slow)


def main():
    print("-- HTTP hardening: server deadlines and the native client (L-09, L-18) --")
    work = tempfile.mkdtemp(prefix='http_hardening_')
    upstream = None
    try:
        engines = build_engines(work)
        if not engines:
            print("  SKIP  no engine could be built")
            return
        server = compile_cryo(SERVER, work, 'server')
        client = compile_cryo(CLIENT, work, 'client')

        if server:
            for engine, vm in engines:
                print(f"[server] {engine}")
                test_server(engine, vm, server, work)

        if client:
            upstream = ThreadingServer(('127.0.0.1', 0), Upstream)
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            up = f'http://127.0.0.1:{upstream.server_address[1]}'
            body = '{"a": 1,\n "b": "duas\nlinhas", "c": "\\"q\\" & | ^ %PATH% $HOME"}\n\nfim'
            body_file = os.path.join(work, 'body.txt')
            with open(body_file, 'w', encoding='utf-8', newline='') as f:
                f.write(body)
            for engine, vm in engines:
                print(f"[client] {engine}")
                test_client(engine, vm, client, work, up, body_file, body)
    finally:
        if upstream:
            upstream.shutdown()
        shutil.rmtree(work, ignore_errors=True)
        print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
