#!/usr/bin/env python3
# ============================================================
#  Burnout — CLI-path verification  (ISSUES/08)
#
#  test_smoke.py builds its ASTs and calls the code generators DIRECTLY:
#
#      CodeGenGo(safe=safe).generate(Parser(Lexer(src).tokenize()).parse())
#
#  The real CLI does more — notably it runs semantic_check(ast) first. So a
#  bug in that extra work is invisible to smoke while breaking every actual
#  `cryoc.py` invocation. That is not hypothetical: `cryo/semantic.py` and
#  `burnout/codegen_wasm.py` both referenced `Block` without importing it,
#  raising NameError on the go/node/wasm CLI paths while smoke stayed green.
#
#  Two layers here:
#    1. drive cryoc.py as a SUBPROCESS over programs x backends, asserting the
#       exit code AND the expected stdout (not merely "it compiled");
#    2. a static audit that every name used in `isinstance(n, X)` is actually
#       imported by that module — which catches the whole bug class at once,
#       including files this matrix does not happen to exercise.
#
#  Legs whose toolchain is absent (Go, Node, a C compiler) skip cleanly.
# ============================================================
import ast as pyast
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(os.path.dirname(_here))
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
TMP = tempfile.gettempdir()

HAS_GO = shutil.which("go") is not None
HAS_NODE = shutil.which("node") is not None
HAS_CC = any(shutil.which(c) for c in ("gcc", "clang", "cc"))

