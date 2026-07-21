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
from codegen_node import CodeGenNode, CodeGenNodeError  # Burnout / backend Node/JS

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

def gen_go(src, safe=True, sandbox=False):
    return CodeGenGo(safe=safe, sandbox=sandbox).generate(Parser(Lexer(src).tokenize()).parse())

def gen_pyro(src, safe=True, encode=True, sandbox=False):
    return CodeGenPyro(safe=safe, encode=encode,
                       sandbox=sandbox).generate(Parser(Lexer(src).tokenize()).parse())

def gen_node(src, safe=True):
    return CodeGenNode(safe=safe).generate(Parser(Lexer(src).tokenize()).parse())

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
# divisao INT64_MIN/-1 aborta (como no runtime C); modulo retorna 0
check("go idiv guarda INT64_MIN/-1", "a == -1<<63 && b == -1" in g and "INT64_MIN / -1" in g)
check("go imod INT64_MIN%-1 -> 0 (nao aborta)",
      "a == -1<<63 && b == -1" in g and "return 0" in g and "INT64_MIN modulo" not in g)
check("go mul guarda INT64_MIN*-1",
      "a == -1<<63 && b == -1 || b == -1<<63 && a == -1" in
      gen_go("fn f(int a, int b) -> int ={ return a * b; }"))
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
      "omitido no backend Go" in gen_go('import >C< >C( printf("x"); )'))

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

# ── Fase 2: concorrência (async) + HTTP (backend Go) ────────
print("[fase2] async: spawn / await / future")
check("go future<T> -> chan", "chan int64" in gen_go("future<int> f = spawn g(); int r = await f;"))
check("go spawn -> goroutine+canal",
      all(s in gen_go("future<int> f = spawn h();")
          for s in ("make(chan int64, 1)", "go func()", "<-")))
check("go await -> receber do canal", "(<-f)" in gen_go("future<int> f = spawn h(); int r = await f;"))
check("go future array", "[]chan int64" in gen_go("future<int>[] ts = [];"))
check("go for-each sobre futures",
      "range ts" in gen_go("future<int>[] ts=[]; int s=0; for (future<int> t in ts) { s += await t; }"))

print("[fase2] HTTP + sleep")
check("go http_get -> helper", "cryoHTTPGet(" in gen_go('string b = http_get("http://x");'))
check("go http_get importa net/http+io",
      all(imp in gen_go('string b = http_get("http://x");') for imp in ('"net/http"', '"io"')))
check("go http_post -> helper", "cryoHTTPPost(" in gen_go('string r = http_post("http://x", "{}");'))
check("go sleep -> time.Sleep", "time.Sleep(" in gen_go("sleep(100);"))

# ── Fase 3: LLM nativo (schema / llm / tool) — backend Go ───
print("[fase3] schema + schema_of")
g = gen_go('schema Fatura { string cliente; number total; string[] itens; } string s = schema_of(Fatura); print(s);')
check("go schema = struct", "type Fatura struct {" in g)
check("go schema_of gera JSON Schema",
      all(x in g for x in ('\\"type\\": \\"object\\"', '\\"cliente\\"', '\\"required\\"')))

print("[fase3] llm structured output")
g = gen_go('schema F { string nome; } F f = llm("m", "p") as F; print(f.nome);')
check("go llm...as T -> cryoLLM + Unmarshal", "cryoLLM(" in g and "json.Unmarshal" in g)
check("go llm...as T passa o schema", '\\"nome\\"' in g)
check("go llm raw", 'cryoLLM("m", "p", "")' in gen_go('string r = llm("m", "p"); print(r);'))

print("[fase3] tools")
g = gen_go('tool fn buscar(string sku) -> number ={ return 1.0; } print(tools_json());')
check("go tool registra", "var cryoTools = map[string]Tool{" in g and '"buscar"' in g)
check("go tool params schema da assinatura", '\\"sku\\"' in g)
check("go tools()", "cryoToolNames()" in gen_go('tool fn f() -> int ={ return 1; } string[] t = tools();'))
check("go tools_json", "cryoJSONEncode(cryoToolList())" in g)

print("[fase3] agent (laço de tool-calling)")
g = gen_go('tool fn buscar(string sku) -> number ={ return 1.0; } string r = agent("m","p"); print(r);')
check("go agent -> cryoAgent", "cryoAgent(" in g)
check("go agent emite laço", "func cryoAgent(model, prompt string, only []string, maxSteps int) string {" in g and "tool_call" in g)
# agent configurável: subconjunto de tools + limite de passos
check("go agent subconjunto de tools + passos",
      '[]string{"buscar"}' in gen_go('tool fn buscar(string s)->int ={ return 1; } string r = agent("m","p",["buscar"],3);'))
check("go pyro_write_file", "os.WriteFile(" in gen_go('bool ok = pyro_write_file("a.txt", "oi");'))
_po = gen_go('bool ok = pyro_open("build/x.html");')
check("go pyro_open chama helper", "cryoOpen(" in _po)
check("go pyro_open emite cryoOpen + start", "func cryoOpen(target string) bool {" in _po
      and 'exec.Command("cmd", "/c", "start"' in _po and 'exec.Command("xdg-open"' in _po)
check("go dispatcher cryoToolCall", "func cryoToolCall(name, args string) string {" in g)
check("go dispatcher chama a tool real", "buscar(_a.Sku)" in g)
check("go dispatcher desempacota args", 'Sku string `json:"sku"`' in g)
check("go dispatcher com retorno struct", "json.Marshal(_r)" in gen_go(
      'struct P{int x;} tool fn t()->P ={ return new P{x:1}; } string j = agent("m","p"); print(j);'))

print("[fase3] coerção int/float em aritmética mista")
check("go int*float coage p/ float64",
      "float64(" in gen_go("fn f(int q) -> number ={ return 12.5 + q * 6.0; }"))
check("go int<float coage", "float64(" in gen_go("bool b = 3 < 3.5;"))

print("[fase3] backend C rejeita")
def _c_err3(src, label):
    try: gen_c(src); check(label + " (deveria falhar)", False)
    except Exception as e: check(label, "backend go" in str(e).lower())
