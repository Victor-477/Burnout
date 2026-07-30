#!/usr/bin/env python3
# ============================================================
#  test_audit.py — security audit (roadmap 11.15)
#
#  Every rule is checked from BOTH sides: it fires on the unsafe
#  form, and it stays silent on the safe one. A rule tested only
#  on unsafe code passes just as well when it reports everything,
#  and a rule that reports everything is a rule people disable.
#
#  The last group runs the audit over the repository's own Cryo
#  sources. Those are supposed to be correct, so a HIGH there is
#  a false positive by construction — and false positives are the
#  expensive failure here: --strict gates CI on HIGH, so the first
#  thing a noisy rule buys is somebody dropping --strict.
# ============================================================
import os
import subprocess
import sys

# Windows consoles default to cp1252 and the section headers below are not
# encodable there; without this the suite dies before its first assertion.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

from lexer    import Lexer          # CRYO
from parser   import Parser         # CRYO
from security import audit_ast      # CRYO

CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def audit(src):
    return audit_ast(Parser(Lexer(src).tokenize()).parse())


def rules(src):
    return {f.rule for f in audit(src)}


def fires(label, rule, unsafe, safe):
    """The rule triggers on `unsafe` and is silent on `safe`."""
    got = rules(unsafe)
    check(f"{label}: fires", rule in got, f"got {sorted(got)}")
    got = rules(safe)
    check(f"{label}: silent on the safe form", rule not in got,
          f"got {sorted(got)}")


# ── 11.15: taint tables know the current natives ─────────────
# Before 11.15 the sources/sinks named only the old pyro_* builtins, so a
# program written entirely against the 11.6-11.8 I/O audited perfectly clean.
def test_tables():
    print("\n── taint sources and sinks (11.15) ──")

    fires("exec of request data", 'tainted-exec',
          'map<string,string> r = http_accept();\n'
          'print(exec(r["body"]));',
          'print(exec("ls -la"));')

    fires("read_file of request data", 'tainted-path',
          'map<string,string> r = http_accept();\n'
          'print(read_file(r["path"]));',
          'print(read_file("config/app.json"));')

    fires("write_file of request data", 'tainted-path',
          'map<string,string> r = http_accept();\n'
          'write_file(r["path"], "x");',
          'write_file("out/log.txt", "x");')

    fires("delete_file of request data", 'tainted-path',
          'map<string,string> r = http_accept();\n'
          'delete_file(r["path"]);',
          'delete_file("out/tmp.txt");')

    # http_accept is a source in its own right: reaching a sink through a
    # local, not just inline, is the shape real code actually has.
    got = rules('map<string,string> r = http_accept();\n'
                'string p = r["path"];\n'
                'string q = p;\n'
                'print(read_file(q));')
    check("taint propagates through assignments", 'tainted-path' in got,
          f"got {sorted(got)}")


# ── 11.15: unvalidated deserialization ───────────────────────
def test_deserialization():
    print("\n── unvalidated deserialization (11.15) ──")
    fires("json_decode of untrusted data cast with `as`",
          'unvalidated-deserialization',
          'map<string,string> r = http_accept();\n'
          'map<string,string> m = json_decode(r["body"]) as map<string,string>;',
          'map<string,string> m = json_decode("{}") as map<string,string>;')

    # A cast of trusted data is not a finding; nor is decoding without one.
    got = rules('map<string,string> r = http_accept();\n'
                'any v = json_decode(r["body"]);')
    check("json_decode without a cast is not this rule",
          'unvalidated-deserialization' not in got, f"got {sorted(got)}")


# ── 11.15: unbounded allocation ──────────────────────────────
def test_allocation():
    print("\n── unbounded allocation (11.15) ──")
    fires("repeat() sized by request data", 'unbounded-allocation',
          'map<string,string> r = http_accept();\n'
          'print(repeat("x", to_int(r["query"])));',
          'print(repeat("x", 80));')

    # The rule is about the SIZE argument. Untrusted content at a bounded
    # size is fine, and reporting it would make the rule noise.
    got = rules('map<string,string> r = http_accept();\n'
                'print(repeat(r["body"], 3));')
    check("untrusted content with a constant count is not a finding",
          'unbounded-allocation' not in got, f"got {sorted(got)}")


