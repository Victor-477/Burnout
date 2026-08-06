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


def run_backend(work, src, backend, expect_fail=False):
    """The program's stdout on one backend, or None when unavailable.

    12.13 — `expect_fail` is for programs that are SUPPOSED to abort: it returns
    stdout+stderr (every engine writes the abort text to stderr) and does not
    read a non-zero exit as a build failure, which is otherwise exactly what a
    successful abort looks like from here.
    """
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
            if r.returncode != 0 and not expect_fail:
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
    out_text = r.stdout + ('\n' + (r.stderr or '') if expect_fail else '')
    return out_text.replace('\r\n', '\n').strip()


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

# ── 11.34: `??` on a data-carrying enum ────────────────────
# `??` asks whether a value is null. An `Err("no")` is not null, so the
# operator handed back the value itself — printing `{tag: Err, val0: no}` on
# pyro and node, leaking the representation into user output, and failing to
# compile on go.
#
# Making `r ?? 9` mean "the Ok payload, else 9" is not available to the
# compiler: Result/Ok/Err are ordinary user declarations here and nothing marks
# which variant is the successful one. So it is refused, in the FRONT END, so
# every backend agrees by construction rather than by three separate fixes.
print("\n── 11.34: `??` on an enum with data ──")
_res = "enum Result { Ok(int), Err(string) }\n"
_work = tempfile.mkdtemp(prefix='cryo_par_')
_src = _res + 'Result r = Err("no");\nprint(r ?? 9);\n'
for _b in ('pyro', 'node', 'go'):
    _r = _compile(_work, _src, _b, os.path.join(_work, 'o.' + _b))
    _m = _r.stdout + _r.stderr
    check(f"{_b}: refused rather than leaking the enum", _r.returncode != 0, _m[:160])
    check(f"{_b}: and the message points at match",
          'match' in _m and "'??'" in _m, _m[:200])

# A constructor call directly, not only a variable.
_r = _compile(_work, _res + 'print(Err("no") ?? 9);\n', 'pyro',
              os.path.join(_work, 'o2.pyro'))
check("a constructor call is caught too", _r.returncode != 0,
      (_r.stdout + _r.stderr)[:160])

# What must NOT be refused. `??` on an optional is the operator's actual job,
# and a payload-less enum is an integer constant on every backend — refusing
# that would reject code that works today for no gain.
agree("`??` on an optional still works",
      "int? a = null;\nprint(a ?? 9);\nint? b = 4;\nprint(b ?? 9);\n",
      expect="9\n4")
agree("`??` on a string optional still works",
      'string? s = null;\nprint(s ?? "dflt");\n', expect="dflt")
_r = _compile(_work, "enum Level { LOW, HIGH }\nLevel a = LOW;\nprint(a ?? HIGH);\n",
              'pyro', os.path.join(_work, 'o3.pyro'))
check("a payload-less enum is NOT refused", _r.returncode == 0,
      (_r.stdout + _r.stderr)[:200])

# ── 11.27: the C backend's gaps shrink ─────────────────────
# Printing an array and using an optional both used to be refused with "use
# --backend go". Both now work, and the point of testing them HERE rather than
# in a C-only test is that the output has to match the other backends
# character for character — `[0, 1, 2]`, not Go's `[0 1 2]` or C's own idea.
print("\n── 11.27: arrays and optionals on the C backend ──")
_ALL = ('pyro', 'node', 'go', 'c')
agree("printing an int array", "int[] a = [0, 1, 2];\nprint(a);\n",
      backends=_ALL, expect="[0, 1, 2]")
agree("printing a string array", 'string[] s = ["a", "b"];\nprint(s);\n',
      backends=_ALL, expect="[a, b]")
agree("printing a bool array", "bool[] t = [true, false];\nprint(t);\n",
      backends=_ALL, expect="[true, false]")
agree("printing a number array", "number[] f = [1.5, 2.0];\nprint(f);\n",
      backends=_ALL, expect="[1.5, 2]")
agree("printing an empty array", "int[] e = [];\nprint(e);\n",
      backends=_ALL, expect="[]")
