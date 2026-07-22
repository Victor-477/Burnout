#!/usr/bin/env python3
# ============================================================
#  Burnout — Self-hosted compiler fidelity test
#  (Phase 9.3). Stage 1: the lexer written in Cryo, running on
#  the Pyro VM, must produce the SAME token stream as the
#  reference lexer (cryo/lexer.py).
# ============================================================
import os
import sys
import subprocess
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

_here = os.path.dirname(os.path.abspath(__file__))       # Burnout/tests
_root = os.path.dirname(os.path.dirname(_here))           # project root
sys.path.insert(0, os.path.join(_root, "Cryo"))
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
SELFHOST = os.path.join(_root, "Cryo", "selfhost")
VM_BIN = os.path.join(_root, "build", "pyrovm.exe" if sys.platform == "win32" else "pyrovm")

from lexer import Lexer   # reference lexer (oracle)
from parser import Parser  # reference parser (stage 2 oracle)
from ast_nodes import (
    FunctionDecl, VarDecl, Assignment, Return, If, While,
    BinaryExpr, UnaryExpr, CallExpr, Identifier, Literal,
)

# Test source on a single line (no internal line breaks/escapes other than quotes),
# exercising keywords, types, ident, int/float, string, block comment
# and the operators covered by the self-hosted lexer.
SAMPLE = ('fn f(int n) -> number ={ /* bloco */ number x = 3.14; '
          'int c = n * 2; if (c >= 10 && n != 0) { c = c - 1; } '
          'return x; } string s = "Cryo";')


def reference_tokens(src):
    """Token stream of the reference lexer in the 'NAME value' format."""
    out = []
    for t in Lexer(src).tokenize():
        out.append(f"{t.type.name} {t.value}")
    return out


def ser(n):
    """Serializes the reference AST into the SAME S-expression as the Cryo parser."""
    if isinstance(n, FunctionDecl):
        params = "".join(f" (p {pt} {pn})" for (pt, pn) in n.params)
        ret = n.return_type or "void"
        body = "".join(" " + ser(s) for s in n.body)
        return f"(fn {n.name} (params{params}) {ret} (body{body}))"
    if isinstance(n, VarDecl):
        init = ser(n.value) if n.value is not None else "nil"
        return f"(var {n.var_type} {n.name} {init})"
    if isinstance(n, Assignment):
        return f"(assign {n.name} {ser(n.value)})"
    if isinstance(n, Return):
        return "(return nil)" if n.value is None else f"(return {ser(n.value)})"
    if isinstance(n, If):
        s = f"(if {ser(n.condition)} (then" + "".join(" " + ser(x) for x in n.then_body) + ")"
        if n.else_body is not None:
            if len(n.else_body) == 1 and isinstance(n.else_body[0], If):
                s += " (else " + ser(n.else_body[0]) + ")"
            else:
                s += " (else" + "".join(" " + ser(x) for x in n.else_body) + ")"
        return s + ")"
    if isinstance(n, While):
        return f"(while {ser(n.condition)} (body" + "".join(" " + ser(x) for x in n.body) + "))"
    if isinstance(n, BinaryExpr):
        return f"(bin {n.op} {ser(n.left)} {ser(n.right)})"
    if isinstance(n, UnaryExpr):
        return f"(un {n.op} {ser(n.operand)})"
    if isinstance(n, CallExpr):
        return f"(call {n.callee}" + "".join(" " + ser(a) for a in n.args) + ")"
    if isinstance(n, Identifier):
        return f"(id {n.name})"
    if isinstance(n, Literal):
        if n.kind == "int":    return f"(int {n.value})"
        if n.kind == "float":  return f"(float {n.value})"
        if n.kind == "string": return f"(str {n.value})"
        if n.kind == "bool":   return f"(bool {'true' if n.value else 'false'})"
        if n.kind == "null":   return "(null)"
    return f"(? {type(n).__name__})"


