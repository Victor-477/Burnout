#!/usr/bin/env python3
# ============================================================
#  Burnout — testes de fumaca do sistema Cryo
#  Valida os geradores (nao monta binario). Rode da raiz:
#      python burnout/tests/test_smoke.py
# ============================================================
import sys, os
_root    = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
_burnout = os.path.join(_root, 'burnout')
sys.path.insert(0, os.path.join(_root, 'cryo'))   # front-end (CRYO)
sys.path.insert(0, _burnout)                      # backends (Burnout)

from lexer        import Lexer          # CRYO
from parser       import Parser         # CRYO
from security     import audit_ast      # CRYO
from codegen_c    import CodeGenC        # Burnout / backend C
from codegen_go   import CodeGenGo       # Burnout / backend Go
from codegen_asm  import CodeGenAsm, CodeGenAsmError  # Burnout / backend asm
from codegen_pyro import CodeGenPyro     # Burnout / backend bytecode Pyro

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

def gen_c(src, safe=True):
    return CodeGenC(safe=safe).generate(Parser(Lexer(src).tokenize()).parse())

def gen_asm(src, safe=True, abi='sysv'):
    return CodeGenAsm(safe=safe, abi=abi).generate(Parser(Lexer(src).tokenize()).parse())

def gen_go(src, safe=True):
    return CodeGenGo(safe=safe).generate(Parser(Lexer(src).tokenize()).parse())

def gen_pyro(src, safe=True, encode=True):
    return CodeGenPyro(safe=safe, encode=encode).generate(Parser(Lexer(src).tokenize()).parse())

def ast_of(src):
    return Parser(Lexer(src).tokenize()).parse()

# ── lexer: literais ─────────────────────────────────────────
print("[lexer] literais numericos")
toks = Lexer("0xFF 0b1010 0o17 1_000_000 3.14").tokenize()
vals = [t.value for t in toks if t.type.name in ('INT_LIT', 'FLOAT_LIT')]
check("0xFF -> 255",      vals[0] == '255')
check("0b1010 -> 10",     vals[1] == '10')
check("0o17 -> 15",       vals[2] == '15')
check("1_000_000",        vals[3] == '1000000')
check("float 3.14",       vals[4] == '3.14')

# ── backend C: bit a bit + precedencia ──────────────────────
print("[c] operadores bit a bit")
c = gen_c("int x = (1 << 4) | (6 & 3) ^ 2;")
check("shift/and/or/xor presentes", all(op in c for op in ('<<', '|', '&', '^')))

# ── backend C: seguranca ────────────────────────────────────
print("[c] instrumentacao de seguranca")
c = gen_c("fn f(int a, int b) -> int ={ return a * b + a - b; }")
check("mul overflow",  "cryo_mul_ovf" in c)
check("add overflow",  "cryo_add_ovf" in c)
check("sub overflow",  "cryo_sub_ovf" in c)

c = gen_c("int q = 10 % 3; int d = 10 / 2;")
check("div protegida",  "cryo_idiv_chk" in c)
check("mod protegida",  "cryo_imod_chk" in c)

cu = gen_c("fn f(int a, int b) -> int ={ return a * b; }", safe=False)
check("--unsafe remove mul_ovf", "cryo_mul_ovf" not in cu)
check("--unsafe mantem div guard",
      "cryo_idiv_chk" in gen_c("int d = 8 / 2;", safe=False))

# ── backend C: assert / switch / unsafe ─────────────────────
print("[c] assert / switch / unsafe")
check("assert",  "cryo_assert" in gen_c('assert(1 == 1, "ok");'))
c = gen_c("fn f(int d) -> int ={ switch (d) { case 1: return 1; default: return 0; } }")
check("switch->case",  "switch (d)" in c and "case 1:" in c)
c = gen_c("int x = 0; unsafe { x = 5 * 5; }")
check("unsafe sem overflow check", "cryo_mul_ovf" not in c)