agree("to_string of an array", 'int[] a = [1, 2];\nprint("v: " + to_string(a));\n',
      backends=_ALL, expect="v: [1, 2]")

agree("an optional defaulting with ??", "int? a = null;\nprint(a ?? 7);\n",
      backends=_ALL, expect="7")
agree("a present optional", "int? b = 3;\nprint(b ?? 7);\nprint(b!);\n",
      backends=_ALL, expect="3\n3")
agree("a string optional", 'string? s = null;\nprint(s ?? "dflt");\n',
      backends=_ALL, expect="dflt")
agree("a number and a bool optional",
      "number? f = 2.5;\nbool? t = true;\nprint(f!);\nprint(t!);\n",
      backends=_ALL, expect="2.5\ntrue")
agree("comparing an optional against null",
      "int? x = null;\nint? y = 5;\nprint(x == null);\nprint(y == null);\n",
      backends=_ALL, expect="true\nfalse")

# `??` must evaluate its left side ONCE. Written as a plain conditional in C it
# would be evaluated twice, doubling any side effect in it.
agree("?? evaluates its left side once",
      "int calls = 0;\n"
      "fn bump() -> int? ={ calls = calls + 1; return null; }\n"
      "print(bump() ?? 9);\nprint(calls);\n",
      backends=('pyro', 'node'), expect="9\n1")

# Maps themselves are supported now — see the 11.27 section below, which
# retired the "a map is still refused" assertion rather than relaxing it. What
# is still refused is a map whose key or value has no C representation, and the
# message has to name the four types that work rather than only point at go.
_w = tempfile.mkdtemp(prefix='cryo_par_')
_r = _compile(_w, 'struct S { int a; }\nmap<string,S> m = {};\nprint(len(m));\n',
              'c', os.path.join(_w, 'm.c'))
check("a map of an unrepresentable value type is refused", _r.returncode != 0,
      (_r.stdout + _r.stderr)[:200])
check("and the message names the types that do work",
      'int, number, string or bool' in (_r.stdout + _r.stderr),
      (_r.stdout + _r.stderr)[:300])

# ── 11.4: or_else ──────────────────────────────────────────
# Deferred since 11.4 because the synthetic `fn(any, any) -> any` it lowers to
# would not compile on go — `any` did not reach a typed context there. 11.31
# fixed that, which is why this can exist at all, and why the arithmetic case
# below is the one that actually proves it: `or_else(a, 0) * 2` is where the
# old failure appeared.
print("\n── 11.4: or_else ──")
_R = "enum Result { Ok(int), Err(string) }\n"
agree("or_else on Ok yields the payload",
      _R + "Result a = Ok(5);\nprint(or_else(a, 0));\n", expect="5")
agree("or_else on Err yields the default",
      _R + 'Result b = Err("no");\nprint(or_else(b, 9));\n', expect="9")
agree("the result is usable in arithmetic",
      _R + "Result a = Ok(5);\nint x = or_else(a, 0);\nprint(x + 1);\n"
           "print(or_else(a, 0) * 2);\n", expect="6\n10")
# A wildcard arm, not `Err(e)`: an enum may have more than two variants and
# every non-Ok one should take the default rather than fall out of a
# non-exhaustive match.
agree("a third variant also takes the default",
      "enum R { Ok(int), Err(string), Timeout(int) }\n"
      "R t = Timeout(3);\nprint(or_else(t, 42));\n", expect="42")

_w2 = tempfile.mkdtemp(prefix='cryo_par_')
_r = _compile(_w2, _R + "Result a = Ok(1);\nprint(or_else(a));\n", 'pyro',
              os.path.join(_w2, 'oe.pyro'))
check("wrong arity is refused", _r.returncode != 0)
check("and the message says what the arguments are",
      'or_else takes 2' in (_r.stdout + _r.stderr), (_r.stdout + _r.stderr)[:200])

# The desugaring must not steal a name the program defines itself.
agree("a user function named or_else still wins",
      "fn or_else(int a, int b) -> int ={ return a + b; }\nprint(or_else(2, 3));\n",
      expect="5")