_c_err3('string s = schema_of(F); print(s);', "c rejeita schema_of")
_c_err3('int x = 0; string r = llm("m","p"); print(r);', "c rejeita llm")

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
check("pyro versão 2 (saltos i32 + debug)", bc[4] == 2)
check("pyro flag codificado (XOR)", (bc[5] & 0x01) == 1)
check("pyro flag debug presente", (bc[5] & 0x02) == 2)
check("pyro const pool tem nomes de função", b'main' in bc and b'f' in bc)
check("pyro sem encode = flag 0", (gen_pyro('print(1);', encode=False)[5] & 0x01) == 0)

print("[pyro-bc] sandbox (--sandbox)")
import disasm_pyro
_bc_sbx = gen_pyro('string b = http_get("http://x");', sandbox=True)
check("pyro --sandbox grava flag bit2", (_bc_sbx[5] & 0x04) == 0x04)
check("pyro sem --sandbox não grava bit2",
      (gen_pyro('string b = http_get("http://x");')[5] & 0x04) == 0)
_dis_sbx = disasm_pyro.disassemble(gen_pyro('print(1);', encode=False, sandbox=True))
check("disasm mostra 'sandbox' nas flags", "sandbox" in _dis_sbx)

print("[go] sandbox (--sandbox)")
_go_sbx = gen_go('string o = pyro_exec("ls");', sandbox=True)
check("go --sandbox: var cryoSandbox = true", "var cryoSandbox = true" in _go_sbx)
check("go --sandbox: guarda no cryoExec",
      'cryoSandboxGuard("pyro_exec")' in _go_sbx)
_go_plain = gen_go('string b = http_get("http://x");')
check("go sem --sandbox: baked false + override por env",
      "var cryoSandbox = false" in _go_plain and
      'os.Getenv("PYRO_SANDBOX")' in _go_plain)
check("go: http_get recebe guarda de sandbox",
      'cryoSandboxGuard("http_get")' in _go_plain)
_go_wf = gen_go('bool ok = pyro_write_file("a.txt", "d");', sandbox=True)
check("go: pyro_write_file guardado por sandbox",
      'cryoSandboxGuard("pyro_write_file")' in _go_wf)
check("go sem builtin sensível não emite sandbox",
      "cryoSandbox" not in gen_go('int x = 1; print(x);'))

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
expect_pyro_err('skill s { desc: "x"; }', "pyro rejeita skill")
expect_pyro_err('import >c< >C( x )', "pyro rejeita bloco estrangeiro")

# ── enum + builtins nativos (NATIVE) no backend pyro ────────
print("[pyro-bc] enum + builtins nativos (NATIVE)")
check("pyro enum compila", isinstance(
      gen_pyro("enum Cor{V,A} Cor c = Cor_A; print(c == Cor_A);"), (bytes, bytearray)))
def _pyro_dis(src):
    import disasm_pyro
    return disasm_pyro.disassemble(gen_pyro(src, encode=False))
d = _pyro_dis("print(sqrt(16.0));")
check("pyro sqrt vira NATIVE", "NATIVE 0 1" in d and "sqrt(argc=1)" in d)
d = _pyro_dis('print(upper("a"));')
check("pyro upper vira NATIVE 12", "NATIVE 12 1" in d)
d = _pyro_dis('print(join(split("a,b", ","), "-"));')
check("pyro split/join NATIVE", "split(argc=2)" in d and "join(argc=2)" in d)
check("pyro to_int compila", isinstance(
      gen_pyro('print(to_int("42") + 1);'), (bytes, bytearray)))
check("pyro remove compila", isinstance(
      gen_pyro('map<string,int> m = {"a":1}; remove(m, "a"); print(has(m,"a"));'),
      (bytes, bytearray)))
def _pyro_argc_err(src):
    try:
        gen_pyro(src); return False
    except _PErr:
        return True
check("pyro NATIVE valida argc", _pyro_argc_err('print(pow(2.0));'))

print("[pyro-bc] containers (arrays/maps/structs)")
# opcodes esperados no código (usa encode=False p/ ler os bytes em claro)
import disasm_pyro
def _pyro_code_ops(src):
    return disasm_pyro.disassemble(gen_pyro(src, encode=False))
check("pyro array (NEWARR/APPEND)",
      "NEWARR" in _pyro_code_ops('int[] a = [1,2,3]; a.push(4);'))
check("pyro index (INDEX/SETIDX)",
      "SETIDX" in _pyro_code_ops('int[] a=[1]; a[0]=9; int x=a[0];') and
      "INDEX" in _pyro_code_ops('int[] a=[1]; int x=a[0];'))
check("pyro map (NEWMAP/HAS/KEYS)",
      all(op in _pyro_code_ops('map<string,int> m = {"a":1}; bool b = has(m,"a"); print(len(m));')
          for op in ("NEWMAP", "HAS", "LEN")))
check("pyro struct = map + field access (INDEX)",
      "NEWMAP" in _pyro_code_ops('struct P{int x;} P p = new P{x:1}; int y = p.x;'))
check("pyro for-each gera", isinstance(
      gen_pyro('int[] a=[1,2]; int s=0; for (int v in a) { s+=v; }'), (bytes, bytearray)))

# ── backend Node.js / JavaScript ────────────────────────────
print("[node] backend JavaScript (CommonJS)")
check("node use strict + console.log", gen_node('print("oi");').startswith('"use strict"')
      and 'console.log("oi")' in gen_node('print("oi");'))
check("node funcao + return",
      "function quad(n)" in gen_node("fn quad(int n)->int ={ return n*n; }"))
check("node for-each -> of",
      "of nums" in gen_node("int[] nums=[1,2]; int s=0; for (int v in nums){ s+=v; }"))
check("node struct -> objeto literal",
      "{x: 3, y: 4}" in gen_node("struct P{int x; int y;} P p = new P{x:3,y:4};"))
check("node map -> objeto + has/keys",
      'Object.prototype.hasOwnProperty' in gen_node(
          'map<string,int> m = {"a":1}; bool h = has(m,"a");'))
check("node enum -> const idx",
      "const Cor_VERDE = 1" in gen_node("enum Cor{VERMELHO,VERDE,AZUL} Cor c = Cor_VERDE;"))
# inferência de tipos: int/int -> divisão inteira; number -> float
check("node int/int -> cryoIDiv (divisão inteira)", "cryoIDiv(" in gen_node("int x = 10 / 2;"))
check("node number/number -> cryoDiv (float)",
      "cryoDiv(" in gen_node("number x = 10.0 / 2.0;"))