# ── backend asm: subconjunto inteiro ────────────────────────
print("[asm] subconjunto inteiro")
a = gen_asm("""
fn fat(int n) -> int ={ if (n <= 1) { return 1; } return n * fat(n - 1); }
int r = fat(5);
print(r);
""")
check("intel syntax",      ".intel_syntax noprefix" in a)
check("main global",       ".globl main" in a)
check("recursao (call)",   "call fat" in a)
check("mul seguro no asm",  "call cryo_mul_ovf" in a)
check("print via runtime",  "call cryo_print_i64" in a)
check("alinhamento 16",     "and rsp, -16" in a)

a = gen_asm("int m = 0xF0 | 0x0F; int s = 1 << 8;")
check("or bit a bit",  "or rax, r10" in a)
check("shift usa r10->cl", "mov rcx, r10" in a and "sal rax, cl" in a)
check("RHS neutro (nao usa rcx direto)", "mov rcx, rax\n    pop rax" not in a)

# ── backend asm: ABI Win64 ──────────────────────────────────
print("[asm] ABI Win64 (Microsoft x64)")
w = gen_asm("""
fn soma(int a, int b) -> int ={ return a + b; }
int r = soma(3, 4);
print("res:");
print(r);
""", abi='win64')
check("win64 spill rcx",   "mov [rbp-8], rcx" in w)
check("win64 spill rdx",   "mov [rbp-16], rdx" in w)
check("win64 shadow space", "sub rsp, 32" in w)
check("win64 arg0=rcx (add_ovf)", "mov rcx, rax" in w and "mov rdx, r10" in w)
check("win64 secao .rdata", ".rdata" in w)
check("win64 print via rcx", "mov rcx, rax" in w)
# sysv NAO deve ter shadow space nem .rdata
s = gen_asm('fn soma(int a,int b)->int ={ return a+b; } print("x"); print(soma(1,2));',
            abi='sysv')
check("sysv sem shadow space", "sub rsp, 32" not in s)
check("sysv spill rdi/rsi",   "mov [rbp-8], rdi" in s and "mov [rbp-16], rsi" in s)
check("sysv secao .rodata",   ".rodata" in s and ".rdata" not in s)

# ── backend asm: argumentos na pilha (> nº de registradores) ─
print("[asm] argumentos na pilha")
SOMA8 = """
fn soma8(int a,int b,int c,int d,int e,int f,int g,int h) -> int ={
    return a+b+c+d+e+f+g+h;
}
int t = soma8(1,2,3,4,5,6,7,8);
print(t);
"""
w = gen_asm(SOMA8, abi='win64')
check("win64 prologo: 4 regs + pilha", "mov [rbp-32], r9" in w and "mov rax, [rbp+48]" in w)
check("win64 prologo: 8o param em rbp+72", "mov rax, [rbp+72]" in w)
check("win64 call: reserva 64 (shadow+4*8)", "sub rsp, 64" in w)
check("win64 call: reg via r11", "mov rcx, [r11+56]" in w)
check("win64 call: arg de pilha acima do shadow", "mov [rsp+32], rax" in w)
check("win64 call: descarta temporarios", "add rsp, 64" in w)

s = gen_asm(SOMA8, abi='sysv')
check("sysv prologo: 6 regs + pilha", "mov [rbp-48], r9" in s and "mov rax, [rbp+16]" in s)
check("sysv prologo: 8o param em rbp+24", "mov rax, [rbp+24]" in s)
check("sysv call: reserva 16 (2*8, sem shadow)", "sub rsp, 16" in s)
check("sysv call: reg via r11", "mov rdi, [r11+56]" in s)
check("sysv call: arg de pilha em rsp+0", "mov [rsp+0], rax" in s)