# ── 11.27: maps on the C backend ───────────────────────────
# The last of the three documented gaps. An open-addressed hash table in the C
# runtime — and the thing worth checking is not that it stores values but that
# it AGREES with the other backends about ORDER: both the VM and go render a
# map, and return keys(), sorted by the key's own TEXT. A C map iterating in
# bucket order would print the same program differently here, and differently
# again after an insertion resized the table.
print("\n── 11.27: maps on the C backend ──")
agree("reading a map", 'map<string,int> m = {"b": 2, "a": 1};\nprint(m["a"]);\n',
      backends=_ALL, expect="1")
agree("rendering a map", 'map<string,int> m = {"b": 2, "a": 1};\nprint(m);\n',
      backends=_ALL, expect="{a: 1, b: 2}")
agree("writing to a map",
      'map<string,int> m = {"a": 1};\nm["c"] = 3;\nprint(m);\nprint(len(m));\n',
      backends=_ALL, expect="{a: 1, c: 3}\n2")
agree("has()", 'map<string,int> m = {"a": 1};\nprint(has(m, "a"));\nprint(has(m, "z"));\n',
      backends=_ALL, expect="true\nfalse")
agree("keys()", 'map<string,int> m = {"b": 2, "a": 1};\nstring[] k = keys(m);\nprint(k);\n',
      backends=_ALL, expect="[a, b]")
agree("remove()", 'map<string,int> m = {"a": 1, "b": 2};\nremove(m, "a");\nprint(m);\n',
      backends=_ALL, expect="{b: 2}")
agree("an empty map", "map<string,int> m = {};\nprint(m);\nprint(len(m));\n",
      backends=_ALL, expect="{}\n0")
# Integer keys sort by TEXT, not numerically — 1, 10, 2. That looks wrong at a
# glance, which is exactly why it is asserted: it is what the VM does, and
# agreeing with it matters more than being intuitive.
agree("int keys order by their text, as on the VM",
      'map<int,string> a = {2: "two", 1: "one", 10: "ten"};\nprint(a);\n',
      backends=_ALL, expect="{1: one, 10: ten, 2: two}")
agree("number and bool values",
      'map<string,number> b = {"pi": 3.14};\nmap<string,bool> c = {"yes": true};\n'
      'print(b);\nprint(c);\n',
      backends=_ALL, expect="{pi: 3.14}\n{yes: true}")
agree("pairs() iteration",
      'map<string,int> m = {"b": 2, "a": 1};\n'
      'for (string k, int v in pairs(m)) { print(k); print(v); }\n',
      backends=_ALL, expect="a\n1\nb\n2")
# Deletion in an open-addressed table is where these go quietly wrong: leaving
# a hole breaks lookups for any key that probed PAST the removed slot. 500
# inserts force several growths, then half are removed.
agree("500 inserts and 250 removes stay consistent",
      "map<int,int> m = {};\n"
      "for (int i = 0; i < 500; i = i + 1) { m[i] = i * 2; }\n"
      "for (int i = 0; i < 500; i = i + 2) { remove(m, i); }\n"
      "int total = 0;\nint missing = 0;\n"
      "for (int i = 0; i < 500; i = i + 1) {\n"
      "    if (has(m, i)) { total = total + m[i]; }\n"
      "    else { missing = missing + 1; }\n}\n"
      "print(len(m));\nprint(total);\nprint(missing);\n",
      backends=_ALL, expect="250\n125000\n250")
# Array element assignment had no typed setter either, so it was refused
# alongside the maps.
agree("array element assignment",
      "int[] a = [1, 2, 3];\na[1] = 99;\nprint(a);\n",
      backends=_ALL, expect="[1, 99, 3]")

# ── 12.12: assert — when the message runs, and what it says ──
#
# Four divergences in one statement, every one found by RUNNING it rather than
# reading the generated code, which looked reasonable on all four backends.
print("\n── 12.12: assert ──")

