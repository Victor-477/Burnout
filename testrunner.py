#!/usr/bin/env python3
# ============================================================
#  Burnout — `cryoc test`  (roadmap 12.1)
#
#  Compiles a .cryo with a runner appended for its `test fn` declarations, then
#  runs it. The runner itself is built in the FRONT END (cryo/testing.py) as
#  ordinary Cryo, so a suite behaves identically on every backend and no code
#  generator knows tests exist.
#
#  This file is only the driver: parse, lower, compile, execute, forward the
#  exit code. It deliberately does NOT interpret the program's output — a
#  runner that parses its own report has two definitions of "failed" and they
#  drift. The failing suite aborts on an assert, and the exit code carries it.
# ============================================================
import argparse
import os
import subprocess
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
for _p in ('Cryo', 'Pyro'):
    sys.path.insert(0, os.path.join(_root, _p))
sys.path.insert(0, _here)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='cryoc test',
        description='Run the `test fn` declarations in a Cryo file.')
    ap.add_argument('input', nargs='+', help='the .cryo file(s) or directory to test')
    ap.add_argument('--backend', default='pyro',
                    choices=['pyro', 'go', 'node', 'c'],
                    help='which backend to run the suite on (default: pyro)')
    ap.add_argument('--unsafe', action='store_true',
                    help='turn off safety instrumentation')
    ap.add_argument('--list', action='store_true',
                    help='list the tests without running them')
    args = ap.parse_args(argv)

    files = []
    for item in args.input:
        if os.path.isdir(item):
            found = []
            for root, _, fnames in os.walk(item):
                for fn in sorted(fnames):
                    if fn.endswith('.cryo'):
                        found.append(os.path.join(root, fn))
            if not found:
                print(f"[cryoc test] no .cryo files found in directory: {item}", file=sys.stderr)
                return 2
            files.extend(found)
        elif os.path.isfile(item):
            files.append(item)
        else:
            print(f"[cryoc test] no such file: {item}", file=sys.stderr)
            return 2

    if not files:
        print("[cryoc test] no input files", file=sys.stderr)
        return 2

    if len(files) == 1:
        return _run_single(files[0], args)

    total_rc = 0
    if args.list:
        total_tests = 0
        for fpath in files:
            rc, count = _list_tests_file(fpath)
            total_tests += count
            if rc != 0:
                total_rc = rc
        print(f"\n{total_tests} test(s) total")
        return total_rc

    for fpath in files:
        rc = _run_single(fpath, args)
        if rc != 0:
            total_rc = rc
    return total_rc


def _list_tests_file(fpath):
    from lexer import Lexer, LexerError
    from parser import Parser, ParseError
    import testing
    try:
        src = open(fpath, encoding='utf-8').read()
        ast = Parser(Lexer(src).tokenize()).parse()
        tests = testing.collect(ast)
        for t in tests:
            print(f"  {t.name}  ({fpath}:{getattr(t, 'line', 0)})")
        return 0, len(tests)
    except Exception as e:
        print(f"[cryoc test] error in {fpath}: {e}", file=sys.stderr)
        return 1, 0


def _run_single(path, args):
    from lexer import Lexer, LexerError
    from parser import Parser, ParseError
    import testing

    src = open(path, encoding='utf-8').read()
    try:
        ast = Parser(Lexer(src).tokenize()).parse()
    except (LexerError, ParseError) as e:
        print(f"\n{e}", file=sys.stderr)
        return 1

    tests = testing.collect(ast)
    if args.list:
        for t in tests:
            print(f"  {t.name}  ({path}:{getattr(t, 'line', 0)})")
        print(f"\n{len(tests)} test(s)")
        return 0
    if not tests:
        # Not an error worth a stack trace, but not a pass either: a file with
        # no tests reporting success is how a suite silently stops running.
        print(f"[cryoc test] {path}: no tests found.\n"
              f"  Declare one with:  test fn name() ={{ assert(…, \"…\"); }}",
              file=sys.stderr)
        return 1

    try:
        testing.build_runner(ast)
    except testing.TestError as e:
        print(f"\n[cryoc test] {e}", file=sys.stderr)
        return 1

    print(f"[cryoc test] {path} — {len(tests)} test(s) on --backend {args.backend}\n")
    # The suite writes to this terminal from a CHILD process, which does not
    # share this one's buffer — without the flush the header lands after the
    # results it introduces.
    sys.stdout.flush()

    # From here it is an ordinary compile-and-run of the lowered program.
    import compiler
    work = tempfile.mkdtemp(prefix='cryo_test_')
    try:
        code = compiler._compile_resolved(
            ast, args.backend, not args.unsafe, compiler.default_abi(),
            True, False, 'html', None, src, path)
        return _run(code, args.backend, work)
    except Exception as e:
        print(f"\n[cryoc test] {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _run(code, backend, work):
    """Execute the compiled suite and return its exit code."""
    if backend == 'pyro':
        out = os.path.join(work, 'suite.pyro')
        with open(out, 'wb') as f:
            f.write(code)
        vm = _find_vm()
        if vm is None:
            print("[cryoc test] no Pyro VM binary found — build Pyro/vm",
                  file=sys.stderr)
            return 2
        return subprocess.run([vm, out]).returncode
    if backend == 'node':
        out = os.path.join(work, 'suite.js')
        with open(out, 'w', encoding='utf-8') as f:
            f.write(code)
        return subprocess.run(['node', out]).returncode
    if backend == 'go':
        out = os.path.join(work, 'suite.go')
        with open(out, 'w', encoding='utf-8') as f:
            f.write(code)
        return subprocess.run(['go', 'run', out], cwd=work).returncode
    # c: the backend emits source; compile it the way the driver does
    out = os.path.join(work, 'suite.c')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(code)
    import shutil
    cc = next((c for c in ('gcc', 'clang', 'cc') if shutil.which(c)), None)
    if cc is None:
        print("[cryoc test] no C toolchain found", file=sys.stderr)
        return 2
    exe = os.path.join(work, 'suite' + ('.exe' if sys.platform == 'win32' else ''))
    rt = os.path.join(_here, 'runtime', 'cryo_runtime.c')
    libs = ['-lm'] + (['-lws2_32'] if sys.platform == 'win32' else [])
    r = subprocess.run([cc, '-O2', '-std=c11', '-I', os.path.join(_here, 'runtime'),
                        '-o', exe, out, rt] + libs, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-800:], file=sys.stderr)
        return 1
    return subprocess.run([exe]).returncode


def _find_vm():
    exe = '.exe' if sys.platform == 'win32' else ''
    for c in (os.path.join(_root, 'build', 'pyrovm' + exe),
              os.path.join(_root, 'Pyro', 'vm', 'pyrovm_go' + exe),
              os.path.join(_root, 'Pyro', 'vm', 'pyrovm' + exe)):
        if os.path.isfile(c):
            return c
    return None


if __name__ == '__main__':
    sys.exit(main())
