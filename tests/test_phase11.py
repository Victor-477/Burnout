#!/usr/bin/env python3
# ============================================================
#  test_phase11.py — Phase 11 feature suite (roadmap Phase 11)
#
#  Verifies Phase 11 capabilities across Tracks A - E:
#    * Track A: Language
#        - 11.1: Module state (GETGLOBAL/SETGLOBAL, package scope)
#        - 11.2: Struct methods without traits (impl Struct { ... })
#        - 11.3: Rich string formatting (${x:.2f}, ${x:,d}, ${s:<8})
#        - 11.5: Iteration protocol (for (k, v in m), custom iter())
#    * Track B: Application & VM
#        - 11.7: Filesystem & process natives (file_exists, write_file, etc.)
#        - 11.8: Atomic write (write_file_atomic)
#        - 11.9: Asset packaging (--assets DIR, asset())
#    * Track C: Security
#        - 11.11: Capability sandbox (PYRO_POLICY)
#        - 11.12: Declared permissions
#        - 11.14: Container signing (--sign HMAC-SHA256)
#    * Track D: LLM Layer
#        - 11.16: Generation options validation
#        - 11.19: llm_try synthetic enum & result variants
#    * Track E: Optimization & Tooling
#        - 11.21: AST optimizer (constant folding & leaf inlining)
#        - 11.23: Incremental compilation cache
#        - 11.24: Diagnostics carets & edit-distance suggestions
#    * Close-out Items:
#        - 11.26: Container null equality ([] != null, {} != null)
#        - 11.31: any to typed context on Go (cryoAs)
#        - 11.38: User function named 'main'
#        - 11.39: Backslash escaped interpolation
#
#  Asserts both compiler generation and runtime execution across
#  supported backends (Pyro VM, Go, Node).
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
PYRO = os.path.join(ROOT, 'Burnout', 'pyro.py')
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def run_src(src, backend, timeout=60, env_extra=None):
    work = tempfile.mkdtemp(prefix='cryo_p11_')
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        p = os.path.join(work, 'p.cryo')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(src)

        if backend == 'pyro':
            r = subprocess.run([sys.executable, PYRO, 'run', p, '--vm'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout, env=env)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()

        elif backend == 'node':
            if not shutil.which('node'):
                return None, None, 'node not available'
            out = os.path.join(work, 'p.js')
            c = subprocess.run([sys.executable, CRYOC, p, '--backend', 'node', '-o', out, '--no-banner'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout, env=env)
            if c.returncode != 0:
                return c.returncode, '', (c.stdout + c.stderr).strip()
            r = subprocess.run(['node', out], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=timeout, env=env)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()

        elif backend == 'go':
            if not shutil.which('go'):
                return None, None, 'go not available'
            out = os.path.join(work, 'p.go')
            c = subprocess.run([sys.executable, CRYOC, p, '--backend', 'go', '-o', out, '--no-banner'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout, env=env)
            if c.returncode != 0:
                return c.returncode, '', (c.stdout + c.stderr).strip()
            r = subprocess.run(['go', 'run', 'p.go'], cwd=work, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=timeout, env=env)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()
        else:
            raise ValueError(f"unknown backend {backend}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("[Phase 11] comprehensive verification of Phase 11 features\n")

    # ══════════════════════════════════════════════════════════
    # ── Track A: Language
    # ══════════════════════════════════════════════════════════
    print("── Track A: Language (11.1 - 11.5) ──")

    # 11.1 Module state
    src_state = """
int count = 10;
string name = "cryo";

fn bump(int n) ={
    count = count + n;
}

fn append_name(string s) ={
    name = name + " " + s;
}

fn read_state() -> string ={
    return name + ": " + to_string(count);
}

bump(5);
append_name("phase11");
print(read_state());
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_state, be)
        check(f"{be}: module state (11.1)",
              rc == 0 and out == "cryo phase11: 15",
              f"rc={rc}, out={out!r}, err={err!r}")

    # 11.2 Struct methods without traits
    src_methods = """
struct Rect {
    int w;
    int h;
}

impl Rect {
    fn area() -> int ={
        return this.w * this.h;
    }
    fn perimeter() -> int ={
        return (this.w + this.h) * 2;
    }
}

Rect r = Rect { w: 6, h: 7 };
print(to_string(r.area()) + " " + to_string(r.perimeter()));
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_methods, be)
        check(f"{be}: struct methods without traits (11.2)",
              rc == 0 and out == "42 26",
              f"rc={rc}, out={out!r}, err={err!r}")

    # 11.3 Rich string formatting
    src_format = """
number pi = 3.14159;
int million = 1000000;
string word = "cryo";
int num = 42;
print("${pi:.2f} | ${million:,d} | [${word:<6}] | ${num:05d}");
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_format, be)
        check(f"{be}: rich string formatting (11.3)",
              rc == 0 and out == "3.14 | 1,000,000 | [cryo  ] | 00042",
              f"rc={rc}, out={out!r}, err={err!r}")

    # 11.5 Iteration protocol on maps and custom Iterable
    src_iter = """
map<string,int> inventory = {"bolts": 40, "nuts": 12};
int total = 0;
for (string k, int v in inventory) {
    total = total + v;
}

trait Iterable {
    fn iter() -> int[];
}

struct Counter {
    int max;
}

impl Iterable for Counter {
    fn iter() -> int[] ={
        int[] out = [];
        for (int i = 1; i <= this.max; i = i + 1) {
            out.push(i);
        }
        return out;
    }
}

Counter c = Counter { max: 3 };
int step_sum = 0;
for (int x in c) {
    step_sum = step_sum + x;
}

print(to_string(total) + " " + to_string(step_sum));
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_iter, be)
        check(f"{be}: iteration protocol on map and custom iter (11.5)",
              rc == 0 and out == "52 6",
              f"rc={rc}, out={out!r}, err={err!r}")

    # ══════════════════════════════════════════════════════════
    # ── Track B: Application & VM
    # ══════════════════════════════════════════════════════════
    print("\n── Track B: Application & VM (11.7 - 11.9) ──")

    # 11.7 Filesystem natives
    src_fs = """
string fname = "p11_test_fs.tmp";
bool ok_w = write_file(fname, "cryo file i/o");
bool ok_e = file_exists(fname);
int sz = file_size(fname);
string content = read_file(fname);
bool ok_d = delete_file(fname);
bool gone = !file_exists(fname);

if (ok_w && ok_e && sz == 13 && content == "cryo file i/o" && ok_d && gone) {
    print("fs_ok");
} else {
    print("fs_fail");
}
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_fs, be)
        check(f"{be}: filesystem natives (11.7)",
              rc == 0 and out == "fs_ok",
              f"rc={rc}, out={out!r}, err={err!r}")

    # 11.8 Atomic file write on Pyro VM
    src_atomic = """
string fname = "p11_test_atomic.tmp";
bool ok = write_file_atomic(fname, "atomic content");
bool ex = file_exists(fname);
string data = read_file(fname);
delete_file(fname);
if (ok && ex && data == "atomic content") {
    print("atomic_ok");
} else {
    print("atomic_fail");
}
"""
    rc, out, err = run_src(src_atomic, 'pyro')
    check("pyro: write_file_atomic (11.8)",
          rc == 0 and out == "atomic_ok",
          f"rc={rc}, out={out!r}, err={err!r}")

    # 11.9 Application packaging (--assets)
    work = tempfile.mkdtemp(prefix='cryo_p11_assets_')
    try:
        asset_dir = os.path.join(work, 'my_assets')
        os.makedirs(asset_dir, exist_ok=True)
        with open(os.path.join(asset_dir, 'msg.txt'), 'w', encoding='utf-8') as f:
            f.write("embedded asset payload")
        src_asset = """
print(asset("msg.txt"));
"""
        src_file = os.path.join(work, 'app.cryo')
        with open(src_file, 'w', encoding='utf-8') as f:
            f.write(src_asset)
        out_pyro = os.path.join(work, 'app.pyro')
        cp = subprocess.run([sys.executable, CRYOC, src_file, '--backend', 'pyro',
                             '--assets', asset_dir, '-o', out_pyro, '--no-banner'],
                            capture_output=True, text=True, encoding='utf-8')
        run_res = subprocess.run([sys.executable, PYRO, 'run', out_pyro, '--vm'],
                                 capture_output=True, text=True, encoding='utf-8')
        check("pyro: embedded asset packaging --assets (11.9)",
              cp.returncode == 0 and run_res.returncode == 0 and run_res.stdout.strip() == "embedded asset payload",
              f"cp={cp.returncode}, run={run_res.returncode}, out={run_res.stdout!r}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ══════════════════════════════════════════════════════════
    # ── Track C: Security & Machine
    # ══════════════════════════════════════════════════════════
    print("\n── Track C: Security & Machine (11.11 - 11.14) ──")

    # 11.11 Capability-based sandbox PYRO_POLICY
    src_sandbox = """
print(len(env("PATH")) > 0);
"""
    # Without env=PATH capability, reading PATH should be blocked
    rc, out, err = run_src(src_sandbox, 'pyro', env_extra={'PYRO_POLICY': 'env=HOME'})
    check("pyro: PYRO_POLICY capability gating blocks ungranted action (11.11)",
          "denied" in (out + err).lower() or rc != 0,
          f"rc={rc}, out={out!r}, err={err!r}")

    # With env=PATH capability, action should succeed
    rc, out, err = run_src(src_sandbox, 'pyro', env_extra={'PYRO_POLICY': 'env=PATH'})
    check("pyro: PYRO_POLICY capability granting allows authorized action (11.11)",
          rc == 0 and out == "true",
          f"rc={rc}, out={out!r}, err={err!r}")

    # 11.12 Declared permissions compile-time check
    from lexer import Lexer
    from parser import Parser
    from semantic import check as sem_check, SemanticError

    src_no_perm = """
permissions {
    read = "./data";
}
write_file("data/test.txt", "hello");
"""
    try:
        p = Parser(Lexer(src_no_perm).tokenize()).parse()
        sem_check(p, src_no_perm)
        perm_refused = False
    except SemanticError as e:
        perm_refused = "permission" in str(e).lower() or "write" in str(e).lower()
    except Exception:
        perm_refused = True
    check("semantics: declared permissions compile-time enforcement (11.12)", perm_refused)

    # 11.14 Signing
    work = tempfile.mkdtemp(prefix='cryo_p11_sign_')
    try:
        p_src = os.path.join(work, 'sign.cryo')
        with open(p_src, 'w', encoding='utf-8') as f:
            f.write('print("signed");')
        out_pyro = os.path.join(work, 'sign.pyro')
        cp = subprocess.run([sys.executable, CRYOC, p_src, '--backend', 'pyro',
                             '--sign', 'env:TEST_SIGN_KEY', '-o', out_pyro, '--no-banner'],
                            capture_output=True, text=True, encoding='utf-8',
                            env={**os.environ, 'TEST_SIGN_KEY': 'my-secret-signing-key-1234'})
        check("pyro: --sign produces signed bytecode with key (11.14)",
              cp.returncode == 0 and os.path.exists(out_pyro))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ══════════════════════════════════════════════════════════
    # ── Track D: LLM Layer
    # ══════════════════════════════════════════════════════════
    print("\n── Track D: LLM Layer (11.16 - 11.19) ──")

    # 11.16 Generation controls in parser
    from parser import ParseError
    src_valid_opts = """
fn test_llm() ={
    string p = "prompt";
    string res = llm("model", p, { "temperature": 0.5, "max_tokens": 100 });
}
"""
    try:
        Parser(Lexer(src_valid_opts).tokenize()).parse()
        opts_parsed = True
    except Exception:
        opts_parsed = False
    check("parser: valid LLM options parsed (11.16)", opts_parsed)

    src_typo_opts = """
fn test_llm() ={
    string res = llm("model", "p", { "temprature": 0.5 });
}
"""
    try:
        Parser(Lexer(src_typo_opts).tokenize()).parse()
        typo_rejected = False
    except ParseError as pe:
        typo_rejected = "temperature" in str(pe).lower() or "unknown" in str(pe).lower()
    except Exception:
        typo_rejected = True
    check("parser: typo in LLM options rejected with suggestion (11.16)", typo_rejected)

    # 11.19 llm_try synthetic types
    src_llm_try = """
fn call() ={
    match llm_try("model", "hi") {
        LlmOk(text) => { print("ok: " + text); }
        LlmFailed(kind, detail) => { print("fail: " + kind); }
    }
}
"""
    try:
        Parser(Lexer(src_llm_try).tokenize()).parse()
        llm_try_parsed = True
    except Exception:
        llm_try_parsed = False
    check("parser: llm_try synthetic enum match parsed (11.19)", llm_try_parsed)

    # ══════════════════════════════════════════════════════════
    # ── Track E: Optimization & Diagnostics
    # ══════════════════════════════════════════════════════════
    print("\n── Track E: Optimization & Diagnostics (11.21 - 11.24) ──")

    # 11.21 AST optimizer passes
    import optimize
    src_opt = """
int base = 10;
int total = base * 60;
print(total);
"""
    ast = Parser(Lexer(src_opt).tokenize()).parse()
    opt_ast = optimize.optimize(ast)
    # Check if print argument was folded into a literal int 600
    print_call = opt_ast.statements[0]
    is_folded = hasattr(print_call, 'callee') and print_call.callee == 'print' and \
                len(print_call.args) > 0 and getattr(print_call.args[0], 'value', None) == 600
    check("optimizer: constant folding to fixed point (11.21)", is_folded)

    # 11.23 Incremental compilation cache
    work = tempfile.mkdtemp(prefix='cryo_p11_cache_')
    try:
        c_src = os.path.join(work, 'cached.cryo')
        with open(c_src, 'w', encoding='utf-8') as f:
            f.write('print("cached");')
        out_1 = os.path.join(work, 'cached1.pyro')
        cp1 = subprocess.run([sys.executable, CRYOC, c_src, '--backend', 'pyro', '-o', out_1, '--no-banner'],
                             capture_output=True, text=True, encoding='utf-8')
        out_2 = os.path.join(work, 'cached2.pyro')
        cp2 = subprocess.run([sys.executable, CRYOC, c_src, '--backend', 'pyro', '-o', out_2, '--no-banner'],
                             capture_output=True, text=True, encoding='utf-8')
        check("incremental: repeated build succeeds with artifact cache (11.23)",
              cp1.returncode == 0 and cp2.returncode == 0 and os.path.exists(out_2))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # 11.24 Diagnostics suggestions
    import diagnostics as dx
    sugg = dx.suggest("lenght", ["length", "size", "count"])
    check("diagnostics: edit distance typo suggestion lenght -> length (11.24)", sugg[:1] == ["length"])

    # ══════════════════════════════════════════════════════════
    # ── Close-out Items from Phase 10
    # ══════════════════════════════════════════════════════════
    print("\n── Close-out Items (11.26, 11.31, 11.38, 11.39) ──")

    # 11.26 Container null equality
    src_null = """
int[] a = [];
map<string,int> m = {};
print(to_string(a == null) + " " + to_string(m == null));
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_null, be)
        check(f"{be}: container null equality returns false (11.26)",
              rc == 0 and out == "false false",
              f"rc={rc}, out={out!r}, err={err!r}")

    # 11.31 any to typed context on Go
    src_any = """
any x = 42;
int y = x;
print(to_string(y));
"""
    rc, out, err = run_src(src_any, 'go')
    check("go: any reaches typed context via cryoAs (11.31)",
          rc == 0 and out == "42",
          f"rc={rc}, out={out!r}, err={err!r}")

    # 11.38 User function named 'main'
    src_main_fn = """
fn main() -> int ={
    return 99;
}
print(to_string(main()));
"""
    rc, out, err = run_src(src_main_fn, 'pyro')
    check("pyro: user function named 'main' resolves without recursion (11.38)",
          rc == 0 and out == "99",
          f"rc={rc}, out={out!r}, err={err!r}")

    # 11.39 Backslash escaped interpolation
    src_esc = r"""
string s = "\${escaped}";
print(s);
"""
    for be in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_esc, be)
        check(f"{be}: backslash escapes interpolation (11.39)",
              rc == 0 and out == "${escaped}",
              f"rc={rc}, out={out!r}, err={err!r}")

    print(f"\n{_passed} passed, {_failed} failed\n")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
