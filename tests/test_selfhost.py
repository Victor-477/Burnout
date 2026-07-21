#!/usr/bin/env python3
# ============================================================
#  Burnout — Teste de fidelidade do compilador auto-hospedado
#  (Fase 9.3). Estágio 1: o lexer escrito em Cryo, rodando na
#  VM Pyro, deve produzir o MESMO fluxo de tokens que o lexer
#  de referência (cryo/lexer.py).
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
_root = os.path.dirname(os.path.dirname(_here))           # raiz do projeto
sys.path.insert(0, os.path.join(_root, "Cryo"))
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
SELFHOST = os.path.join(_root, "Cryo", "selfhost")
VM_BIN = os.path.join(_root, "build", "pyrovm.exe" if sys.platform == "win32" else "pyrovm")

from lexer import Lexer   # lexer de referência (oráculo)
from parser import Parser  # parser de referência (oráculo do estágio 2)
from ast_nodes import (
    FunctionDecl, VarDecl, Assignment, Return, If, While,
    BinaryExpr, UnaryExpr, CallExpr, Identifier, Literal,
)

# Fonte de teste em uma linha (sem quebras/escapes internos além de aspas),
# exercitando palavras-chave, tipos, ident, int/float, string, comentário de
# bloco e os operadores cobertos pelo lexer auto-hospedado.
SAMPLE = ('fn f(int n) -> number ={ /* bloco */ number x = 3.14; '
          'int c = n * 2; if (c >= 10 && n != 0) { c = c - 1; } '
          'return x; } string s = "Cryo";')


def reference_tokens(src):
    """Fluxo de tokens do lexer de referência no formato 'NOME valor'."""
    out = []
    for t in Lexer(src).tokenize():
        out.append(f"{t.type.name} {t.value}")
    return out


def ser(n):
    """Serializa a AST de referência na MESMA S-expression que o parser Cryo."""
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
    """Escreve um driver no dir selfhost, compila p/ .pyro, roda na VM e
    devolve as linhas de saída que são S-expressions (começam com '(')."""
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
    """Compila o lexer Cryo p/ .pyro e roda na VM, capturando os tokens."""
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
    # mantém só as linhas de token: "NOME valor", onde NOME é um nome de
    # TokenType (maiúsculas/_). Assim descartamos qualquer ruído de progresso
    # do compilador, independentemente da codificação do terminal.
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


print("[9.3] lexer auto-hospedado (Cryo na VM Pyro) vs. lexer de referência")

expected = reference_tokens(SAMPLE)
got, res = selfhost_tokens(SAMPLE)

check("o lexer Cryo compilou e rodou na VM", res.returncode == 0 and len(got) > 0)
check("mesmo número de tokens", len(got) == len(expected))

if got != expected:
    # mostra a primeira divergência para diagnóstico
    n = max(len(got), len(expected))
    for i in range(n):
        g = got[i] if i < len(got) else "<falta>"
        e = expected[i] if i < len(expected) else "<extra>"
        if g != e:
            print(f"    divergência na posição {i}: esperado {e!r}, obtido {g!r}")
            break

check("fluxo de tokens idêntico ao lexer de referência", got == expected)
check("termina com EOF", len(got) > 0 and got[-1] == "EOF ")

print("[9.3] parser auto-hospedado (Cryo na VM Pyro) vs. parser de referência")
_esc = SAMPLE.replace("\\", "\\\\").replace('"', '\\"')
res2 = _run_vm('import "parser.cryo"\nparse("' + _esc + '");\n')
got2 = [ln for ln in (res2.stdout or "").replace("\r\n", "\n").split("\n") if ln.startswith("(")]
exp2 = reference_ast(SAMPLE)

check("o parser Cryo compilou e rodou na VM", res2.returncode == 0 and len(got2) > 0)
check("mesmo número de statements de topo", len(got2) == len(exp2))
if got2 != exp2:
    for i in range(max(len(got2), len(exp2))):
        g = got2[i] if i < len(got2) else "<falta>"
        e = exp2[i] if i < len(exp2) else "<extra>"
        if g != e:
            print(f"    divergência no statement {i}:")
            print(f"      esperado: {e}")
            print(f"      obtido:   {g}")
            break
