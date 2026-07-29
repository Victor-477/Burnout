#!/usr/bin/env python3
# ============================================================
#  test_taskapp.py — the reference application (roadmap 11.10)
#
#  cryo/examples/taskapp/ is a task tracker written ENTIRELY in
#  Cryo: no foreign blocks anywhere. It is the proof for Phase 11
#  Track B, so this suite tests it as an application rather than
#  as a language feature:
#
#    * multi-module: app / tasks / store / http / web, each with
#      private internals behind a pub API (ISSUES/19)
#    * module state across requests (11.1)
#    * the HTTP natives (11.6)
#    * durable writes (11.8) — and it RESTARTS the process to
#      prove the data actually came back off disk
#    * the same data file served by a DIFFERENT ENGINE, which is
#      the parity claim made concrete
#
#  Skips cleanly when no VM can be built.
# ============================================================
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
APP = os.path.join(ROOT, 'Cryo', 'examples', 'taskapp', 'app.cryo')
BUILD = os.path.join(ROOT, 'build')
PYRO = os.path.join(BUILD, 'taskapp_test.pyro')
GOVM = os.path.join(BUILD, 'pyrovm.exe' if sys.platform == 'win32' else 'pyrovm')
CVM = os.path.join(BUILD, 'pyroc.exe' if sys.platform == 'win32' else 'pyroc')

TOKEN = 'test-token'
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