# alinhamento: reserva sempre multipla de 16
w9 = gen_asm("""
fn s9(int a,int b,int c,int d,int e,int f,int g,int h,int i)->int ={ return a+i; }
print(s9(1,2,3,4,5,6,7,8,9));
""", abi='sysv')   # sysv: 3 args na pilha -> 24 -> padded 32
check("sysv 3 args pilha -> reserva 32", "sub rsp, 32" in w9)

# ── backend asm: retorno de struct em registrador ───────────
print("[asm] retorno de struct em registrador")
STRUCTS = """
struct Point { int x; int y; }
struct Boxed { int v; }
fn make(int a, int b) -> Point ={ return new Point { x: a, y: b }; }
fn boxed(int n) -> Boxed ={ Boxed w = new Boxed { v: n*n }; return w; }
Point p = make(3, 4);
int sx = p.x;
int sy = p.y;
Boxed q = boxed(5);
print(sx); print(sy); print(q.v);
"""
# SysV: Point (16B) retorna em RAX:RDX; Boxed (8B) em RAX
s = gen_asm(STRUCTS, abi='sysv')
check("sysv make: StructInit -> rax e rdx", "mov rdx, rax" in s and "push rax" in s)
check("sysv recepcao: p.x<-rax, p.y<-rdx",
      "mov [rbp-16], rax" in s and "mov [rbp-8], rdx" in s)
check("sysv boxed: retorna campo em rax", "boxed:" in s)
check("sysv field access: le p.x de [rbp-16]", "mov rax, [rbp-16]" in s)

# Win64: Boxed (8B) retorna em RAX; Point (16B) -> memoria (erro claro)
WIN_OK = """
struct Boxed { int v; }
fn boxed(int n) -> Boxed ={ return new Boxed { v: n }; }
Boxed q = boxed(7);
print(q.v);
"""
w = gen_asm(WIN_OK, abi='win64')
check("win64 struct 1 campo retorna em rax", "boxed:" in w and "mov [rbp-8], rax" in w)

def expect_asm_err(src, label, abi='sysv'):
    try:
        gen_asm(src, abi=abi); check(label + " (deveria falhar)", False)
    except CodeGenAsmError:
        check(label, True)

# Win64: struct de 2 campos nao cabe em registrador -> erro (sret nao impl.)
expect_asm_err(
    "struct P { int x; int y; } fn m()->P ={ return new P{x:1,y:2}; } P a=m(); print(a.x);",
    "win64 struct 2 campos -> erro sret", abi='win64')
# struct como parametro por valor -> erro
expect_asm_err(
    "struct P { int x; } fn f(P p)->int ={ return p.x; } print(1);",
    "struct como parametro -> erro")
# campo de tipo nao-int -> erro
expect_asm_err(
    "struct P { number x; } fn f()->P ={ return new P{x:1}; } print(1);",
    "campo number em struct -> erro")

# ── backend asm: recursos nao suportados dao erro ───────────
print("[asm] erros claros")
def expect_err(src, label):
    try:
        gen_asm(src)
        check(label + " (deveria falhar)", False)
    except CodeGenAsmError:
        check(label, True)

expect_err("number x = 1.5;",      "double rejeitado")
expect_err('string s = "oi";',     "string decl rejeitada")
expect_err("enum E { A, B }",      "enum rejeitado")

# ── backend Go (nova base de compilacao) ────────────────────
print("[go] estrutura basica")
g = gen_go('int x = 40 + 2; print(x);')
check("go package main",  "package main" in g)
check("go import fmt",    '"fmt"' in g)
check("go func main",     "func main() {" in g)
check("go print->Println", "fmt.Println(x)" in g)
check("go suprime nao-usado", "_ = x" in g)

print("[go] tipos e declaracoes")
g = gen_go("""
enum Cor { R, G, B }
struct P { int x; int y; }
fn area(number r) -> number ={ return r * r; }
P p = new P { x: 1, y: 2 };
int[] a = [1, 2, 3];
print(p.x);
""")
check("go enum iota",   "Nivel_" not in g and "Cor_R Cor = iota" in g)
# campos exportados + tag json (necessário p/ encoding/json)
check("go struct",      "type P struct {" in g and 'X int64 `json:"x"`' in g)
check("go func tipada", "func area(r float64) float64 {" in g)
check("go struct init", "P{X: 1, Y: 2}" in g)
check("go array lit",   "[]int64{1, 2, 3}" in g)
check("go field access", "p.X" in g)

