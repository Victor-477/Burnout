#!/usr/bin/env python3
# ============================================================
#  test_packages.py — cryo.toml and cryo.lock (roadmap 12.3)
#
#  A package manager earns trust by what it REFUSES, so most of
#  what is tested here is refusal:
#
#    * ADDING A cryo.toml CANNOT CHANGE AN EXISTING IMPORT.
#      This is the one that would make 12.3 not worth having.
#      Only `@name/...` is claimed; a relative import resolves
#      against the importing file exactly as it always did, and
#      a project with no manifest never reads one.
#    * A DEPENDENCY IS NOT A FILE-READ PRIMITIVE. `@dep/../../x`
#      names a path outside the package it claims to come from,
#      and is refused rather than quietly followed.
#    * AN UNDECLARED DEPENDENCY IS AN ERROR, not a lookup that
#      happens to succeed because a directory of that name is
#      sitting next door.
#
#  And the lock is tested for the property it exists for: the
#  same sources give the same digest, and ANY edit to a
#  dependency shows up — including one that leaves the file
#  count unchanged, which is what a version number would miss.
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
sys.path.insert(0, os.path.join(ROOT, 'Cryo'))
sys.path.insert(0, os.path.join(ROOT, 'Burnout'))

import packages          # noqa: E402
import modules           # noqa: E402

_passed = _failed = 0


def check(label, cond, detail=''):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ''))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def cryoc(args, cwd):
    r = subprocess.run([sys.executable, CRYOC] + args, cwd=cwd,
                       capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=900)
    return (r.stdout or '') + (r.stderr or ''), r.returncode


# ── a two-package workspace ─────────────────────────────────
GEOMETRY = '''\
const number GEO_PI = 3.14159;

pub fn area(number r) -> number ={
    return GEO_PI * r * r;
}
'''

APP = '''\
import "@geometry/shapes.cryo";

print(area(2.0));
'''


def workspace(tmp, with_manifest=True):
    """app/ depending on ../geometry."""
    write(os.path.join(tmp, 'geometry', 'shapes.cryo'), GEOMETRY)
    write(os.path.join(tmp, 'app', 'main.cryo'), APP)
    if with_manifest:
        write(os.path.join(tmp, 'app', 'cryo.toml'),
              '[package]\nname = "app"\nversion = "0.1.0"\n\n'
              '[dependencies]\ngeometry = "../geometry"\n')
    return os.path.join(tmp, 'app')


