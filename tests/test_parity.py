#!/usr/bin/env python3
# ============================================================
#  test_parity.py — the backends must agree (11.30/11.31/11.33/11.36)
#
#  Invariant 1 says a program means the same thing on every
#  backend. These are the cases where it did not, and the tests
#  are written the only way that actually proves it: RUN the
#  program on each backend and compare the output text.
#
#  That matters more than it sounds. Every one of these defects
#  passed a "does it generate code" check — the existing suite
#  asserts that go TEXT is produced and never that the text
#  compiles, which is exactly why 11.33 went unnoticed. A test
#  that does not run the program cannot see any of this.
#
#  Backends that are unavailable (no go toolchain, no node, no
#  gcc) are skipped rather than failed: the point is agreement
#  among those present.
# ============================================================
import os
import shutil
import subprocess
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRYOC = os.path.join(ROOT, 'Burnout', 'cryoc.py')
EXE = '.exe' if sys.platform == 'win32' else ''
VM = os.path.join(ROOT, 'Pyro', 'vm', 'pyrovm_go' + EXE)

_passed = _failed = _skipped = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def _compile(work, src, backend, out):
    p = os.path.join(work, 'p.cryo')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(src)
    r = subprocess.run([sys.executable, CRYOC, p, '--backend', backend,
                        '-o', out, '--no-banner', '--no-cache'],
                       capture_output=True, text=True, timeout=900)
    return r


def run_backend(work, src, backend):
    """The program's stdout on one backend, or None when unavailable."""
    try:
        if backend == 'pyro':
            if not os.path.isfile(VM):
                return None
            out = os.path.join(work, 'p.pyro')
            if _compile(work, src, 'pyro', out).returncode != 0:
                return '<compile failed>'
            r = subprocess.run([VM, out], capture_output=True, text=True, timeout=300)
        elif backend == 'node':
            if not shutil.which('node'):
                return None
            out = os.path.join(work, 'p.js')
            if _compile(work, src, 'node', out).returncode != 0:
                return '<compile failed>'
            r = subprocess.run(['node', out], capture_output=True, text=True, timeout=300)
        elif backend == 'go':
            if not shutil.which('go'):
                return None
            out = os.path.join(work, 'p.go')
            if _compile(work, src, 'go', out).returncode != 0:
                return '<compile failed>'
            # `go run`, not just codegen: 11.33 produced perfectly plausible Go
            # that the Go compiler then rejected.
            r = subprocess.run(['go', 'run', out], capture_output=True, text=True,
                               timeout=900, cwd=work)
            if r.returncode != 0:
                return '<go build failed> ' + r.stderr.strip()[:160]
        elif backend == 'c':
            if not shutil.which('gcc'):
                return None
            out = os.path.join(work, 'p.c')
            cr = _compile(work, src, 'c', out)
            exe = os.path.splitext(out)[0] + EXE
            if cr.returncode != 0 or not os.path.isfile(exe):
                return '<compile failed>'
            r = subprocess.run([exe], capture_output=True, text=True, timeout=300)
        else:
            return None
    except subprocess.TimeoutExpired:
        return '<timeout>'
    return r.stdout.replace('\r\n', '\n').strip()


def agree(label, src, backends=('pyro', 'node', 'go'), expect=None):
    """Every available backend must produce the same output."""
    global _skipped
    work = tempfile.mkdtemp(prefix='cryo_par_')
    got = {}
    for b in backends:
        o = run_backend(work, src, b)
        if o is None:
            _skipped += 1
            continue
        got[b] = o
    if len(got) < 2:
        print(f"  skip {label} (fewer than two backends available)")
        return
    vals = set(got.values())
    detail = '; '.join(f"{b}={v!r}" for b, v in got.items())
    check(label + f"  [{', '.join(got)}]", len(vals) == 1, detail)
    if expect is not None and vals:
        check(label + " — and the agreed output is right",
              got.get(next(iter(got))) == expect, detail)


print("[11.30/11.31/11.33/11.36] backend agreement")

# ── 11.31: `any` reaching a typed slot ─────────────────────
# `any a = 5; int b = a;` ran on pyro and node and did not COMPILE on go:
# "cannot use a (variable of interface type any) as int64 value". go is the
# only backend where the interface is explicit.
print("\n── 11.31: `any` into a typed context ──")
agree("any -> int", "any a = 5;\nint b = a;\nprint(b + 1);\n", expect="6")
agree("any -> string", 'any s = "hi";\nstring t = s;\nprint(t + "!");\n', expect="hi!")
agree("any -> number", "any f = 2.5;\nnumber g = f;\nprint(g);\n", expect="2.5")
agree("any -> bool", "any b = true;\nbool c = b;\nprint(c);\n", expect="true")
agree("any into an assignment (not just a declaration)",
      "any a = 5;\nint c = 0;\nc = a;\nprint(c);\n", expect="5")