print("[go] seguranca")
g = gen_go("fn f(int a, int b) -> int ={ return a*b + a - b; }")
check("go mul overflow", "cryoMulOvf" in g)
check("go add overflow", "cryoAddOvf" in g)
check("go sub overflow", "cryoSubOvf" in g)
g = gen_go("int q = 10 % 3; int d = 8 / 2;")
check("go div check", "cryoIDivChk" in g)
check("go mod check", "cryoIModChk" in g)
gu = gen_go("int x = 0; unsafe { x = 5 * 5; }")
check("go unsafe sem overflow", "cryoMulOvf" not in gu)
check("go --unsafe global", "cryoMulOvf" not in gen_go("fn f(int a)->int ={ return a*a; }", safe=False))
check("go assert", "cryoAssert" in gen_go('assert(1==1, "ok");'))

print("[go] recursos de alto nivel")
check("go null coalescing", "cryoOr(" in gen_go('string s = null; string r = s ?? "x"; print(r);'))
check("go null->zero", 'var s string = ""' in gen_go('string s = null; print(s);'))
check("go concat int->str", "cryoStr(" in gen_go('int n = 5; string s = "n=" + n; print(s);'))
check("go try/catch->recover",
      "recover()" in gen_go('try { throw("x"); } catch (string e) { print(e); }'))
check("go switch",
      "switch d {" in gen_go("fn f(int d)->int ={ switch(d){ case 1: return 1; default: return 0; } }"))
check("go while->for", "for (" in gen_go("int i=0; while(i<3){ i++; }"))
check("go bloco C omitido",
      "omitido no backend Go" in gen_go('>C( printf("x"); )'))

# ── novos recursos v0.6 (ternario, do-while, for-each, etc.) ─
print("[v0.6] novos recursos — frontend")
ast = ast_of("int x = c ? 1 : 2;")
from ast_nodes import TernaryExpr, DoWhile, ForEach
check("ternario parseia", isinstance(ast.statements[0].value, TernaryExpr))
check("do-while parseia",
      isinstance(ast_of("do { x++; } while(x<3);").statements[0], DoWhile))
check("for-each parseia",
      isinstance(ast_of("for (int x in a) { print(x); }").statements[0], ForEach))
lx = Lexer("a %= 1; b &= 2; c |= 3; d ^= 4; e <<= 5; f >>= 6;").tokenize()
opnames = [t.type.name for t in lx if t.type.name.endswith('_ASSIGN')]
check("compostos estendidos", set(opnames) >= {
    'PERCENT_ASSIGN','AMP_ASSIGN','PIPE_ASSIGN','CARET_ASSIGN','SHL_ASSIGN','SHR_ASSIGN'})

print("[v0.6] backend Go")
check("go ternario (IIFE)", "func()" in gen_go("int s = c ? 1 : 2;") and "return 1" in gen_go("int s = c ? 1 : 2;"))
check("go do-while", "for {" in gen_go("do { x++; } while(x<3);"))
check("go for-each range", "range" in gen_go("int[] a = [1,2]; for (int x in a) { print(x); }"))
check("go compostos", "x <<= 2" in gen_go("int x = 1; x <<= 2;"))
check("go min/max builtin", "min(" in gen_go("int m = min(3, 4);"))
check("go round via math", "math.Round" in gen_go("number r = round(3.5);"))