def main():
    print("[12.3] packages")
    tmp = tempfile.mkdtemp(prefix='cryopkg_')
    try:
        # ── resolution ─────────────────────────────────────
        print("\n── resolving @dep/file.cryo ──")
        app = workspace(tmp)
        mf = packages.load(app)
        check("the manifest is found from inside the package",
              mf is not None and mf.name == 'app')
        check("the dependency is declared", mf.deps == {'geometry': '../geometry'},
              repr(mf.deps))

        got = packages.resolve_import('@geometry/shapes.cryo', app, mf)
        check("@geometry/shapes.cryo resolves into the dependency",
              got == os.path.abspath(os.path.join(tmp, 'geometry', 'shapes.cryo')),
              str(got))

        # find_manifest walks UP: a build invoked deep inside still finds it.
        deep = write(os.path.join(app, 'a', 'b', 'deep.cryo'), '')
        check("the manifest is found from a subdirectory",
              packages.load(os.path.dirname(deep)).name == 'app')

        # ── what is refused ────────────────────────────────
        print("\n── refusals ──")

        def refused(spec, needle, mfx=mf, label=None):
            try:
                packages.resolve_import(spec, app, mfx)
                check(label or spec, False, "resolved instead of refusing")
            except packages.PackageError as e:
                check(label or spec, needle in str(e), str(e))

        refused('@geometry/../../secret.cryo', 'escapes',
                label="a path escaping the dependency is refused")
        refused('@nosuchdep/x.cryo', 'not a dependency',
                label="an undeclared dependency is refused")
        refused('@geometry/missing.cryo', 'no such file',
                label="a missing file inside a dependency is refused")
        refused('@geometry', 'expected', label="'@geometry' alone is refused")
        refused('@/x.cryo', 'expected', label="'@/x.cryo' is refused")
        refused('@geometry/shapes.cryo', 'cryo.toml', mfx=None,
                label="an @ import with no manifest says so")

        # An undeclared dependency is refused even when a directory of that name
        # is sitting right there — being on disk is not what makes it a
        # dependency, declaring it is.
        write(os.path.join(tmp, 'sneaky', 'x.cryo'), 'pub fn f() -> int ={ return 1; }')
        refused('@sneaky/x.cryo', 'not a dependency',
                label="a directory that exists but is undeclared is still refused")

        # ── the invariant: nothing else changes ────────────
        print("\n── adding a cryo.toml changes nothing else ──")
        check("a relative import is not claimed by the package layer",
              packages.resolve_import('./local.cryo', app, mf) is None)
        check("nor is a bare one",
              packages.resolve_import('lib.cryo', app, mf) is None)
        check("nor is one containing an @ elsewhere",
              packages.resolve_import('lib@2.cryo', app, mf) is None)
        check("the hook is unset by default — no manifest is read unless "
              "an @ import appears", modules.PACKAGE_MANIFEST is None)

        # The same relative program must behave identically with and without a
        # manifest present. This is the whole safety argument for 12.3.
        for label, toml in (("without cryo.toml", False), ("with cryo.toml", True)):
            d = os.path.join(tmp, 'rel_' + str(int(toml)))
            write(os.path.join(d, 'lib.cryo'),
                  'pub fn twice(int n) -> int ={ return n * 2; }')
            write(os.path.join(d, 'main.cryo'),
                  'import "lib.cryo";\n\nprint(twice(21));\n')
            if toml:
                write(os.path.join(d, 'cryo.toml'),
                      '[package]\nname = "rel"\n\n[dependencies]\n')
            out, rc = cryoc(['main.cryo', '--backend', 'pyro', '--run'], d)
            check(f"a relative import still works {label}",
                  rc == 0 and '42' in out, out[-400:])

        # ── end to end ─────────────────────────────────────
        print("\n── compiling across a dependency ──")
        out, rc = cryoc(['main.cryo', '--backend', 'pyro', '--run'], app)
        check("a program importing @geometry/... runs",
              rc == 0 and '12.56' in out, out[-500:])

        # Resolution happens in the front end, so it is backend-independent by
        # construction — which is a reason to expect parity, not evidence of it.
        for b in ('go', 'node'):
            out, rc = cryoc(['main.cryo', '--backend', b, '--run'], app)
            check(f"and on the {b} backend, with the same value",
                  rc == 0 and '12.56' in out, out[-400:])
        # The C backend needs gcc to RUN; generating is what proves the
        # dependency reached codegen.
        out, rc = cryoc(['main.cryo', '--backend', 'c'], app)
        gen = os.path.join(app, 'build', 'main.c')
        check("the C backend generates with the dependency compiled in",
              rc == 0 and os.path.isfile(gen) and
              'area' in open(gen, encoding='utf-8').read(), out[-400:])

        # Same program, manifest removed: the import must now FAIL, and say why,
        # rather than falling back to some path that happens to exist.
        bare = workspace(os.path.join(tmp, 'nomanifest'), with_manifest=False)
        out, rc = cryoc(['main.cryo', '--backend', 'pyro', '--run'], bare)
        check("without a manifest the @ import is an error", rc != 0, out[-300:])
        check("and the error names cryo.toml", 'cryo.toml' in out, out[-300:])

        # ── the lock ───────────────────────────────────────
        print("\n── the lock pins content, not a version ──")
        d1, n1 = packages.digest_of(os.path.join(tmp, 'geometry'))
        d2, n2 = packages.digest_of(os.path.join(tmp, 'geometry'))
        check("the digest is stable", d1 == d2 and n1 == 1, f"{d1} {d2} {n1}")

        out, rc = cryoc(['pkg', 'lock'], app)
        check("`pkg lock` writes the lockfile", rc == 0, out[-300:])
        lock = os.path.join(app, 'cryo.lock')
        check("cryo.lock exists", os.path.isfile(lock))
        text = open(lock, encoding='utf-8').read()
        check("it records the digest", d1 in text, text[-300:])

        out, rc = cryoc(['pkg', 'check'], app)
        check("`pkg check` passes right after locking", rc == 0, out[-300:])

        # An in-place edit — same file count, same version number, different
        # sources. A version-pinned lock would report this as unchanged.
        shape = os.path.join(tmp, 'geometry', 'shapes.cryo')
        original = open(shape, encoding='utf-8').read()
        write(shape, original.replace('3.14159', '3.0'))
        out, rc = cryoc(['pkg', 'check'], app)
        check("`pkg check` fails after an in-place edit", rc == 1, out[-300:])
        check("and names the dependency that moved", 'geometry' in out, out[-300:])
        check("the file COUNT is unchanged — content is what caught it",
              packages.digest_of(os.path.join(tmp, 'geometry'))[1] == 1)
        write(shape, original)
        out, rc = cryoc(['pkg', 'check'], app)
        check("restoring the sources restores the lock", rc == 0, out[-300:])

        # A file added to the dependency is drift too.
        extra = os.path.join(tmp, 'geometry', 'extra.cryo')
        write(extra, 'pub fn zero() -> int ={ return 0; }')
        _out, rc = cryoc(['pkg', 'check'], app)
        check("adding a file to a dependency is drift", rc == 1)
        os.remove(extra)

        # A dependency declared but never locked.
        write(os.path.join(tmp, 'other', 'o.cryo'), 'pub fn o() -> int ={ return 1; }')
        toml = os.path.join(app, 'cryo.toml')
        base = open(toml, encoding='utf-8').read()
        write(toml, base + 'other = "../other"\n')
        out, rc = cryoc(['pkg', 'check'], app)
        check("a newly declared dependency is drift", rc == 1, out[-300:])
        check("and the message says it is not in the lock",
              'not in cryo.lock' in out, out[-300:])
        write(toml, base)

        # ── the CLI ────────────────────────────────────────
        print("\n── cryoc pkg ──")
        out, rc = cryoc(['pkg', 'list'], app)
        check("`pkg list` shows the dependency",
              rc == 0 and 'geometry' in out and '../geometry' in out, out[-300:])

        fresh = os.path.join(tmp, 'fresh')
        os.makedirs(fresh, exist_ok=True)
        out, rc = cryoc(['pkg', 'init'], fresh)
        check("`pkg init` writes cryo.toml", rc == 0 and
              os.path.isfile(os.path.join(fresh, 'cryo.toml')), out[-300:])
        check("and names the package after the directory",
              'name = "fresh"' in open(os.path.join(fresh, 'cryo.toml'),
                                       encoding='utf-8').read())
        out, rc = cryoc(['pkg', 'init'], fresh)
        check("`pkg init` refuses to overwrite", rc == 1, out[-300:])

        out, rc = cryoc(['pkg', 'check'], fresh)
        check("`pkg check` with no lockfile fails and says to lock",
              rc == 1 and 'pkg lock' in out, out[-300:])

        empty = os.path.join(tmp, 'empty')
        os.makedirs(empty, exist_ok=True)
        out, rc = cryoc(['pkg', 'list'], empty)
        check("outside a package, `pkg list` says there is no cryo.toml",
              rc == 1 and 'cryo.toml' in out, out[-300:])

        out, rc = cryoc(['pkg', 'nonsense'], app)
        check("an unknown verb is an error", rc == 1 and 'unknown' in out,
              out[-300:])
        out, rc = cryoc(['pkg'], app)
        check("bare `pkg` prints usage", 'usage' in out.lower(), out[-300:])
        check("and there is no `install` to promise a registry",
              'install' not in out.lower(), out[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == '__main__':
    sys.exit(main())