check("node array index bounds-check", "cryoIndex(" in gen_node("int[] a=[1,2]; int v = a[0];"))
check("node map[k] sem bounds-check",
      "cryoIndex(" not in gen_node('map<string,int> m = {"a":1}; int v = m["a"];'))
check("node string index bounds-check",
      "cryoIndex(" in gen_node('string s = "abc"; string c = s[1];'))
check("node escrita aninhada bounds-check (interna+externa)",
      "cryoSetIndex(cryoIndex(" in gen_node("int[][] m = [[1]]; m[0][0] = 9;"))
check("node leitura aninhada bounds-check",
      "cryoIndex(cryoIndex(" in gen_node("int[][] m = [[1]]; int v = m[0][0];"))
check("node campo-array write bounds-check",
      "cryoSetIndex(" in gen_node("struct S{int[] xs;} S s = new S{xs:[1]}; s.xs[0] = 9;"))
check("node len -> cryoLen", "cryoLen(" in gen_node('int n = len("abc");'))
check("node switch com break automatico",
      "break;" in gen_node("fn f(int d)->int ={ switch(d){ case 1: print(\"um\"); default: print(\"x\"); } }"))
# library node -> require; bloco Node emitido; bloco Go omitido
check("node library >node fs< -> require",
      'const fs = require("fs");' in gen_node('import >node< library >node fs< >Node( fs.stat("x"); )'))
check("node bloco >Node< emitido",
      "os.platform()" in gen_node('import >node< >Node( console.log(os.platform()); )'))
check("node bloco >Go< omitido",
      "omitido no backend Node" in gen_node('import >go< import >node< >Go( fmt.Println(1) )'))
# rejeita recursos fora do nucleo
def _node_raises(src):
    try:
        gen_node(src); return False
    except CodeGenNodeError:
        return True
check("node rejeita tool fn", _node_raises("tool fn t()->int ={ return 1; }"))
check("node rejeita llm", _node_raises('string r = llm("m","p");'))
check("node rejeita spawn/await", _node_raises("future<int> f = spawn g(1); int r = await f;"))
check("node rejeita pyro_exec", _node_raises('string s = pyro_exec("x");'))

# ── builtins de string (go / node) ──────────────────────────
print("[strings] builtins de string em go/node")
g = gen_go('string s = upper(trim("  a  ")); bool b = contains(s, "A"); '
           'int i = find(s, "A"); string r = replace(s, "A", "B"); '
           'string sub = substr(s, 0, 1); string[] p = split("a,b", ","); '
           'string j = join(p, "-");')
check("go strings.ToUpper/TrimSpace", "strings.ToUpper" in g and "strings.TrimSpace" in g)
check("go contains/find/replace", "strings.Contains" in g and "strings.Index" in g
      and "strings.ReplaceAll" in g)
check("go substr helper", "func cryoSubstr(" in g)
check("go split/join", "strings.Split" in g and "strings.Join" in g)
check("go to_int(string) -> parse", "cryoParseInt(" in gen_go('int n = to_int("42");'))
check("go to_number(string) -> parse", "cryoParseNum(" in gen_go('number n = to_number("1.5");'))
check("go to_int(number) segue cast", "int64(" in gen_go('number f=1.9; int n = to_int(f);'))
nj = gen_node('string s = upper("a"); bool b = contains(s, "A"); '
              'string[] p = split("a,b", ","); string j = join(p, "-"); '
              'string sub = substr(s, 0, 1);')
check("node toUpperCase/includes", "toUpperCase()" in nj and ".includes(" in nj)
check("node split/join/substr", ".split(" in nj and ".join(" in nj and "cryoSubstr(" in nj)
try:
    gen_c('string s = upper("a");'); check("c rejeita upper()", False)
except Exception:
    check("c rejeita upper()", True)

# ── blocos estrangeiros verificados + libraries ─────────────
print("[foreign] verificacao de blocos estrangeiros + libraries")
from foreign import verify as _verify, ForeignError as _ForeignError, \
    resolve_library_lang as _resolve_lib

def _verify_raises(src):
    try:
        _verify(ast_of(src)); return False
    except _ForeignError:
        return True

# bloco sem import -> rejeitado
check("bloco >C< sem import falha", _verify_raises('>C( printf("x"); )'))
check("bloco >Go< sem import falha", _verify_raises('>Go( fmt.Println("x") )'))
# bloco com import -> ok
check("bloco >C< com import passa",
      _verify(ast_of('import >C< >C( printf("x"); )')) == {'c'})
# import case-insensitive
check("import >C< cobre bloco >c<",
      not _verify_raises('import >C< >c( printf("x"); )'))
# library nao qualificada exige import; ambigua com 2 langs
check("library sem import falha", _verify_raises('library >math<'))
check("library ambigua (2 langs) falha",
      _verify_raises('import >c< import >go< library >math<'))
check("library qualificada exige seu import",
      _verify_raises('import >go< library >c math<'))
check("library qualificada ok com import",
      not _verify_raises('import >c< library >c math<'))
# resolucao de linguagem da library
from ast_nodes import Library as _Lib
check("resolve library explicita", _resolve_lib(_Lib(name='fmt', lang='go'), {'go'}) == 'go')
check("resolve library por unica importada", _resolve_lib(_Lib(name='math', lang=''), {'c'}) == 'c')
# codegen: library vira import Go / include C
check("go library >go strings< -> import \"strings\"",
      '"strings"' in gen_go('import >go< library >go strings< >Go( _ = strings.ToUpper("a") )'))
check("c library >c math< -> include math.h",
      '#include <math.h>' in gen_c('import >c< library >c math< >C( double r = sqrt(2.0); )'))
check("c bloco >C< emitido com import",
      'sqrt(2.0)' in gen_c('import >c< >C( double r = sqrt(2.0); )'))

# ── seleção automática de backend ───────────────────────────
print("[auto] seleção de backend por análise de recursos")
from backends import select_backend as _select
def _sel(src):
    return _select(ast_of(src))[0]
check("auto núcleo puro -> pyro", _sel("int s=0; for(int i=0;i<3;i++){s+=i;} print(s);") == 'pyro')
check("auto arrays/maps/struct -> pyro",
      _sel('struct P{int x;} int[] a=[1]; map<string,int> m = {"k":1}; print(len(a));') == 'pyro')