print("[v0.6] backend C")
check("c ternario nativo", "?" in gen_c("int s = c ? 1 : 2;"))
check("c do-while", "do {" in gen_c("do { x++; } while(x<3);") and "} while" in gen_c("do { x++; } while(x<3);"))
check("c for-each", "->length" in gen_c("int[] a = [1,2]; for (int x in a) { print(x); }"))
check("c min helper", "cryo_min_i" in gen_c("int m = min(3, 4);"))
check("c floor helper", "cryo_floor" in gen_c("number f = floor(2.5);"))

print("[v0.6] backend asm — compound op fix")
check("asm <<= usa <<", "sal rax, cl" in gen_asm("int x = 1; x <<= 3; print(x);"))

# ── Fase 1: maps, JSON, optionals (backend Go) ──────────────
print("[fase1] mapas")
g = gen_go('map<string,int> m = {"a": 1}; m["b"] = 2; int v = m["a"];')
check("go map tipo", "map[string]int64" in g)
check("go map literal", 'map[string]int64{"a": 1}' in g)
check("go map index write", 'm["b"] = 2' in g)
check("go map index read", 'm["a"]' in g)
check("go map has", "has" not in gen_go('map<string,int> m = {}; bool b = has(m,"x");') or "ok :=" in gen_go('map<string,int> m = {}; bool b = has(m,"x");'))
check("go map keys", "cryoKeys" in gen_go('map<string,int> m = {}; for (string k in keys(m)) { print(k); }'))
check("go map vazio inicializa", "map[string]int64{}" in gen_go('map<string,int> m; m["a"]=1;'))

print("[fase1] JSON")
g = gen_go('struct U { string nome; int idade; } U u = new U{nome:"A",idade:1}; string s = json_encode(u);')
check("go json_encode", "cryoJSONEncode" in g)
check("go json tag lowercase", '`json:"nome"`' in g)
check("go encoding/json import", '"encoding/json"' in g)
g = gen_go('struct U { string nome; } U v = json_decode(s) as U;')
check("go json_decode as T", "json.Unmarshal" in g and "var _v U" in g)

print("[fase1] opcionais / null-safety")
check("go optional tipo ptr", "*int64" in gen_go("int? x = null;"))
check("go optional null->nil", "= nil" in gen_go("int? x = null;"))
check("go optional valor->ptr", "cryoPtr" in gen_go("int? x = 5;"))
check("go optional ?? usa orptr", "cryoOrPtr" in gen_go("int? x = null; int y = x ?? 0;"))
check("go unwrap", "cryoUnwrap" in gen_go('string? n = "a"; string s = n!;'))
# retorno opcional: valor base -> cryoPtr; valor ja opcional -> direto
check("go return valor->optional (cryoPtr)",
      "cryoPtr" in gen_go("fn f(int n) -> int? ={ if (n>0) { return n; } return null; }"))
check("go atribui chamada-opcional sem re-embrulhar",
      gen_go("fn f() -> int? ={ return null; } int? x = f();").count("cryoPtr") == 0)

print("[fase1] backend C rejeita com erro claro")
def expect_c_err(src, label):
    try:
        gen_c(src); check(label + " (deveria falhar)", False)
    except Exception as e:
        check(label, "backend go" in str(e).lower())
expect_c_err('map<string,int> m = {};', "c rejeita map")
expect_c_err('int? x = null;', "c rejeita optional")

# ── Pyro: skills nativas + acesso à máquina (backend Go) ────
print("[pyro] skills nativas")
SK = '''
skill resumir {
    desc: "Resume texto";
    model: "gpt-x";
    temperature: 0.2;
    tools: ["contar"];
}
string[] ns = skills();
Skill s = skill_get("resumir");
print(s.desc);
print(s.config["temperature"]);
string j = skills_json();
'''
g = gen_go(SK)
check("go tipo Skill emitido", "type Skill struct {" in g)
check("go registro cryoSkills", "var cryoSkills = map[string]Skill{" in g)
check("go skill literal", 'Skill{Name: "resumir"' in g and 'Desc: "Resume texto"' in g)
check("go skill tools", '[]string{"contar"}' in g)
check("go skill config compacto", 'Config: map[string]string{"temperature": "0.2"' in g)
check("go skills() names", "cryoSkillNames()" in g)
check("go skill_get", 'cryoSkills["resumir"]' in g)
check("go skill field access", "s.Desc" in g)
check("go skills_json", "cryoJSONEncode(cryoSkillList())" in g)
check("go sort import (skills)", '"sort"' in g)

