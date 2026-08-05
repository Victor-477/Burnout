#!/usr/bin/env python3
# ============================================================
#  Burnout — differential testing across backends (roadmap 13.1)
#
#  Generates VALID Cryo programs, runs them on every backend that is
#  available, and compares the output. A disagreement is a breach of
#  invariant 1 and is reported with a MINIMAL reproducer.
#
#  WHY THIS EXISTS
#
#  Every parity break closed in Phase 12 was found by hand — and several
#  only incidentally, while testing something else. They share a shape:
#  the program compiled on every backend and then disagreed. A suite that
#  inspects generated TEXT cannot see that, which is why 12.4 exists and
#  why it still missed 12.9. The only thing that catches the class is
#  running the program and comparing the answer.
#
#  TWO DESIGN DECISIONS DO ALL THE WORK
#
#  1. Every generated program is DETERMINISTIC. No clock, no random, no
#     network, no filesystem, no concurrency, no map iteration order that
#     is not already specified. A generator that emits `now_ms()` reports
#     a difference on every run and is worse than no generator at all,
#     because it trains you to ignore it.
#
#  2. Programs are built from independent UNITS — one function each,
#     called in order — so a failing case can be SHRUNK by deleting
#     units. Shrinking is what makes this usable: a 200-line random
#     program that disagrees tells you nothing. The reducer here gets
#     most cases down to one or two statements.
#
#  Usage:
#      python burnout/difftest.py [--runs N] [--seed S] [--units U]
#                                 [--backends pyro,node,go] [-v]
#  Exit code 1 if any disagreement survived shrinking.
# ============================================================
import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
CRYOC = os.path.join(_HERE, 'cryoc.py')
EXE = '.exe' if sys.platform == 'win32' else ''
VM = os.path.join(_ROOT, 'Pyro', 'vm', 'pyrovm_go' + EXE)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass


# ── the generator ───────────────────────────────────────────
#
# A tiny typed expression grammar. Typed rather than untyped because an
# untyped generator spends almost all its time producing programs the
# semantic analyser rejects, and a suite whose cases mostly fail to
# compile is measuring the analyser, not the backends.

INT, NUM, STR, BOOL = 'int', 'number', 'string', 'bool'


