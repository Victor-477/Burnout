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
    BinaryExpr, UnaryExpr, TernaryExpr, CallExpr, Identifier, Literal,
    # 11.28 — the constructs the self-hosted parser gained
    For, ForEach, Break, Continue, CompoundAssignment, Increment,
    ArrayLiteral, IndexAccess, IndexAssignment, FieldAccess,
    StructDecl, StructInit, EnumDecl, MatchStatement, Block,
    # 12.6 — the desugarings and remaining shapes
    TryCatch, Switch, SwitchCase, Lambda, MapLiteral, CastExpr,
    ModuleImport, TraitDecl, SpawnExpr, AwaitExpr,
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


def _tparams(n):
    """13.3 — " (tparams T U)", or "" when the declaration is not generic.

    Emitted only when non-empty so every non-generic declaration keeps the
    exact shape 11.28 and 12.6 already assert."""
    tps = getattr(n, 'type_params', None) or []
    if not tps:
        return ""
    bounds = getattr(n, 'type_bounds', None) or {}
    return " (tparams" + "".join(
        " " + (f"{t}:{bounds[t]}" if t in bounds else t) for t in tps) + ")"


def ser(n):
    """Serializes the reference AST into the SAME S-expression as the Cryo parser."""
    if isinstance(n, FunctionDecl):
        params = "".join(f" (p {pt} {pn})" for (pt, pn) in n.params)
        ret = n.return_type or "void"
        body = "".join(" " + ser(s) for s in n.body)
        # 13.3 — a bound travels with its parameter (`T:Ord`); the reference
        # keeps type_params and type_bounds in step, so splitting them here
        # would let the two drift without this comparison noticing.
        return f"(fn {n.name}{_tparams(n)} (params{params}) {ret} (body{body}))"
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
    if isinstance(n, TernaryExpr):
        return f"(tern {ser(n.condition)} {ser(n.then_value)} {ser(n.else_value)})"
    if isinstance(n, CallExpr):
        targs = getattr(n, 'type_args', None) or []
        ta = (" (targs" + "".join(" " + t for t in targs) + ")") if targs else ""
        return f"(call {n.callee}{ta}" + "".join(" " + ser(a) for a in n.args) + ")"
    if isinstance(n, Identifier):
        return f"(id {n.name})"
    if isinstance(n, Literal):
        if n.kind == "int":    return f"(int {n.value})"
        if n.kind == "float":  return f"(float {n.value})"
        if n.kind == "string": return f"(str {n.value})"
        if n.kind == "bool":   return f"(bool {'true' if n.value else 'false'})"
        if n.kind == "null":   return "(null)"

    # ── 11.28 ──────────────────────────────────────────────
    # Each shape below is also produced by parser.cryo. They are written here
    # first, deliberately: the reference AST is the ORACLE, so the Cryo parser
    # is made to match it rather than the two being defined together.
    if isinstance(n, For):
        init = ser(n.init) if n.init is not None else "nil"
        cond = ser(n.condition) if n.condition is not None else "nil"
        upd = ser(n.update) if n.update is not None else "nil"
        body = "".join(" " + ser(x) for x in n.body)
        return f"(for {init} {cond} {upd} (body{body}))"
    if isinstance(n, ForEach):
        vt = n.var_type or "-"
        body = "".join(" " + ser(x) for x in n.body)
        return f"(foreach {vt} {n.var_name} {ser(n.iterable)} (body{body}))"
    if isinstance(n, Break):
        return "(break)"
    if isinstance(n, Continue):
        return "(continue)"
    if isinstance(n, CompoundAssignment):
        return f"(cassign {n.op} {n.name} {ser(n.value)})"
    if isinstance(n, Increment):
        return f"(incr {n.op} {n.name})"
    if isinstance(n, IndexAssignment):
        return f"(setidx {ser(n.obj)} {ser(n.index)} {ser(n.value)})"
    if isinstance(n, ArrayLiteral):
        return "(arr" + "".join(" " + ser(e) for e in n.elements) + ")"
    if isinstance(n, IndexAccess):
        return f"(idx {ser(n.obj)} {ser(n.index)})"
    if isinstance(n, FieldAccess):
        return f"(field {ser(n.obj)} {n.field})"
    if isinstance(n, StructDecl):
        fs = "".join(f" (f {f.field_type} {f.name})" for f in n.fields)
        return f"(struct {n.name}{_tparams(n)} (fields{fs}))"
    if isinstance(n, StructInit):
        fs = "".join(f" (fv {k} {ser(v)})" for k, v in n.fields)
        return f"(new {n.struct_name}{fs})"
    if isinstance(n, EnumDecl):
        ms = "".join(" (m " + m.name
                     + "".join(" " + t for t in (m.fields or [])) + ")"
                     for m in n.members)
        return f"(enum {n.name}{ms})"
    if isinstance(n, MatchStatement):
        cs = ""
        for c in n.cases:
            vs = "".join(" " + v for v in (c.pattern_vars or []))
            body = "".join(" " + ser(x) for x in c.body)
            cs += f" (case {c.pattern_name} (vars{vs}) (body{body}))"
        return f"(match {ser(n.subject)}{cs})"
    if isinstance(n, Block):
        return "(block" + "".join(" " + ser(x) for x in n.body) + ")"

    # ── 12.6 ──────────────────────────────────────────────
    # The constructs 11.28 left out. Same rule as above: the reference AST is
    # the oracle and parser.cryo is made to match it.
    if isinstance(n, TryCatch):
        tb = "".join(" " + ser(x) for x in n.try_body)
        cb = "".join(" " + ser(x) for x in (n.catch_body or []))
        out = (f"(try (body{tb}) (catch {n.catch_type or '-'} "
               f"{n.catch_name or '-'} (body{cb}))")
        if n.finally_body is not None:
            out += "(finally (body" + "".join(" " + ser(x) for x in n.finally_body) + "))"
            out = out.replace(")(finally", ") (finally")
        return out + ")"
    if isinstance(n, Switch):
        cs = ""
        for c in n.cases:
            vs = "".join(" " + ser(v) for v in c.values)
            body = "".join(" " + ser(x) for x in c.body)
            cs += f" (case (vals{vs}) (body{body}))"
        out = f"(switch {ser(n.subject)}{cs}"
        if n.default_body is not None:
            out += " (default (body" + "".join(" " + ser(x) for x in n.default_body) + "))"
        return out + ")"
    if isinstance(n, Lambda):
        ps = "".join(f" (p {pt} {pn})" for (pt, pn) in n.params)
        body = "".join(" " + ser(x) for x in n.body)
        return f"(lam (params{ps}) (body{body}))"
    if isinstance(n, MapLiteral):
        return "(map" + "".join(f" (kv {ser(k)} {ser(v)})" for k, v in n.pairs) + ")"
    if isinstance(n, CastExpr):
        return f"(cast {ser(n.expr)} {n.target_type})"
    if isinstance(n, ModuleImport):
        return f"(import {n.path} {n.alias or '-'})"
    if isinstance(n, SpawnExpr):
        return f"(spawn {ser(n.expr)})"
    if isinstance(n, AwaitExpr):
        return f"(await {ser(n.expr)})"
    if isinstance(n, TraitDecl):
        ms = ""
        for m in n.methods:
            ps = "".join(f" (p {pt} {pn})" for (pt, pn) in m.params)
            ms += f" (m {m.name} (params{ps}) {m.return_type or 'void'})"
        return f"(trait {n.name}{ms})"
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

