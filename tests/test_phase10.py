#!/usr/bin/env python3
# ============================================================
#  test_phase10.py — Phase 10 feature suite (roadmap Phase 10)
#
#  Verifies Phase 10 capabilities across the toolchain:
#    * 10.1: Range-based for loops (a..b, a..=b)
#    * 10.2: Collection operations (sort, reverse, slice, index_of, map, filter, reduce, any, all)
#    * 10.3: Iterators, enumerate, pairs & comprehensions (list/map comprehensions)
#    * 10.4: Extended standard library (math, strings, padding, reducers, time/random)
#    * 10.5: Generics via monomorphization (functions and structs)
#    * 10.6: Function values and closures on the Pyro VM
#    * 10.7: Interfaces / traits with static dispatch & generic bounds
#    * 10.8: Module namespaces & pub visibility
#    * 10.9: Ranges & slices as values (array and string slicing)
#    * 10.10: Postfix calls f(a)(b) & strict argument lists
#    * 10.11 - 10.13: Frontend structure & HTML outputs
#
#  Asserts both compiler generation and actual execution across
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


def run_src(src, backend, timeout=60):
    work = tempfile.mkdtemp(prefix='cryo_p10_')
    try:
        p = os.path.join(work, 'p.cryo')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(src)

        if backend == 'pyro':
            r = subprocess.run([sys.executable, PYRO, 'run', p, '--vm'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()

        elif backend == 'node':
            if not shutil.which('node'):
                return None, None, 'node not available'
            out = os.path.join(work, 'p.js')
            c = subprocess.run([sys.executable, CRYOC, p, '--backend', 'node', '-o', out, '--no-banner'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
            if c.returncode != 0:
                return c.returncode, '', (c.stdout + c.stderr).strip()
            r = subprocess.run(['node', out], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=timeout)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()

        elif backend == 'go':
            if not shutil.which('go'):
                return None, None, 'go not available'
            out = os.path.join(work, 'p.go')
            c = subprocess.run([sys.executable, CRYOC, p, '--backend', 'go', '-o', out, '--no-banner'],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout)
            if c.returncode != 0:
                return c.returncode, '', (c.stdout + c.stderr).strip()
            r = subprocess.run(['go', 'run', 'p.go'], cwd=work, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=timeout)
            return r.returncode, (r.stdout or '').replace('\r\n', '\n').strip(), (r.stderr or '').strip()

        return None, None, f'unknown backend: {backend}'
    except subprocess.TimeoutExpired:
        return -1, '', 'timed out'
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("[Phase 10] comprehensive verification of Phase 10 features")

    # ────────────────────────────────────────────────────────────
    # 10.1: Range-based for loops
    # ────────────────────────────────────────────────────────────
    print("\n── 10.1: range-based for loops ──")
    src_range = """
fn main_entry() ={
    int s1 = 0;
    for (int i in 0..4) {
        s1 = s1 + i;
    }
    int s2 = 0;
    for (int j in 1..=4) {
        s2 = s2 + j;
    }
    print(s1);
    print(s2);
}
main_entry();
"""
    for be in ('pyro', 'node', 'go'):
        code, out, err = run_src(src_range, be)
        check(f"{be}: exclusive (0..4) and inclusive (1..=4) range loops",
              code == 0 and out == "6\n10",
              f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.2: Collection operations
    # ────────────────────────────────────────────────────────────
    print("\n── 10.2: collection operations ──")
    src_colls = """
fn main_entry() ={
    int[] nums = [30, 10, 20];
    int[] s = sort(nums);
    print(s[0]);
    print(s[2]);

    int[] r = reverse(s);
    print(r[0]);

    int idx = index_of(nums, 20);
    print(idx);

    int[] base = [1, 2, 3];
    int[] d = map(base, (int x) => x * 3);
    print(d[1]);

    int[] ev = filter(base, (int x) => x % 2 != 0);
    print(len(ev));

    int tot = reduce(base, (int acc, int x) => acc + x, 10);
    print(tot);

    print(any(base, (int x) => x == 2));
    print(all(base, (int x) => x > 0));
}
main_entry();
"""
    expected_colls = "10\n30\n30\n2\n6\n2\n16\ntrue\ntrue"
    for be in ('pyro', 'node', 'go'):
        code, out, err = run_src(src_colls, be)
        check(f"{be}: collection operations (sort, reverse, index_of, map, filter, reduce, any, all)",
              code == 0 and out == expected_colls,
              f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.3: Iterators, enumerate, pairs & comprehensions
    # ────────────────────────────────────────────────────────────
    print("\n── 10.3: iterators, enumerate, pairs & comprehensions ──")
    src_iter = """
fn main_entry() ={
    string[] items = ["x", "y"];
    for (i, v in enumerate(items)) {
        print(i);
        print(v);
    }

    any[] sq = [x * 2 for int x in 1..=3 if x > 1];
    print(len(sq));
    print(sq[0]);
    print(sq[1]);
}
main_entry();
"""
    expected_iter = "0\nx\n1\ny\n2\n4\n6"
    for be in ('pyro', 'node', 'go'):
        code, out, err = run_src(src_iter, be)
        check(f"{be}: enumerate and list comprehension",
              code == 0 and out == expected_iter,
              f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.4: Extended standard library
    # ────────────────────────────────────────────────────────────
    print("\n── 10.4: extended standard library ──")
    src_stdlib = """
fn main_entry() ={
    print(clamp(25, 0, 10));
    print(sign(-99));
    print(gcd(36, 24));
    print(starts_with("abcdef", "abc"));
    print(ends_with("abcdef", "def"));
    print(repeat("z", 4));
    print(pad_start("7", 3, "0"));
    print(pad_end("7", 3, "x"));
    int[] nums = [10, 20, 30];
    print(sum(nums));
    print(count(nums, 20));
}
main_entry();
"""
    expected_stdlib = "10\n-1\n12\ntrue\ntrue\nzzzz\n007\n7xx\n60\n1"
    for be in ('pyro', 'node', 'go'):
        code, out, err = run_src(src_stdlib, be)
        check(f"{be}: stdlib builtins (clamp, sign, gcd, starts/ends_with, repeat, pad, sum, count)",
              code == 0 and out == expected_stdlib,
              f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.5: Generics via monomorphization
    # ────────────────────────────────────────────────────────────
    print("\n── 10.5: generics via monomorphization ──")
    src_generics = """
fn pick_larger<T>(T a, T b) -> T ={
    if (a > b) {
        return a;
    }
    return b;
}

fn main_entry() ={
    int mi = pick_larger<int>(10, 42);
    string ms = pick_larger<string>("alpha", "zeta");
    print(mi);
    print(ms);
}
main_entry();
"""
    for be in ('go', 'node'):
        code, out, err = run_src(src_generics, be)
        check(f"{be}: generic function monomorphization",
              code == 0 and out == "42\nzeta",
              f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.6: Function values & closures on Pyro VM
    # ────────────────────────────────────────────────────────────
    print("\n── 10.6: function values & closures on Pyro VM ──")
    src_closure = """
fn make_multiplier(int factor) -> fn(int)->int ={
    return (int n) => n * factor;
}

fn main_entry() ={
    fn(int)->int times_three = make_multiplier(3);
    fn(int)->int times_four = make_multiplier(4);
    print(times_three(5));
    print(times_four(5));
}
main_entry();
"""
    code, out, err = run_src(src_closure, 'pyro')
    check("pyro: capturing closure instantiation and execution",
          code == 0 and out == "15\n20",
          f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.7: Interfaces / traits with static dispatch
    # ────────────────────────────────────────────────────────────
    print("\n── 10.7: interfaces / traits with static dispatch ──")
    src_trait = """
trait Describable {
    fn describe() -> string;
}

struct Item {
    string name;
    int price;
}

impl Describable for Item {
    fn describe() -> string ={
        return this.name + ": $" + to_string(this.price);
    }
}

fn main_entry() ={
    Item item = Item { name: "Book", price: 15 };
    print(item.describe());
    print(Describable.describe(item));
}
main_entry();
"""
    for be in ('pyro', 'node', 'go'):
        code, out, err = run_src(src_trait, be)
        check(f"{be}: trait declaration, implementation, and static dispatch",
              code == 0 and out == "Book: $15\nBook: $15",
              f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.8: Module namespaces & visibility
    # ────────────────────────────────────────────────────────────
    print("\n── 10.8: module namespaces & pub visibility ──")
    work = tempfile.mkdtemp(prefix='cryo_mod_')
    try:
        mod_p = os.path.join(work, 'math_utils.cryo')
        with open(mod_p, 'w', encoding='utf-8') as f:
            f.write("""
pub fn double_val(int x) -> int ={
    return x * 2;
}

fn private_helper() -> int ={
    return 999;
}
""")
        main_p = os.path.join(work, 'main.cryo')
        with open(main_p, 'w', encoding='utf-8') as f:
            f.write("""
import "math_utils.cryo" as mu;

fn main_entry() ={
    print(mu::double_val(21));
}
main_entry();
""")
        for be in ('pyro', 'node', 'go'):
            if be == 'pyro':
                r = subprocess.run([sys.executable, PYRO, 'run', main_p, '--vm'],
                                   capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', timeout=30)
                out = (r.stdout or '').replace('\r\n', '\n').strip()
                check(f"{be}: namespaced module import and call", r.returncode == 0 and out == "42")
            elif be == 'node' and shutil.which('node'):
                out_js = os.path.join(work, 'main.js')
                c = subprocess.run([sys.executable, CRYOC, main_p, '--backend', 'node', '-o', out_js, '--no-banner'],
                                   capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
                r = subprocess.run(['node', out_js], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
                out = (r.stdout or '').replace('\r\n', '\n').strip()
                check(f"{be}: namespaced module import and call", c.returncode == 0 and r.returncode == 0 and out == "42")
            elif be == 'go' and shutil.which('go'):
                out_go = os.path.join(work, 'main.go')
                c = subprocess.run([sys.executable, CRYOC, main_p, '--backend', 'go', '-o', out_go, '--no-banner'],
                                   capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
                r = subprocess.run(['go', 'run', 'main.go'], cwd=work, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
                out = (r.stdout or '').replace('\r\n', '\n').strip()
                check(f"{be}: namespaced module import and call", c.returncode == 0 and r.returncode == 0 and out == "42")

        # Test non-pub access refusal
        bad_main = os.path.join(work, 'bad.cryo')
        with open(bad_main, 'w', encoding='utf-8') as f:
            f.write("""
import "math_utils.cryo" as mu;
fn main_entry() ={
    print(mu::private_helper());
}
main_entry();
""")
        c_bad = subprocess.run([sys.executable, CRYOC, bad_main, '--backend', 'pyro', '--no-banner'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        output_bad = c_bad.stdout + c_bad.stderr
        check("semantics: private member access across namespace rejected",
              c_bad.returncode != 0 and "is not pub in module" in output_bad)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ────────────────────────────────────────────────────────────
    # 10.9: Ranges & slices as values
    # ────────────────────────────────────────────────────────────
    print("\n── 10.9: ranges & slices as values ──")
    src_slices = """
fn main_entry() ={
    int[] nums = [10, 20, 30, 40, 50];
    int[] s1 = nums[1..3];
    print(len(s1));
    print(s1[0]);
    print(s1[1]);

    string text = "cryolang";
    string s2 = text[0..4];
    print(s2);
}
main_entry();
"""
    expected_slices = "2\n20\n30\ncryo"
    for be in ('pyro', 'node', 'go'):
        code, out, err = run_src(src_slices, be)
        check(f"{be}: array and string slice syntax (xs[a..b])",
              code == 0 and out == expected_slices,
              f"code={code} out={out} err={err}")

    # ────────────────────────────────────────────────────────────
    # 10.10: Postfix calls & strict argument lists
    # ────────────────────────────────────────────────────────────
    print("\n── 10.10: postfix calls & strict argument lists ──")
    src_postfix = """
fn make_adder(int a) -> fn(int)->int ={
    return (int b) => a + b;
}

fn main_entry() ={
    print(make_adder(10)(32));
}
main_entry();
"""
    for be in ('pyro', 'node', 'go'):
        code, out, err = run_src(src_postfix, be)
        check(f"{be}: chained postfix call f(a)(b)",
              code == 0 and out == "42",
              f"code={code} out={out} err={err}")

    from lexer import Lexer
    from parser import Parser, ParseError
    tokens = Lexer("add(1 2);").tokenize()
    p = Parser(tokens)
    try:
        p.parse()
        check("parser: missing comma in argument list rejected", False)
    except ParseError:
        check("parser: missing comma in argument list rejected", True)

    # ────────────────────────────────────────────────────────────
    # 10.11 - 10.13: Frontend structure & HTML outputs
    # ────────────────────────────────────────────────────────────
    print("\n── 10.11 - 10.13: frontend structure & outputs ──")
    import foreign
    import frontend
    src_fe = """
import >html<
import >javascript<
import >CSS<
fn styles()   ={ >CSS( body { color: #eee; } ) }
fn behavior() ={ >javascript( document.title = "T"; ) }
fn page()     ={ >html( <h1>Hi</h1> )<script = behavior, style = styles> }
"""
    tokens_fe = Lexer(src_fe).tokenize()
    ast_fe = Parser(tokens_fe).parse()
    foreign.verify(ast_fe)
    m = frontend.collect(ast_fe)
    html = frontend.render_html(m)
    check("frontend: HTML rendered with embedded CSS and JS",
          "<h1>Hi</h1>" in html and "body { color: #eee; }" in html)
    check("frontend: has_logic detects logic functions", not frontend.has_logic(ast_fe))

    # ── Summary ──
    print(f"\n{_passed} passed, {_failed} failed\n")
    if _failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
