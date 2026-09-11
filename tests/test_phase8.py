#!/usr/bin/env python3
# ============================================================
#  test_phase8.py — Phase 8 feature suite (roadmap Phase 8)
#
#  Verifies Phase 8 capabilities across the toolchain:
#    * 8.1: First-class functions and lambdas (fn(T)->U, closures)
#    * 8.2: Enums with data & pattern matching (ADTs, exhaustiveness)
#    * 8.3: Error propagation with '?' (Result unwrapping & early return)
#    * 8.4: Generics (monomorphized functions and structs)
#    * 8.8: Interfaces and Traits (contracts and static dispatch)
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
    work = tempfile.mkdtemp(prefix='cryo_p8_')
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
    print("[Phase 8] comprehensive verification of Phase 8 features")

    # ============================================================
    # ── 8.1: First-Class Functions & Lambdas ───────────────────
    # ============================================================
    print("\n── 8.1: first-class functions & lambdas ──")

    # Passing lambda to higher-order function
    src_hof = """
    fn apply_int(fn(int)->int f, int val) -> int ={
        return f(val);
    }
    fn(int)->int double_fn = (int x) => x * 2;
    print(apply_int(double_fn, 21));
    """
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_hof, b)
        if rc is None:
            continue
        check(f"{b}: higher-order function with lambda", rc == 0 and out == '42', f"rc={rc}, out={out}, err={err}")

    # Returning function from function (closure/factory)
    src_factory = """
    fn make_adder(int base) -> fn(int)->int ={
        return (int x) => x + base;
    }
    fn(int)->int add10 = make_adder(10);
    print(add10(5));
    print(add10(32));
    """
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_factory, b)
        if rc is None:
            continue
        check(f"{b}: closure capturing variable", rc == 0 and out == '15\n42', f"rc={rc}, out={out}, err={err}")

    # Named function passed as value
    src_named_fn = """
    fn square(int n) -> int ={ return n * n; }
    fn call_it(fn(int)->int f, int x) -> int ={ return f(x); }
    fn(int)->int g = square;
    print(call_it(g, 9));
    """
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_named_fn, b)
        if rc is None:
            continue
        check(f"{b}: named function assigned to variable and called", rc == 0 and out == '81', f"rc={rc}, out={out}, err={err}")

    # ============================================================
    # ── 8.2: Enums with Data & Pattern Matching ────────────────
    # ============================================================
    print("\n── 8.2: enums with data & pattern matching ──")

    src_adt = """
    enum Status {
        Active(int),
        Suspended(string),
        Inactive
    }

    fn check_status(Status s) ={
        match s {
            Active(code) => { print("active: " + to_string(code)); }
            Suspended(reason) => { print("suspended: " + reason); }
            Inactive => { print("inactive"); }
        }
    }

    check_status(Active(200));
    check_status(Suspended("maintenance"));
    check_status(Inactive);
    """
    expected_adt = "active: 200\nsuspended: maintenance\ninactive"
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_adt, b)
        if rc is None:
            continue
        check(f"{b}: enums with data and match destructuring", rc == 0 and out == expected_adt, f"rc={rc}, out={out}, err={err}")

    # Match with wildcard
    src_wildcard = """
    enum Shape {
        Circle(int),
        Square(int),
        Triangle(int, int)
    }
    fn is_circle(Shape s) -> bool ={
        match s {
            Circle(r) => { return true; }
            _ => { return false; }
        }
    }
    print(is_circle(Circle(5)));
    print(is_circle(Square(4)));
    """
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_wildcard, b)
        if rc is None:
            continue
        check(f"{b}: match with wildcard arm", rc == 0 and out == 'true\nfalse', f"rc={rc}, out={out}, err={err}")

    # Semantic exhaustiveness verification: missing a case must be rejected
    from lexer import Lexer
    from parser import Parser
    from semantic import check as semantic_check, SemanticError

    src_non_exhaustive = """
    enum Color { Red, Green, Blue }
    Color c = Red;
    match c {
        Red => { print("red"); }
        Green => { print("green"); }
    }
    """
    try:
        ast_ne = Parser(Lexer(src_non_exhaustive).tokenize()).parse()
        semantic_check(ast_ne)
        check("semantics: non-exhaustive match rejected", False, "expected SemanticError")
    except SemanticError:
        check("semantics: non-exhaustive match rejected", True)
    except Exception as ex:
        check("semantics: non-exhaustive match rejected", False, f"unexpected exception: {ex}")

    # ============================================================
    # ── 8.3: Error Propagation with '?' ────────────────────────
    # ============================================================
    print("\n── 8.3: error propagation with '?' ──")

    src_try_prop = """
    enum Result {
        Ok(int),
        Err(string)
    }

    fn step1(int n) -> Result ={
        if (n > 0) { return Ok(n * 2); }
        return Err("n must be positive");
    }

    fn step2(int n) -> Result ={
        int a = step1(n)?;
        int b = step1(a)?;
        return Ok(b + 1);
    }

    fn test_try(int n) ={
        match step2(n) {
            Ok(v) => { print("success: " + to_string(v)); }
            Err(e) => { print("error: " + e); }
        }
    }

    test_try(5);
    test_try(0);
    """
    expected_try = "success: 21\nerror: n must be positive"
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_try_prop, b)
        if rc is None:
            continue
        check(f"{b}: '?' operator propagates Ok and Err", rc == 0 and out == expected_try, f"rc={rc}, out={out}, err={err}")

    # Ternary disambiguation: '?' in ternary should remain ternary
    src_ternary = """
    int x = 10;
    string res = (x > 5) ? "greater" : "lesser";
    print(res);
    """
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_ternary, b)
        if rc is None:
            continue
        check(f"{b}: ternary expression not confused with '?' propagation", rc == 0 and out == 'greater', f"rc={rc}, out={out}")

    # ============================================================
    # ── 8.4: Generics ──────────────────────────────────────────
    # ============================================================
    print("\n── 8.4: generics (functions & structs) ──")

    src_generics = """
    fn pick_first<T>(T a, T b) -> T ={
        return a;
    }

    print(pick_first<int>(42, 99));
    print(pick_first<string>("alpha", "beta"));
    """
    for b in ('go', 'node'):
        rc, out, err = run_src(src_generics, b)
        if rc is None:
            continue
        check(f"{b}: generic function monomorphization", rc == 0 and out == '42\nalpha', f"rc={rc}, out={out}, err={err}")

    # ============================================================
    # ── 8.8: Interfaces / Traits ───────────────────────────────
    # ============================================================
    print("\n── 8.8: interfaces / traits ──")

    src_traits = """
    trait Describable {
        fn describe() -> string;
    }

    struct Item {
        string name;
        int qty;
    }

    impl Describable for Item {
        fn describe() -> string ={
            return this.name + ": " + to_string(this.qty);
        }
    }

    Item it = Item { name: "Widget", qty: 7 };
    print(it.describe());
    print(Describable.describe(it));
    """
    expected_traits = "Widget: 7\nWidget: 7"
    for b in ('pyro', 'node', 'go'):
        rc, out, err = run_src(src_traits, b)
        if rc is None:
            continue
        check(f"{b}: trait definition and implementation", rc == 0 and out == expected_traits, f"rc={rc}, out={out}, err={err}")

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