# 1. LAZINESS. pyro, go and c evaluated the message on every assert; node did
#    not. An assertion that holds should cost nothing, and a message that is
#    only valid when the condition is true must not run on the passing path.
#    This case shows it directly: the message prints, and must not.
agree("a passing assert does not evaluate its message",
      'fn note() -> string ={ print("EVALUATED"); return "x"; }\n'
      'int n = 4;\nassert(n == 4, note());\nprint("done");\n',
      expect="done")

# 2. The failing path still reports the message, dynamically (12.8).
agree("a failing assert reports a dynamic message",
      'int n = 5;\ntry { assert(n == 4, "n was " + to_string(n)); }\n'
      'catch (string e) { print(e); }\n',
      expect="[Cryo Assert] n was 5")

# 3. The DEFAULT message carries the line number. node said "assert failed"
#    with no line, where every other backend named one.
agree("a message-less assert names its line",
      'int n = 5;\ntry { assert(n == 4); } catch (string e) { print(e); }\n',
      expect="[Cryo Assert] assert failed (line 2)")

# 4. The caught VALUE is the message string. node threw an Error OBJECT, so
#    `catch (string e)` bound an Error and printing it gave "{}" — a caught
#    assert said nothing about why it failed, and said it quietly.
agree("a caught assert binds the message, not an object",
      'int n = 5;\ntry { assert(n == 4, "boom"); }\n'
      'catch (string e) { print("caught: " + e); }\n',
      expect="caught: [Cryo Assert] boom")

agree("an assert that holds falls through",
      'int n = 4;\nassert(n == 4, "unused");\nprint("through");\n',
      expect="through")

# ── 12.13: one out-of-bounds message, not four ───────────────
#
# The same program aborted four different ways: the VM's English text, node's
# Portuguese one, the C runtime's third spelling with a different prefix, and
# go — which emitted no check at all and surfaced Go's own runtime panic.
#
# These run the failing program and compare the ABORT text, so they are written
# against a caught value where possible. On the VM a bounds abort is fail-fast
# and cannot be caught, so the comparison is on what reaches the output.
print("\n── 12.13: the out-of-bounds message ──")


def aborts_with(label, src, needle, backends=('pyro', 'node', 'go')):
    """Every available backend must abort with the same message text."""
    global _passed, _failed, _skipped
    work = tempfile.mkdtemp(prefix='cryo_oob_')
    got = {}
    for b in backends:
        o = run_backend(work, src, b, expect_fail=True)
        if o is None:
            _skipped += 1
            continue
        # The CRYO message, from the marker onwards. Each engine wraps its own
        # abort — the VM prefixes "[Pyro VM] ", Go prints "panic: ", node prints
        # nothing — and that envelope is the engine's to choose. What has to
        # agree is the message itself, which is what 12.13 is about.
        msg = None
        for l in o.splitlines():
            l = l.strip()
            if '[Cryo Security]' in l and 'throw' not in l:
                msg = l[l.index('[Cryo Security]'):]
                break
        got[b] = msg or f'<no message: {o.strip()[:80]}>'
    if len(got) < 2:
        print(f"  skip {label} (fewer than two backends available)")
        return
    detail = '; '.join(f"{b}={v!r}" for b, v in got.items())
    check(label + f"  [{', '.join(got)}]", len(set(got.values())) == 1, detail)
    check(label + " — and it is the VM's wording",
          all(needle in v for v in got.values()), detail)


aborts_with("array read out of range",
            'int[] a = [1, 2];\nint i = 5;\nprint(a[i]);\n',
            "[Cryo Security] IndexError: index 5 out of bounds (len=2)")
aborts_with("a negative index is out of range too",
            'int[] a = [1, 2];\nint i = 0 - 1;\nprint(a[i]);\n',
            "[Cryo Security] IndexError: index -1 out of bounds (len=2)")
# The VM's SET message carries no (len=…). That asymmetry is the shape to
# match, not to tidy up — a fourth spelling is the bug being fixed.
aborts_with("array write out of range",
            'int[] a = [1, 2];\nint i = 5;\na[i] = 9;\n',
            "[Cryo Security] IndexError: index 5 out of bounds")