class Gen:
    def __init__(self, rnd: random.Random):
        self.r = rnd
        self._n = 0

    def fresh(self, p='v'):
        self._n += 1
        return f"{p}{self._n}"

    # ── expressions ─────────────────────────────────────────
    def expr(self, t, depth, scope):
        """An expression of type `t`. `scope` maps name -> type."""
        if depth <= 0 or self.r.random() < 0.28:
            return self.atom(t, scope)
        if t == INT:
            return self._int_expr(depth, scope)
        if t == NUM:
            a = self.expr(NUM, depth - 1, scope)
            b = self.expr(NUM, depth - 1, scope)
            op = self.r.choice(['+', '-', '*'])
            return f"({a} {op} {b})"
        if t == STR:
            a = self.expr(STR, depth - 1, scope)
            b = self.expr(STR, depth - 1, scope)
            return self.r.choice([
                f"({a} + {b})",
                f"upper({a})", f"lower({a})", f"trim({a})",
                f"substr({a}, 0, 2)",
                f"replace({a}, \"a\", \"z\")",
                f"to_string({self.expr(INT, depth - 1, scope)})",
            ])
        if t == BOOL:
            k = self.r.random()
            if k < 0.45:
                a = self.expr(INT, depth - 1, scope)
                b = self.expr(INT, depth - 1, scope)
                return f"({a} {self.r.choice(['<', '>', '<=', '>=', '==', '!='])} {b})"
            if k < 0.7:
                a = self.expr(STR, depth - 1, scope)
                b = self.expr(STR, depth - 1, scope)
                return self.r.choice([f"({a} == {b})", f"contains({a}, {b})",
                                      f"starts_with({a}, {b})"])
            a = self.expr(BOOL, depth - 1, scope)
            b = self.expr(BOOL, depth - 1, scope)
            return f"({a} {self.r.choice(['&&', '||'])} {b})"
        return self.atom(t, scope)

    def _int_expr(self, depth, scope):
        a = self.expr(INT, depth - 1, scope)
        k = self.r.random()
        if k < 0.55:
            b = self.expr(INT, depth - 1, scope)
            # `/` and `%` use a NON-ZERO literal on the right. A random
            # divisor makes most programs abort with DivByZero, and while
            # the backends agree on that text (12.13's neighbourhood), a
            # generator that mostly produces aborts is not testing much.
            op = self.r.choice(['+', '-', '*', '+', '-'])
            return f"({a} {op} {b})"
        if k < 0.7:
            d = self.r.choice([2, 3, 5, 7, 11])
            return f"({a} {self.r.choice(['/', '%'])} {d})"
        if k < 0.85:
            return self.r.choice([f"abs({a})", f"min({a}, {self.r.randint(-9, 9)})",
                                  f"max({a}, {self.r.randint(-9, 9)})",
                                  f"len({self.expr(STR, depth - 1, scope)})"])
        return f"(0 - {a})"

    def atom(self, t, scope):
        names = [n for n, ty in scope.items() if ty == t]
        if names and self.r.random() < 0.55:
            return self.r.choice(names)
        if t == INT:
            return str(self.r.randint(-20, 20))
        if t == NUM:
            return self.r.choice(['1.5', '2.0', '0.25', '3.75', '10.0'])
        if t == STR:
            return '"' + self.r.choice(['a', 'bc', 'cryo', 'xyz', '', 'Ab']) + '"'
        if t == BOOL:
            return self.r.choice(['true', 'false'])
        return '0'

    # ── a unit: one self-contained function ─────────────────
    def unit(self, name):
        """`fn name() ={ ... print(...) ... }` — depends on nothing outside."""
        scope, body = {}, []
        for _ in range(self.r.randint(1, 3)):
            t = self.r.choice([INT, INT, STR, BOOL, NUM])
            v = self.fresh()
            body.append(f"    {t} {v} = {self.expr(t, 2, scope)};")
            scope[v] = t

        shape = self.r.random()
        if shape < 0.25:
            body += self._if_block(scope)
        elif shape < 0.45:
            body += self._loop(scope)
        elif shape < 0.6:
            body += self._array(scope)
        elif shape < 0.72:
            body += self._map(scope)
        elif shape < 0.84:
            body += self._enum_use(name)

        t = self.r.choice([INT, STR, BOOL])
        body.append(f"    print({self.expr(t, 2, scope)});")
        return f"fn {name}() ={{\n" + "\n".join(body) + "\n}"

    def _if_block(self, scope):
        c = self.expr(BOOL, 2, scope)
        v = self.fresh()
        out = [f"    int {v} = 0;",
               f"    if ({c}) {{ {v} = {self.expr(INT, 1, scope)}; }}",
               f"    else {{ {v} = {self.expr(INT, 1, scope)}; }}",
               f"    print({v});"]
        scope[v] = INT
        return out

    def _loop(self, scope):
        i, acc = self.fresh('i'), self.fresh('acc')
        n = self.r.randint(1, 4)
        out = [f"    int {acc} = 0;",
               f"    for (int {i} = 0; {i} < {n}; {i}++) {{",
               f"        {acc} += {i} * {self.r.randint(1, 5)};",
               "    }",
               f"    print({acc});"]
        scope[acc] = INT
        return out

    def _array(self, scope):
        a = self.fresh('a')
        elems = ', '.join(str(self.r.randint(-9, 9)) for _ in range(self.r.randint(1, 4)))
        out = [f"    int[] {a} = [{elems}];",
               f"    print({a});",
               f"    print(len({a}));"]
        if self.r.random() < 0.5:
            out.append(f"    print(sort({a}));")
        if self.r.random() < 0.4:
            out.append(f"    {a}.push({self.r.randint(-9, 9)});")
            out.append(f"    print({a});")
        return out

    def _map(self, scope):
        m = self.fresh('m')
        keys = self.r.sample(['a', 'b', 'c', 'd'], self.r.randint(1, 3))
        pairs = ', '.join(f'"{k}": {self.r.randint(-9, 9)}' for k in keys)
        return [f"    map<string,int> {m} = {{{pairs}}};",
                f"    print({m});",
                f"    print(len({m}));",
                f"    print(json_encode({m}));"]

    def _enum_use(self, name):
        # Members are suffixed with the enum's index: two generated enums would
        # otherwise both declare A/B/C, and a BARE member name would resolve to
        # whichever was registered last. That is a property of the generator,
        # not of the language, and it would show up as a phantom disagreement.
        e = 'E' + name[1:]
        k = name[1:]
        return [f"    {e} c = {e}_B{k};",
                f"    print(c);",
                f"    if (c == {e}_B{k}) {{ print(\"eq\"); }}"]

    def program(self, n_units):
        units, calls, enums = [], [], []
        for k in range(n_units):
            fname = f"u{k}"
            src = self.unit(fname)
            if f"_B{k};" in src:                  # the unit used an enum
                enums.append(f"enum E{k} {{ A{k}, B{k}, C{k} }}")
            units.append(src)
            calls.append(f"{fname}();")
        return enums + units + calls


# ── running and comparing ───────────────────────────────────
def available(backends):
    out = []
    for b in backends:
        if b == 'pyro' and os.path.isfile(VM):
            out.append(b)
        elif b == 'node' and shutil.which('node'):
            out.append(b)
        elif b == 'go' and shutil.which('go'):
            out.append(b)
        elif b == 'c' and shutil.which('gcc'):
            out.append(b)
    return out