check("auto enum -> pyro (agora suportado)", _sel("enum E{A,B} E e = E_A;") == 'pyro')
check("auto optional/json -> pyro (agora suportado)",
      _sel('number? x = null; string j = json_encode(x);') == 'pyro')
check("auto http -> pyro (agora suportado)",
      _sel('string r = http_get("http://x");') == 'pyro')
check("auto concorrencia -> go (pyro nao suporta spawn/await)",
      _sel('future<int> f = spawn g(1); int r = await f;') == 'go')
check("auto llm -> go", _sel('string r = agent("m","p");') == 'go')
check("auto machine (pyro_exec) -> go", _sel('string s = pyro_exec("x");') == 'go')
check("auto to_string/strings -> pyro (agora suportado)",
      _sel('int n=5; string s = upper(to_string(n));') == 'pyro')
check("auto try/catch -> pyro (agora suportado)",
      _sel('try { print(1); } catch (string e) { print(e); }') == 'pyro')
check("auto concorrencia -> go (pyro nao suporta)",
      _sel('future<int> f = spawn g(1); int r = await f;') == 'go')
check("auto bloco Go -> go", _sel('import >go< >Go( fmt.Println(1) )') == 'go')
check("auto bloco Node -> node", _sel('import >node< >Node( console.log(1); )') == 'node')
check("auto bloco C -> c", _sel('import >c< >C( printf("x"); )') == 'c')
check("auto conflito (Go+C) -> go fallback",
      _sel('import >go< import >c< >Go( x )\n>C( y )') == 'go')
# capacidades faltando por backend (base da sugestão do --audit)
from backends import missing_capabilities as _miss
def _mt(src, b):
    return _miss(ast_of(src), b)
check("miss: map em c -> {map}", 'map' in _mt('map<string,int> m = {"a":1};', 'c')[0])
check("miss: concorrencia em pyro -> {concurrency}",
      'concurrency' in _mt('future<int> f = spawn g(1); int r = await f;', 'pyro')[0])
check("miss: enum/try em pyro agora cobertos",
      _mt('enum E{A} try { print(1); } catch (string e) { print(e); }', 'pyro') == (set(), set()))
check("miss: bloco C em go -> {c}", 'c' in _mt('import >c< >C( x )', 'go')[1])
check("miss: go cobre map (sem faltas)",
      _mt('map<string,int> m = {"a":1};', 'go') == (set(), set()))

# ── Fase 4: módulos + interpolação + const global no pyro ───
print("[fase4] modulos (import \"arquivo.cryo\")")
import tempfile, shutil
from modules import resolve_modules, ModuleError
from ast_nodes import ModuleImport as _MI, FunctionDecl as _FD

