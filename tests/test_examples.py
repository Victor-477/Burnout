#!/usr/bin/env python3
# ============================================================
#  test_examples.py — every example on every backend  (roadmap 12.4)
#
#  Two jobs, sharing one piece of machinery.
#
#  1. THE EXAMPLES. Compile each `Cryo/examples/*.cryo` on each backend, run
#     the ones that can be run, and require every backend that produced output
#     to produce the SAME output. That last clause is the real test: "it
#     compiled" is a much weaker claim than "it agrees", and the difference is
#     where every parity break in Phase 11 lived.
#
#  2. THE CAPABILITY MATRIX. The table in the docs saying which backend
#     supports what was maintained BY HAND, and was wrong about the C backend
#     for two entire phases — it still said "no maps, no optionals" after 11.27
#     gave it both. A table that has to be remembered will eventually be wrong
#     and nobody will notice, because nothing checks it.
#
#     So it is generated: one minimal probe per row, compiled on every backend,
#     and the answer is whatever actually happened. `--matrix` prints the table
#     in the docs' own format.
#
#  WHICH EXAMPLES CAN BE RUN
#
#  Decided mechanically, not by a hand-kept list, for the same reason: an
#  example whose source mentions a native that touches the network, an LLM, the
#  clock, the filesystem or stdin is COMPILED but not run. Those fail for
#  reasons that have nothing to do with the backend, and a suite that goes red
#  when the wifi drops gets ignored.
# ============================================================
import argparse
import glob
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
EXAMPLES = os.path.join(ROOT, 'Cryo', 'examples')
EXE = '.exe' if sys.platform == 'win32' else ''

# A source mentioning any of these is compiled but not run.
_OUTSIDE_WORLD = (
    'http_get', 'http_post', 'http_listen', 'http_accept', 'http_respond',
    'http_serve', 'llm(', 'llm_', 'agent_', 'skill', 'input(', 'exec(',
    'write_file', 'delete_file', 'make_dir', 'sleep(', 'read_file', 'asset(',
    'random', 'now_ms', 'monotonic_ms',
)

# Defects this sweep FOUND, and that are recorded on the roadmap. Reported as
# `known` rather than as failures: a suite that is permanently red gets ignored,
# and then it stops reporting the NEW breakage too. Removing an entry here is
# how a fix proves itself — the check goes green on its own.
KNOWN = {
    'example_json.cryo': '12.10 — json_encode key order (pyro sorts, node inserts)',
    'example_saas.cryo': '12.10 — json_encode key order (pyro sorts, node inserts)',
    'example_v2.cryo':   '12.9 — enum member used as a value',
}

_passed = _failed = _known = 0


def check(label, cond, detail='', known=None):
    global _passed, _failed, _known
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    elif known:
        _known += 1
        print(f"  known {label}  [{known}]")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def available(backend):
    if backend in ('pyro', 'asm', 'wasm'):
        return True
    if backend == 'node':
        return shutil.which('node') is not None
    if backend == 'go':
        return shutil.which('go') is not None
    if backend == 'c':
        return any(shutil.which(c) for c in ('gcc', 'clang', 'cc'))
    if backend == 'csharp':
        return shutil.which('dotnet') is not None
    if backend == 'cpp':
        sys.path.insert(0, os.path.join(ROOT, 'Burnout'))
        sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
        import compiler as _cc
        return _cc.find_cxx()[0] is not None
    return False


def find_vm():
    for c in (os.path.join(ROOT, 'build', 'pyrovm' + EXE),
              os.path.join(ROOT, 'Pyro', 'vm', 'pyrovm_go' + EXE),
              os.path.join(ROOT, 'Pyro', 'vm', 'pyrovm' + EXE)):
        if os.path.isfile(c):
            return c
    return None


def compile_to(path, backend, out, timeout=600):
    """(ok, message). `message` is the compiler's own words when it refuses."""
    cmd = [sys.executable, CRYOC, path, '--backend', backend, '-o', out,
           '--no-banner']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, 'compiler timed out'
    if r.returncode != 0:
        msg = (r.stdout + r.stderr).strip().replace('\n', ' ')
        return False, msg[-160:]
    return True, ''


def run_artifact(out, backend, work, timeout=120):
    """(ok, output). Never raises."""
    try:
        if backend == 'pyro':
            vm = find_vm()
            if vm is None:
                return False, '<no VM>'
            cmd = [vm, out]
        elif backend == 'node':
            cmd = ['node', out]
        elif backend == 'go':
            cmd = ['go', 'run', out]
        elif backend in ('c', 'cpp'):
            exe = os.path.splitext(out)[0] + EXE
            if not os.path.isfile(exe):
                return False, '<no binary>'
            cmd = [exe]
        elif backend == 'csharp':
            # The SDK wants a project, not a loose file; the compiler builds one
            # beside the .cs and this reuses that helper rather than repeating
            # the .csproj here, where it would drift.
            sys.path.insert(0, os.path.join(ROOT, 'Burnout'))
            sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
            import compiler as _c
            proj = _c._csharp_project(out)
            if proj is None:
                return False, '<no dotnet>'
            cmd = ['dotnet', 'run', '--project', proj, '-v', 'quiet', '--nologo']
        else:
            return False, '<not runnable>'
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout, cwd=work)
        return r.returncode == 0, (r.stdout or '').replace('\r\n', '\n').strip()
    except subprocess.TimeoutExpired:
        return False, '<timeout>'
    except Exception as e:
        return False, f'<{type(e).__name__}>'