# ── 11.28: the tokens the self-hosted lexer used to be missing ──
#
# Fifteen kinds the reference lexer produced and this one did not. The failure
# mode is what makes it worth its own sample: `pub`, `trait`, `impl` and
# `permissions` lexed as plain IDENT, so the stream stayed the same LENGTH and
# only a name-by-name comparison sees it. `..=` and `<<=` are the opposite
# problem — they came out as two tokens each, and the trailing `=` then reads
# as an assignment, so the divergence surfaces far from its cause.
#
# The three-character forms are listed first here because they are the ones an
# ordering mistake breaks: `..=` must be tried before `..`, `<<=` before `<<`,
# `::` before `:`.
SAMPLE3 = ('int a = 0; a &= 1; a |= 2; a ^= 3; a <<= 1; a >>= 1; int b = ~a; '
           'for (i in 0..5) { } for (j in 0..=5) { } '
           'pub fn f() -> void ={ } trait T { } impl T for S { } '
           'permissions { read = "./x"; } ns::name();')

print("\n[11.28] tokens the self-hosted lexer was missing")
exp_new = reference_tokens(SAMPLE3)
got_new, res_new = selfhost_tokens(SAMPLE3)
check("the extended sample compiled and ran", res_new.returncode == 0 and got_new)
check("same number of tokens", len(got_new) == len(exp_new))
if got_new != exp_new:
    for i in range(max(len(got_new), len(exp_new))):
        g = got_new[i] if i < len(got_new) else "<missing>"
        e = exp_new[i] if i < len(exp_new) else "<extra>"
        if g != e:
            print(f"    divergence at position {i}: expected {e!r}, got {g!r}")
            break