agree("any as a call argument",
      "fn takes(int n) -> int ={ return n * 2; }\nany a = 5;\nprint(takes(a));\n",
      expect="10")
# The 11.21 optimizer INLINES small leaf functions, so the call above can reach
# the code generator as `a * 2` with the parameter type already gone. A fix
# that only handled call sites would pass until the optimizer ran.
agree("any in arithmetic directly", "any a = 5;\nprint(a * 2 + 1);\n", expect="11")

# ── 11.33: the mangled enum constructor ────────────────────
# Both `Ok(...)` and `Result_Ok(...)` are accepted on purpose. On go the
# mangled one collided with the generated STRUCT of the same name, so it was
# read as a type conversion: "cannot convert 5 to type Result_Ok".
print("\n── 11.33: Enum_Member(...) spelling ──")
_enum = "enum Result { Ok(int), Err(string) }\n"
agree("bare Ok(...)", _enum + "Result r = Ok(5);\nprint(r);\n")
agree("mangled Result_Ok(...)", _enum + "Result r = Result_Ok(7);\nprint(r);\n")
agree("mangled Result_Err(...)", _enum + 'Result r = Result_Err("bad");\nprint(r);\n')
# A user function whose name merely contains an underscore must not be
# rewritten by the same rule.
agree("a user function with an underscore is untouched",
      "fn my_fn(int x) -> int ={ return x + 1; }\nprint(my_fn(3));\n", expect="4")

# ── 11.30: values must render identically ──────────────────
# print() and to_string() are the program's observable output, so a backend
# that renders a value differently breaks invariant 1 in the most visible way
# there is.
print("\n── 11.30: rendering ──")
agree("int array", "int[] a = [0, 1, 2];\nprint(a);\n", expect="[0, 1, 2]")
agree("string array", 'string[] a = ["a", "b"];\nprint(a);\n', expect="[a, b]")
agree("bool array", "bool[] a = [true, false];\nprint(a);\n", expect="[true, false]")
agree("number array", "number[] a = [1.5, 2.0];\nprint(a);\n", expect="[1.5, 2]")
# A nested literal is where go can infer least, so it needs the type hint most:
# it used to emit []any{…} inside [][]int64{…} and fail to build.
agree("nested array", "int[][] n = [[1, 2], [3]];\nprint(n);\n",
      expect="[[1, 2], [3]]")
# An enum value is a tagged map on pyro/node and a struct on go, where the
# rendering dropped the tag entirely — `{val0: 5}` instead of `{tag: Ok,...}`,
# losing the one part that says which variant it is.
agree("an enum value leads with its tag",
      _enum + "Result r = Ok(5);\nprint(r);\n", expect="{tag: Ok, val0: 5}")
agree("...for the other variant too",
      _enum + 'Result r = Err("no");\nprint(r);\n', expect="{tag: Err, val0: no}")

# ── 11.36: Block in the C backend ──────────────────────────
# Every front-end desugaring that opens a scope emits a Block. It was routed to
# the SafetyBlock handler, which reads `n.safe` — an attribute a plain Block
# does not have — so `enumerate` on --backend c aborted with a Python
# AttributeError instead of compiling.
print("\n── 11.36: Block on the C backend ──")
agree("enumerate", 'string[] a = ["x", "y"];\n'
                   'for (int i, string s in enumerate(a)) { print(s); }\n',
      backends=('pyro', 'node', 'go', 'c'), expect="x\ny")
agree("a typed for-in over an array",
      "int[] xs = [1, 2, 3];\nfor (int v in xs) { print(v); }\n",
      backends=('pyro', 'node', 'go', 'c'), expect="1\n2\n3")

# The C backend still has no `any`, and that is a documented gap (11.27). What
# matters is that it REFUSES clearly instead of emitting `any` into the output
# for gcc to choke on — the compiler's job is to report its own errors.
print("\n── 11.27: the C backend's `any` gap is refused, not leaked ──")
_work = tempfile.mkdtemp(prefix='cryo_par_')
_r = _compile(_work, "int[] xs = [1, 2];\nint[] d = [v * 2 for v in xs];\nprint(d);\n",
              'c', os.path.join(_work, 'p.c'))
_msg = (_r.stdout + _r.stderr)
check("a comprehension is refused by the compiler", _r.returncode != 0, _msg[:200])
check("with a Cryo diagnostic, not a gcc error",
      'CodeGen Error' in _msg and 'unknown type name' not in _msg, _msg[:300])
check("and the message says where the `any` came from",
      'comprehension' in _msg, _msg[:300])

print(f"\n{_passed} passed, {_failed} failed"
      + (f", {_skipped} backend runs skipped" if _skipped else ""))
sys.exit(1 if _failed else 0)
