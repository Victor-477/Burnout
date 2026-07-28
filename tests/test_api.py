#!/usr/bin/env python3
# ============================================================
#  test_api.py — a Cryo REST API, compiled to a standalone .exe
#
#  The end-to-end claim under test is deliberately literal:
#
#    1. `cryoc --backend go` on Cryo/examples/api/server.cryo
#       produces a NATIVE EXECUTABLE.
#    2. Launching that executable — with no VM, no Python, no Go
#       toolchain, nothing but the file itself — serves the API.
#    3. Every payload and every rule it answers with came from a
#       Cryo function, not from the Go transport glue.
#
#  Point 3 is what makes this a Cryo test rather than a Go test,
#  so the assertions target the things Cryo decides: the JSON
#  field order json_encode emits, the trimming and length rules
#  in validate_title, and the integer percentage in stats_json.
# ============================================================
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC   = os.path.join(ROOT, 'Burnout', 'cryoc.py')
SOURCE  = os.path.join(ROOT, 'Cryo', 'examples', 'api', 'server.cryo')
OUT_DIR = os.path.join(ROOT, 'build', 'api_test')
OUT_GO  = os.path.join(OUT_DIR, 'server.go')
EXE     = os.path.join(OUT_DIR, 'server.exe' if sys.platform == 'win32' else 'server')

_passed = 0
_failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def free_port():
    """Ask the OS for an unused port so parallel runs cannot collide."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def request(base, path, method='GET', body=None):
    """-> (status, parsed_json_or_raw_text). Never raises on 4xx/5xx."""
    url = base + path
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw, status = r.read().decode(), r.getcode()
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def wait_until_serving(base, proc, timeout=25.0):
    """Poll until the API answers, or the process dies, or we give up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            request(base, '/api/health')
            return True
        except Exception:
            time.sleep(0.2)
    return False


def build():
    print("[build] .cryo -> .go -> native executable")
    if shutil.which('go') is None:
        print("  SKIP  the go toolchain is not installed")
        return False
    os.makedirs(OUT_DIR, exist_ok=True)
    for stale in (EXE, OUT_GO):
        if os.path.exists(stale):
            os.remove(stale)

    p = subprocess.run(
        [sys.executable, CRYOC, SOURCE, '--backend', 'go', '-o', OUT_GO, '--no-banner'],
        capture_output=True, text=True, cwd=ROOT)
    check("cryoc exits 0", p.returncode == 0, (p.stdout + p.stderr)[-500:])
    check("an executable was produced", os.path.isfile(EXE),
          f"missing: {EXE}")
    if not os.path.isfile(EXE):
        return False
    # A real binary, not a script: big enough to be a linked Go program.
    check("the executable is a real native binary",
          os.path.getsize(EXE) > 1_000_000,
          f"{os.path.getsize(EXE)} bytes")
    return True


def test_api():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PORT=str(port))

    print(f"[run] launching the executable on port {port} — nothing else")
    proc = subprocess.Popen([EXE], env=env, cwd=OUT_DIR,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        if not wait_until_serving(base, proc):
            out = ''
            if proc.poll() is not None:
                out = (proc.stdout.read() or '')[-500:]
            check("the executable serves the API", False, out or "timed out")
            return
        check("the executable serves the API", True)

        # ── GET /api/health — payload built by Cryo's health_json ──
        st, b = request(base, '/api/health')
        check("GET /api/health -> 200", st == 200, st)
        check("health reports the service and version",
              isinstance(b, dict) and b.get('status') == 'ok'
              and b.get('service') == 'cryo-api' and b.get('version') == '1.1.0', b)
        check("health counts the seeded tasks", b.get('tasks') == 3, b)

        # ── GET /api/tasks — json_encode over the Task struct ──
        st, tasks = request(base, '/api/tasks')
        check("GET /api/tasks -> 200", st == 200, st)
        check("returns the three seeded tasks",
              isinstance(tasks, list) and len(tasks) == 3, tasks)
        check("each task has the Cryo struct's fields",
              all(set(t) == {'id', 'title', 'done'} for t in tasks), tasks)
        check("field ORDER follows the Cryo struct declaration",
              all(list(t) == ['id', 'title', 'done'] for t in tasks), tasks)
        check("types survive the boundary (int / string / bool)",
              isinstance(tasks[0]['id'], int)
              and isinstance(tasks[0]['title'], str)
              and isinstance(tasks[0]['done'], bool), tasks[0])

        # ── GET /api/tasks/{id} ──
        st, b = request(base, '/api/tasks/2')
        check("GET /api/tasks/2 -> 200 with that task",
              st == 200 and b.get('id') == 2, (st, b))
        st, b = request(base, '/api/tasks/999')
        check("GET a missing id -> 404 with Cryo's error payload",
              st == 404 and b.get('error') == 'no task with that id', (st, b))
        st, b = request(base, '/api/tasks/abc')
        check("GET a non-numeric id -> 400",
              st == 400 and b.get('error') == 'id must be a number', (st, b))

        # ── GET /api/stats — percent_done() is Cryo integer maths ──
        st, b = request(base, '/api/stats')
        check("GET /api/stats -> 200", st == 200, st)
        check("stats: 1 of 3 done, 2 pending",
              b.get('total') == 3 and b.get('done') == 1 and b.get('pending') == 2, b)
        check("percent_done truncates like Cryo integer division (33, not 33.3)",
              b.get('percent_done') == 33, b)

        # ── POST /api/tasks — validate_title() decides all of this ──
        st, b = request(base, '/api/tasks', 'POST', '{"title":"  ship it  "}')
        check("POST a valid task -> 201", st == 201, (st, b))
        check("the new task is returned, id assigned", b.get('id') == 4, b)
        check("Cryo's normalize_title trimmed the whitespace",
              b.get('title') == 'ship it', b)
        check("a new task starts not done", b.get('done') is False, b)

        st, b = request(base, '/api/tasks', 'POST', '{"title":"   "}')
        check("POST a blank title -> 400 (whitespace is not a title)",
              st == 400 and b.get('error') == 'title is required', (st, b))

        st, b = request(base, '/api/tasks', 'POST',
                        json.dumps({'title': 'x' * 61}))
        check("POST a 61-character title -> 400",
              st == 400 and 'fewer' in str(b.get('error')), (st, b))
        st, b = request(base, '/api/tasks', 'POST',
                        json.dumps({'title': 'y' * 60}))
        check("POST a 60-character title -> 201 (the boundary is inclusive)",
              st == 201, (st, b))

        st, b = request(base, '/api/tasks', 'POST', 'not json at all')
        check("POST a malformed body -> 400",
              st == 400 and b.get('error') == 'body must be valid JSON', (st, b))

        st, b = request(base, '/api/tasks', 'DELETE')
        check("an unsupported method -> 405",
              st == 405 and b.get('error') == 'method not allowed', (st, b))

        # ── the writes actually persisted ──
        st, tasks = request(base, '/api/tasks')
        check("the two accepted POSTs are now in the list",
              len(tasks) == 5, len(tasks))
        st, b = request(base, '/api/stats')
        check("stats reflect the new tasks (1 of 5 done -> 20%)",
              b.get('total') == 5 and b.get('percent_done') == 20, b)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    print("-- a Cryo REST API as a standalone executable --")
    if not build():
        print("\nskipped (no go toolchain, or the build failed)")
        sys.exit(0 if _failed == 0 else 1)
    test_api()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