check("AST idêntica ao parser de referência", got2 == exp2)

# segunda fonte: while, if/else-if/else, unário, chamada, precedência mista
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
        g = got3[i] if i < len(got3) else "<falta>"
        e = exp3[i] if i < len(exp3) else "<extra>"
        if g != e:
            print(f"    divergência (fonte 2) no statement {i}:")
            print(f"      esperado: {e}")
            print(f"      obtido:   {g}")
            break
check("AST idêntica (fonte 2: while/else-if/unário/chamada)", got3 == exp3)

# ── estágio 3: codegen em Cryo -> .pyro executável ──────────
print("[9.3] codegen auto-hospedado (Cryo na VM emite .pyro executável)")

def _int_lines(text):
    out = []
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        s = ln.strip()
        if s and (s.lstrip("-")).isdigit():
            out.append(s)
    return out

def oracle_run(prog):
    """Compila+roda `prog` pelo compilador de referência (backend pyro)."""
    with tempfile.NamedTemporaryFile(suffix=".cryo", delete=False, mode="w", encoding="utf-8") as tp:
        tp.write(prog); path = tp.name
    try:
        r = subprocess.run([sys.executable, CRYOC, path, "--backend", "pyro", "--run", "--no-banner"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    finally:
        try: os.remove(path)
        except OSError: pass
    return _int_lines(r.stdout)

def selfhost_run(prog, label):
    """O compilador-em-Cryo (na VM) gera out.pyro; executa e devolve a saída."""
    out_pyro = os.path.join(tempfile.gettempdir(), "selfhost_out.pyro").replace("\\", "/")
    try: os.remove(out_pyro)
    except OSError: pass
    escp = prog.replace("\\", "\\\\").replace('"', '\\"')
    gen = _run_vm('import "codegen.cryo"\ncompile("' + escp + '", "' + out_pyro + '");\n')
    check(f"[{label}] codegen Cryo rodou e gravou o .pyro",
          gen.returncode == 0 and os.path.isfile(out_pyro))
    if os.path.isfile(out_pyro) and os.path.isfile(VM_BIN):
        r = subprocess.run([VM_BIN, out_pyro], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return _int_lines(r.stdout)
    return []

def check_selfhost(prog, label):
    exp = oracle_run(prog)
    got = selfhost_run(prog, label)
    if got != exp:
        print(f"    [{label}] oráculo:        {exp}")
        print(f"    [{label}] auto-hospedado: {got}")
    check(f"[{label}] .pyro auto-hospedado roda igual ao compilador de referência",
          got == exp and len(got) > 0)

# aritmética (int, precedência, unário, parênteses)
check_selfhost("int a = 2; int b = 3; print(a + b * 2); print((a + b) * 2); "
               "print(10 - 4 / 2); print(-b + 5);", "aritmética")

# controle de fluxo: while + if/else-if/else com comparações e saltos
check_selfhost("int i = 0; int sum = 0; while (i < 5) { sum = sum + i; i = i + 1; } "
               "print(sum); int x = 7; if (x > 5) { print(1); } else { print(0); } "
               "if (x == 5) { print(100); } else if (x > 6) { print(2); } else { print(3); }",
               "fluxo")

# funções de usuário: recursão (fib), múltiplos params, chamadas em expressão
check_selfhost("fn fib(int n) -> int ={ if (n < 2) { return n; } return fib(n - 1) + fib(n - 2); } "
               "fn soma(int a, int b) -> int ={ return a + b; } "
               "print(fib(10)); print(soma(20, 22)); int s = fib(7) + soma(5, 5); print(s);",
               "funções")

print(f"\n{_passed} passaram, {_failed} falharam")
sys.exit(1 if _failed else 0)