aborts_with("a string index has its own wording",
            'string s = "ab";\nint i = 5;\nprint(s[i]);\n',
            "[Cryo Security] IndexError: string index out of bounds")

# And the checks must not have broken indexing that is in range.
agree("indexing in range still works",
      'int[] a = [1, 2, 3];\nint i = 1;\nprint(a[i]);\na[i] = 9;\nprint(a);\n'
      'string s = "abc";\nprint(s[0]);\n',
      expect="2\n[1, 9, 3]\na")

# ── 12.9: a payload-less enum member used as a value ─────────
#
# `enum E { A, B } E e = A;` ran on pyro and emitted a bare `A` elsewhere,
# where the member is declared `E_A`. It COMPILED and then failed at run time,
# which is exactly why 12.4's compile-only matrix reported it as supported —
# and why these run the program.
print("\n── 12.9: enum members as values ──")

# All three backends, go included. It was excluded for one round while its enum
# path was mid-rewrite elsewhere; the fix landed as a separate table for
# data-LESS enums, so it does not collide with the constructor path that
# rewrite is about.
_ENUM_BACKENDS = ('pyro', 'node', 'go')

agree("a bare payload-less member is a value",
      'enum E { A, B }\nE e = A;\nprint(e);\n',
      backends=_ENUM_BACKENDS, expect="0")
agree("and compares against its own name",
      'enum E { A, B }\nE e = B;\nif (e == B) { print("yes"); }\n',
      backends=_ENUM_BACKENDS, expect="yes")
agree("qualified access resolves to the same member",
      'enum Status { ATIVO, INATIVO }\nStatus s = Status.ATIVO;\nprint(s);\n',
      backends=_ENUM_BACKENDS, expect="0")
agree("qualified access compares too",
      'enum Status { ATIVO, INATIVO }\nStatus s = Status.INATIVO;\n'
      'if (s == Status.INATIVO) { print("match"); }\n',
      backends=_ENUM_BACKENDS, expect="match")
agree("a member drives a switch",
      'enum E { A, B }\nE e = B;\n'
      'switch (e) { case A: print("a"); case B: print("b"); }\n',
      backends=_ENUM_BACKENDS, expect="b")

# The resolution must not swallow an ordinary field read. A struct field can
# legitimately share a name with an enum member, and `p.RED` is the field.
agree("a struct field of the same name is still a field",
      'enum Col { RED }\nstruct P { int RED; }\nP p = new P { RED: 3 };\n'
      'int a = p.RED;\nCol c = RED;\nprint(a);\n',
      backends=_ENUM_BACKENDS, expect="3")
agree("a variable shadows the member",
      'enum E { A }\nint A2 = 9;\nprint(A2);\n',
      backends=_ENUM_BACKENDS, expect="9")

# Data-carrying enums keep working — their payload-less members are values of
# a different shape (a tag map), not integers.
agree("a payload-less member of a DATA enum is still its tag",
      'enum R { Ok(int), None }\nR r = None;\nprint(r);\n', expect="{tag: None}")

# ── ISSUES/17: replace() with an empty needle ────────────────
#
# Three engines, three answers: the Go VM inserted the replacement at every
# boundary (`-a-b-c-`), the C runtime special-cased an empty needle and gave
# the input back unchanged (`abc`), and node's split("").join(rep) inserted
# only BETWEEN characters (`a-b-c`). Each was locally reasonable; nobody had
# picked one, so the program meant three things.
#
# The VM's behaviour is canonical for runtime semantics, so `-a-b-c-` wins —
# which is also what Python's str.replace and Go's strings.ReplaceAll do.
# PYRO_RUNTIME.md states the rule. The expected values are asserted, not just
# agreement: node's answer differed from BOTH VMs, and a fix copied from the
# wrong engine would have made all four agree on the wrong string.
print("\n── ISSUES/17: replace() with an empty needle ──")
_R17 = ('pyro', 'node', 'go', 'c')
agree("an empty needle inserts at every boundary",
      'print(replace("abc", "", "-"));\n', backends=_R17, expect="-a-b-c-")