def call(base, path, method='GET', body=None, token=TOKEN):
    """-> (status, parsed-or-text). Appends the token unless token=None."""
    if token is not None:
        path += ('&' if '?' in path else '?') + 'token=' + token
    req = urllib.request.Request(base + path,
                                 data=body.encode() if body is not None else None,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw, status = r.read().decode(), r.getcode()
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


class App:
    """The application as a subprocess, so it can be restarted."""

    def __init__(self, workdir, port, engine):
        self.workdir, self.port, self.engine = workdir, port, engine
        self.proc = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        env = dict(os.environ, PORT=str(self.port), TASKS_TOKEN=TOKEN,
                   TASKS_FILE='tasks.json')
        self.proc = subprocess.Popen([self.engine, PYRO], cwd=self.workdir, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True)
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                call(self.base, '/api/stats')
                return True
            except Exception:
                time.sleep(0.2)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def build():
    print("[build] compiling the reference application")
    if not os.path.isfile(APP):
        check("app.cryo present", False, APP)
        return False
    if not os.path.isfile(GOVM):
        print("  SKIP  no Pyro VM built")
        return False
    os.makedirs(BUILD, exist_ok=True)
    p = subprocess.run([sys.executable, CRYOC, APP, '--backend', 'pyro',
                        '-o', PYRO, '--no-banner'],
                       capture_output=True, text=True, cwd=ROOT)
    check("compiles with no foreign blocks", p.returncode == 0,
          (p.stdout + p.stderr)[-400:])
    return p.returncode == 0


def test_api(app):
    b = app.base
    check("serves the UI at /", *(lambda s, t: (s == 200 and '<title>Tasks' in t,
                                                (s, str(t)[:80])))(*call(b, '/', token=None)))

    st, body = call(b, '/api/tasks', token=None)
    check("rejects a request with no token", st == 401 and body.get('error') == 'unauthorized',
          (st, body))
    st, body = call(b, '/api/tasks', token='wrong')
    check("rejects a wrong token", st == 401, (st, body))

    st, body = call(b, '/api/tasks')
    check("starts empty", st == 200 and body == [], (st, body))

    st, body = call(b, '/api/tasks', 'POST', 'write the tests')
    check("POST creates a task -> 201 with its id", st == 201 and body.get('id') == 1,
          (st, body))
    st, body = call(b, '/api/tasks', 'POST', '   ship 11.10   ')
    check("second task gets the next id", st == 201 and body.get('id') == 2, (st, body))

    st, body = call(b, '/api/tasks', 'POST', '   ')
    check("a blank title is rejected", st == 400 and body.get('error') == 'title is required',
          (st, body))
    st, body = call(b, '/api/tasks', 'POST', 'x' * 121)
    check("an over-long title is rejected", st == 400 and 'fewer' in str(body.get('error')),
          (st, body))

    st, tasks = call(b, '/api/tasks')
    check("lists both tasks in id order",
          st == 200 and [t['id'] for t in tasks] == [1, 2], tasks)
    check("the title was trimmed",
          any(t['title'] == 'ship 11.10' for t in tasks), tasks)
    check("a new task starts not done", all(t['done'] is False for t in tasks), tasks)

    st, body = call(b, '/api/tasks/complete?id=1', 'POST')
    check("completing a task succeeds", st == 200 and body.get('ok') is True, (st, body))
    st, body = call(b, '/api/tasks/complete?id=99', 'POST')
    check("completing a missing id -> 404",
          st == 404 and body.get('error') == 'no task with that id', (st, body))

    st, tasks = call(b, '/api/tasks')
    check("the completed task is marked done",
          next(t for t in tasks if t['id'] == 1)['done'] is True, tasks)

    st, body = call(b, '/api/stats')
    check("stats count total/done/pending",
          body == {'total': 2, 'done': 1, 'pending': 1}, body)

    st, body = call(b, '/nope')
    check("an unknown route -> 404", st == 404, (st, body))


def test_persistence(workdir, port):
    """Restart the process — on the OTHER engine — and re-read the data."""
    engine = CVM if os.path.isfile(CVM) else GOVM
    label = "the C VM" if engine == CVM else "the Go VM"

    app = App(workdir, port, engine)
    if not app.start():
        check(f"restarts on {label}", False, "did not come up")
        return
    try:
        check(f"restarts on {label}", True)

        st, tasks = call(app.base, '/api/tasks')
        check("the tasks came back off disk",
              st == 200 and [t['id'] for t in tasks] == [1, 2], tasks)
        check("the completed flag survived",
              next(t for t in tasks if t['id'] == 1)['done'] is True, tasks)
        check("the trimmed title survived",
              next(t for t in tasks if t['id'] == 2)['title'] == 'ship 11.10', tasks)

        # ids must not restart at 1 and collide with a stored task
        st, body = call(app.base, '/api/tasks', 'POST', 'after restart')
        check("a new id continues past the stored ones (no collision)",
              st == 201 and body.get('id') == 3, (st, body))

        st, body = call(app.base, '/api/tasks/delete?id=1', 'POST')
        check("delete succeeds", st == 200 and body.get('ok') is True, (st, body))
        st, tasks = call(app.base, '/api/tasks')
        check("the deleted task is gone", [t['id'] for t in tasks] == [2, 3], tasks)

        st, body = call(app.base, '/api/stats')
        check("stats recomputed after the delete",
              body == {'total': 2, 'done': 0, 'pending': 2}, body)
    finally:
        app.stop()

    # and the delete reached the disk, not just memory
    data = os.path.join(workdir, 'tasks.json')
    check("the data file exists", os.path.isfile(data))
    if os.path.isfile(data):
        raw = open(data, encoding='utf-8').read()
        check("the deleted task is not in the file", 'task:1' not in raw, raw[:120])
        check("the surviving tasks are in the file",
              'task:2' in raw and 'task:3' in raw, raw[:120])
        check("no .tmp file was left behind",
              not os.path.isfile(data + '.tmp'))


def main():
    print("-- the reference application: a task tracker in pure Cryo --")
    if not build():
        print("\nskipped")
        sys.exit(0 if _failed == 0 else 1)

    workdir = tempfile.mkdtemp(prefix='taskapp_')
    port = free_port()
    app = App(workdir, port, GOVM)
    try:
        print("[run] first launch (Go VM)")
        if not app.start():
            out = (app.proc.stdout.read() or '')[-400:] if app.proc.poll() is not None else ''
            check("the application starts", False, out or "timed out")
        else:
            check("the application starts", True)
            test_api(app)
    finally:
        app.stop()

    print("[restart] same data, different engine")
    test_persistence(workdir, port)

    shutil.rmtree(workdir, ignore_errors=True)
    if os.path.isfile(PYRO):
        os.remove(PYRO)
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
