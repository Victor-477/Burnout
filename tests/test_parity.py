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
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=900)
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
            r = subprocess.run([VM, out], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        elif backend == 'node':
            if not shutil.which('node'):
                return None
            out = os.path.join(work, 'p.js')
            if _compile(work, src, 'node', out).returncode != 0:
                return '<compile failed>'
            r = subprocess.run(['node', out], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        elif backend == 'go':
            if not shutil.which('go'):
                return None
            out = os.path.join(work, 'p.go')
            if _compile(work, src, 'go', out).returncode != 0:
                return '<compile failed>'
            # `go run`, not just codegen: 11.33 produced perfectly plausible Go
            # that the Go compiler then rejected.
            r = subprocess.run(['go', 'run', out], capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=900, cwd=work)
            if r.returncode != 0 and not expect_fail:
                return '<go build failed> ' + r.stderr.strip()[:160]
        elif backend == 'csharp':
            # The .NET SDK needs a project rather than a loose file; the
            # compiler builds one beside the .cs, and reusing that helper keeps
            # the test and the CLI on a single code path.
            if not shutil.which('dotnet'):
                return None
            out = os.path.join(work, 'p.cs')
            if _compile(work, src, 'csharp', out).returncode != 0:
                return '<compile failed>'
            sys.path.insert(0, os.path.join(ROOT, 'Burnout'))
            sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
            import compiler as _c
            proj = _c._csharp_project(out)
            if proj is None:
                return None
            r = subprocess.run(['dotnet', 'run', '--project', proj,
                                '-v', 'quiet', '--nologo'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600)
        elif backend == 'cpp':
            # find_cxx, not shutil.which: a MinGW that is installed but not on
            # PATH is the normal state of a Windows box, and the compiler
            # already knows where to look. Using which() here made the suite
            # skip cpp silently on a machine that can build it perfectly well.
            sys.path.insert(0, os.path.join(ROOT, 'Burnout'))
            sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
            import compiler as _cc
            if _cc.find_cxx()[0] is None:
                return None
            out = os.path.join(work, 'p.cpp')
            if _compile(work, src, 'cpp', out).returncode != 0:
                return '<compile failed>'
            exe = os.path.splitext(out)[0] + EXE
            if not os.path.isfile(exe):
                return '<compile failed>'
            r = subprocess.run([exe], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        elif backend == 'c':
            if not shutil.which('gcc'):
                return None
            out = os.path.join(work, 'p.c')
            cr = _compile(work, src, 'c', out)
            exe = os.path.splitext(out)[0] + EXE
            if cr.returncode != 0 or not os.path.isfile(exe):
                return '<compile failed>'
            r = subprocess.run([exe], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
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
    # A TIMEOUT IS NOT A DISAGREEMENT, and it used to read as one:
    # under memory pressure a `go run` hung for its full 900s and the
    # failure printed as "a string index has its own wording" with
    # go='<no message: <timeout>>' buried in the detail, so it looked
    # like a wording defect. The case completed in 1.1s on its own.
    # Still a failure — a hang is not a pass — but named.
    _stalled = [b for b, v in got.items() if '<timeout>' in str(v)]
    if _stalled:
        _who = ', '.join(_stalled)
        check(label + " — TIMED OUT on " + _who
              + " (a stall, not a disagreement; re-run it alone)",
              False, detail)
        return
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
# ISSUES/26 — this used to assert that a comprehension was REFUSED here.
# It is not any more: `int[] d = [v * 2 for v in xs]` states its own result
# type, so the lowering no longer invents `any` and the c backend compiles it
# (see the comprehension section at the end of this file, which runs it).
# Pinning the refusal would now be pinning the bug, the same way three smoke
# assertions pinned the C deny-list in ab20b8b.
#
# The `any` GAP is still real and still has to fail closed, so the case moves
# to a comprehension whose result type genuinely cannot be read: no
# declaration to take it from, and an element expression that is not just the
# loop variable.
_work = tempfile.mkdtemp(prefix='cryo_par_')
_r = _compile(_work, "int[] xs = [1, 2];\nany z = [v * 2 for v in xs];\nprint(z);\n",
              'c', os.path.join(_work, 'p.c'))
_msg = (_r.stdout + _r.stderr)
check("an unreadable comprehension type is still refused", _r.returncode != 0,
      _msg[:200])
check("with a Cryo diagnostic, not a gcc error",
      'CodeGen Error' in _msg and 'unknown type name' not in _msg, _msg[:300])
check("and the message still says where the `any` came from",
      'comprehension' in _msg, _msg[:300])
# The other surviving source of `any`: a loop variable the program did not
# declare. cpp's wording names both, and it is the message that tells a user
# what to do about it.
_r2 = _compile(_work, 'for (x in ["a", "b"]) { print(x); }\n',
               'c', os.path.join(_work, 'p2.c'))
_msg2 = (_r2.stdout + _r2.stderr)
check("an undeclared loop variable over a literal is refused too",
      _r2.returncode != 0 and 'CodeGen Error' in _msg2, _msg2[:200])
check("and that message says to give the variable a type",
      'declared type' in _msg2 or 'explicit type' in _msg2, _msg2[:300])

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
    # Same guard as agree(): a stall is not a message mismatch. This is the
    # function the misleading failure came out of — "a string index has its
    # own wording" with go='<no message: <timeout>>' reads as a wording
    # defect when the program actually runs in 1.1s.
    _stalled = [b for b, v in got.items() if '<timeout>' in str(v)]
    if _stalled:
        check(label + ' — TIMED OUT on ' + ', '.join(_stalled)
              + ' (a stall, not a disagreement; re-run it alone)',
              False, detail)
        return
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

# ── 13.5: string builtins lowered in the front end ───────────
#
# `lines`, `chars`, `title_case`, `trim_start` and `trim_end` add NO native.
# Each lowers to a synthetic Cryo function, which is why they are tested here
# rather than in a VM suite: the lowering is the feature, and what has to be
# true is that all four backends compute the same string from the same source.
# A new native would have needed six mirrored edits and could disagree; this
# cannot, because there is only one implementation and every backend compiles
# it.
print("\n── 13.5: string builtins with no new native ──")
_S135 = ('pyro', 'node', 'go', 'c')

# CRLF and a trailing newline are what text read off disk actually looks like.
# Splitting on "\n" alone leaves a "\r" on every line and a phantom empty line
# at the end — both are the kind of wrong that only shows up in the output.
agree("lines() handles CRLF and a trailing newline",
      'string[] l = lines("a\\r\\nb\\nc\\n");\nprint(l);\nprint(len(l));\n',
      backends=_S135, expect="[a, b, c]\n3")
agree("lines() of text without a trailing newline",
      'print(lines("one"));\n', backends=_S135, expect="[one]")
agree("lines() of the empty string has no lines",
      'print(lines(""));\nprint(len(lines("")));\n',
      backends=_S135, expect="[]\n0")
agree("chars() splits into single characters",
      'print(chars("abc"));\nprint(len(chars("")));\n',
      backends=_S135, expect="[a, b, c]\n0")
# Python's str.title(): the rest of the word goes DOWN, so "hELLO" is "Hello".
agree("title_case() upper-cases each word and lowers the rest",
      'print(title_case("hELLO wORLD"));\n', backends=_S135, expect="Hello World")
agree("title_case() treats every whitespace as a boundary, and keeps it",
      'print(title_case("a\\tb  c"));\n', backends=_S135, expect="A\tB  C")
agree("trim_start / trim_end strip one end each",
      'print("[" + trim_start("  hi  ") + "]");\n'
      'print("[" + trim_end("  hi  ") + "]");\n',
      backends=_S135, expect="[hi  ]\n[  hi]")
# The one that would rot silently: if these ever strip a different character
# set than trim(), nothing else in the suite would notice.
agree("...over the same character set trim() uses",
      'string s = " \\t\\r\\n x \\t\\r\\n ";\n'
      'print(trim_start(trim_end(s)) == trim(s));\n',
      backends=_S135, expect="true")
agree("an empty and an all-space string are handled",
      'print("[" + trim_start("") + "]");\nprint("[" + trim_end("   ") + "]");\n',
      backends=_S135, expect="[]\n[]")
# The `\r` escape, found by the test above. The lexer's escape table had n, t,
# \\, " and ', and its fallback turns an unknown escape into the character
# itself — so `"\r"` was the LETTER r and no literal could hold a carriage
# return. Both lexers were missing it, so the token streams still agreed and
# the 9.4 fixed point never noticed.
agree("\\r is a carriage return, not the letter r",
      'string s = "a\\rb";\nprint(len(s));\nprint(s == "arb");\nprint(s[1] == "\\r");\n',
      backends=_S135, expect="3\nfalse\ntrue")

# A program that declares its own must keep it — the lowering only fires for a
# name the program has not defined.
agree("a user function of the same name still wins",
      'fn chars(string s) -> int ={ return len(s); }\nprint(chars("abcd"));\n',
      backends=_S135, expect="4")


# ── C# and C++ backends: the two newest targets ──────────────
#
# Both are real output comparisons wherever their toolchain exists: `csharp`
# needs the .NET SDK, `cpp` a C++ compiler, and `agree()` skips a backend it
# cannot run rather than failing. On a machine with neither, these cases prove
# nothing — so read a green run together with the backend list each line prints.
print("\n── C# / C++ backends ──")
_NEW = ('pyro', 'node', 'go', 'csharp', 'cpp')

agree("scalars render identically",
      'int a = 5;\nnumber f = 2.5;\nstring s = "hi";\nbool b = true;\n'
      'print(a);\nprint(f);\nprint(s);\nprint(b);\n',
      backends=_NEW, expect="5\n2.5\nhi\ntrue")
# A float with no fractional part prints as an integer, and a locale with a
# comma decimal separator must not change that — the C# project pins
# InvariantGlobalization for exactly this line.
agree("a whole number prints without a fraction",
      "number[] f = [1.5, 2.0];\nprint(f);\nnumber g = 4.0;\nprint(g);\n",
      backends=_NEW, expect="[1.5, 2]\n4")
agree("containers render in the VM's notation",
      'int[] a = [0, 1, 2];\nstring[] s = ["x", "y"];\n'
      'map<string,int> m = {"b": 2, "a": 1};\n'
      'print(a);\nprint(s);\nprint(m);\nprint(keys(m));\n',
      backends=_NEW, expect="[0, 1, 2]\n[x, y]\n{a: 1, b: 2}\n[a, b]")
# Int keys order by TEXT, not numerically. It looks wrong and it is what the
# VM does; a Dictionary iterated in its own order would print 1, 2, 10.
agree("map keys order by their text",
      'map<int,string> m = {2: "two", 1: "one", 10: "ten"};\nprint(m);\n',
      backends=_NEW, expect="{1: one, 10: ten, 2: two}")
# A struct renders as a map of its field names (PYRO_RUNTIME 3.1). C# printed
# the TYPE NAME here until it was given a ToString — the same class of bug as
# Go's fmt.Sprint giving "[0 1 2]" for an array.
agree("a struct renders as a map of its fields",
      "struct P { int x; int y; }\nP p = new P { x: 1, y: 2 };\n"
      "print(p);\nprint(p.x);\n",
      backends=_NEW, expect="{x: 1, y: 2}\n1")
# 12.9 — a payload-less member is an INTEGER. A C# `enum` would have rendered
# "A", which is why they lower to constants.
agree("a payload-less enum member is its integer",
      "enum E { A, B }\nE e = A;\nprint(e);\nE f = B;\nprint(f);\n",
      backends=_NEW, expect="0\n1")
agree("optionals and ?? behave",
      'int? a = null;\nint? b = 3;\nprint(a ?? 7);\nprint(b ?? 7);\nprint(b!);\n'
      'print(a == null);\nprint(b == null);\n',
      backends=_NEW, expect="7\n3\n3\ntrue\nfalse")
agree("ISSUES/18 holds on the new backends too",
      'map<string,int> m = {"a": 1};\nint[] xs = [1];\nstring s = "";\nint z = 0;\n'
      'print(m == null);\nprint(xs == null);\nprint(s == null);\nprint(z == null);\n'
      'print(null == null);\n',
      backends=_NEW, expect="false\nfalse\nfalse\nfalse\ntrue")
agree("ISSUES/17 holds on the new backends too",
      'print(replace("abc", "", "-"));\nprint(replace("", "", "-"));\n',
      backends=_NEW, expect="-a-b-c-\n-")
agree("13.5's string builtins reach them as well",
      'print(lines("a\\r\\nb\\n"));\nprint(chars("abc"));\n'
      'print(title_case("hELLO wORLD"));\nprint("[" + trim_start("  hi") + "]");\n',
      backends=_NEW, expect="[a, b]\n[a, b, c]\nHello World\n[hi]")
agree("integer division truncates and modulo agrees",
      "int a = 7;\nint b = 2;\nprint(a / b);\nprint(a % b);\n"
      "print((0 - 7) / 2);\n",
      backends=_NEW, expect="3\n1\n-3")
agree("try/catch binds the message string",
      'try { throw("boom"); } catch (string e) { print("caught: " + e); }\n',
      backends=_NEW, expect="caught: boom")


# ── data-carrying enums on the C++ backend ───────────────────
#
# The cpp backend refused ADTs outright until now. It compiles them to a
# small class hierarchy behind shared_ptr — C++14 has no std::variant — so
# these RUN it against pyro rather than inspecting the emitted C++: the
# point is that the ANSWER agrees, and "it generated plausible C++" is the
# weaker claim 11.33 already showed can be false.
#
# cpp is named explicitly because the default set is pyro/node/go.
print("\n── data enums on cpp ──")
_CPPSET = ('pyro', 'cpp')

agree("a variant carrying a payload renders like everywhere else",
      'enum R { Ok(int), Err(string) }\nR r = Ok(5);\nprint(r);\n',
      backends=_CPPSET, expect="{tag: Ok, val0: 5}")

agree("a payload-less variant of a DATA enum is a value, not an int",
      'enum R { Ok(int), None }\nR r = None;\nprint(r);\n',
      backends=_CPPSET, expect="{tag: None}")

agree("both constructor spellings build the same value",
      'enum R { Ok(int), Err(string) }\nR a = Ok(7); R b = R_Ok(7);\nprint(a); print(b);\n',
      backends=_CPPSET, expect="{tag: Ok, val0: 7}\n{tag: Ok, val0: 7}")

agree("match binds the payload",
      'enum R { Ok(int), Err(string) }\nR r = Err(\"bad\");\nmatch r { Ok(v) => { print(v); } Err(e) => { print(\"err:\" + e); } }\n',
      backends=_CPPSET, expect="err:bad")

agree("a wildcard arm catches the rest",
      'enum R { Ok(int), Err(string) }\nR r = Err(\"x\");\nmatch r { Ok(v) => { print(\"ok\"); } _ => { print(\"other\"); } }\n',
      backends=_CPPSET, expect="other")

agree("several payloads bind in order",
      'enum P { Pt(int,int), Z }\nP p = Pt(3,4);\nprint(p);\nmatch p { Pt(x,y) => { print(x + y); } Z => { print(0); } }\n',
      backends=_CPPSET, expect="{tag: Pt, val0: 3, val1: 4}\n7")

# The subject goes into a temporary and is evaluated ONCE. An if/else
# chain over the raw expression would call mk() again per arm —
# silently, and visible only when the subject has a side effect.
agree("the match subject is evaluated once",
      'enum R { Ok(int), Err(string) }\nfn mk() -> R ={ print(\"called\"); return Ok(1); }\nmatch mk() { Ok(v) => { print(v); } Err(e) => { print(e); } }\n',
      backends=_CPPSET, expect="called\n1")

agree("match nests without the temporaries colliding",
      'enum R { Ok(int), Err(string) }\nR a = Ok(2);\nmatch a { Ok(v) => { match a { Ok(w) => { print(v + w); } _ => { print(0); } } } _ => { print(9); } }\n',
      backends=_CPPSET, expect="4")

agree("enums survive in an array",
      'enum R { Ok(int), Err(string) }\nR[] rs = [Ok(1), Err(\"x\")];\nfor (R r in rs) { match r { Ok(v) => { print(v); } Err(e) => { print(e); } } }\n',
      backends=_CPPSET, expect="1\nx")

# ── first-class functions on the C++ backend ─────────────────
#
# `fn(T)->R` maps to std::function and a lambda literal to a C++ lambda
# capturing BY VALUE. By value is not a style choice: a Cryo lambda can
# outlive the scope that built it — stored in a std::function and returned
# — and `[&]` would leave it holding references to dead locals. That fails
# later and elsewhere, never at the capture, so "returned closure" below is
# the case that actually tests it.
print("\n── first-class functions on cpp ──")

agree("a lambda literal runs",
      'fn(int)->int f = (int v) => v + 1;\nprint(f(41));\n',
      backends=_CPPSET, expect="42")

agree("a named function is a value",
      'fn dbl(int n) -> int ={ return n*2; }\nfn(int)->int f = dbl;\nprint(f(21));\n',
      backends=_CPPSET, expect="42")

agree("a lambda captures an enclosing local",
      'int base = 10;\nfn(int)->int f = (int v) => v + base;\nprint(f(5));\n',
      backends=_CPPSET, expect="15")

agree("a returned closure outlives the frame that built it",
      'fn mk(int b) -> fn(int)->int ={ return (int v) => v + b; }\nfn(int)->int g = mk(100);\nprint(g(5));\n',
      backends=_CPPSET, expect="105")

agree("a function is passed as a parameter",
      'fn apply(fn(int)->int f, int x) -> int ={ return f(x); }\nprint(apply((int v) => v * 3, 7));\n',
      backends=_CPPSET, expect="21")

agree("functions live in an array",
      '(fn(int)->int)[] ops = [(int v) => v + 1, (int v) => v * 2];\nprint(ops[0](10)); print(ops[1](10));\n',
      backends=_CPPSET, expect="11\n20")

agree("map / filter / reduce",
      'int[] xs=[1,2,3,4];\nprint(map(xs, (int v) => v * 2));\nprint(filter(xs, (int v) => v % 2 == 0));\nprint(reduce(xs, (int a, int b) => a + b, 0));\n',
      backends=_CPPSET, expect="[2, 4, 6, 8]\n[2, 4]\n10")

# ── optionals and `?` propagation on the C++ backend ─────────
#
# Two defects sat on top of each other here. The cpp backend never wrapped
# a bare value into an optional, so ANY function declared `-> T?` failed to
# compile — `return x;` emitted an int64_t where a shared_ptr was wanted.
# That made `?` over an optional untestable until it was fixed.
#
# `?` over a Result is compared against pyro; `?` over an OPTIONAL is
# compared against go, because pyro gets it wrong — see ISSUES/24.
print("\n── optionals and ? on cpp ──")
_CPPGO = ('go', 'cpp')

agree("a function may return a bare value as an optional",
      'fn f(int x) -> int? ={ return x; }\nprint(f(7) ?? 0);\n',
      backends=_CPPSET, expect="7")

agree("and may return null",
      'fn f(int x) -> int? ={ if (x > 0) { return x; } return null; }\nprint(f(7) ?? 0); print(f(-1) ?? 99);\n',
      backends=_CPPSET, expect="7\n99")

agree("`?` over a Result yields the payload",
      'enum Res { Ok(int), Err(string) }\nfn p(int x) -> Res ={ if (x > 0) { return Ok(x); } return Err(\"neg\"); }\nfn u(int x) -> Res ={ int v = p(x)?; return Ok(v * 10); }\nmatch u(5) { Ok(v) => { print(v); } Err(e) => { print(\"E:\" + e); } }\n',
      backends=_CPPSET, expect="50")

agree("`?` over a Result returns the error early",
      'enum Res { Ok(int), Err(string) }\nfn p(int x) -> Res ={ if (x > 0) { return Ok(x); } return Err(\"neg\"); }\nfn u(int x) -> Res ={ int v = p(x)?; return Ok(v * 10); }\nmatch u(-1) { Ok(v) => { print(v); } Err(e) => { print(\"E:\" + e); } }\n',
      backends=_CPPSET, expect="E:neg")

agree("`?` over an optional short-circuits on null",
      'fn lk(int x) -> int? ={ if (x > 0) { return x; } return null; }\nfn u(int x) -> int? ={ int v = lk(x)?; return v * 2; }\nprint(u(21) ?? 0); print(u(-1) ?? 0);\n',
      backends=_CPPGO, expect="42\n0")

# ── text helpers added to the language core ──────────────────
#
# These are DESUGARINGS in Cryo/parser.py, not natives: one Cryo
# implementation is injected as a synthetic function and every backend
# compiles it (golden rule 2). So the interesting property is not that any
# one backend is right — it is that a single source produced the same
# answer through six independent code generators.
#
# Each case pins a decision that could defensibly have gone the other way,
# because those are what a later "cleanup" would quietly change.
print("\n── text helpers (front-end desugarings) ──")

# The three fast backends for the per-helper cases, and ONE composite case on
# cpp below. Every helper is built from the same constructs — while, string
# indexing, len/substr/contains/find — so a construct cpp lacked would fail all
# of them alike; running ten g++ builds to learn that once is a poor trade in a
# suite people have to be willing to run.
_ALLTEXT = ('pyro', 'node', 'go')

agree("is_digit is whole-string, and empty is false",
      'print(is_digit("123")); print(is_digit("12a")); print(is_digit(""));\n',
      backends=_ALLTEXT, expect="true\nfalse\nfalse")

agree("is_alpha rejects a digit",
      'print(is_alpha("abc")); print(is_alpha("ab1"));\n',
      backends=_ALLTEXT, expect="true\nfalse")

agree("is_alnum accepts both, rejects punctuation",
      'print(is_alnum("a1")); print(is_alnum("a-1"));\n',
      backends=_ALLTEXT, expect="true\nfalse")

agree("is_space agrees with trim about whitespace",
      'print(is_space("  ")); print(is_space(" x"));\n',
      backends=_ALLTEXT, expect="true\nfalse")

agree("reverse_str",
      'print(reverse_str("cryo"));\n',
      backends=_ALLTEXT, expect="oyrc")

agree("replace_first replaces only the first",
      'print(replace_first("a-b-c", "-", "+"));\n',
      backends=_ALLTEXT, expect="a+b-c")

agree("replace_first with an empty needle returns the string unchanged",
      'print(replace_first("abc", "", "+"));\n',
      backends=_ALLTEXT, expect="abc")

agree("count_of does not count overlaps",
      'print(count_of("aaaa", "aa")); print(count_of("abc", "z"));\n',
      backends=_ALLTEXT, expect="2\n0")

agree("last_index_of finds the LAST, and -1 when absent",
      'print(last_index_of("a/b/c", "/")); print(last_index_of("abc", "z"));\n',
      backends=_ALLTEXT, expect="3\n-1")

agree("the helpers compose",
      'print(reverse_str(replace_first("ab-cd", "-", "+")));\n',
      backends=_ALLTEXT, expect="dc+ba")

# One composite case DOES go through cpp: it calls every helper, so the
# synthetic functions all get compiled by a real C++ compiler and their
# answers compared, at the cost of a single build.
agree("every text helper compiles and agrees on cpp too",
      'print(is_digit("12")); print(is_alpha("ab")); print(is_alnum("a1"));\nprint(is_space(" ")); print(reverse_str("ab"));\nprint(replace_first("a-b-c", "-", "+"));\nprint(count_of("aaaa", "aa")); print(last_index_of("a/b", "/"));\n',
      backends=('pyro', 'cpp'),
      expect="true\ntrue\ntrue\ntrue\nba\na+b-c\n2\n1")

# ── text helpers, second set: affixes, case, words ───────────
#
# Same mechanism as above, so the same trade: the per-helper cases run on the
# three fast backends and one composite case goes through cpp.
#
# Every case here pins a DECISION rather than an implementation. The ones most
# worth having in writing:
#   - is_blank("") is true while is_digit("") is false. Different questions:
#     "does every character qualify" is vacuous on "", "is there nothing of
#     substance here" is emphatically yes.
#   - capitalize lowercases the tail, so "mcDONALD" becomes "Mcdonald".
#   - strip_prefix returns the string unchanged on a miss and on an empty
#     affix, matching replace_first's empty-needle rule.
#   - word_count counts RUNS of non-whitespace, so repeated separators do not
#     invent empty words the way len(split(s, " ")) would.
print("\n── text helpers: affixes, case, words ──")

agree("is_blank is true for empty and for whitespace, false otherwise",
      'print(is_blank("")); print(is_blank("  \\t ")); print(is_blank(" x "));\n',
      backends=_ALLTEXT, expect="true\ntrue\nfalse")

agree("strip_prefix strips, and leaves a miss or an empty affix alone",
      'print(strip_prefix("foobar", "foo")); print(strip_prefix("foobar", "baz"));\nprint(strip_prefix("foobar", ""));\n',
      backends=_ALLTEXT, expect="bar\nfoobar\nfoobar")

agree("strip_suffix strips from the end only",
      'print(strip_suffix("foobar", "bar")); print(strip_suffix("foobar", "foo"));\n',
      backends=_ALLTEXT, expect="foo\nfoobar")

agree("capitalize lowercases the tail, and leaves empty empty",
      'print(capitalize("mcDONALD")); print(capitalize("x")); print("[" + capitalize("") + "]");\n',
      backends=_ALLTEXT, expect="Mcdonald\nX\n[]")

agree("equals_ignore_case",
      'print(equals_ignore_case("HeLLo", "hello")); print(equals_ignore_case("a", "b"));\n',
      backends=_ALLTEXT, expect="true\nfalse")

agree("replace_last replaces only the last",
      'print(replace_last("a-b-c", "-", "+"));\n',
      backends=_ALLTEXT, expect="a-b+c")

agree("replace_last leaves an empty needle and a miss alone",
      'print(replace_last("a-b-c", "", "+")); print(replace_last("abc", "-", "+"));\n',
      backends=_ALLTEXT, expect="a-b-c\nabc")

agree("word_count counts runs, not separators",
      'print(word_count("  one   two  three ")); print(word_count("solo")); print(word_count(""));\n',
      backends=_ALLTEXT, expect="3\n1\n0")

agree("common_prefix is character-wise, and empty when there is none",
      'print(common_prefix("prefix_a", "prefix_b")); print(common_prefix("abc", "abcdef"));\nprint("[" + common_prefix("abc", "xyz") + "]");\n',
      backends=_ALLTEXT, expect="prefix_\nabc\n[]")

agree("the second set composes with the first",
      'print(reverse_str(capitalize(strip_suffix("hello!", "!"))));\n',
      backends=_ALLTEXT, expect="olleH")

agree("every helper in the second set compiles and agrees on cpp too",
      'print(is_blank(" ")); print(strip_prefix("foobar", "foo"));\nprint(strip_suffix("foobar", "bar")); print(capitalize("mcDONALD"));\nprint(equals_ignore_case("A", "a")); print(replace_last("a-b-c", "-", "+"));\nprint(word_count("  one   two ")); print(common_prefix("abc", "abd"));\n',
      backends=('pyro', 'cpp'),
      expect="true\nbar\nfoo\nMcdonald\ntrue\na-b+c\n2\nab")

# ── a string index on the C backend ──────────────────────────
#
# The helpers above walk a string with `s[i]`, and the C backend compiled that
# to cryo_array_get() — the ARRAY accessor — which read an array header off a
# char pointer and SEGFAULTED (0xC0000005). It stayed invisible because no
# local gcc worked, so every `c` row in this suite was skipped; fixing the
# toolchain is what surfaced seven failures here at once.
#
# The second case is the one that matters most. The obvious repair is
# cryo_str_slice(s, i, i+1), and slice CLAMPS — so `"abc"[9]` would have been
# "" on c while every other engine aborts. An out-of-range read is the one
# direction a bounds check must not fail, so this pins the abort and not just
# the happy path.
print("\n── string indexing, and the C backend's array accessor ──")

agree("s[i] is a one-character string, on c as everywhere else",
      'string s = "abc"; print(s[0]); print(s[1]); print(s[2]);\n',
      backends=('pyro', 'node', 'go', 'c'), expect="a\nb\nc")

aborts_with("an out-of-range string index aborts rather than returning empty",
            'string s = "abc"; print(s[9]);\n',
            '[Cryo Security] IndexError: string index out of bounds',
            backends=('pyro', 'node', 'go', 'c'))

agree("the second set of helpers agrees on c too",
      'print(is_blank("  ")); print(capitalize("mcDONALD"));\nprint(strip_prefix("foobar", "foo")); print(replace_last("a-b-c", "-", "+"));\nprint(word_count("  one   two ")); print(common_prefix("abc", "abd"));\n',
      backends=('pyro', 'node', 'go', 'c'),
      expect="true\nMcdonald\nbar\na-b+c\n2\nab")

# ── nine builtins the C backend refused but could already compile ──
#
# clamp, sign, gcd, hypot, starts_with, ends_with, repeat, pad_start and
# pad_end were implemented by ISSUES/09 — runtime helpers, codegen dispatch
# and type inference, all present — and left on `_call`'s deny-list, which
# runs first. So the C backend refused nine builtins it could compile, and
# the code that compiles them was unreachable for the whole of Phase 10.4.
#
# Found because strip_prefix/strip_suffix lower to starts_with/ends_with: a
# front-end desugaring inherited a refusal that had no reason left.
#
# Two cases rather than nine, to keep this to two gcc builds: the nine share
# nothing but the deny-list, so a name still on it fails its group outright.
print("\n── builtins the C backend refused but had implemented ──")

agree("starts_with / ends_with / repeat / pad_start / pad_end on c",
      'print(starts_with("foobar", "foo")); print(ends_with("foobar", "bar"));\nprint(repeat("ab", 3)); print(pad_start("7", 3, "0")); print(pad_end("7", 3, "0"));\n',
      backends=('pyro', 'node', 'go', 'c'),
      expect="true\ntrue\nababab\n007\n700")

agree("clamp / sign / gcd / hypot on c",
      'print(clamp(5, 1, 3)); print(clamp(0, 1, 3)); print(sign(-7)); print(sign(0));\nprint(gcd(12, 18)); print(hypot(3.0, 4.0));\n',
      backends=('pyro', 'node', 'go', 'c'),
      expect="3\n1\n-1\n0\n6\n5")

# ── a comprehension containing a unary operator ──────────────
#
# `_extract_identifiers` (Cryo/parser.py), the walker that finds what a
# comprehension CAPTURES, read `node.expr` on a UnaryExpr whose field is
# `operand`. So `[-x for x in xs]` and `[x for x in xs if !flag]` aborted the
# compiler with "[Internal Error] 'UnaryExpr' object has no attribute 'expr'"
# — on input the analyser accepts. The neighbouring Spawn/Await/Unwrap/Try
# branch genuinely does use `expr`, which is how the wrong name looked right.
#
# These ran on pyro and node only until ISSUES/26 landed, because the go
# backend did not compile ANY list comprehension over an array — the
# synthetic function came out typed `any`/`[]any`. go is in the list now;
# changing the tuple was the only edit the note had promised.
#
# No parity case ran a comprehension on go before, which is why both bugs
# survived: test_smoke calls the generators directly, so "it generated
# plausible Go" is all it could check, and 11.33 is the standing proof that
# the claim can be false.
print("\n── comprehensions with unary operators ──")

agree("a comprehension negating its element",
      'int[] xs = [1, 2, 3]; int[] ys = [-x for x in xs]; print(ys);\n',
      backends=('pyro', 'node', 'go'), expect="[-1, -2, -3]")

agree("a comprehension whose guard is a negation",
      'int[] xs = [1, 2, 3]; bool f = false; int[] ys = [x for x in xs if !f];\nprint(ys);\n',
      backends=('pyro', 'node', 'go'), expect="[1, 2, 3]")

agree("and the guard really does filter",
      'int[] xs = [1, 2, 3]; bool f = true; int[] ys = [x for x in xs if !f];\nprint(ys);\n',
      backends=('pyro', 'node', 'go'), expect="[]")

# ── ISSUES/26: comprehensions, on every backend that has types ──────
#
# A comprehension lowers to a synthetic function, and every part of it used
# to be typed `any`:
#
#   fn __list_comp_1(any xs) -> any[] { any[] __res = []; … }
#
# pyro and node are dynamically typed, so `any` cost them nothing and the
# feature looked finished. go is not, and emitted Go the Go compiler
# rejected — "cannot use __list_comp_1(xs) (value of type []any) as []int64
# value" — while c and cpp refused the construct outright, having no `any` at
# all. A headline 10.3 feature worked on two backends out of six.
#
# The fix stopped the lowering saying `any` where the PROGRAM had already
# said something better: the declaration being initialised states the result
# type, a range is always int, and an array's element type types the loop
# variable. Nothing is inferred — where the knowledge runs out the lowering
# is unchanged, which is why nothing that worked before could start failing.
#
# These run on pyro/node/go per case, with ONE composite case through c and
# cpp: the constructs are shared, so a backend missing one fails all of them
# alike, and two more native builds per case is a poor trade in a suite
# people have to be willing to run.
print("\n── ISSUES/26: comprehensions across the typed backends ──")

_COMP = ('pyro', 'node', 'go')

agree("a comprehension over a named array",
      'int[] xs = [1, 2, 3]; int[] ys = [x for x in xs]; print(ys);\n',
      backends=_COMP, expect="[1, 2, 3]")

agree("…with an expression per element",
      'int[] xs = [1, 2, 3]; int[] ys = [x * 2 for x in xs]; print(ys);\n',
      backends=_COMP, expect="[2, 4, 6]")

agree("…over an array LITERAL, which is bound to a typed local first",
      'int[] ys = [x * 2 for x in [1, 2, 3, 4] if x % 2 == 0]; print(ys);\n',
      backends=_COMP, expect="[4, 8]")

agree("…over a range, whose loop variable no longer has to be written out",
      'int[] ys = [x * 2 for x in 1..=3]; print(ys);\n',
      backends=_COMP, expect="[2, 4, 6]")

agree("…with the loop variable declared, as before",
      'int[] ys = [x * x for int x in 1..=5 if x % 2 == 0]; print(ys);\n',
      backends=_COMP, expect="[4, 16]")

agree("…over strings, through a call",
      'string[] ss = ["a", "b"]; string[] up = [upper(s) for s in ss]; print(up);\n',
      backends=_COMP, expect="[A, B]")

# The literal's own contents may name things that DO have to be captured, so
# the capture scan reads the original expression and not the temp it is bound
# to. Getting this wrong passed `__it_1` in as a parameter.
agree("…over a literal built from captured names",
      'int a = 7; int b = 9; int[] ys = [v + 1 for v in [a, b]]; print(ys);\n',
      backends=_COMP, expect="[8, 10]")

agree("a map comprehension",
      'int[] xs = [1, 2, 3]; map<int,int> m = {x: x * 2 for x in xs}; print(m);\n',
      backends=_COMP, expect="{1: 2, 2: 4, 3: 6}")

agree("comprehensions compile and agree on c and cpp too",
      'int[] xs = [1, 2, 3];\nint[] a = [x for x in xs];\nint[] b = [x * 2 for x in [1, 2, 3, 4] if x % 2 == 0];\nint[] c = [x * 2 for x in 1..=3];\nmap<int,int> m = {x: x * 2 for x in xs};\nprint(a); print(b); print(c); print(m);\n',
      backends=('pyro', 'c', 'cpp'),
      expect="[1, 2, 3]\n[4, 8]\n[2, 4, 6]\n{1: 2, 2: 4, 3: 6}")

# ── the two defects the literal-binding fix also closed ─────
#
# `for (int x in [1, 2, 3])` is not a comprehension, and it was broken in the
# same place for the same reason: the c backend has no inline array literal
# as an expression ("expected expression before ')'"), and cpp emitted
# `for (… : *(cryo::arr<…>(…)))`, dereferencing a temporary shared_ptr whose
# vector was already freed — so it printed GARBAGE rather than failing to
# build. pyro, node and go were fine, so the two engines that disagreed were
# the two nobody could test without a C compiler.
agree("a plain for-each over an array literal",
      'for (int x in [1, 2, 3]) { print(x); }\n',
      backends=('pyro', 'node', 'go', 'c', 'cpp'), expect="1\n2\n3")

# A module-level map initialised from anything that is not a literal lost its
# initialiser on the c backend: `cryo_map_new()` was emitted and the
# expression discarded, so this printed `{}` there and the right thing
# everywhere else. `_var` had the guard for locals; `_module_var` did not.
# Not a build failure and not a refusal — a wrong answer.
agree("a module-level map copied from another map",
      'map<int,int> a = {1: 2, 3: 4}; map<int,int> b = a; print(b);\n',
      backends=('pyro', 'node', 'go', 'c'), expect="{1: 2, 3: 4}")

agree("a module-level map from a function's return",
      'fn mk() -> map<int,int> ={ map<int,int> r = {}; r[5] = 6; return r; }\nmap<int,int> c = mk(); print(c);\n',
      backends=('pyro', 'node', 'go', 'c'), expect="{5: 6}")

# ── C4: parsing that returns instead of killing the process ──
#
# to_int("abc") and to_number("abc") ABORT, and the abort is not catchable:
# `try { to_int(s) } catch (string e)` does not run, the process ends. For a
# server parsing user input that is a denial of service — one bad form field
# ends the request loop for every tenant.
#
# parse_int/parse_number are front-end desugarings returning an optional, so
# `parse_int(s) ?? 0` is the whole idiom. The validator is deliberately a
# SUBSET of the native's accept set: more permissive and the call it guards
# would abort, which is the thing being fixed.
#
# NOT on the c backend, and that is a real gap rather than an omission: the
# C backend refuses to_int()/to_number() on a string outright, because its
# runtime has no parse helper, so the desugaring has nothing to lower onto
# there. Recorded as ISSUES/27.
print("\n── C4: parse_int / parse_number ──")

_PARSE = ('pyro', 'node', 'go')

agree("parse_int takes what to_int takes",
      'print(parse_int("42") ?? -1); print(parse_int("-42") ?? -1);\nprint(parse_int("+42") ?? -1); print(parse_int(" 7 ") ?? -1);\nprint(parse_int("007") ?? -1); print(parse_int("0") ?? -1);\n',
      backends=_PARSE, expect="42\n-42\n42\n7\n7\n0")

agree("…and returns null for what it does not, instead of aborting",
      'print(parse_int("") ?? -1); print(parse_int("abc") ?? -1);\nprint(parse_int("4.2") ?? -1); print(parse_int("0x10") ?? -1);\nprint(parse_int("-") ?? -1);\n',
      backends=_PARSE, expect="-1\n-1\n-1\n-1\n-1")

# The overflow half, which is the part a character-only validator would miss:
# to_int aborts on 99999999999999999999 exactly as it does on "abc".
#
# pyro and go ONLY. Not a parse_int defect — the node backend cannot
# represent int64 at all, because a Cryo `int` is a JavaScript number there:
# `int a = 9223372036854775807; print(a)` already prints
# 9223372036854775000 with no conversion involved. Recorded as ISSUES/28.
agree("the exact int64 limits parse, and one past them does not",
      'print(parse_int("9223372036854775807") ?? -1);\nprint(parse_int("9223372036854775808") ?? -1);\nprint(parse_int("-9223372036854775808") ?? -1);\nprint(parse_int("-9223372036854775809") ?? -1);\nprint(parse_int("99999999999999999999") ?? -1);\n',
      backends=('pyro', 'go'),
      expect="9223372036854775807\n-1\n-9223372036854775808\n-1\n-1")

# Leading zeros are stripped BEFORE the length test, or a padded field would
# read as an overflow purely for being long.
agree("a zero-padded number is its value, not an overflow",
      'print(parse_int("00000000000000000000042") ?? -1);\n',
      backends=_PARSE, expect="42")

agree("parse_number takes plain decimals",
      'print(parse_number("4.2") ?? -1.0); print(parse_number("-3.5") ?? -1.0);\nprint(parse_number(".5") ?? -1.0); print(parse_number("5.") ?? -1.0);\n',
      backends=_PARSE, expect="4.2\n-3.5\n0.5\n5")

# Narrower than to_number ON PURPOSE: no exponent, no NaN/Inf. to_number
# rejects "1e999" for being out of range, and reproducing that here would
# mean encoding the double range in the validator.
agree("…and refuses everything else, exponents included",
      'print(parse_number("abc") ?? -1.0); print(parse_number("1e3") ?? -1.0);\nprint(parse_number("1.2.3") ?? -1.0); print(parse_number(".") ?? -1.0);\nprint(parse_number("") ?? -1.0);\n',
      backends=_PARSE, expect="-1\n-1\n-1\n-1\n-1")

agree("parse_int and parse_number reach cpp too",
      'print(parse_int("42") ?? -1); print(parse_int("abc") ?? -1);\nprint(parse_number("4.2") ?? -1.0); print(parse_number("x") ?? -1.0);\n',
      backends=('pyro', 'cpp'), expect="42\n-1\n4.2\n-1")

# The whole point: the program keeps running. An abort would end it before
# the second line, so the second line IS the assertion.
agree("a rejected parse does not end the program",
      'int v = parse_int("nao-e-int") ?? 0;\nprint(v); print("ainda vivo");\n',
      backends=_PARSE, expect="0\nainda vivo")

# ── C1: checked_add / checked_sub / checked_mul ──────────────
#
# `a + b` itself still means two things — the compiled backends abort and
# both VMs wrap silently — and that is ISSUES/29, which needs a safe-mode
# bit the .pyro format does not have. These are requirement C1's other
# accepted answer, and they agree everywhere today.
#
# The cases are BOUNDARIES, not samples. A guard that is one off at the
# limit is indistinguishable from a correct one on ordinary input, and
# ordinary input is all anybody tries.
print("\n── C1: checked arithmetic ──")

_CK = ('pyro', 'node', 'go')
_MXMN = 'int MX = 9223372036854775807;\nint MN = -9223372036854775807 - 1;\n'

agree("checked_add at and past the top",
      _MXMN + 'print(checked_add(2, 3) ?? -999); print(checked_add(MX, 0) ?? -999);\nprint(checked_add(MX, 1) ?? -999);\n',
      backends=('pyro', 'go'), expect="5\n9223372036854775807\n-999")

agree("checked_add at and past the bottom",
      _MXMN + 'print(checked_add(MN, 1) ?? -999); print(checked_add(MN, -1) ?? -999);\n',
      backends=('pyro', 'go'), expect="-9223372036854775807\n-999")

agree("checked_sub at both ends",
      _MXMN + 'print(checked_sub(10, 3) ?? -999); print(checked_sub(MN, 1) ?? -999);\nprint(checked_sub(MX, -1) ?? -999); print(checked_sub(0, MN) ?? -999);\n',
      backends=('pyro', 'go'), expect="7\n-999\n-999\n-999")

# MIN * -1 is the case a guard written over absolute values gets wrong,
# because abs(MIN) is itself the overflow. MIN * 1 is NOT an overflow and
# must come back.
agree("checked_mul and the MIN * -1 asymmetry",
      _MXMN + 'print(checked_mul(6, 7) ?? -999); print(checked_mul(MX, 2) ?? -999);\nprint(checked_mul(MN, -1) ?? -999); print(checked_mul(-1, MN) ?? -999);\nprint(checked_mul(MN, 1) ?? -999);\n',
      backends=('pyro', 'go'),
      expect="42\n-999\n-999\n-999\n-9223372036854775808")

# The sharpest pair in the whole set: -2^62 * 2 is EXACTLY MIN and must be
# allowed, one step further must not. A conservative guard passes every
# other case here and fails this one.
agree("the guard is exact at MIN, not merely conservative",
      'print(checked_mul(-4611686018427387904, 2) ?? -999);\nprint(checked_mul(-4611686018427387905, 2) ?? -999);\nprint(checked_mul(4611686018427387903, 2) ?? -999);\nprint(checked_mul(4611686018427387904, 2) ?? -999);\n',
      backends=('pyro', 'go'),
      expect="-9223372036854775808\n-999\n9223372036854775806\n-999")

agree("zero short-circuits before any sign analysis",
      _MXMN + 'print(checked_mul(0, MN) ?? -999); print(checked_mul(MN, 0) ?? -999);\n',
      backends=('pyro', 'go'), expect="0\n0")

agree("the sign combinations",
      'print(checked_mul(-3, -4) ?? -999); print(checked_mul(3, -4) ?? -999);\nprint(checked_mul(-3, 4) ?? -999); print(checked_add(-5, -7) ?? -999);\n',
      backends=_CK, expect="12\n-12\n-12\n-12")

agree("checked arithmetic reaches cpp too",
      'print(checked_add(2, 3) ?? -999); print(checked_mul(6, 7) ?? -999);\nprint(checked_mul(4611686018427387904, 2) ?? -999);\n',
      backends=('pyro', 'cpp'), expect="5\n42\n-999")

# ── the text helpers on text that is not ASCII ───────────────
#
# Every text case above is ASCII, which is exactly how four of these
# helpers shipped corrupting UTF-8. They indexed with s[i] — a BYTE — so
# reverse_str("Ação") came back as "o\xc2\xa3\xc3\x83A" and
# common_prefix("ação", "açúcar") as the bytes 61 C3 A7 C3: a dangling lead
# byte, because ã and ú SHARE one and the loop stopped between the halves of
# a character. Fixed by walking split(s, ""), which is character-aware.
#
# These cases are the ones that were missing. The output is compared across
# backends as usual, so a backend whose split() is byte-oriented fails here
# rather than silently producing different mojibake.
print("\n── text helpers on non-ASCII input ──")

_U8 = ('pyro', 'node', 'go', 'c', 'cpp')

agree("reverse_str reverses characters, not bytes",
      u'print(reverse_str("Ação"));\n',
      backends=_U8, expect=u"oãçA")

agree("capitalize upper-cases a multi-byte first character",
      u'print(capitalize("ótimo"));\n',
      backends=_U8, expect=u"Ótimo")

agree("title_case too",
      u'print(title_case("ação ótima"));\n',
      backends=_U8, expect=u"Ação Ótima")

# The case that would not have been predicted by reading the loop: a shared
# LEAD byte. ã is C3 A3 and ú is C3 BA, so a byte-wise comparison matches
# the C3 and then stops, returning half a character.
agree("common_prefix stops at a character, not inside one",
      u'print("[" + common_prefix("ação", "açúcar") + "]");\n',
      backends=_U8, expect=u"[aç]")

agree("…and the result is a whole number of characters",
      u'print(char_len(common_prefix("ação", "açúcar")));\n',
      backends=_U8, expect="2")

# split("", ...) is the primitive all four now rest on, so pin that it is
# character-aware rather than trusting it.
agree("split with an empty separator yields characters",
      u'string[] cs = split("Ação", ""); print(len(cs)); print(cs);\n',
      backends=_U8, expect=u"4\n[A, ç, ã, o]")

agree("chars() agrees with it",
      u'print(len(chars("Ação")));\n',
      backends=_U8, expect="4")

# The byte-oriented natives, pinned as they ARE rather than as D1 wants
# them, so that ISSUES/30 changing them is a visible decision and not a
# silent one.
# node is EXCLUDED here and that IS the finding, not an exemption: len() on
# a string counts CHARACTERS on node (11) and BYTES on the other four (14).
# Both are asserted, so ISSUES/30 cannot be settled by changing one of them
# without this failing.
agree("len() counts BYTES on pyro and go — ISSUES/30",
      u'print(len("Ação Ônibus"));\n',
      backends=('pyro', 'go'), expect="14")

# NOT agree(): it needs two backends to compare and SILENTLY SKIPS with
# fewer, so this read as a passing assertion about node while running
# nothing at all. One backend means asserting its output directly.
_n30 = tempfile.mkdtemp(prefix='cryo_par_')
_n30_out = run_backend(_n30, u'print(len("Ação Ônibus"));\n', 'node')
if _n30_out is None:
    print("  skip len() on node (node not available)")
else:
    check("…and CHARACTERS on node, which is the divergence",
          _n30_out == "11", repr(_n30_out))

# The classifiers stay byte-wise, and that is safe: every byte of a
# multi-byte character is >= 0x80, so it can never match an ASCII set and
# there are no false positives.
agree("is_alpha is ASCII-defined, and says so consistently",
      u'print(is_alpha("ação")); print(is_alpha("acao"));\n',
      backends=_U8, expect="false\ntrue")

# cpp gets reverse_str and common_prefix, which are pure character walks,
# but NOT capitalize: upper() is ASCII-only on c and cpp, so "ótimo" comes
# back unchanged there (ISSUES/31). Asserting the casing here would be
# asserting a bug away; asserting the character walk is the real question,
# since that is what the split fix changed.
agree("the character walks reach cpp too",
      u'print(reverse_str("Ação"));\nprint("[" + common_prefix("ação", "açúcar") + "]");\n',
      backends=('pyro', 'cpp'), expect=u"oãçA\n[aç]")
# ── D1 (part): counting and slicing by character ─────────────
#
# len() counts BYTES and that is ISSUES/30, asserted as it is just above.
# These are the additive half: split(s, "") is already character-aware, so
# char_len/char_at/char_slice are desugarings over it.
#
# The ERP case this exists for: NF-e field limits are stated in characters,
# so len() rejects a legitimate accented product name early and substr()
# can cut a character in half.
print("\n── D1: char_len / char_at / char_slice ──")

agree("char_len counts characters where len counts bytes",
      u'print(char_len("Ação Ônibus")); print(len("Ação Ônibus"));\nprint(char_len("")); print(char_len("abc"));\n',
      backends=('pyro', 'go'), expect="11\n14\n0\n3")

agree("char_len agrees everywhere, whatever len() does",
      u'print(char_len("Ação Ônibus")); print(char_len("")); print(char_len("abc"));\n',
      backends=_U8, expect="11\n0\n3")

agree("char_at reaches a multi-byte character",
      u'print(char_at("Ação", 0)); print(char_at("Ação", 2));\n',
      backends=_U8, expect=u"A\nã")

agree("char_slice does not cut a character in half",
      u'print("[" + char_slice("Ação Ônibus", 0, 4) + "]");\nprint("[" + char_slice("Ação", 1, 2) + "]");\n',
      backends=_U8, expect=u"[Ação]\n[çã]")

# Clamping, like substr: past the end, before the start, negative length.
agree("char_slice clamps in every direction",
      u'print("[" + char_slice("Ação", 2, 99) + "]");\nprint("[" + char_slice("Ação", 99, 2) + "]");\nprint("[" + char_slice("Ação", -3, 2) + "]");\nprint("[" + char_slice("Ação", 1, -5) + "]");\n',
      backends=_U8, expect=u"[ão]\n[]\n[Aç]\n[]")

# char_at ABORTS out of range, as s[i] does. Returning "" would be the
# fail-open answer to a read past the end.
aborts_with("char_at past the end aborts, it does not return empty",
            u'print(char_at("Ação", 9));\n',
            '[Cryo Security] IndexError',
            backends=_U8)

agree("the character helpers reach cpp too",
      u'print(char_len("Ação")); print(char_at("Ação", 2));\nprint("[" + char_slice("Ação", 1, 2) + "]");\n',
      backends=('pyro', 'cpp'), expect=u"4\nã\n[çã]")

# ── ISSUES/22: thrown null renders as "null" ──────────────────
print("\n── ISSUES/22: thrown null renders as null ──")
agree("thrown null renders as null (not empty map/struct)",
      'try { throw(null); } catch (string e) { print("caught: " + e); }\n',
      backends=('pyro', 'node', 'go'), expect="caught: null")

# ── ISSUES/24: ? on optional ──────────────────────────────────
print("\n── ISSUES/24: ? propagation over an optional ──")
agree("? propagation over an optional agrees across backends",
      'fn lk(int x) -> int? ={ if (x > 0) { return x; } return null; }\n'
      'fn u(int x) -> int? ={ int v = lk(x)?; return v * 2; }\n'
      'print(u(21) ?? 0);\n'
      'print(u(-5) ?? 0);\n',
      backends=('pyro', 'node', 'go', 'cpp'), expect="42\n0")

# ── go: an untyped array literal takes its type from the slot ──
# `xs = [];` and `concat(xs, ["a"])` on a `string[]` came out as `[]any{…}`
# on go, which Go will neither assign to a []string nor unify with one in the
# generic cryoConcat. Found by the EMP's event queue (nucleo/eventos.cryo).
print("\n── go: array literals typed by their context ──")
agree("[] assigned to a typed array, and concat with a literal",
      'string[] xs = [];\n'
      'fn f() -> int ={ xs = concat(xs, ["a"]); int n = len(xs); xs = []; return n + len(xs); }\n'
      'print(f());\n',
      expect="1")
agree("concat of two literals into a typed slot",
      'string[] s = concat(["x"], ["y"]);\nprint(s);\n'
      'int[] n = [];\nn = concat([1], n);\nprint(n);\n',
      expect="[x, y]\n[1]")
agree("an array literal as a typed call argument",
      'fn total(int[] v) -> int ={ if (len(v) == 0) { return 0; } return v[0] + len(v); }\n'
      'print(total([]));\nprint(total([2, 3]));\n',
      expect="0\n4")

# ── go: module vars initialised in source order ──
# Go runs package-level initialisers before main(), so a module var declared
# AFTER a top-level statement saw the state from before that statement ran.
print("\n── go: module var initialisers run in source order ──")
agree("a module var sees the statements above it",
      'int[] a = [];\na.push(1);\nint[] b = a;\nprint(len(b));\n',
      expect="1")
agree("…including a call with side effects, and functions still see it",
      'int n = 1;\nfn bump() -> int ={ n = n + 1; return n; }\n'
      'print("start");\nint m = bump();\nfn get_m() -> int ={ return m; }\n'
      'map<string,int> d = {"a": n};\nprint(get_m());\nprint(d["a"]);\n',
      expect="start\n2\n2")

# ── M1: an accented path, as the FILESYSTEM sees it ──────────
#
# This cannot be an agree() case, and that is the whole point. The C
# runtimes passed a UTF-8 path to fopen/stat, which take the process ANSI
# codepage on Windows, so write_file("Produção.txt", …) created
# "ProduÃ§Ã£o.txt" on disk — and then file_exists, read_file and file_size
# ALL AGREED, because they mangled the name the same way. Every assertion a
# program can make about its own file passed, before the fix and after it.
#
# So the test has to look at the DIRECTORY, from outside the program. Each
# backend writes into a directory of its own and the name on disk is
# compared — to the other backends and to the literal the program used.
print("\n── M1: accented paths reach the filesystem intact ──")

_M1_NAME = u"Produ\u00e7\u00e3o.txt"
_m1_seen = {}
for _b in ('pyro', 'node', 'go', 'c'):
    _d = tempfile.mkdtemp(prefix='cryo_m1_')
    _p = (_d + '/' + _M1_NAME).replace('\\', '/')
    _src = (u'string p = "%s";\n' % _p
            + u'print(write_file(p, "conteudo"));\n'
            + u'print(read_file(p));\n')
    _o = run_backend(_d, _src, _b)
    if _o is None:
        _skipped += 1
        continue
    # The program's own view, which was never the problem.
    check("M1/%s: the program's round trip works" % _b,
          _o == "true\nconteudo", repr(_o))
    # The filesystem's view, which was.
    _names = [n for n in os.listdir(_d) if n.startswith('Produ')]
    _m1_seen[_b] = _names[0] if len(_names) == 1 else repr(_names)

if len(_m1_seen) >= 2:
    _detail = '; '.join('%s=%r' % kv for kv in _m1_seen.items())
    check("M1: every backend writes the SAME name on disk",
          len(set(_m1_seen.values())) == 1, _detail)
    check("M1: and it is the name the program asked for",
          all(v == _M1_NAME for v in _m1_seen.values()), _detail)
else:
    print("  skip M1 (fewer than two backends available)")

# ── url_decode / url_encode beyond pyro ──
# Natives 65/66 existed only on the VMs. go failed to COMPILE any program
# that used them ("undefined: url_decode") and node threw a ReferenceError
# only when the call ran — the EMP's form and contract parsers hit both.
# Both VMs work on UTF-8 BYTES, '+' is a space, and a malformed escape
# passes through; node's decodeURIComponent would throw on that one.
print("\n── url_decode / url_encode on go and node ──")
agree("url_decode: '+', %20 and a multi-byte character",
      'print(url_decode("a+b%20c"));\nprint(url_decode("Jo%C3%A3o"));\n'
      'print(url_decode("%41%42"));\n',
      expect=u"a b c\nJoão\nAB")
agree("url_decode: a malformed or truncated escape passes through",
      'print(url_decode("100%zz%"));\nprint(url_decode("%4"));\n'
      'print(url_decode("x%g1y"));\n',
      expect="100%zz%\n%4\nx%g1y")
agree("url_encode: the unreserved set, upper-case hex, UTF-8 bytes",
      u'print(url_encode("a b&c=d/ã~_.-"));\n',
      expect="a%20b%26c%3Dd%2F%C3%A3~_.-")
agree("url_encode then url_decode is the identity",
      u'string s = "ação + 50% off?";\nprint(url_decode(url_encode(s)) == s);\n'
      u'print(url_decode(url_encode(s)));\n',
      expect=u"true\nação + 50% off?")

# ── round() breaks ties AWAY FROM ZERO, on every backend ────
#
# node mapped round() straight to JavaScript's Math.round, which breaks ties
# toward +Infinity: Math.round(-0.5) is -0, Math.round(-1.5) is -1 and
# Math.round(-2.5) is -2. The VM (Go's math.Round), go, c and cpp all round
# half away from zero, so node gave a different answer for EVERY negative
# half — and for an ERP that is a monetary divergence, since credits,
# returns and discounts are the negative numbers in the system.
#
# The positive halves agreed all along, which is why this survived: the
# obvious cases to try are 0.5, 1.5, 2.5.
print("\n── round(): ties away from zero ──")

_RND = ('pyro', 'node', 'go')

agree("positive halves round up",
      'print(round(0.5)); print(round(1.5)); print(round(2.5));\n',
      backends=_RND, expect="1\n2\n3")

agree("NEGATIVE halves round away from zero, not toward +Inf",
      'print(round(-0.5)); print(round(-1.5)); print(round(-2.5));\n',
      backends=_RND, expect="-1\n-2\n-3")

agree("a value that is not a tie is unaffected",
      'print(round(2.675)); print(round(3.0)); print(round(-3.2));\n',
      backends=_RND, expect="3\n3\n-3")

agree("floor and ceil were never in question, and still are not",
      'print(floor(-2.5)); print(ceil(-2.5)); print(floor(2.5)); print(ceil(2.5));\n',
      backends=_RND, expect="-3\n-2\n2\n3")

agree("round reaches c and cpp with the same rule",
      'print(round(2.5)); print(round(-2.5)); print(round(-0.5));\n',
      backends=('pyro', 'c', 'cpp'), expect="3\n-3\n-1")

# ── L-07: struct field assignment across all backends & engines ──
print("\n── L-07: struct field assignment (s.campo = expr) ──")

_L07_SIMPLE = (
    'struct Sensor { int id; number valor; }\n'
    'Sensor s = Sensor { id: 1, valor: 36.6 };\n'
    's.valor = 40.0;\n'
    'print(s.valor);\n'
)
agree("L-07: simple struct field assignment (Sensor s; s.valor = 40.0)",
      _L07_SIMPLE,
      backends=('pyro', 'node', 'go', 'c', 'cpp', 'csharp'),
      expect="40")

# Also verify C VM and AOT native on the canonical Sensor reproducer
_C_VM = os.path.join(ROOT, 'build', 'pyrovm' + EXE)
_PYRO_CLI = os.path.join(ROOT, 'Burnout', 'pyro.py')
_l07_work = tempfile.mkdtemp(prefix='cryo_l07_')
if os.path.isfile(_C_VM):
    _p_out = os.path.join(_l07_work, 'sensor.pyro')
    if _compile(_l07_work, _L07_SIMPLE, 'pyro', _p_out).returncode == 0:
        _r_cvm = subprocess.run([_C_VM, _p_out], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=60)
        check("L-07: C VM prints 40 on Sensor reproducer",
              _r_cvm.returncode == 0 and _r_cvm.stdout.strip() == "40",
              _r_cvm.stdout + _r_cvm.stderr)
if shutil.which('gcc'):
    _s_cryo = os.path.join(_l07_work, 'sensor.cryo')
    with open(_s_cryo, 'w', encoding='utf-8') as _f:
        _f.write(_L07_SIMPLE)
    _aot_exe = os.path.join(_l07_work, 'sensor_aot' + EXE)
    _b_aot = subprocess.run([sys.executable, _PYRO_CLI, 'build', _s_cryo, '-o', _aot_exe],
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=300)
    if _b_aot.returncode == 0 and os.path.isfile(_aot_exe):
        _r_aot = subprocess.run([_aot_exe], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=60)
        check("L-07: AOT native binary prints 40 on Sensor reproducer",
              _r_aot.returncode == 0 and _r_aot.stdout.strip() == "40",
              _r_aot.stdout + _r_aot.stderr)
    else:
        check("L-07: AOT native binary builds for Sensor reproducer", False,
              (_b_aot.stdout + _b_aot.stderr)[-300:])

agree("L-07: compound assignments (+=, -=, *=, /=, %=) and ++/-- on fields",
      'struct Counter { int n; number v; }\n'
      'Counter c = Counter { n: 10, v: 5.0 };\n'
      'c.n += 5; print(c.n);\n'
      'c.n -= 3; print(c.n);\n'
      'c.n *= 2; print(c.n);\n'
      'c.n /= 4; print(c.n);\n'
      'c.n %= 4; print(c.n);\n'
      'c.n++; c.n++; c.n--; print(c.n);\n'
      'c.v += 2.5; c.v *= 2.0; print(c.v);\n'
      'int sum = 0;\n'
      'for (c.n = 0; c.n < 4; c.n++) { sum += c.n; }\n'
      'print(sum);\n',
      backends=('pyro', 'node', 'go', 'c', 'cpp', 'csharp'),
      expect="15\n12\n24\n6\n2\n3\n15\n6")

agree("L-07: struct passed as parameter mutates in-place (reference semantics)",
      'struct Sensor { int id; number valor; }\n'
      'fn recalibrate(Sensor x, number v) -> void ={\n'
      '    x.valor = v;\n'
      '    x.id += 10;\n'
      '}\n'
      'Sensor s = Sensor { id: 1, valor: 36.6 };\n'
      'recalibrate(s, 42.5);\n'
      'print(s.id);\n'
      'print(s.valor);\n',
      backends=('pyro', 'node', 'go', 'c', 'cpp', 'csharp'),
      expect="11\n42.5")

agree("L-07: this.campo = x inside impl methods",
      'struct Account { string owner; int balance; }\n'
      'impl Account {\n'
      '    fn deposit(int amount) -> void ={\n'
      '        this.balance += amount;\n'
      '    }\n'
      '    fn rename(string new_owner) -> void ={\n'
      '        this.owner = new_owner;\n'
      '    }\n'
      '    fn info() -> string ={\n'
      '        return this.owner + ":" + to_string(this.balance);\n'
      '    }\n'
      '}\n'
      'Account acc = Account { owner: "Ana", balance: 100 };\n'
      'acc.deposit(50);\n'
      'acc.rename("Bia");\n'
      'print(acc.info());\n',
      backends=('pyro', 'node', 'go', 'c', 'cpp', 'csharp'),
      expect="Bia:150")

agree("L-07: nested structs (a.b.c = x) and struct arrays (arr[i].campo = x)",
      'struct Inner { int x; }\n'
      'struct Mid { Inner inner; }\n'
      'struct Outer { Mid mid; }\n'
      'Outer o = Outer { mid: Mid { inner: Inner { x: 1 } } };\n'
      'o.mid.inner.x = 10;\n'
      'o.mid.inner.x += 5;\n'
      'o.mid.inner.x++;\n'
      'print(o.mid.inner.x);\n'
      'Inner[] arr = [Inner { x: 10 }, Inner { x: 20 }];\n'
      'arr[0].x = 99;\n'
      'arr[1].x += 5;\n'
      'arr[1].x++;\n'
      'print(arr[0].x);\n'
      'print(arr[1].x);\n',
      backends=('pyro', 'node', 'go', 'c', 'cpp', 'csharp'),
      expect="16\n99\n26")

# Semantic errors: non-existent field (with caret + did-you-mean) and non-struct target
_bad_field = _compile(_l07_work,
                      'struct Sensor { int id; number valor; }\n'
                      'Sensor s = Sensor { id: 1, valor: 36.6 };\n'
                      's.valr = 40.0;\n',
                      'pyro', os.path.join(_l07_work, 'bad1.pyro'))
_bad_field_out = (_bad_field.stdout or '') + (_bad_field.stderr or '')
check("L-07: assigning to non-existent field is a semantic error with caret and hint",
      _bad_field.returncode != 0
      and "has no field 'valr'" in _bad_field_out
      and "did you mean `valor`?" in _bad_field_out
      and "^" in _bad_field_out,
      _bad_field_out)

_bad_nonstruct = _compile(_l07_work,
                          'int x = 10;\nx.valor = 20;\n',
                          'pyro', os.path.join(_l07_work, 'bad2.pyro'))
_bad_nonstruct_out = (_bad_nonstruct.stdout or '') + (_bad_nonstruct.stderr or '')
check("L-07: assigning to a field on a non-struct is a semantic error",
      _bad_nonstruct.returncode != 0
      and "cannot assign to field 'valor' on non-struct" in _bad_nonstruct_out,
      _bad_nonstruct_out)

# Backend refusal on asm and wasm
for _unsup in ('asm', 'wasm'):
    _res_u = _compile(_l07_work, 'struct S { int x; }\nS s = S { x: 1 };\ns.x = 2;\n', _unsup, os.path.join(_l07_work, 'u.' + _unsup))
    _u_out = (_res_u.stdout or '') + (_res_u.stderr or '')
    check(f"L-07: --backend {_unsup} refuses explicitly with 'use --backend'",
          _res_u.returncode != 0 and "use --backend" in _u_out,
          _u_out)
# Verify C, C++, and AOT C codegen emit pointer/handle field assignments even if gcc/g++ is absent
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
import compiler as _comp_l07, aot_pyro as _aot_l07
_c_gen = _comp_l07.compile_source(_L07_SIMPLE, 'c', True, 'win64')
_cpp_gen = _comp_l07.compile_source(_L07_SIMPLE, 'cpp', True, 'win64')
_aot_gen = _aot_l07.compile_to_c(_comp_l07.compile_source(_L07_SIMPLE, 'pyro', True, 'win64'))
check("L-07: C backend generates heap-pointer struct field assignment (s->valor = 40.0)",
      "__cryo_new_Sensor" in _c_gen and "(s)->valor = 40.0;" in _c_gen, _c_gen)
check("L-07: C++ backend generates shared_ptr struct field assignment (s->valor = 40.0)",
      "std::make_shared<Sensor>" in _cpp_gen and "s->valor = 40.0;" in _cpp_gen, _cpp_gen)
check("L-07: AOT native C translation emits index_set for struct field assignment",
      "index_set(" in _aot_gen, _aot_gen[:300])
agree("upper and lower handle non-ASCII Latin text (ISSUES/31)",
      u'print(upper("ação"));\nprint(lower("AÇÃO"));\n',
      backends=_U8, expect=u"AÇÃO\nação")

agree("negative zero (-0.0) and 0.0 - 0.0 agree across all backends (ISSUES/33)",
      'number z = -1.0;\nnumber w = 0.0;\nprint(z * w);\nprint(0.0 - 0.0);\n',
      backends=('pyro', 'node', 'go', 'c', 'cpp', 'csharp'),
      expect="-0\n0")

shutil.rmtree(_l07_work, ignore_errors=True)

# round(-0.0) is DELIBERATELY ABSENT. It disagrees four ways across the
# backends for a reason that has nothing to do with rounding — node prints
# -0 as 0 while pyro/go/c print -0, and pyro folds `0.0 - 0.0` to -0 —
# and asserting any of them here would be asserting one of those four as
# correct before anyone has decided which. ISSUES/33.

print(f"\n{_passed} passed, {_failed} failed"
      + (f", {_skipped} backend runs skipped" if _skipped else ""))
sys.exit(1 if _failed else 0)