def reference_ast(src):
    prog = Parser(Lexer(src).tokenize()).parse()
    return [ser(s) for s in prog.statements]


def _run_vm(driver_body):
    """Writes a driver in the selfhost dir, compiles to .pyro, runs it on the VM and
    returns the output lines that are S-expressions (start with '(')."""
    driver = os.path.join(SELFHOST, "_test_driver.cryo")
    with open(driver, "w", encoding="utf-8") as f:
        f.write(driver_body)
    try:
        res = subprocess.run(
            [sys.executable, CRYOC, driver, "--backend", "pyro", "--run", "--no-banner"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    finally:
        try:
            os.remove(driver)
        except OSError:
            pass
    return res


def selfhost_tokens(src):
    """Compiles the Cryo lexer to .pyro and runs it on the VM, capturing the tokens."""
    escaped = src.replace("\\", "\\\\").replace('"', '\\"')
    driver = os.path.join(SELFHOST, "_test_driver.cryo")
    with open(driver, "w", encoding="utf-8") as f:
        f.write('import "lexer.cryo"\n')
        f.write(f'tokenize("{escaped}");\n')
    try:
        res = subprocess.run(
            [sys.executable, CRYOC, driver, "--backend", "pyro", "--run", "--no-banner"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
    finally:
        try:
            os.remove(driver)
        except OSError:
            pass
    # keep only the token lines: "NAME value", where NAME is a TokenType name
    # (uppercase/_). This way we discard any progress noise from the compiler,
    # regardless of the terminal encoding.
    lines = []
    for ln in (res.stdout or "").replace("\r\n", "\n").split("\n"):
        head = ln.split(" ", 1)[0]
        if head and all(ch == "_" or ("A" <= ch <= "Z") for ch in head):
            lines.append(ln)
    return lines, res


_passed = 0
_failed = 0
def check(desc, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {desc}")
    else:
        _failed += 1
        print(f"  FAIL {desc}")


print("[9.3] self-hosted lexer (Cryo on the Pyro VM) vs. reference lexer")

expected = reference_tokens(SAMPLE)
got, res = selfhost_tokens(SAMPLE)

check("the Cryo lexer compiled and ran on the VM", res.returncode == 0 and len(got) > 0)
check("same number of tokens", len(got) == len(expected))

if got != expected:
    # show the first divergence for diagnosis
    n = max(len(got), len(expected))
    for i in range(n):
        g = got[i] if i < len(got) else "<missing>"
        e = expected[i] if i < len(expected) else "<extra>"
        if g != e:
            print(f"    divergence at position {i}: expected {e!r}, got {g!r}")
            break

check("token stream identical to the reference lexer", got == expected)
check("ends with EOF", len(got) > 0 and got[-1] == "EOF ")

print("[9.3] self-hosted parser (Cryo on the Pyro VM) vs. reference parser")
_esc = SAMPLE.replace("\\", "\\\\").replace('"', '\\"')
res2 = _run_vm('import "parser.cryo"\nparse("' + _esc + '");\n')
got2 = [ln for ln in (res2.stdout or "").replace("\r\n", "\n").split("\n") if ln.startswith("(")]
exp2 = reference_ast(SAMPLE)

check("the Cryo parser compiled and ran on the VM", res2.returncode == 0 and len(got2) > 0)
check("same number of top-level statements", len(got2) == len(exp2))
if got2 != exp2:
    for i in range(max(len(got2), len(exp2))):
        g = got2[i] if i < len(got2) else "<missing>"
        e = exp2[i] if i < len(exp2) else "<extra>"
        if g != e:
            print(f"    divergence at statement {i}:")
            print(f"      expected: {e}")
            print(f"      got:      {g}")
            break
check("AST identical to the reference parser", got2 == exp2)

# second source: while, if/else-if/else, unary, call, mixed precedence
SAMPLE2 = ('fn g(int a, int b) -> bool ={ int r = a + b * 2 - 1; '
           'while (r > 0) { r = r - 1; } '
           'if (a == b) { return true; } else if (a > b) { return false; } '
           'else { return !false; } } bool z = g(1, 2);')
_esc2 = SAMPLE2.replace("\\", "\\\\").replace('"', '\\"')
res3 = _run_vm('import "parser.cryo"\nparse("' + _esc2 + '");\n')
got3 = [ln for ln in (res3.stdout or "").replace("\r\n", "\n").split("\n") if ln.startswith("(")]
exp3 = reference_ast(SAMPLE2)
if got3 != exp3:
    for i in range(max(len(got3), len(exp3))):
        g = got3[i] if i < len(got3) else "<missing>"
        e = exp3[i] if i < len(exp3) else "<extra>"
        if g != e:
            print(f"    divergence (source 2) at statement {i}:")
            print(f"      expected: {e}")
            print(f"      got:      {g}")
            break
check("AST identical (source 2: while/else-if/unary/call)", got3 == exp3)

# ── stage 3: codegen in Cryo -> executable .pyro ──────────
print("[9.3] self-hosted codegen (Cryo on the VM emits executable .pyro)")

def _int_lines(text):
    out = []
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        s = ln.strip()
        if s and (s.lstrip("-")).isdigit():
            out.append(s)
    return out

def _out_lines(text):
    """All non-empty stdout lines (for string/bool output, not just integers)."""
    return [ln.strip() for ln in (text or "").replace("\r\n", "\n").split("\n") if ln.strip()]

def oracle_run(prog, lines_fn=_int_lines):
    """Reference compiler emits the .pyro; runs it on the SAME C VM as the
    self-hosted path — so we compare pure program output (codegen vs codegen),
    free of the compiler's own status chatter on stdout."""
    with tempfile.NamedTemporaryFile(suffix=".cryo", delete=False, mode="w", encoding="utf-8") as tp:
        tp.write(prog); path = tp.name
    out_pyro = os.path.join(tempfile.gettempdir(), "oracle_out.pyro").replace("\\", "/")
    try: os.remove(out_pyro)
    except OSError: pass
    try:
        subprocess.run([sys.executable, CRYOC, path, "--backend", "pyro", "-o", out_pyro, "--no-banner"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    finally:
        try: os.remove(path)
        except OSError: pass
    if os.path.isfile(out_pyro) and os.path.isfile(VM_BIN):
        r = subprocess.run([VM_BIN, out_pyro], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return lines_fn(r.stdout)
    return []

def selfhost_run(prog, label, lines_fn=_int_lines):
    """The Cryo-in-Cryo compiler (on the VM) generates out.pyro; executes it and returns the output."""
    out_pyro = os.path.join(tempfile.gettempdir(), "selfhost_out.pyro").replace("\\", "/")
    try: os.remove(out_pyro)
    except OSError: pass
    escp = prog.replace("\\", "\\\\").replace('"', '\\"')
    gen = _run_vm('import "codegen.cryo"\ncompile("' + escp + '", "' + out_pyro + '");\n')
    check(f"[{label}] Cryo codegen ran and wrote the .pyro",
          gen.returncode == 0 and os.path.isfile(out_pyro))
    if os.path.isfile(out_pyro) and os.path.isfile(VM_BIN):
        r = subprocess.run([VM_BIN, out_pyro], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return lines_fn(r.stdout)
    return []

def check_selfhost(prog, label, lines_fn=_int_lines):
    exp = oracle_run(prog, lines_fn)
    got = selfhost_run(prog, label, lines_fn)
    if got != exp:
        print(f"    [{label}] oracle:      {exp}")
        print(f"    [{label}] self-hosted: {got}")
    check(f"[{label}] self-hosted .pyro runs the same as the reference compiler",
          got == exp and len(got) > 0)

# arithmetic (int, precedence, unary, parentheses)
check_selfhost("int a = 2; int b = 3; print(a + b * 2); print((a + b) * 2); "
               "print(10 - 4 / 2); print(-b + 5);", "arithmetic")

# flow control: while + if/else-if/else with comparisons and jumps
check_selfhost("int i = 0; int sum = 0; while (i < 5) { sum = sum + i; i = i + 1; } "
               "print(sum); int x = 7; if (x > 5) { print(1); } else { print(0); } "
               "if (x == 5) { print(100); } else if (x > 6) { print(2); } else { print(3); }",
               "flow")

# user functions: recursion (fib), multiple params, calls in expressions
check_selfhost("fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n - 1) + fib(n - 2); } "
               "fn sum(int a, int b) -> int ={ return a + b; } "
               "print(fib(10)); print(sum(20, 22)); int s = fib(7) + sum(5, 5); print(s);",
               "functions")

# logical operators: && / || with short-circuit, gating integer output
check_selfhost("int x = 7; if (x > 0 && x < 10) { print(1); } else { print(0); } "
               "if (x < 0 || x > 5) { print(2); } else { print(3); } "
               "if (x > 100 && x < 200) { print(4); } else { print(5); } "
               "if (x == 7 || x == 8) { print(6); } else { print(7); }",
               "logic")

# strings + bools (full stdout comparison, not just integers)
check_selfhost('string s = "Cryo"; print(s); print("hello " + s); '
               'bool a = true; bool b = false; print(a); print(b); '
               'print(1 < 2); print(3 == 4); print(a && b); print(a || b);',
               "str-bool", lines_fn=_out_lines)

# float literals in the constant pool (IEEE-754 bytes must match the reference)
check_selfhost('number a = 3.14; number b = 2.5; print(a); print(b); '
               'print(a + b); print(a * 2.0); print(0.5); '
               'number c = 100.0; print(c / 8.0); number d = 0.1; print(d);',
               "floats", lines_fn=_out_lines)

# native builtins: OP_NATIVE (math/conversions/strings) + OP_LEN dispatch
check_selfhost('print(to_string(42)); print(len("hello")); print(upper("abc")); '
               'print(lower("XYZ")); print(sqrt(16.0)); print(abs(-7)); '
               'print(max(3, 9)); print(min(3, 9)); print(floor(3.9)); '
               'print(to_int("100") + 23); print(substr("hello world", 0, 5)); '
               'print(contains("hello", "ell")); print(find("hello", "l"));',
               "natives", lines_fn=_out_lines)

# arrays: literal (NEWARR), index read/write (INDEX/SETIDX), .push (APPEND), len
check_selfhost('int[] a = [10, 20, 30]; print(a[0]); print(a[2]); print(len(a)); '
               'a[1] = 99; print(a[1]); a.push(40); print(len(a)); print(a[3]); '
               'int sum = 0; int i = 0; while (i < len(a)) { sum = sum + a[i]; i = i + 1; } '
               'print(sum);',
               "arrays", lines_fn=_out_lines)

# maps: literal (NEWMAP), index read/write, has, len
check_selfhost('map<string,int> m = {"x": 1, "y": 2}; print(m["x"]); print(m["y"]); '
               'm["z"] = 3; print(m["z"]); print(has(m, "y")); print(has(m, "w")); '
               'print(len(m));',
               "maps", lines_fn=_out_lines)

# structs: new S{...} -> map; field reads (Cryo has no field-assignment syntax)
check_selfhost('struct Point { int x; int y; } '
               'Point p = new Point{ x: 3, y: 4 }; print(p.x); print(p.y); '
               'print(p.x + p.y); '
               'struct Box { int w; int h; } fn area(Box b) -> int ={ return b.w * b.h; } '
               'Box bx = new Box{ w: 5, h: 6 }; print(area(bx));',
               "structs", lines_fn=_out_lines)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