def ext_for(backend):
    return {'pyro': '.pyro', 'node': '.js', 'go': '.go', 'c': '.c',
            'asm': '.s', 'wasm': '.wasm', 'csharp': '.cs',
            'cpp': '.cpp'}[backend]


def runnable_source(path):
    src = open(path, encoding='utf-8', errors='replace').read()
    return not any(n in src for n in _OUTSIDE_WORLD)


def has_foreign_block(path):
    """A `>Go( … )` / `>Node( … )` / `>C( … )` block runs on ONE backend and is
    omitted on the others, so such an example is SUPPOSED to print different
    things in different places. Requiring agreement there would be asserting
    the opposite of the feature."""
    src = open(path, encoding='utf-8', errors='replace').read()
    return any(t in src for t in ('>Go(', '>Node(', '>JS(', '>C(', '>Python('))


# ── part 1: the examples ────────────────────────────────────
def sweep_examples(backends, only=None):
    paths = sorted(glob.glob(os.path.join(EXAMPLES, '*.cryo')))
    if only:
        paths = [p for p in paths if only in os.path.basename(p)]
    work = tempfile.mkdtemp(prefix='cryo_ex_')
    results = {}      # name -> {backend: (compiled, ran, output)}
    try:
        for p in paths:
            name = os.path.basename(p)
            runnable = runnable_source(p)
            foreign = has_foreign_block(p)
            per = {}
            for b in backends:
                out = os.path.join(work, os.path.splitext(name)[0] + '_' + b + ext_for(b))
                ok, msg = compile_to(p, b, out)
                if not ok:
                    per[b] = (False, False, msg)
                    continue
                if not runnable:
                    per[b] = (True, None, '')     # compiled; deliberately not run
                    continue
                ran, output = run_artifact(out, b, work)
                per[b] = (True, ran, output)
            results[name] = (runnable, foreign, per)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return results


def report_examples(results, backends):
    print(f"\n── {len(results)} examples on {', '.join(backends)} ──")
    compiled_anywhere = 0
    for name, (runnable, foreign, per) in sorted(results.items()):
        oks = [b for b, (c, _r, _o) in per.items() if c]
        if oks:
            compiled_anywhere += 1
        # Outputs of every backend that actually RAN must agree. This is the
        # assertion with teeth: compiling proves far less than agreeing.
        outs = {} if foreign else {b: o for b, (c, r, o) in per.items() if c and r}
        if len(outs) >= 2 and len(set(outs.values())) > 1:
            detail = '; '.join(f"{b}={o[:60]!r}" for b, o in outs.items())
            check(f"{name}: backends agree", False, detail, known=KNOWN.get(name))
        elif len(outs) >= 2:
            check(f"{name}: {len(outs)} backends agree", True)
        # A backend that compiled it but then failed at RUN time is a real
        # defect — the program was accepted and then did not work.
        broke = [b for b, (c, r, o) in per.items() if c and r is False]
        if broke and runnable:
            for b in broke:
                check(f"{name}: runs on {b}", False, per[b][2][:160],
                      known=KNOWN.get(name))
    # Only meaningful with the whole set. With a subset, an example that
    # legitimately targets a backend not being tested (a foreign `>Go(...)`
    # block, say) compiles nowhere for a reason that is not a defect — and a
    # check that fires on correct behaviour trains people to ignore it.
    nowhere = len(results) - compiled_anywhere
    if set(backends) >= {'pyro', 'node', 'go', 'c'}:
        check("every example compiles on at least one backend", nowhere == 0,
              f"{nowhere} compiled nowhere")
    elif nowhere:
        print(f"  note {nowhere} example(s) compiled on none of "
              f"{', '.join(backends)} — expected for backend-specific ones; "
              f"use --full to judge")


