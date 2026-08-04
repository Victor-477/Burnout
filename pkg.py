# ============================================================
#  Burnout — `cryoc pkg`  (roadmap 12.3)
#
#  The CLI over Cryo/packages.py. Four verbs and no more:
#
#    cryoc pkg init [--name N]   write a cryo.toml here
#    cryoc pkg list              what this package depends on, and where
#    cryoc pkg lock              record what the dependencies contain right now
#    cryoc pkg check             does the tree still match the lock?
#
#  There is deliberately no `install`, `add`, `update` or `publish`. Nothing is
#  fetched, so there is nothing to install; a dependency is a path you already
#  have. A command named `install` that only rewrote a local file would be a
#  promise the system does not keep.
#
#  `check` is the one meant for CI: exit 1 on drift, and say which dependency
#  moved. That is the whole value of the lock — a build that quietly used
#  different sources than the one before it is exactly what 11.14 exists to
#  make impossible, and 12.3 extends across the dependency boundary.
# ============================================================
import os
import sys

import packages


def _fail(msg):
    print(f"[Package Error] {msg}", file=sys.stderr)
    return 1


def _manifest(start):
    mf = packages.load(start)
    if mf is None:
        raise packages.PackageError(
            f"no {packages.MANIFEST} at or above {os.path.abspath(start)} — "
            f"run `cryoc pkg init`")
    return mf


def cmd_init(args, cwd):
    path = os.path.join(cwd, packages.MANIFEST)
    if os.path.isfile(path):
        return _fail(f"{path} already exists")
    name = None
    if '--name' in args:
        i = args.index('--name')
        if i + 1 >= len(args):
            return _fail("--name needs a value")
        name = args[i + 1]
    name = name or os.path.basename(os.path.abspath(cwd)) or 'package'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(
            '[package]\n'
            f'name = "{name}"\n'
            'version = "0.1.0"\n'
            '\n'
            '# Dependencies are paths — there is no registry.\n'
            '#\n'
            '#   [dependencies]\n'
            '#   geometry = "../geometry"\n'
            '#\n'
            '# and then, in a .cryo file:  import "@geometry/shapes.cryo"\n'
            '[dependencies]\n')
    print(f"wrote {path}")
    return 0


def cmd_list(args, cwd):
    mf = _manifest(cwd)
    print(f"{mf.name} {mf.version}  ({mf.root})")
    if not mf.deps:
        print("  (no dependencies)")
        return 0
    for dep in sorted(mf.deps):
        root = mf.dep_root(dep)
        if os.path.isdir(root):
            _digest, n = packages.digest_of(root)
            print(f"  {dep:<20} {mf.deps[dep]:<24} {n} file(s)")
        else:
            print(f"  {dep:<20} {mf.deps[dep]:<24} MISSING ({root})")
    return 0


def cmd_lock(args, cwd):
    mf = _manifest(cwd)
    rows = packages.compute_lock(mf)
    text = packages.render_lock(mf, rows)
    path = os.path.join(mf.root, packages.LOCKFILE)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"wrote {path} ({len(rows)} dependenc"
          f"{'y' if len(rows) == 1 else 'ies'})")
    return 0


def cmd_check(args, cwd):
    mf = _manifest(cwd)
    problems = packages.verify(mf)
    if not problems:
        print(f"{packages.LOCKFILE} is up to date ({len(mf.deps)} "
              f"dependenc{'y' if len(mf.deps) == 1 else 'ies'})")
        return 0
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    print("run `cryoc pkg lock` if the change was intended",
          file=sys.stderr)
    return 1


_VERBS = {
    'init': cmd_init,
    'list': cmd_list,
    'lock': cmd_lock,
    'check': cmd_check,
}

USAGE = """usage: cryoc pkg <command>

  init [--name N]   create cryo.toml in the current directory
  list              show the declared dependencies and where they point
  lock              write cryo.lock, pinning each dependency by content
  check             fail if the dependencies no longer match cryo.lock

Dependencies are local paths; nothing is downloaded. Import from one with
the @ sigil:  import "@geometry/shapes.cryo"
"""


def main(argv=None, cwd=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cwd = cwd or os.getcwd()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0 if argv else 1
    verb = argv[0]
    fn = _VERBS.get(verb)
    if fn is None:
        print(f"unknown command 'pkg {verb}'\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1
    try:
        return fn(argv[1:], cwd)
    except packages.PackageError as e:
        return _fail(e)
    except OSError as e:
        return _fail(e)


if __name__ == '__main__':
    sys.exit(main())