print("[pyro] acesso à máquina")
check("go pyro_exec", "cryoExec(" in gen_go('string o = pyro_exec("ls");'))
check("go pyro_env->os.Getenv", "os.Getenv(" in gen_go('string u = pyro_env("HOME");'))
check("go pyro_args->os.Args", "os.Args" in gen_go("string[] a = pyro_args();"))
check("go pyro_time->UnixMilli", "time.Now().UnixMilli()" in gen_go("int t = pyro_time();"))
check("go pyro_exit->os.Exit", "os.Exit(int(" in gen_go("pyro_exit(1);"))
check("go pyro_exec cross-platform", 'runtime.GOOS == "windows"' in gen_go('string o = pyro_exec("x");'))

print("[pyro] backend C rejeita")
def expect_c_err2(src, label):
    try:
        gen_c(src); check(label + " (deveria falhar)", False)
    except Exception as e:
        check(label, "backend go" in str(e).lower())
expect_c_err2('skill s { desc: "x"; }', "c rejeita skill")
expect_c_err2('string o = pyro_exec("x");', "c rejeita pyro_exec")

# ── Pyro: bytecode próprio (.pyro) ──────────────────────────
print("[pyro-bc] bytecode e formato")
from codegen_pyro import CodeGenPyroError as _PErr
bc = gen_pyro('fn f(int n) -> int ={ return n * 2; } int x = f(21); print(x);')
check("pyro é bytes", isinstance(bc, (bytes, bytearray)))
check("pyro magic PYRO", bc[:4] == b'PYRO')
check("pyro versão 1", bc[4] == 1)
check("pyro flag codificado (XOR)", (bc[5] & 0x01) == 1)
check("pyro const pool tem nomes de função", b'main' in bc and b'f' in bc)
check("pyro sem encode = flag 0", (gen_pyro('print(1);', encode=False)[5] & 0x01) == 0)

print("[pyro-bc] cobertura e erros")
check("pyro if/while/for geram", isinstance(
    gen_pyro('int s=0; for(int i=0;i<3;i++){ s+=i; } while(s>0){ s--; } if(s==0){ print(s); }'),
    (bytes, bytearray)))
check("pyro ternario/do-while geram", isinstance(
    gen_pyro('int a = true ? 1 : 2; do { a++; } while (a < 3); print(a);'),
    (bytes, bytearray)))
def expect_pyro_err(src, label):
    try:
        gen_pyro(src); check(label + " (deveria falhar)", False)
    except _PErr:
        check(label, True)
expect_pyro_err('struct P { int x; }', "pyro rejeita struct")
expect_pyro_err('int[] a = [1,2];', "pyro rejeita array")
expect_pyro_err('map<string,int> m = {};', "pyro rejeita map")

# ── auditoria estatica ──────────────────────────────────────
print("[audit] regras")
f = audit_ast(ast_of(">C( printf(\"x\"); )"))
check("foreign-block ALTO", any(x.rule == 'foreign-block' and x.level == 'ALTO' for x in f))
f = audit_ast(ast_of("int x = 5; unsafe { x = 1; }"))
check("unsafe-block MEDIO", any(x.rule == 'unsafe-block' for x in f))
f = audit_ast(ast_of("int x = 5 / 0;"))
check("div-by-zero ALTO", any(x.rule == 'div-by-zero' for x in f))

# ── resultado ───────────────────────────────────────────────
print(f"\n{_passed} passaram, {_failed} falharam")
sys.exit(1 if _failed else 0)