# The empty INPUT is the case the spec has to spell out: the result is the
# replacement, not "".
agree("an empty input yields the replacement itself",
      'print(replace("", "", "-"));\n', backends=_R17, expect="-")
agree("an empty replacement leaves the input alone",
      'print(replace("abc", "", ""));\n', backends=_R17, expect="abc")
agree("a multi-character replacement lands at every boundary",
      'print(replace("ab", "", "xy"));\n', backends=_R17, expect="xyaxybxy")
# And the ordinary needle must not have moved.
agree("an ordinary needle still replaces every occurrence",
      'print(replace("abab", "b", "-"));\nprint(replace("abc", "z", "-"));\n',
      backends=_R17, expect="a-a-\nabc")

# ── ISSUES/18: `== null` on a value that cannot be null ──────
#
# `valueEq` fell through to comparing the integer field, where a map, an array
# and `null` all carry 0 — so `m == null` was TRUE for a perfectly good map and
# the obvious guard `if (req == null) { continue; }` skipped every request. The
# VMs and node have been fixed; PYRO_RUNTIME.md settles the rule: null is equal
# only to null, and containers compare by REFERENCE identity.
#
# What that spec sentence also covers, and what was still broken, is the
# non-container half of it. `string s = ""; s == null` is false on both VMs and
# on node; on go the generated `("" == nil)` did not COMPILE, and on the C
# backend `(0 == NULL)` is folded by the C compiler to the wrong answer, TRUE,
# in code that builds clean. A by-value struct did not compile there either.
print("\n── ISSUES/18: container and scalar `== null` ──")
_N18 = ('pyro', 'node', 'go', 'c')

agree("a populated map is not null",
      'map<string,int> m = {"a": 1};\nprint(m == null);\nprint(m != null);\n',
      backends=_N18, expect="false\ntrue")
# The empty one is the case that reads most like null and is not.
agree("an empty map is not null either",
      "map<string,int> e = {};\nprint(e == null);\n", backends=_N18, expect="false")
agree("an array is not null",
      "int[] xs = [1];\nint[] ex = [];\nprint(xs == null);\nprint(ex == null);\n",
      backends=_N18, expect="false\nfalse")
agree("null is equal to null", "print(null == null);\n", backends=_N18, expect="true")

# The one place `== null` is the documented idiom must keep working.
agree("an unset optional is null, a set one is not",
      "int? x = null;\nint? y = 5;\nprint(x == null);\nprint(y == null);\n",
      backends=_N18, expect="true\nfalse")

# The scalars. Every one of these is a value with no null to be.
agree("a scalar is never null",
      'string s = "";\nint z = 0;\nnumber f = 0.0;\nbool b = false;\n'
      'print(s == null);\nprint(z == null);\nprint(f == null);\nprint(b == null);\n',
      backends=_N18, expect="false\nfalse\nfalse\nfalse")
agree("...and `!= null` is its inverse",
      'string s = "";\nint z = 0;\nprint(s != null);\nprint(z != null);\n',
      backends=_N18, expect="true\ntrue")
agree("a struct value is not null",
      "struct P { int x; }\nP p = new P { x: 1 };\nprint(p == null);\nprint(p != null);\n",
      backends=_N18, expect="false\ntrue")

# The container semantics PYRO_RUNTIME.md commits to: identity, not structure.
# Two arrays with equal contents are DIFFERENT arrays, and that is observable —
# which is exactly why it is written down and asserted rather than left to
# whichever engine is being read.
agree("containers compare by identity, not contents",
      "int[] a1 = [1];\nint[] a2 = [1];\nprint(a1 == a2);\nprint(a1 == a1);\n",
      backends=('pyro', 'node', 'go'), expect="false\ntrue")

print(f"\n{_passed} passed, {_failed} failed"
      + (f", {_skipped} backend runs skipped" if _skipped else ""))
sys.exit(1 if _failed else 0)