check("token stream identical to the reference lexer", got_new == exp_new)

# Each kind named individually: "the streams match" would still pass if a whole
# construct silently disappeared from the sample during an edit.
_kinds = {t.split(' ', 1)[0] for t in got_new}
for _k in ('AMP_ASSIGN', 'PIPE_ASSIGN', 'CARET_ASSIGN', 'SHL_ASSIGN', 'SHR_ASSIGN',
           'TILDE', 'RANGE', 'RANGE_INCL', 'PUB', 'TRAIT', 'IMPL',
           'PERMISSIONS', 'COLON_COLON'):
    check(f"emits {_k}", _k in _kinds)

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

# ── 11.28: the constructs the self-hosted parser gained ──────
#
# The reference AST is the ORACLE: each sample is parsed by both and the
# S-expressions compared statement by statement. That is far sharper than "it
# did not crash", which is otherwise all a parser can be checked for — a parser
# that quietly drops a clause still produces output.
#
# One sample per group, so a failure names the group rather than the file.
def _selfhost_ast(src):
    """The Cryo parser's S-expressions for `src`, run on the VM."""
    # The '$' must be escaped in the DRIVER's own literal or the reference
    # compiler interpolates the sample while compiling the driver. That escape
    # is 11.39, and this harness is what needed it.
    esc = (src.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$"))
    r = _run_vm('import "parser.cryo"\nparse("' + esc + '");\n')
    return [ln for ln in (r.stdout or "").replace("\r\n", "\n").split("\n")
            if ln.startswith("(")], r


SAMPLES_1128 = [
    ("arrays, indexing and element assignment",
     'int[] a = [1, 2, 3]; int[][] g = [[1, 2], [3]]; '
     'a[0] = a[1] * 2; print(g[0][1]);'),
    ("C-style for, compound assignment, increment",
     'int q = 0; for (int i = 0; i < 3; i++) { q += i; } print(q);'),
    ("for-in, typed and untyped",
     'int[] a = [1, 2]; for (v in a) { print(v); } for (int w in a) { print(w); }'),
    ("break and continue",
     'int i = 0; while (true) { if (i > 2) { break; } else { continue; } }'),
    ("struct declaration, construction and field access",
     'struct P { int x; string s; } P p = new P{x: 1, s: "h"}; print(p.x);'),
    ("struct-typed arrays and chained postfix",
     'struct It { string n; } It[] xs = []; print(xs[0].n);'),
    ("enum declaration, with and without a payload",
     'enum R { Ok(int), Err(string), None }'),
    ("match with bound payloads",
     'enum R { Ok(int), Err(string) } R r = Ok(1); '
     'match (r) { Ok(v) => { print(v); } Err(e) => { print(e); } }'),
    ("optional and array types in declarations",
     'int? o = null; string[] names = [];'),
    ("ternary, bitwise operators and unary ~",
     'int x = 1; int y = x > 0 ? 1 : 2; int a = 6 & 3; int b = 6 | 1; '
     'int c = 6 ^ 1; int d = 1 << 2; int e = ~a;'),
    # The point of matching the reference's precedence chain, rather than any
    # chain that happens to work: a level in the wrong place still parses, and
    # still produces a tree — just a different one. `a & b == c` is the case
    # that catches it, since & binds LOOSER than == here (as in C).
    ("precedence across the new levels",
     'int a = 1; int b = 2; int c = 3; bool r = a & b == c; '
     'int s = a | b ^ c & a; bool t = a < b == true;'),
    ("slices lower to the slice native",
     'int[] a = [1, 2, 3, 4]; int[] m = a[1..3]; int[] h = a[..2];'),
    ("a range for-loop is a C-style for, not a foreach",
     'for (int i in 0..3) { print(i); } for (int j in 1..=3) { print(j); }'),
    # Last because it is the subtlest: an interpolated string is NOT a string in
    # the reference AST but a CONCATENATION, and the fold has a rule that is
    # easy to miss — a LEADING interpolation is prefixed with `"" +` to force
    # string context, so `"${a}${b}"` cannot add two numbers.
    ("string interpolation is a concatenation, not a string",
     'int x = 1; int y = 2; print("${x}"); print("a${x}b${y}c"); '
     'print("sum: ${x + y}"); print("plain");'),
]

print("\n[11.28] constructs the self-hosted parser gained")
# ── 12.6: the constructs 11.28 deliberately left out ─────────
#
# 11.28 stopped at the parser's *shapes* and recorded these as "each is a
# desugaring, not a parse rule". That was true of one of them — `=> expr`
# lowers to a body of [Return(expr)], so emitting the bare expression would
# diverge — and overstated for the rest, which turned out to be ordinary rules
# once the type grammar could spell `map<K,V>` and `fn(T)->R`.
SAMPLES_126 = [
    ("try / catch", 'try { print(1); } catch (string e) { print(e); }'),
    ("try / catch / finally",
     'try { print(1); } catch (string e) { print(e); } finally { print(2); }'),
    ("switch with default",
     'switch (x) { case 1: print(1); default: print(0); }'),
    # Stacked labels share ONE case in the reference. Emitting two would parse
    # the same source into a different tree, and still "work".
    ("switch with stacked labels",
     'switch (x) { case 1: case 2: print(1); case 3: print(3); }'),
    ("lambda, expression body", 'fn(int)->int f = (int n) => n + 1;'),
    ("lambda, block body", 'fn(int)->int g = (int n) => { return n + 1; };'),
    ("map literal and map type", 'map<string,int> m = {"a": 1, "b": 2};'),
    ("nested type arguments", 'map<string,int[]> m = {"a": [1]};'),
    ("cast", 'int n = x as int;'),
    # `as` binds LOOSER than ||, so this casts the whole disjunction. A cast
    # level in the wrong place still parses — only the oracle catches it.
    ("cast precedence against ||", 'int n = a || b as int;'),
    ("import", 'import "lib.cryo";'),
    ("import with alias", 'import "lib.cryo" as geo;'),
    ("trait with method signatures",
     'trait Ord { fn cmp(int o) -> int; fn zero() -> int; }'),
    # 12.5 shipped concurrency and the self-hosted parser had never been told.
    ("spawn / await", 'future<int> f = spawn g(); int r = await f;'),
]

# ── 13.3: generics ──────────────────────────────────────────
#
# 12.6 left these out. The interesting one is the LAST: `a < b` must not be
# read as a type-argument list, and only the token after the matching `>`
# tells the two apart — the same shape of lookahead the lambda needed.
SAMPLES_133 = [
    ("a generic function declaration", 'fn id<T>(T x) -> T ={ return x; }'),
    ("a bounded type parameter", 'fn m<T: Ord>(T a) -> T ={ return a; }'),
    ("a generic struct", 'struct Pair<A, B> { A first; B second; }'),
    ("an explicit type argument at the call site", 'print(id<int>(42));'),
    ("two type arguments", 'print(mk<int, string>(1, "a"));'),
    ("a comparison is NOT a type-argument list",
     'int a = 1; int b = 2; print(a < b);'),
    ("a non-generic declaration is unchanged",
     'fn f(int a) -> int ={ return a; }'),
]

for _label, _src in SAMPLES_1128 + SAMPLES_126 + SAMPLES_133:
    _exp = reference_ast(_src)
    _got, _res = _selfhost_ast(_src)
    if _got != _exp:
        for _i in range(max(len(_got), len(_exp))):
            _g = _got[_i] if _i < len(_got) else "<missing>"
            _x = _exp[_i] if _i < len(_exp) else "<extra>"
            if _g != _x:
                print(f"    divergence in {_label} at statement {_i}:")
                print(f"      expected: {_x}")
                print(f"      got:      {_g}")
                break
    check(f"AST identical: {_label}", _got == _exp)


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
    # The program is embedded as a string literal in the driver. If it contains
    # `${`, the reference compiler (compiling the driver) would interpolate it
    # there. Split `${` across a concat so the driver has no literal `${`; the
    # runtime value handed to compile() is unchanged.
    escp = escp.replace("${", '$" + "{')
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

# for loops: C-style (init; cond; upd) and for-each (T x in coll)
check_selfhost('int total = 0; for (int i = 0; i < 5; i = i + 1) { total = total + i; } '
               'print(total); '                                           # 0+1+2+3+4 = 10
               'int[] xs = [2, 4, 6, 8]; int s = 0; for (int v in xs) { s = s + v; } '
               'print(s); '                                               # 20
               'string r = ""; for (string c in "abc") { r = r + c; } print(r); '   # abc
               'int prod = 1; for (int k = 1; k <= 4; k = k + 1) { prod = prod * k; } '
               'print(prod);',                                            # 24
               "for", lines_fn=_out_lines)

# compound assignment (+=, -=, *=) and increment/decrement (++/--)
check_selfhost('int x = 10; x += 5; print(x); x -= 3; print(x); x *= 2; print(x); '
               'int c = 0; c++; c++; c++; print(c); c--; print(c); '
               'string s = "a"; s += "b"; s += "c"; print(s); '
               'int acc = 0; for (int i = 0; i < 5; i++) { acc += i; } print(acc);',  # 0+1+2+3+4 = 10
               "compound", lines_fn=_out_lines)

# ternary  cond ? a : b  (incl. nested / right-associative and in expressions)
check_selfhost('int x = 7; print(x > 5 ? 1 : 0); print(x < 5 ? 1 : 0); '
               'string s = x % 2 == 0 ? "even" : "odd"; print(s); '
               'int g = 85; string grade = g >= 90 ? "A" : g >= 80 ? "B" : "C"; print(grade); '
               'int m = 3 > 2 ? (10 + 5) : 0; print(m);',
               "ternary", lines_fn=_out_lines)

# switch: single case, stacked cases (shared body), default, no fall-through
check_selfhost('int x = 2; switch (x) { case 1: print(10); case 2: print(20); '
               'case 3: print(30); default: print(99); } '
               'switch (x) { case 5: print(1); default: print(0); } '                # default -> 0
               'int d = 3; switch (d) { case 1: case 2: case 3: print(123); '        # stacked -> 123
               'default: print(-1); } '
               'string cmd = "go"; switch (cmd) { case "stop": print(1); '           # string subject
               'case "go": print(2); default: print(3); }',
               "switch", lines_fn=_out_lines)

# match on enums-with-data: variant constructors + tag check + destructuring + '_'
check_selfhost('enum Result { Ok(int), Err(string) } '
               'fn describe(int code) -> string ={ '
               '  Result r = code == 0 ? Ok(100) : Err("fail"); '
               '  match r { Ok(v) => { return "ok:" + to_string(v); } '
               '            Err(e) => { return "err:" + e; } } '
               '  return "none"; } '
               'print(describe(0)); print(describe(9)); '                     # ok:100 / err:fail
               'enum Shape { Circle(int), Rect(int, int) } '
               'fn area(Shape s) -> int ={ '
               '  match s { Circle(rr) => { return 3 * rr * rr; } '
               '            Rect(w, h) => { return w * h; } } return 0; } '
               'print(area(Circle(10))); print(area(Rect(4, 5))); '           # 300 / 20
               'Result r2 = Ok(7); '
               'match r2 { Err(e) => { print("E"); } _ => { print("other"); } }',  # other
               "match", lines_fn=_out_lines)

# try / catch / finally + throw
check_selfhost('fn risky(int n) -> int ={ if (n < 0) { throw("negative"); } return n * 2; } '
               'int a = 0; try { a = risky(5); } catch (string e) { a = -1; } print(a); '     # 10
               'try { a = risky(-3); } catch (string e) { print("caught:" + e); a = -99; } '  # caught:negative
               'print(a); '                                                                    # -99
               'try { throw("boom"); } catch (string e) { print(e); } finally { print("fin"); }',  # boom / fin
               "trycatch", lines_fn=_out_lines)

# optionals: ?? (null-coalescing) and ! (unwrap)
check_selfhost('int? x = null; int y = x ?? 7; print(y); '            # 7
               'int? z = 42; print(z ?? 0); '                         # 42
               'int? w = 5; print(w!); '                              # 5
               'string? s = null; print(s ?? "default");',            # default
               "optionals", lines_fn=_out_lines)

# string interpolation: "a ${expr} b" -> concatenation + to_string
check_selfhost('int n = 42; string name = "World"; '
               'print("n = ${n}"); '                                  # n = 42
               'print("Hello, ${name}!"); '                           # Hello, World!
               'print("${n} + ${n} = ${n + n}"); '                    # 42 + 42 = 84
               'int[] arr = [1, 2, 3]; print("len=${len(arr)} first=${arr[0]}");',  # len=3 first=1
               "interp", lines_fn=_out_lines)

# bitwise & shifts (needed for the compiler's own byte-emit helpers)
check_selfhost('int a = 240; int b = 15; print(a & b); print(a | b); print(a ^ b); '  # 0 / 255 / 255
               'print(240 >> 4); print(1 << 8); print((255 >> 4) & 3); '               # 15 / 256 / 3
               'int v = 5000; print(v & 255); print((v >> 8) & 255);',                 # 136 / 19
               "bitwise", lines_fn=_out_lines)

# break / continue in while and for loops
check_selfhost('int s = 0; int i = 0; while (i < 10) { i = i + 1; if (i == 3) { continue; } '
               'if (i == 7) { break; } s = s + i; } print(s); '                     # 1+2+4+5+6 = 18
               'int t = 0; for (int k = 0; k < 100; k++) { if (k >= 5) { break; } t += k; } print(t); '  # 0+1+2+3+4 = 10
               'int u = 0; int[] xs = [1,2,3,4,5,6]; for (int v in xs) { if (v % 2 == 0) { continue; } u += v; } '
               'print(u);',                                                          # 1+3+5 = 9
               "breakcont", lines_fn=_out_lines)

# first-class function values (10.6 / ISSUES-01): the self-hosted compiler must
# emit PUSHFN for a function used as a value and CALL_VALUE for a call through a
# variable, and must parse the `fn(T)->R` TYPE in declarations, parameters and
# return positions (at statement level `fn` otherwise reads as a declaration).
check_selfhost('fn dbl(int x) -> int ={ return x * 2; } '
               'fn inc(int x) -> int ={ return x + 1; } '
               'fn apply(fn(int)->int f, int v) -> int ={ return f(v); } '
               'fn twice(fn(int)->int f, int v) -> int ={ return f(f(v)); } '
               'fn pick(bool b) -> fn(int)->int ={ if (b) { return dbl; } return inc; } '
               'print(apply(dbl, 21)); print(apply(inc, 41)); '     # 42 / 42
               'print(twice(dbl, 3)); '                              # 12
               'fn(int)->int g = dbl; print(g(50)); '                # 100
               'g = inc; print(g(50)); '                             # 51
               'fn(int)->int c = pick(true); print(c(10));',         # 20
               "funcvalues", lines_fn=_out_lines)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