def run_one(work, src, backend):
    """(status, text). status is 'ok', 'refused' or 'error'."""
    cf = os.path.join(work, 'p.cryo')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write(src)
    ext = {'pyro': '.pyro', 'node': '.js', 'go': '.go', 'c': '.c'}[backend]
    out = os.path.join(work, 'p' + ext)
    c = subprocess.run([sys.executable, CRYOC, cf, '--backend', backend,
                        '-o', out, '--no-banner', '--no-cache'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=900)
    if c.returncode != 0:
        # A backend REFUSING a construct is not a disagreement — it is the
        # documented answer for c and node on several features. Only
        # backends that produced output are compared.
        return 'refused', (c.stdout or '') + (c.stderr or '')
    try:
        if backend == 'pyro':
            r = subprocess.run([VM, out], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=300)
        elif backend == 'node':
            r = subprocess.run(['node', out], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=300)
        elif backend == 'go':
            r = subprocess.run(['go', 'run', out], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=900,
                               cwd=work)
            if r.returncode != 0 and 'command-line-arguments' in (r.stderr or ''):
                return 'error', r.stderr          # generated Go did not build
        else:
            exe = os.path.splitext(out)[0] + EXE
            if not os.path.isfile(exe):
                return 'refused', 'no binary'
            r = subprocess.run([exe], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=300)
    except subprocess.TimeoutExpired:
        return 'error', '<timeout>'
    return 'ok', (r.stdout or '').replace('\r\n', '\n').strip()


def disagreement(work, lines, backends):
    """None when every available backend agrees, else a report string."""
    src = '\n'.join(lines) + '\n'
    got, refused = {}, {}
    for b in backends:
        st, text = run_one(work, src, b)
        if st == 'ok':
            got[b] = text
        elif st == 'error':
            return f"{b} failed to build/run its own output:\n{text[:400]}"
        else:
            refused[b] = text
    if len(got) < 2:
        return None
    if len(set(got.values())) == 1:
        return None
    return '; '.join(f"{b}={v!r}" for b, v in got.items())


def shrink(work, lines, backends, verbose=False):
    """Delete units while the disagreement survives.

    Units are independent functions, so dropping one (and its call) always
    leaves a valid program. That is the whole reason the generator emits
    functions rather than a flat sequence of statements.
    """
    cur = list(lines)
    changed = True
    while changed:
        changed = False
        # First pass: drop any element that is not a unit — an enum
        # declaration the surviving units never mention, typically. Cheap, and
        # it is the difference between a reproducer you can read and one you
        # have to edit before reporting.
        for i in range(len(cur)):
            if cur[i].startswith('fn u'):
                continue
            cand = [l for j, l in enumerate(cur) if j != i]
            if disagreement(work, cand, backends):
                cur = cand
                changed = True
                if verbose:
                    print(f"    shrunk to {len(cur)} elements")
                break
        if changed:
            continue
        for i in range(len(cur)):
            if not cur[i].startswith('fn u'):
                continue
            name = cur[i][3:cur[i].index('(')]        # 'fn u0() ={' -> 'u0'
            call = f"{name}();"
            cand = [l for j, l in enumerate(cur) if j != i and l != call]
            # Dropping the definition must ALSO drop the call, or the program
            # calls a function that no longer exists — every backend refuses
            # it, which `disagreement` correctly reads as "nothing to compare",
            # so the candidate is rejected and the reducer stalls having shrunk
            # nothing. That is how this shipped the first time, with the name
            # rebuilt as `uu0` so no call ever matched.
            #
            # The call may ALREADY be gone: the pass above removes a dead call
            # on its own, which is a real reduction worth keeping. So the test
            # is that something was removed, not that exactly two were.
            if len(cand) >= len(cur):
                continue
            if disagreement(work, cand, backends):
                cur = cand
                changed = True
                if verbose:
                    print(f"    shrunk to {len(cur)} elements")
                break
    return cur


def main(argv=None):
    ap = argparse.ArgumentParser(description="differential testing (13.1)")
    ap.add_argument('--runs', type=int, default=25)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--units', type=int, default=4)
    ap.add_argument('--backends', default='pyro,node,go')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    backends = available([b.strip() for b in a.backends.split(',') if b.strip()])
    if len(backends) < 2:
        print(f"need at least two backends; have {backends or 'none'}")
        return 0
    print(f"[13.1] differential testing — backends: {', '.join(backends)}, "
          f"{a.runs} programs from seed {a.seed}")

    work = tempfile.mkdtemp(prefix='cryo_diff_')
    failures = []
    try:
        for k in range(a.runs):
            seed = a.seed + k
            lines = Gen(random.Random(seed)).program(a.units)
            rep = disagreement(work, lines, backends)
            if rep is None:
                if a.verbose:
                    print(f"  ok   seed {seed}")
                continue
            print(f"  FAIL seed {seed}: {rep[:200]}")
            small = shrink(work, lines, backends, a.verbose)
            print("  ── minimal reproducer ──")
            for l in small:
                print("    " + l)
            print("  ── ── ──")
            failures.append((seed, rep, small))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{a.runs - len(failures)}/{a.runs} programs agreed")
    if failures:
        print(f"{len(failures)} disagreement(s): seeds "
              + ', '.join(str(s) for s, _, _ in failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