_passed = _failed = 0
def check(desc, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {desc}")
    else:
        _failed += 1
        print(f"  FAIL {desc}")


# ── 1. the CLI matrix ───────────────────────────────────────
#
# (tag, source, expected stdout lines, backends it should work on)
#
# `pyro` needs the VM to run, so it is exercised with --run only when a VM can
# be built; generation alone is still checked everywhere.
PROGRAMS = [
    ("flow",
     'int s = 0; for (int i = 0; i < 6; i++) { if (i == 3) { continue; } s += i; } '
     'print(s); int f = 1; int k = 1; while (k <= 5) { f = f * k; k = k + 1; } print(f);',
     ["12", "120"],
     ("pyro", "go", "node", "c")),

    ("funcs",
     'fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n-1) + fib(n-2); } '
     'print(fib(12));',
     ["144"],
     ("pyro", "go", "node", "c")),

    ("strings",
     'string s = "Cryo"; print(upper(s)); print("hi " + s); print(len(s));',
     ["CRYO", "hi Cryo", "4"],
     ("pyro", "go", "node")),

    ("containers",
     'int[] a = [3, 1, 2]; a.push(4); int t = 0; for (int v in a) { t += v; } print(t); '
     'map<string,int> m = {"x": 10}; m["y"] = 20; print(m["x"] + m["y"]);',
     ["10", "30"],
     ("pyro", "go", "node")),

    ("structs_match",
     'enum Res { Ok(int), Err(string) } '
     'fn f(int x) -> string ={ Res r = x > 0 ? Ok(x) : Err("neg"); '
     '  match r { Ok(v) => { return "ok:" + to_string(v); } Err(e) => { return e; } } return "?"; } '
     'print(f(7)); print(f(-1));',
     ["ok:7", "neg"],
     ("pyro", "go", "node")),

    ("trycatch",
     'fn risky(int n) -> int ={ if (n < 0) { throw("neg"); } return n * 2; } '
     'try { print(risky(5)); print(risky(-3)); } catch (string e) { print("caught:" + e); }',
     ["10", "caught:neg"],
     ("pyro", "go", "node")),

    ("interp",
     'int n = 7; print("n=${n} sq=${n * n}");',
     ["n=7 sq=49"],
     ("pyro", "go", "node")),

    # Conversions and 64-bit width (ISSUES/11 and /15). The literal exceeds both
    # 2^32 and 2^53, so a truncating format (%ld on Windows) or a stray
    # pointer-cast shows up as a wrong number rather than a crash. abs() is here
    # because typing it `int` unconditionally silently truncated abs(-2.5).
    ("conversions",
     'int big = 9007199254740993; print(big); print(to_string(big)); '
     'print(to_string(big) + "!"); '
     'number f = 2.5; print(to_string(f)); print(abs(0.0 - 2.5)); '
     'print(to_string(true)); print(0 - 4294967296);',
     ["9007199254740993", "9007199254740993!", "2.5", "true", "-4294967296"],
     # node is excluded ON PURPOSE, not because it is broken: its numbers are
     # IEEE-754 doubles, exact only to 2^53, so 2^53+1 rounds to ...992. That
     # is the documented "no 64-bit integer" limit of the node backend, and
     # asserting otherwise would encode a wrong expectation.
     ("pyro", "go", "c")),

    # The construct whose NameError this suite exists to catch. There is no
    # bare-block syntax in Cryo: a `Block` node is produced by the parser when
    # it DESUGARS a for-each over an expression, so that is what reaches the
    # `isinstance(n, Block)` branch in semantic.py and the code generators.
    ("foreach_block",
     'fn mk() -> int[] ={ return [1, 2, 3]; } '
     'int t = 0; for (int v in mk()) { t += v; } print(t); '
     'for (string c in "ab") { print(c); }',
     ["6", "a", "b"],
     ("pyro", "go", "node")),

    ("large_int_to_string",
     'int big = 8000000000000; print(to_string(big)); print(to_string(to_string(big))); print(to_string(big) + "!");',
     ["8000000000000", "8000000000000", "8000000000000!"],
     ("pyro", "go", "node", "c")),
]

# backends that can RUN here (generation is always checked)
_RUNNABLE = {"go": HAS_GO, "node": HAS_NODE, "c": HAS_CC, "pyro": HAS_GO or HAS_CC}

# Real, already-filed bugs this suite surfaced. They are reported as `xfail`
# rather than failing the run, so the suite stays actionable — and if one starts
# PASSING the test fails instead, so a fixed bug cannot linger here unnoticed.
KNOWN_FAIL = {
    ("structs_match", "go"):  "ISSUES/12 — ternary yielding an enum is typed `any`",
}


def cryoc(args, timeout=180):
    return subprocess.run([sys.executable, CRYOC] + args, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def norm(s):
    return [l for l in s.replace("\r\n", "\n").strip().split("\n") if l.strip()]


print(f"[cli] CLI path   go:{'y' if HAS_GO else 'n'} "
      f"node:{'y' if HAS_NODE else 'n'} cc:{'y' if HAS_CC else 'n'}")

for tag, src, expected, backends in PROGRAMS:
    cf = os.path.join(TMP, f"cli_{tag}.cryo")
    with open(cf, "w", encoding="utf-8") as fh:
        fh.write(src)
    for be in backends:
        out = os.path.join(TMP, f"cli_{tag}_{be}.out")
        # generation must always succeed through the FULL pipeline
        r = cryoc([cf, "--backend", be, "-o", out, "--no-banner", "--emit-only"])
        ok = r.returncode == 0
        if not ok:
            print(f"    {tag}/{be}: {(r.stderr or r.stdout).strip()[:200]}")
        check(f"[{tag}/{be}] compiles via CLI", ok)

        if ok and _RUNNABLE.get(be):
            r2 = cryoc([cf, "--backend", be, "--run", "--no-banner"])
            got = norm(r2.stdout)
            # the runners print progress lines around the program output
            got = [l for l in got if not l.startswith(("→", "✓", "──", "["))]
            hit = all(e in got for e in expected)
            known = KNOWN_FAIL.get((tag, be))
            if known and not hit:
                # a real, already-filed bug: report it but do not fail the suite
                print(f"  xfail {tag}/{be} runs — known bug ({known})")
            elif known and hit:
                # the list must not rot: a fixed bug should be un-listed
                check(f"[{tag}/{be}] KNOWN_FAIL entry is stale, remove it ({known})", False)
            else:
                if not hit:
                    print(f"    {tag}/{be} expected {expected} got {got[:8]}")
                check(f"[{tag}/{be}] runs with expected output", hit)


# ── 2. static audit: isinstance(n, X) implies X is imported ──
#
# This is the check that generalises the two NameError bugs. Any module that
# tests `isinstance(..., Foo)` must have Foo in scope; otherwise the branch
# explodes the moment it is reached, no matter which program triggers it.
AUDITED = [
    os.path.join(_root, "Cryo", "semantic.py"),
    os.path.join(_root, "Cryo", "security.py"),
    os.path.join(_root, "Burnout", "codegen_pyro.py"),
    os.path.join(_root, "Burnout", "codegen_go.py"),
    os.path.join(_root, "Burnout", "codegen_node.py"),
    os.path.join(_root, "Burnout", "codegen_c.py"),
    os.path.join(_root, "Burnout", "codegen_asm.py"),
    os.path.join(_root, "Burnout", "codegen_wasm.py"),
]

_AST_NODES = os.path.join(_root, "Cryo", "ast_nodes.py")
_node_names = set(re.findall(r"^class\s+(\w+)", open(_AST_NODES, encoding="utf-8").read(), re.M))


def undefined_isinstance_names(path):
    """Names used in isinstance(...) that the module neither imports nor defines."""
    src = open(path, encoding="utf-8").read()
    tree = pyast.parse(src)

    bound, star_import = set(), False
    for node in pyast.walk(tree):
        if isinstance(node, pyast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    star_import = True
                else:
                    bound.add(a.asname or a.name)
        elif isinstance(node, pyast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (pyast.ClassDef, pyast.FunctionDef)):
            bound.add(node.name)
        elif isinstance(node, pyast.Assign):
            for t in node.targets:
                if isinstance(t, pyast.Name):
                    bound.add(t.id)
    if star_import:
        return []          # `from ast_nodes import *` brings everything in

    used = set()
    for node in pyast.walk(tree):
        if (isinstance(node, pyast.Call) and isinstance(node.func, pyast.Name)
                and node.func.id == "isinstance" and len(node.args) == 2):
            targets = node.args[1]
            elts = targets.elts if isinstance(targets, pyast.Tuple) else [targets]
            for e in elts:
                if isinstance(e, pyast.Name):
                    used.add(e.id)
    # only judge AST node classes; builtins like str/int are always in scope
    return sorted(n for n in used if n in _node_names and n not in bound)


print("\n-- static audit: isinstance targets are imported --")
for path in AUDITED:
    if not os.path.exists(path):
        continue
    missing = undefined_isinstance_names(path)
    if missing:
        print(f"    {os.path.basename(path)}: {missing}")
    check(f"{os.path.basename(path)}: all isinstance() AST names imported", not missing)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