# ── part 2: the capability matrix, generated ────────────────
# One minimal probe per row of the table in the documentation. Each is the
# smallest program that needs the feature and nothing else, so a red cell means
# that feature, not something incidental.
PROBES = [
    ('int/bool, arithmetic, bitwise', 'int a = 6 & 3; bool b = a > 1; print(a);'),
    ('functions, recursion, control flow',
     'fn f(int n) -> int ={ if (n <= 1) { return 1; } return n * f(n - 1); }\nprint(f(5));'),
    ('break/continue/assert',
     'int i = 0;\nwhile (true) { i++; if (i > 2) { break; } }\nassert(i == 3, "x");\nprint(i);'),
    ('number (double), string', 'number x = 1.5; string s = "a" + "b"; print(s);'),
    ('arrays', 'int[] a = [1, 2, 3]; print(a[1]); print(a);'),
    ('enum', 'enum E { A, B }\nE e = A;\nprint(1);'),
    ('struct', 'struct P { int x; }\nP p = new P{x: 1};\nprint(p.x);'),
    ('try/catch/finally',
     'try { throw("e"); } catch (string m) { print(m); }'),
    ('map `map<K,V>`', 'map<string,int> m = {"a": 1}; print(m["a"]); print(len(m));'),
    ('optionals `T?` / null-safety',
     'int? o = null; print(o ?? 7); int? p = 3; print(p!);'),
    ('string interpolation', 'int n = 2; print("n=${n}");'),
    ('match with payloads',
     'enum R { Ok(int), Err(string) }\nR r = Ok(1);\nmatch (r) { Ok(v) => { print(v); } Err(e) => { print(e); } }'),
    ('lambdas / higher-order',
     'int[] a = [1, 2]; int[] b = map(a, (int v) => v * 2); print(b);'),
    ('generics `fn f<T>`',
     'fn id<T>(T v) -> T ={ return v; }\nprint(id<int>(5));'),
    ('native JSON', 'string s = json_encode([1, 2]); print(s);'),
    ('module state', 'int counter = 0;\nfn bump() -> void ={ counter = counter + 1; }\nbump();\nprint(counter);'),
]


def build_matrix(backends):
    work = tempfile.mkdtemp(prefix='cryo_mx_')
    rows = []
    try:
        for i, (label, src) in enumerate(PROBES):
            p = os.path.join(work, f'probe{i}.cryo')
            open(p, 'w', encoding='utf-8').write(src + '\n')
            cells = {}
            for b in backends:
                out = os.path.join(work, f'probe{i}_{b}' + ext_for(b))
                ok, msg = compile_to(p, b, out)
                if not ok:
                    cells[b] = ('no', msg)
                    continue
                # Compiling is not enough for a green cell: 11.33 produced
                # perfectly plausible Go that the Go compiler then rejected,
                # and the matrix would have called that a yes.
                ran, _o = run_artifact(out, b, work)
                cells[b] = ('yes' if ran else 'compiles-only', '')
            rows.append((label, cells))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return rows


def print_matrix(rows, backends):
    mark = {'yes': '✅', 'no': '❌', 'compiles-only': '⚠️'}
    print('\n| Feature | ' + ' | '.join(backends) + ' |')
    print('|---|' + '---|' * len(backends))
    for label, cells in rows:
        print(f'| {label} | ' +
              ' | '.join(mark[cells[b][0]] for b in backends) + ' |')
    print('\n✅ compiles and runs · ⚠️ compiles but does not run · ❌ refused')


def main():
    ap = argparse.ArgumentParser(description='Every example on every backend (12.4)')
    ap.add_argument('--backends', default='pyro,node',
                    help='comma-separated; default pyro,node (the fast pair)')
    ap.add_argument('--full', action='store_true',
                    help='pyro,node,go,c — everything that can run a program')
    ap.add_argument('--matrix', action='store_true',
                    help='print the generated capability matrix and exit')
    ap.add_argument('--only', help='limit to examples whose name contains this')
    args = ap.parse_args()

    backends = ['pyro', 'node', 'go', 'c', 'csharp', 'cpp'] if args.full else \
        [b.strip() for b in args.backends.split(',') if b.strip()]
    missing = [b for b in backends if not available(b)]
    backends = [b for b in backends if available(b)]
    if missing:
        print(f"[12.4] skipping unavailable backend(s): {', '.join(missing)}")
    if not backends:
        print("[12.4] no backend available")
        return 0

    print(f"[12.4] examples and capability matrix — backends: {', '.join(backends)}")

    if args.matrix:
        print_matrix(build_matrix(backends), backends)
        return 0

    results = sweep_examples(backends, only=args.only)
    report_examples(results, backends)

    print("\n── the capability matrix, generated ──")
    rows = build_matrix(backends)
    # The point of generating it: a claim nobody checks eventually goes stale,
    # and this one did. Every row must hold on at least one backend, or the
    # probe is broken rather than the backend.
    for label, cells in rows:
        ok = any(v[0] == 'yes' for v in cells.values())
        check(f"probe works somewhere: {label}", ok,
              '; '.join(f"{b}={v[0]}" for b, v in cells.items()))
    print_matrix(rows, backends)

    print(f"\n{_passed} passed, {_failed} failed"
          + (f", {_known} known (see the roadmap)" if _known else ""))
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
