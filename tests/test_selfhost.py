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

from lexer import Lexer   # lexer de referência (oráculo)

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

print(f"\n{_passed} passaram, {_failed} falharam")
sys.exit(1 if _failed else 0)