# ── 11.15: broad permissions ─────────────────────────────────
def test_permissions():
    print("\n── broad permissions (11.15) ──")
    fires("exec = \"*\" grants the whole class", 'broad-permission',
          'permissions { exec = "*"; }\nprint(exec("ls"));',
          'permissions { exec = "/bin/ls"; }\nprint(exec("ls"));')

    # exec/write are HIGH; a broad read is real but less severe, and the
    # distinction is what keeps --strict usable.
    f = [x for x in audit('permissions { read = "*"; }\n'
                          'print(read_file("a"));')
         if x.rule == 'broad-permission']
    check("a broad read is reported below HIGH",
          len(f) == 1 and f[0].level == 'MEDIUM',
          str([(x.level, x.rule) for x in f]))

    f = [x for x in audit('permissions { exec = "*"; }\nprint(exec("ls"));')
         if x.rule == 'broad-permission']
    check("a broad exec is HIGH", len(f) == 1 and f[0].level == 'HIGH',
          str([(x.level, x.rule) for x in f]))


# ── 11.15: taint is scoped per function ──────────────────────
# Taint is tracked by NAME. Computed over the whole program, a `path` fed by
# http_accept in one function poisoned every unrelated `path` elsewhere — two
# false HIGHs on the reference application, which is what prompted the scoping.
def test_scoping():
    print("\n── per-function taint scoping (11.15) ──")
    src = ('fn handle() ={\n'
           '  map<string,string> req = http_accept();\n'
           '  string path = req["path"];\n'
           '  print(path);\n'
           '}\n'
           'fn load(string path) -> string ={\n'
           '  return read_file(path);\n'
           '}\n')
    got = rules(src)
    check("a name tainted in one function does not taint another",
          'tainted-path' not in got, f"got {sorted(got)}")

    # ...and the scoping must not silence the real case in the same file.
    src2 = src + ('fn bad() ={\n'
                  '  map<string,string> r = http_accept();\n'
                  '  print(read_file(r["path"]));\n'
                  '}\n')
    got = rules(src2)
    check("the genuine finding in the same file still fires",
          'tainted-path' in got, f"got {sorted(got)}")


# ── the --strict CI gate ─────────────────────────────────────
# This gate compared f.level against 'ALTO' after the levels were renamed to
# English, so it never fired: --strict printed HIGH findings and exited 0.
# These tests exist so it cannot silently stop gating again.
def test_strict_gate(tmp):
    print("\n── --strict CI gate ──")
    unsafe = os.path.join(tmp, 'unsafe.cryo')
    safe = os.path.join(tmp, 'safe.cryo')
    open(unsafe, 'w', encoding='utf-8').write(
        'map<string,string> r = http_accept();\nprint(exec(r["body"]));\n')
    open(safe, 'w', encoding='utf-8').write('print("hello");\n')

    def code(path, *flags):
        return subprocess.run(
            [sys.executable, CRYOC, path, '--backend', 'pyro',
             '-o', path.replace('.cryo', '.pyro'), '--no-banner', *flags],
            capture_output=True, text=True, timeout=120).returncode

    check("--strict exits 2 on a HIGH finding", code(unsafe, '--strict') == 2)
    check("--strict exits 2 with --audit too",
          code(unsafe, '--audit', '--strict') == 2)
    check("--strict exits 0 on clean code", code(safe, '--strict') == 0)
    # Without the gate the same program must still compile: --strict is opt-in.
    check("no --strict: the unsafe program still compiles", code(unsafe) == 0)


# ── the repository's own sources stay clean ──────────────────
def test_no_false_positives():
    print("\n── no HIGH findings on the project's own code ──")
    targets = [
        os.path.join(ROOT, 'Cryo', 'examples', 'taskapp', 'store.cryo'),
        os.path.join(ROOT, 'Cryo', 'examples', 'taskapp', 'http.cryo'),
        os.path.join(ROOT, 'Cryo', 'examples', 'taskapp', 'tasks.cryo'),
        os.path.join(ROOT, 'Cryo', 'examples', 'api_native', 'server.cryo'),
    ]
    for t in targets:
        if not os.path.isfile(t):
            continue
        src = open(t, encoding='utf-8').read()
        high = [f for f in audit(src) if f.level == 'HIGH']
        check(f"{os.path.basename(t)}: 0 HIGH", not high,
              str([(f.rule, f.message[:60]) for f in high]))


def main():
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix='cryo_audit_')
    try:
        test_tables()
        test_deserialization()
        test_allocation()
        test_permissions()
        test_scoping()
        test_strict_gate(tmp)
        test_no_false_positives()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


if __name__ == '__main__':
    main()