_tmp = tempfile.mkdtemp(prefix="cryo_mod_")
def _w(name, content):
    p = os.path.join(_tmp, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(content)
    return p

_w("util.cryo", 'const int BASE = 10\nfn dobro(int x) -> int ={ return x * 2; }\nprint("nao deve rodar");\n')
_w("fundo.cryo", 'fn triplo(int x) -> int ={ return x * 3; }\n')
_w("meio.cryo", 'import "fundo.cryo"\nfn quad(int x) -> int ={ return triplo(x) + x; }\n')
_w("ciclo_a.cryo", 'import "ciclo_b.cryo"\nfn fa() -> int ={ return 1; }\n')
_w("ciclo_b.cryo", 'import "ciclo_a.cryo"\nfn fb() -> int ={ return 2; }\n')
_w("colide.cryo", 'fn dobro(int x) -> int ={ return x; }\n')

# parser reconhece a forma de módulo
ast = ast_of('import "util.cryo" print(dobro(4));')
check("parser: import \"...\" -> ModuleImport", isinstance(ast.statements[0], _MI))
# resolução: declarações entram, statements executáveis do módulo não
r = resolve_modules(ast, _tmp)
fns = [s.name for s in r.statements if isinstance(s, _FD)]
check("resolve: fn do modulo incorporada", 'dobro' in fns)
check("resolve: sem ModuleImport no resultado",
      not any(isinstance(s, _MI) for s in r.statements))
from ast_nodes import CallExpr as _CE
check("resolve: print de topo do modulo ignorado",
      sum(1 for s in r.statements if isinstance(s, _CE) and s.callee == 'print') == 1)
# aninhado + dedup (mesmo arquivo por 2 caminhos)
r2 = resolve_modules(ast_of('import "meio.cryo" import "fundo.cryo" print(quad(2));'), _tmp)
fns2 = [s.name for s in r2.statements if isinstance(s, _FD)]
check("resolve: import aninhado", 'triplo' in fns2 and 'quad' in fns2)
check("resolve: dedup (fundo 1x)", fns2.count('triplo') == 1)
# erros: ciclo, colisão, ausente
def _mod_err(src):
    try:
        resolve_modules(ast_of(src), _tmp); return False
    except ModuleError:
        return True
check("resolve: ciclo detectado", _mod_err('import "ciclo_a.cryo"'))
check("resolve: colisao de nome detectada",
      _mod_err('import "util.cryo" import "colide.cryo"'))
check("resolve: modulo ausente", _mod_err('import "nao_existe.cryo"'))
check("resolve: colisao com o programa principal",
      _mod_err('import "util.cryo" fn dobro(int x) -> int ={ return x; }'))
# codegen ponta-a-ponta com módulo (via compile_source do compiler)
import compiler as _comp
g = _comp.compile_source('import "util.cryo" print(dobro(21));', 'go', True, base_dir=_tmp)
check("compile_source resolve modulo (go)", "func dobro(" in g)
bc = _comp.compile_source('import "util.cryo" print(dobro(21) + BASE);', 'pyro', True, base_dir=_tmp)
check("compile_source resolve modulo (pyro)", isinstance(bc, (bytes, bytearray)))
shutil.rmtree(_tmp, ignore_errors=True)

print("[fase4] interpolacao de strings")
g = gen_go('int n = 3; print("n vale ${n}!");')
check("interp vira concat + to_string", "cryoStr" in g and '"n vale "' in g)
g = gen_go('print("${1 + 1}");')
check("interp no inicio ganha contexto string", '"" +' in g.replace("  ", " ") or '""+' in g)
check("interp expressao aninhada", isinstance(
      gen_pyro('map<string,int> m = {"a":1}; print("a=${m[\\"a\\"]}");'), (bytes, bytearray)))
check("string sem ${} intacta", '"sem interp"' in gen_go('print("sem interp");'))
def _interp_err(src):
    try:
        ast_of(src); return False
    except Exception:
        return True
check("interp sem fechamento falha", _interp_err('print("x ${aberto");'))
check("interp vazia falha", _interp_err('print("x ${}");'))

print("[fase4] const global no backend pyro")
check("pyro const global inlined em fn", isinstance(
      gen_pyro('const number PI = 3.14\nfn area(number r) -> number ={ return PI * r * r; }\nprint(area(2.0));'),
      (bytes, bytearray)))
d = _pyro_dis('const int K = 7\nfn f() -> int ={ return K; }\nprint(f());')
check("pyro const global vira CONST (sem LOAD)", "; 7" in d)

# ── Fase 4/5: semântica, try/catch, opcionais, char, input ──
print("[fase4] analise semantica")
from semantic import check as _sem_check, SemanticError as _SemErr
def _sem_err(src):
    try:
        _sem_check(ast_of(src)); return False
    except _SemErr:
        return True
check("sem: variavel nao declarada", _sem_err("print(y);"))
check("sem: funcao desconhecida", _sem_err("foo(1);"))
check("sem: aridade errada",
      _sem_err("fn add(int a, int b)->int ={ return a+b; } print(add(1));"))
check("sem: atribuicao a nao declarada", _sem_err("x = 5;"))
check("sem: break fora de laco", _sem_err("break;"))
check("sem: declaracao duplicada",
      _sem_err("fn f()->int ={ return 1; } fn f()->int ={ return 2; }"))
check("sem: programa valido passa",
      not _sem_err("fn q(int n)->int ={ return n*n; } int s=0; for(int i=0;i<3;i++){ s+=q(i); } print(s);"))
check("sem: tipo usado em schema_of ok",
      not _sem_err("schema F { int x; } print(schema_of(F));"))
check("sem: enum membro ok", not _sem_err("enum E{A,B} E e = E_A; print(e == E_B);"))
check("sem: recursao/forward-ref ok",
      not _sem_err("fn par(int n)->bool ={ if(n==0){return true;} return impar(n-1); } "
                   "fn impar(int n)->bool ={ if(n==0){return false;} return par(n-1); } print(par(4));"))

print("[fase4] try/catch + opcionais no pyro")
def _pdis(src):
    import disasm_pyro
    return disasm_pyro.disassemble(gen_pyro(src, encode=False))
check("pyro try/catch compila", isinstance(
      gen_pyro('try { throw("x"); } catch (string e) { print(e); }'), (bytes, bytearray)))
d = _pdis('try { throw("x"); } catch (string e) { print(e); } finally { print("f"); }')
check("pyro emite TRYPUSH/TRYPOP/THROW", "TRYPUSH" in d and "TRYPOP" in d and "THROW" in d)
check("pyro ?? emite COALESCE", "COALESCE" in _pdis('int? x = null; int y = x ?? 5; print(y);'))
check("pyro x! emite UNWRAP", "UNWRAP" in _pdis('int? x = 3; int y = x!; print(y);'))
check("pyro const global inlined (CONST, sem LOAD do nome)",
      isinstance(gen_pyro('const int K = 9\nfn f()->int ={ return K + 1; } print(f());'),
                 (bytes, bytearray)))
# go/node continuam suportando (nao devem quebrar)
check("go try/catch ok", "recover()" in gen_go('try { throw("e"); } catch (string x) { print(x); }'))

print("[fase4] iteracao de caracteres")
check("go for-char converte rune->string", "string(_r_" in gen_go('for (string c in "ab") { print(c); }'))
check("node for-char usa of", "of " in gen_node('for (string c in "ab") { print(c); }'))
check("pyro for-char compila", isinstance(
      gen_pyro('for (string c in "ab") { print(c); }'), (bytes, bytearray)))

print("[fase5] input nativo no pyro")
check("pyro input vira NATIVE 21", "NATIVE 21 1" in _pdis('string s = input("? ");'))

print("[fase5] otimizador de bytecode (pyro)")
def _gen_pyro_noopt(src):
    return CodeGenPyro(safe=True, encode=False, optimize=False).generate(
        Parser(Lexer(src).tokenize()).parse())
import disasm_pyro as _dpm
def _dis_noopt(src):
    return _dpm.disassemble(_gen_pyro_noopt(src))
# constant folding: (0xF0|0x0F)&0xFF -> um único CONST 255, sem BAND/BOR
d = _pdis("int m = (0xF0 | 0x0F) & 0xFF; print(m);")
check("fold: bitwise -> CONST 255", "; 255" in d and "BOR" not in d and "BAND" not in d)
check("fold: aritmetica int (2*21 -> 42)", "; 42" in _pdis("int x = 2 * 21; print(x);"))
check("fold: negacao unaria (-(2*21) -> -42)", "; -42" in _pdis("int x = -(2 * 21); print(x);"))
check("fold: comparacao -> TRUE (sem GT)",
      "GT" not in _pdis("bool b = 100 > 50; print(b);"))
check("fold: concat de string literal",
      '"Ola, mundo"' in _pdis('string s = "Ola" + ", " + "mundo"; print(s);'))
check("fold: float preserva tipo (10.0, nao int)",
      "float 10" in _pdis("number x = (3.5 + 1.5) * 2.0; print(x);"))
check("fold NAO ocorre em --no-opt (mantem BOR/BAND)",
      "BOR" in _dis_noopt("int m = (0xF0 | 0x0F) & 0xFF; print(m);"))
# dead-code elimination: código após return some
d = _pdis('fn f(int x)->int ={ return x; print("morto"); return 9; } print(f(1));')
check("dce: remove codigo apos return", '"morto"' not in d)
check("dce: mantem no --no-opt",
      '"morto"' in _dis_noopt('fn f(int x)->int ={ return x; print("morto"); return 9; } print(f(1));'))
# prune de constantes: intermediários mortos somem do pool
d = _pdis("number x = (3.5 + 1.5) * 2.0; print(x);")
check("prune: intermediarios (3.5/1.5) fora do pool", "3.5" not in d and "1.5" not in d)
# tamanho: otimizado <= nao-otimizado
_src_big = "int a = (1 << 8) | 0x34; int b = 2 * 3 * 7; bool c = 10 > 5; print(a); print(b); print(c);"
check("otimizado <= nao-otimizado (bytes)",
      len(gen_pyro(_src_big)) <= len(_gen_pyro_noopt(_src_big)))
# correção: divisão inteira NÃO é dobrada (semântica de trunc/segurança fica p/ runtime)
check("fold: div inteira nao dobra (DIV presente)", "DIV" in _pdis("int x = 10 / 2; print(x);"))

print("[fase5] saltos i32 + info de depuração (pyro v2)")
# a seção de depuração aparece no disasm com "; linha N"
d = _pdis('fn f(int n)->int ={ return n * 2; } int x = f(3); print(x);')
check("debug: disasm anota linhas", "; linha" in d)
# tabela pc->linha no header (flag debug) + entradas
_bcj = gen_pyro('fn f(int n)->int ={ return n; } print(f(1));', encode=False)
check("debug: flag 0x02 setado", (_bcj[5] & 0x02) == 2)
# salto i32: JMP ocupa 5 bytes (opcode + i32). Um laço grande continua
# compilando e rodando (validação de que o offset i32 funciona).
_bigloop = "int s = 0; for (int i = 0; i < 3; i++) { s += i; } print(s);"
check("i32: laço compila e é v2", gen_pyro(_bigloop)[4] == 2)
# --no-opt não deve conter seção de debug quebrada (round-trip do disasm)
check("debug: disasm --no-opt ok", "; linha" in _dis_noopt(_bigloop))

print("[fase5] HTTP/sleep nativos no pyro")
check("pyro http_get vira NATIVE 24", "NATIVE 24 1" in _pdis('string c = http_get("http://x");'))
check("pyro http_post vira NATIVE 25", "NATIVE 25 2" in _pdis('string c = http_post("http://x", "b");'))
check("pyro sleep vira NATIVE 26", "NATIVE 26 1" in _pdis("sleep(5);"))
check("pyro http cobre backend (backends.py)",
      _mt('string c = http_get("http://x");', 'pyro') == (set(), set()))

print("[fase5] JSON nativo no pyro")
check("pyro json_encode vira NATIVE 22",
      "NATIVE 22 1" in _pdis('struct P{int x;} P p = new P{x:1}; string s = json_encode(p);'))
check("pyro json_decode vira NATIVE 23",
      "NATIVE 23 1" in _pdis('struct P{int x;} P p = json_decode("{}") as P; print(p.x);'))
check("pyro json compila (struct round-trip)", isinstance(
      gen_pyro('struct P{string a; int b;} P p = new P{a:"x", b:2}; '
               'string s = json_encode(p); P q = json_decode(s) as P; print(q.a);'),
      (bytes, bytearray)))
check("pyro json cobre backend (backends.py)",
      _mt('struct P{int x;} string s = json_encode(new P{x:1});', 'pyro') == (set(), set()))
check("auto json -> pyro (agora suportado)",
      _sel('struct P{int x;} string s = json_encode(new P{x:1});') == 'pyro')

# ── Fase 6: Language Server (LSP) ───────────────────────────
print("[fase6] language server (lsp)")
import lsp as _lsp
_src_ok = 'fn quad(int n) -> int ={ return n * n; }\nint s = quad(3);\nprint(s);\n'
_src_bad = 'fn add(int a, int b) -> int ={ return a+b; }\nprint(add(1));\nprint(zzz);\n'
check("lsp: programa válido sem diagnósticos",
      _lsp.compute_diagnostics("file:///x.cryo", _src_ok) == [])
_d = _lsp.compute_diagnostics("file:///x.cryo", _src_bad)
check("lsp: dois diagnósticos (aridade + não-declarada)", len(_d) == 2)
check("lsp: diagnóstico traz range/linha 0-based",
      all('range' in x and x['range']['start']['line'] >= 0 for x in _d))
check("lsp: erro léxico vira diagnóstico com coluna",
      _lsp.compute_diagnostics("file:///x.cryo", "int x = @;")[0]['range']['start']['character'] >= 0)
check("lsp: hover de builtin", "print" in (_lsp.hover("print(1);", 0, 1) or ""))
check("lsp: hover de função do usuário", "quad" in (_lsp.hover(_src_ok, 1, 9) or ""))
check("lsp: hover de palavra-chave", _lsp.hover("fn f()->int ={ return 1; }", 0, 0) is not None)
check("lsp: definição aponta p/ a declaração",
      _lsp.definition("file:///x.cryo", _src_ok, 1, 9)['range']['start']['line'] == 0)
_syms = _lsp.document_symbols("struct P{int x;}\nenum E{A,B}\nfn g()->int ={ return 1; }\n")
check("lsp: documentSymbol lista struct/enum/fn",
      {s['name'] for s in _syms} == {'P', 'E', 'g'})
check("lsp: word_at extrai identificador", _lsp._word_at("abc def", 0, 1) == 'abc')

# ── Fase 6: formatador (cryoc fmt) ──────────────────────────
print("[fase6] formatador (cryoc fmt)")
import format as _fmt
_messy = 'fn f(int n)->int ={\nif(n<1){\nreturn 1;\n}\nreturn n*f(n-1);\n}\nprint(f(5));\n'
_fm = _fmt.format_source(_messy)
check("fmt: reindenta a 4 espaços", "    if(n<1){" in _fm and "        return 1;" in _fm)
check("fmt: garante quebra final única", _fm.endswith("}\nprint(f(5));\n"))
check("fmt: idempotente", _fmt.format_source(_fm) == _fm)
check("fmt: preserva tokens (segurança)", _fmt._tokens(_messy) == _fmt._tokens(_fm))
# blocos estrangeiros ficam verbatim (token LANG_BLOCK inalterado)
_fsrc = 'import >C<\nfn g()->int ={\nreturn 1;\n}\n>C(\n    printf("x");\n)\n'
_ff, _ok = _fmt._safe_format(_fsrc)
check("fmt: bloco estrangeiro seguro (tokens iguais)", _ok)
# strings com chaves não confundem a profundidade
_sbr = 'string s = "a{b}c";\nprint(s);\n'
check("fmt: chaves em string não afetam indent", _fmt.format_source(_sbr) == _sbr)
# comentário de bloco verbatim
_cm = '/* bloco\n   com chaves { } */\nint x = 1;\n'
check("fmt: comentário de bloco preservado", "{ }" in _fmt.format_source(_cm))
# entrada já formatada é no-op
check("fmt: já formatado é no-op", _fmt.format_source(_fm) == _fm)
# comentários de linha preservados
check("fmt: comentário de linha preservado",
      "// oi" in _fmt.format_source("int x = 1; // oi\n"))

# ── auditoria estatica ──────────────────────────────────────
print("[audit] regras")
f = audit_ast(ast_of(">C( printf(\"x\"); )"))
check("foreign-block ALTO", any(x.rule == 'foreign-block' and x.level == 'ALTO' for x in f))
f = audit_ast(ast_of("int x = 5; unsafe { x = 1; }"))
check("unsafe-block MEDIO", any(x.rule == 'unsafe-block' for x in f))
f = audit_ast(ast_of("int x = 5 / 0;"))
check("div-by-zero ALTO", any(x.rule == 'div-by-zero' for x in f))
# operacoes sensiveis (maquina / rede / LLM)
f = audit_ast(ast_of('string s = pyro_exec("ls");'))
check("command-exec ALTO",
      any(x.rule == 'command-exec' and x.level == 'ALTO' for x in f))
f = audit_ast(ast_of('bool ok = pyro_write_file("a.txt", "x");'))
check("file-write MEDIO",
      any(x.rule == 'file-write' and x.level == 'MEDIO' for x in f))
f = audit_ast(ast_of('string s = http_get("http://x");'))
check("net-egress MEDIO",
      any(x.rule == 'net-egress' and x.level == 'MEDIO' for x in f))
f = audit_ast(ast_of('string r = agent("m", "p");'))
check("llm-egress BAIXO",
      any(x.rule == 'llm-egress' for x in f))

# ── analise de taint (fluxo de dados nao confiaveis) ─────────
print("[audit] taint")

def _rules(src):
    return {(x.level, x.rule) for x in audit_ast(ast_of(src))}

f = _rules('string c = input("cmd"); string o = pyro_exec(c);')
check("tainted-exec ALTO (input -> pyro_exec)",
      ('ALTO', 'tainted-exec') in f)

f = _rules('string o = pyro_exec(input("x"));')
check("tainted-exec ALTO (fonte direta no sink)",
      ('ALTO', 'tainted-exec') in f)

f = _rules('string o = pyro_exec("ls -la");')
check("comando literal NAO gera tainted-exec",
      ('ALTO', 'tainted-exec') not in f)

f = _rules('string a = input("a"); string b = a; string o = pyro_exec(b);')
check("taint propaga por atribuicao (a -> b -> sink)",
      ('ALTO', 'tainted-exec') in f)

f = _rules('string u = input("u"); string b = http_get(u);')
check("tainted-ssrf ALTO (input -> http_get)",
      ('ALTO', 'tainted-ssrf') in f)

f = _rules('string b = http_get("http://x");')
check("URL literal NAO gera tainted-ssrf",
      ('ALTO', 'tainted-ssrf') not in f)

f = _rules('string p = pyro_env("P"); bool ok = pyro_write_file(p, "d");')
check("tainted-path ALTO (pyro_env -> pyro_write_file)",
      ('ALTO', 'tainted-path') in f)

f = _rules('string t = input("t"); bool ok = pyro_open(t);')
check("tainted-open ALTO (input -> pyro_open)",
      ('ALTO', 'tainted-open') in f)

# ── segredos embutidos ───────────────────────────────────────
print("[audit] segredos")

f = _rules('const string K = "sk-abcdefghij0123456789";')
check("hardcoded-secret ALTO (formato de chave)",
      ('ALTO', 'hardcoded-secret') in f)

f = _rules('string api_key = "hunter2hunter2";')
check("hardcoded-secret MEDIO (nome sensivel)",
      ('MEDIO', 'hardcoded-secret') in f)

f = _rules('string nome = "Ana";')
check("string comum NAO gera hardcoded-secret",
      not any(r == 'hardcoded-secret' for _, r in f))

# ── [fase8] funções de 1ª classe + lambdas ──────────────────
print("[fase8] funções de 1ª classe + lambdas")
from ast_nodes import Lambda as _Lambda
from backends import select_backend as _select
from semantic import findings as _find

def _ast8(s):
    return Parser(Lexer(s).tokenize()).parse()

_p8 = _ast8('fn(int)->int f = (int x) => x * 2;')
check("parser: var de tipo função + lambda",
      _p8.statements[0].var_type == 'fn(int)->int' and
      isinstance(_p8.statements[0].value, _Lambda))
check("parser: agrupamento não vira lambda",
      _ast8('int a = (1 + 2) * 3;').statements[0].value.__class__.__name__ == 'BinaryExpr')
check("parser: declaração de função ainda funciona",
      _ast8('fn d(int x) -> int ={ return x*2; }').statements[0].__class__.__name__ == 'FunctionDecl')

_g8 = gen_go('fn(int)->int f = (int x) => x + 1; int r = f(41); print(r);')
check("go: lambda vira func literal", "func(x int64) int64" in _g8)
check("go: tipo de retorno função", "func(int64) int64" in
      gen_go('fn mk(int b) -> fn(int)->int ={ return (int x) => x + b; }'))

check("node: lambda vira arrow function",
      "=>" in gen_node('fn(int)->int f = (int x) => x + 1; int r = f(41); print(r);'))

def _pyro_err8(src):
    try:
        gen_pyro(src); return False
    except _PErr:
        return True
check("pyro: lambda barrada (fail-closed)", _pyro_err8('fn(int)->int f = (int x) => x + 1;'))
check("pyro: função como valor barrada",
      _pyro_err8('fn d(int n)->int ={return n;} fn(int)->int f = d;'))

_bsel, _ = _select(_ast8('fn(int)->int f = (int x) => x + 1; int r = f(1); print(r);'))
check("auto: firstclassfn não escolhe pyro", _bsel in ('go', 'node'))

check("semântica: função como valor não acusa 'não declarada'",
      _find(_ast8('fn d(int n)->int ={return n;} '
                  'fn ap(fn(int)->int f)->int ={return f(1);} '
                  'int r = ap(d); print(r);')) == [])

# ── enums com dados & match (etapa 8.2) ──────────────────────
print("[8.2] enums com dados e pattern matching")
from ast_nodes import EnumDecl, MatchStatement
from semantic import check as semantic_check, SemanticError

src_adt = """
enum Result {
    Ok(int),
    Err(string),
    Empty
}
Result r = Ok(42);
match r {
    Ok(v) => { print(v); }
    Err(e) => { print(e); }
    Empty => { print("vazio"); }
}
"""

ast_adt = Parser(Lexer(src_adt).tokenize()).parse()
check("parser: EnumDecl com EnumMember", isinstance(ast_adt.statements[0], EnumDecl) and len(ast_adt.statements[0].members) == 3)
check("parser: EnumMember Ok tem 1 campo", ast_adt.statements[0].members[0].fields == ["int"])
check("parser: EnumMember Empty tem 0 campos", ast_adt.statements[0].members[2].fields == [])
check("parser: MatchStatement no AST", any(isinstance(s, MatchStatement) for s in ast_adt.statements))

try:
    semantic_check(ast_adt)
    check("semântica: Result + match válido passa sem erros", True)
except Exception as ex:
    check(f"semântica: Result + match válido falhou: {ex}", False)

src_bad_match = """
enum Result {
    Ok(int),
    Err(string)
}
Result r = Ok(42);
match r {
    Ok(v) => { print(v); }
}
"""
try:
    semantic_check(Parser(Lexer(src_bad_match).tokenize()).parse())
    check("semântica: match não exaustivo deveria falhar", False)
except SemanticError:
    check("semântica: match não exaustivo falha (correto)", True)
except Exception as ex:
    check(f"semântica: match não exaustivo falhou com exceção inesperada: {ex}", False)

# Code generators output checks
go_code = gen_go(src_adt)
check("go: Result interface", "type Result interface" in go_code)
check("go: Result_Ok struct", "type Result_Ok struct" in go_code)
check("go: Ok constructor", "func Ok(" in go_code)
check("go: type switch in match", "switch __m := interface{}(" in go_code)

node_code = gen_node(src_adt)
check("node: Ok constructor factory", "const Ok = (val0)" in node_code)
check("node: tag switch in match", "switch (__match_subj_" in node_code)

pyro_dis = disasm_pyro.disassemble(gen_pyro(src_adt, encode=False))
check("pyro: variant functions compiled", "tag" in pyro_dis and "val0" in pyro_dis)
check("pyro: match compiled with EQ and jumps", "EQ" in pyro_dis and "JMPF" in pyro_dis)

try:
    gen_c(src_adt)
    check("c: enums com dados barrado", False)
except Exception:
    check("c: enums com dados barrado (correto)", True)

try:
    gen_asm(src_adt)
    check("asm: enums com dados barrado", False)
except Exception:
    check("asm: enums com dados barrado (correto)", True)

# ── [8.3] propagação de erro com '?' ────────────────────────
print("[8.3] propagação de erro com '?'")

# parser: expr? vira TryExpr; ternário continua ternário
from ast_nodes import TryExpr as _TryExpr, TernaryExpr as _TernaryExpr
_p83 = ast_of('int v = parse(s)?;')
check("parser: expr? vira TryExpr", isinstance(_p83.statements[0].value, _TryExpr))
_pt = ast_of('int x = c > 0 ? 1 : 2;')
check("parser: ternário não vira propagação",
      isinstance(_pt.statements[0].value, _TernaryExpr))

src_try = (
    'enum Result { Ok(int), Err(string) }\n'
    'fn parse(string s) -> Result ={ if (s == "42") { return Ok(42); } return Err("x"); }\n'
    'fn dobro(string s) -> Result ={ int v = parse(s)?; return Ok(v * 2); }\n'
    'Result r = dobro("42");\n'
)

g83 = gen_go(src_try)
check("go: propagação gera temporária + guarda",
      "__try" in g83 and ".(Result_Ok)" in g83)
check("go: via de erro retorna cedo", "!__ok { return __try" in g83)

n83 = gen_node(src_try)
check("node: propagação testa tag Ok", '.tag !== "Ok"' in n83 and "return __try" in n83)
check("node: desempacota val0", ".val0" in n83)

d83 = disasm_pyro.disassemble(gen_pyro(src_try, encode=False))
check("pyro: propagação usa JMPT + RET", "JMPT" in d83 and "RET" in d83)
check("pyro: propagação indexa tag/val0/Ok",
      '"tag"' in d83 and '"val0"' in d83 and '"Ok"' in d83)

# '?' aninhado em outra expressão é barrado com erro claro (go)
try:
    gen_go('enum R { Ok(int), Err(string) }\n'
           'fn f(string s) -> R ={ int v = parse(s)? + 1; return Ok(v); }')
    check("go: '?' aninhado barrado", False)
except Exception:
    check("go: '?' aninhado barrado (correto)", True)

# c/asm barram a propagação com erro claro
try:
    gen_c('fn f(int x) -> int ={ int v = g(x)?; return v; }')
    check("c: propagação barrada", False)
except Exception:
    check("c: propagação barrada (correto)", True)
try:
    gen_asm('fn f(int x) -> int ={ int v = g(x)?; return v; }')
    check("asm: propagação barrada", False)
except Exception:
    check("asm: propagação barrada (correto)", True)

# ── [9.3] primitivo de escrita binária (write_bytes) ────────
print("[9.3] write_bytes (I/O binária)")
_wb = 'int[] b = [80, 89]; bool ok = write_bytes("out.bin", b); print(ok);'
_dwb = disasm_pyro.disassemble(gen_pyro(_wb, encode=False))
check("pyro: write_bytes vira NATIVE 27 (argc 2)", "NATIVE 27 2" in _dwb)
check("go: write_bytes usa cryoWriteBytes", "cryoWriteBytes(" in gen_go(_wb))
check("go: write_bytes guardado por sandbox",
      'cryoSandboxGuard("write_bytes")' in gen_go(_wb))
try:
    from semantic import check as _sem_check
    _sem_check(ast_of(_wb))   # não deve acusar 'função desconhecida'
    check("semântica: write_bytes é builtin conhecido", True)
except Exception:
    check("semântica: write_bytes é builtin conhecido", False)

# ── resultado ───────────────────────────────────────────────
print(f"\n{_passed} passaram, {_failed} falharam")
sys.exit(1 if _failed else 0)
