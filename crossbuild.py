#!/usr/bin/env python3
# ============================================================
#  cryoc build — one .cryo, native executables for Windows,
#  Linux and macOS, produced on ONE machine.
#
#  The toolchain could already make a native binary: `--backend c`
#  calls gcc, `--backend go` calls go build, `cryoc bundle` appends
#  bytecode to the VM. All three built for the machine doing the
#  building and nothing else, so shipping a program to three
#  operating systems meant owning three machines.
#
#  This builds for all of them from one. Two routes, both already
#  in the tree, neither of them a new engine:
#
#    native  .cryo -> Go  -> GOOS/GOARCH go build   (one exe per target)
#    bundle  .cryo -> .pyro + a cross-built VM      (cryoc bundle's format)
#
#  Go cross-compiles its own standard library, so `GOOS=darwin
#  GOARCH=arm64 go build` produces a real Mach-O binary from a
#  Windows box with no Apple toolchain, no SDK and no container.
#  That is the whole reason this can exist at all; the C backend
#  cannot do it, because a C cross build needs the target's libc
#  and headers, which are not ours to ship.
#
#  WHY NOT A SEVENTH BACKEND. Invariant 1 says a program means the
#  same thing on every backend, and an engine no parity suite runs
#  is the surest way to break that quietly. Nothing here generates
#  code: `native` is the go backend the parity suite already
#  covers, and `bundle` is the Pyro VM running the same bytecode
#  it runs everywhere else. A cross build can change which CPU
#  executes the program. It cannot change what the program means.
#
#  Usage:
#      cryoc build app.cryo                      # host only
#      cryoc build app.cryo --target all         # six binaries
#      cryoc build app.cryo --target windows,linux,macos
#      cryoc build app.cryo --target linux-arm64 --via bundle
#      cryoc build --list-targets
# ============================================================
import argparse
import glob
import hashlib
import os
import platform
import shutil
import subprocess
import sys

_here = os.path.dirname(os.path.abspath(__file__))        # Burnout/
_root = os.path.dirname(_here)                            # project root
for _p in (_here, os.path.join(_root, 'Cryo')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Windows: the summary table prints ✓, and a bare `python crossbuild.py`
# would die in cp1252 before saying anything useful.
for _stream in ('stdout', 'stderr'):
    _s = getattr(sys, _stream, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass


class BuildError(Exception):
    """Something about the request or the environment, not the program."""


# ── the target table ────────────────────────────────────────
#
# Six entries, not "whatever Go supports". Each one is a platform this
# project claims to ship to, which means a name that a person would
# type and a binary someone has looked at. `go tool dist list` offers
# forty; listing them here would be advertising support for machines
# nobody has ever run Cryo on.
class Target:
    __slots__ = ('name', 'goos', 'goarch', 'exe', 'label')

    def __init__(self, name, goos, goarch, exe, label):
        self.name, self.goos, self.goarch = name, goos, goarch
        self.exe, self.label = exe, label

    def __repr__(self):
        return f'<Target {self.name}>'


TARGETS = {t.name: t for t in (
    Target('windows-x64',   'windows', 'amd64', '.exe', 'Windows 10+ (x86-64)'),
    Target('windows-arm64', 'windows', 'arm64', '.exe', 'Windows on ARM'),
    Target('linux-x64',     'linux',   'amd64', '',     'Linux (x86-64)'),
    Target('linux-arm64',   'linux',   'arm64', '',     'Linux (ARM64)'),
    Target('macos-x64',     'darwin',  'amd64', '',     'macOS (Intel)'),
    Target('macos-arm64',   'darwin',  'arm64', '',     'macOS (Apple Silicon)'),
)}

# An OS on its own means every architecture this project ships for it.
# "I want it to run on macOS" is a statement about an operating system,
# and answering it with one of the two macOS binaries would be answering
# a different question — the Intel one does not run natively on Apple
# Silicon, and the Apple Silicon one does not run on Intel at all.
OS_GROUPS = {
    'windows': ('windows-x64', 'windows-arm64'),
    'linux':   ('linux-x64', 'linux-arm64'),
    'macos':   ('macos-x64', 'macos-arm64'),
}

# Spellings people actually type. Both the Go names (darwin, amd64) and
# the names printed on the download page of every other language
# (x64, aarch64, osx) resolve, because being told "unknown target:
# linux-amd64" by a tool that has a linux-x64 is a pointless error.
ALIASES = {
    'win': 'windows', 'win32': 'windows-x64', 'win64': 'windows-x64',
    'windows-amd64': 'windows-x64', 'windows-x86_64': 'windows-x64',
    'windows-aarch64': 'windows-arm64',
    'linux-amd64': 'linux-x64', 'linux-x86_64': 'linux-x64',
    'linux-aarch64': 'linux-arm64',
    'darwin': 'macos', 'osx': 'macos', 'mac': 'macos', 'apple': 'macos',
    'darwin-amd64': 'macos-x64', 'darwin-arm64': 'macos-arm64',
    'macos-amd64': 'macos-x64', 'macos-x86_64': 'macos-x64',
    'macos-aarch64': 'macos-arm64', 'macos-apple-silicon': 'macos-arm64',
    'macos-intel': 'macos-x64',
}

_HOST_OS = {'win32': 'windows', 'cygwin': 'windows',
            'linux': 'linux', 'darwin': 'macos'}
_HOST_ARCH = {'amd64': 'x64', 'x86_64': 'x64', 'x64': 'x64',
              'arm64': 'arm64', 'aarch64': 'arm64'}


def host_target():
    """The Target for the machine doing the building, or None.

    None rather than a guess: a 32-bit or s390x host has no entry in the
    table, and inventing one would turn `--target host` into a build for
    somebody else's computer.
    """
    os_name = _HOST_OS.get(sys.platform)
    arch = _HOST_ARCH.get(platform.machine().lower())
    return TARGETS.get(f'{os_name}-{arch}') if os_name and arch else None


def resolve_targets(specs):
    """['all'] or ['windows', 'linux-arm64'] -> [Target, ...].

    Order is the order asked for, duplicates dropped — a `--target
    windows,all` should not build Windows twice, and the summary table
    reads better when it follows the command line.
    """
    out = []

    def add(t):
        if t not in out:
            out.append(t)

    for raw in specs:
        for part in str(raw).replace(';', ',').split(','):
            spec = part.strip().lower().replace('_', '-')
            if not spec:
                continue
            spec = ALIASES.get(spec, spec)
            if spec == 'all':
                for name in TARGETS:
                    add(TARGETS[name])
            elif spec == 'host':
                h = host_target()
                if h is None:
                    raise BuildError(
                        f"--target host: this machine ({sys.platform}/"
                        f"{platform.machine()}) is not one of the platforms "
                        f"this builds for; name a target explicitly")
                add(h)
            elif spec in OS_GROUPS:
                for name in OS_GROUPS[spec]:
                    add(TARGETS[name])
            elif spec in TARGETS:
                add(TARGETS[spec])
            else:
                raise BuildError(
                    f"unknown target: {part.strip()}\n"
                    f"  targets: {', '.join(TARGETS)}\n"
                    f"  or a platform: {', '.join(OS_GROUPS)}, all, host")
    return out


def describe_targets():
    lines = ["Targets (cryoc build --target ...):", ""]
    for t in TARGETS.values():
        host = '  (this machine)' if t is host_target() else ''
        lines.append(f"  {t.name:<15} {t.label}{host}")
    lines += [
        "",
        "  windows | linux | macos    both architectures of that system",
        "  all                        every target above",
        "  host                       this machine only (the default)",
        "",
        "Several at once, comma-separated: --target windows,linux,macos-arm64",
    ]
    return "\n".join(lines)


# ── the Go toolchain ────────────────────────────────────────

def _go():
    go = shutil.which('go')
    if go is None:
        raise BuildError(
            "a cross build needs the Go toolchain on THIS machine (nothing is "
            "needed on the target machines).\n"
            "  install it from https://go.dev/dl/ — nothing else is required: "
            "Go ships the standard library for every target it can build for, "
            "which is what makes a macOS binary possible from here.")
    return go


def _go_env(target):
    env = dict(os.environ)
    env['GOOS'] = target.goos
    env['GOARCH'] = target.goarch
    # No cgo, always. With cgo on, a build for another system looks for that
    # system's C compiler and fails; with it off, Go uses its own pure-Go
    # implementations. Nothing the Go backend emits needs cgo — the generated
    # code imports only the standard library — so this costs nothing and is
    # the difference between six binaries and one.
    env['CGO_ENABLED'] = '0'
    # An inherited GOFLAGS can carry -mod=vendor or -tags from the user's own
    # projects and break a build that has nothing to do with them.
    env.pop('GOFLAGS', None)
    return env


def _go_build(src, target, out_path, env=None, strip=True, cwd=None):
    """go build for one target. Returns (ok, stderr)."""
    cmd = [_go(), 'build', '-trimpath']
    if strip:
        # -s -w drops the symbol table and DWARF: about a third off a hello
        # world, and a release binary is not where anyone debugs. --debug
        # keeps them.
        cmd += ['-ldflags', '-s -w']
    cmd += ['-o', out_path, src]
    r = subprocess.run(cmd, cwd=cwd, env=env or _go_env(target),
                       capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    return r.returncode == 0, (r.stderr or '').strip()


# ── route 1: native (the Go backend, cross-compiled) ────────

def _emit(path, backend, safe, sandbox, use_cache, assets=None, sign_key=None):
    """Front end + one backend, in process. Returns str or bytes."""
    import compiler
    with open(path, 'r', encoding='utf-8') as f:
        source = f.read()
    return compiler.compile_source(
        source, backend=backend, safe=safe,
        base_dir=os.path.dirname(os.path.abspath(path)),
        sandbox=sandbox, use_cache=use_cache, path=path, assets=assets,
        sign_key=sign_key)


def build_native(input_path, targets, out_dir, name,
                 safe=True, sandbox=False, use_cache=True,
                 strip=True, verbose=False):
    """One .go, one `go build` per target. Returns [(target, path|None, err)]."""
    work = os.path.join(out_dir, '.build')
    os.makedirs(work, exist_ok=True)
    # The .go lives in its own directory, away from any go.mod the user's
    # project might have: `go build file.go` reads the module in the current
    # directory even in file mode, and a go.mod asking for a newer toolchain
    # fails a build that needs nothing from it.
    src = os.path.join(work, name + '.go')
    code = _emit(input_path, 'go', safe, sandbox, use_cache)
    with open(src, 'w', encoding='utf-8') as f:
        f.write(code)
    if verbose:
        print(f"  generated {_short(src)}  ({len(code)} bytes of Go)")

    results = []
    for t in targets:
        out = os.path.join(out_dir, f'{name}-{t.name}{t.exe}')
        if verbose:
            print(f"  GOOS={t.goos} GOARCH={t.goarch} go build -> "
                  f"{os.path.basename(out)}")
        ok, err = _go_build(os.path.basename(src), t, os.path.abspath(out),
                            strip=strip, cwd=work)
        results.append((t, out if ok else None, err))
    return results


# ── route 2: bundle (the Pyro VM, cross-compiled) ───────────

def _vm_sources():
    vm_dir = os.path.join(_root, 'Pyro', 'vm')
    srcs = sorted(glob.glob(os.path.join(vm_dir, '*.go')))
    if not srcs:
        raise BuildError(f"no Go VM sources in {vm_dir}")
    return vm_dir, srcs


def cross_vm(target, cache_dir=None, strip=True, verbose=False):
    """Build (or reuse) the Pyro VM for `target`. Returns its path.

    Cached and staleness-checked against every VM source, not just main.go:
    a six-target bundle build is six VM compiles, and doing them again on
    every invocation turns a one-second build into half a minute. Watching
    one file would silently ship a VM older than the debugger or the
    profiler beside it.
    """
    vm_dir, srcs = _vm_sources()
    cache_dir = cache_dir or os.path.join(_root, 'build', 'vm')
    os.makedirs(cache_dir, exist_ok=True)
    vm = os.path.join(cache_dir, f'pyrovm-{target.name}{target.exe}')
    newest = max(os.path.getmtime(s) for s in srcs)
    if os.path.isfile(vm) and os.path.getmtime(vm) >= newest:
        if verbose:
            print(f"  VM {target.name}: reusing {_short(vm)}")
        return vm
    if verbose:
        print(f"  VM {target.name}: go build ({len(srcs)} sources)")
    # The .go files by name, not the package: Pyro/vm also holds the C VM, and
    # `go build .` refuses with "C source files not allowed".
    cmd = [_go(), 'build', '-trimpath']
    if strip:
        cmd += ['-ldflags', '-s -w']
    cmd += ['-o', os.path.abspath(vm)] + [os.path.basename(s) for s in srcs]
    r = subprocess.run(cmd, cwd=vm_dir, env=_go_env(target),
                       capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise BuildError(f"could not build the Pyro VM for {target.name}:\n"
                         f"{(r.stderr or '').strip()}")
    return vm


def build_bundle(input_path, targets, out_dir, name,
                 safe=True, sandbox=False, use_cache=True, assets=None,
                 sign_key=None, strip=True, verbose=False):
    """One .pyro, one cross-built VM per target, bundled.

    The bytecode is identical for every target — it is bytecode — so it is
    compiled once and appended to each VM. Compiling it per target would be
    the same bytes six times over, and any difference between them would be
    a bug.
    """
    import bundle as _bundle

    work = os.path.join(out_dir, '.build')
    os.makedirs(work, exist_ok=True)
    # Signing happens in the pyro backend, at generation time, so the key goes
    # in here rather than being applied to the bytes afterwards.
    code = _emit(input_path, 'pyro', safe, sandbox, use_cache,
                 assets=assets, sign_key=sign_key)
    pyro_path = os.path.join(work, name + '.pyro')
    with open(pyro_path, 'wb') as f:
        f.write(code)
    if verbose:
        print(f"  generated {_short(pyro_path)}  "
              f"({len(code)} bytes of bytecode)")

    results = []
    for t in targets:
        out = os.path.join(out_dir, f'{name}-{t.name}{t.exe}')
        try:
            vm = cross_vm(t, strip=strip, verbose=verbose)
            _bundle.bundle(vm, pyro_path, out)
            results.append((t, out, ''))
        except (BuildError, OSError, SystemExit) as e:
            results.append((t, None, str(e)))
    return results


# ── orchestration ───────────────────────────────────────────

def build(input_path, targets=None, out_dir='dist', name=None, via='auto',
          safe=True, sandbox=False, use_cache=True, assets=None,
          sign_key=None, strip=True, sums=True, verbose=False):
    """Build `input_path` for every target.

    Returns ([(target, path|None, error), ...], route) — the route as well as
    the results, because 'auto' resolves to one of the two and the caller has
    to be able to SAY which one produced the binaries. "built via auto" would
    tell a reader nothing about what they are shipping.
    """
    if not os.path.isfile(input_path):
        raise BuildError(f"not found: {input_path}")
    targets = list(targets or [])
    if not targets:
        h = host_target()
        if h is None:
            raise BuildError(
                f"this machine ({sys.platform}/{platform.machine()}) is not "
                f"one of the platforms this builds for, so there is no "
                f"default target.\n\n{describe_targets()}")
        targets = [h]
    name = name or os.path.splitext(os.path.basename(input_path))[0]
    os.makedirs(out_dir, exist_ok=True)

    # Embedded assets and a signature are properties of the BYTECODE: the
    # container carries them and the VM reads them. The Go backend has no
    # equivalent, so asking for either decides the route rather than being
    # quietly dropped from the binaries — which is the failure mode where
    # `asset("logo.png")` is missing only on the machine you shipped to.
    wants_pyro = bool(assets) or bool(sign_key)
    if wants_pyro and via == 'native':
        which = ' and '.join(
            w for w in (('--assets' if assets else None),
                        ('--sign' if sign_key else None)) if w)
        raise BuildError(
            f"{which} put data inside the Pyro container, which only the VM "
            f"reads — the Go backend has nowhere to keep it. Build these with "
            f"--via bundle (the default route takes care of this).")
    if wants_pyro and via == 'auto':
        via = 'bundle'

    if via == 'native':
        results = build_native(input_path, targets, out_dir, name, safe=safe,
                               sandbox=sandbox, use_cache=use_cache,
                               strip=strip, verbose=verbose)
    elif via == 'bundle':
        results = build_bundle(input_path, targets, out_dir, name, safe=safe,
                               sandbox=sandbox, use_cache=use_cache,
                               assets=assets, sign_key=sign_key, strip=strip,
                               verbose=verbose)
    else:
        # auto: the Go backend first — a real native binary with no
        # interpreter in it, a quarter of the size. It is a backend, though,
        # not the whole language: a program using something it does not
        # cover is refused at code generation, and falling back to the VM
        # route is better than telling the user to work out which flag
        # would have worked.
        #
        # CodeGenGoError and nothing else. A program with a syntax, semantic
        # or module error is BROKEN, not unsupported: the bundle route would
        # refuse it too, for the same reason, and trying anyway would bury the
        # real diagnostic under a line blaming the Go backend for it.
        from codegen_go import CodeGenGoError
        try:
            results = build_native(input_path, targets, out_dir, name,
                                   safe=safe, sandbox=sandbox,
                                   use_cache=use_cache, strip=strip,
                                   verbose=verbose)
            via = 'native'
        except CodeGenGoError as e:
            print(f"  the Go backend does not cover this program:\n    {e}\n"
                  f"  falling back to --via bundle (the Pyro VM carrying the "
                  f"bytecode)", file=sys.stderr)
            results = build_bundle(input_path, targets, out_dir, name,
                                   safe=safe, sandbox=sandbox,
                                   use_cache=use_cache, assets=assets,
                                   sign_key=sign_key, strip=strip,
                                   verbose=verbose)
            via = 'bundle'
    if sums:
        write_sums(out_dir, [p for _t, p, _e in results if p])
    return results, via


def write_sums(out_dir, paths):
    """SHA256SUMS beside the binaries, in `sha256sum -c` format.

    Shipping a binary to someone else's machine is the one case where the
    bytes have to be checkable without trusting the channel they arrived
    through, and the format is the one their machine already has a tool for.
    """
    if not paths:
        return None
    lines = []
    for p in sorted(paths, key=os.path.basename):
        h = hashlib.sha256()
        with open(p, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {os.path.basename(p)}")
    dest = os.path.join(out_dir, 'SHA256SUMS')
    with open(dest, 'w', encoding='utf-8', newline='\n') as f:
        f.write("\n".join(lines) + "\n")
    return dest


def _short(path):
    r"""The shorter of the relative and absolute spellings.

    A dist directory outside the working tree turns relpath into
    ..\..\..\AppData\Local\..., which is longer than the path it is
    shortening and much harder to read.
    """
    try:
        rel = os.path.relpath(path)
    except ValueError:        # different drive on Windows
        return path
    return rel if len(rel) <= len(path) else path


def _fmt_size(n):
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1 << 20 else f"{n / 1024:.0f} KB"


def report(results, out_dir, via):
    """The summary table. Returns the number of failures."""
    ok = [(t, p) for t, p, _e in results if p]
    bad = [(t, e) for t, p, e in results if not p]
    if ok:
        print(f"\n  {'target':<15} {'size':>9}  binary")
        for t, p in ok:
            print(f"  {t.name:<15} {_fmt_size(os.path.getsize(p)):>9}  "
                  f"{os.path.basename(p)}")
        print(f"\n✓ {len(ok)} executable{'' if len(ok) == 1 else 's'} in "
              f"{_short(out_dir)}{os.sep}  (via {via})")
        print("  each one runs on its platform with no Cryo, Python, Go or "
              "C installed")
    for t, e in bad:
        print(f"\n✗ {t.name}: {e or 'build failed'}", file=sys.stderr)
    return len(bad)


# ── CLI ─────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='cryoc build',
        description='Compile a .cryo to native executables for Windows, '
                    'Linux and macOS — all of them, from this one machine.',
        epilog='Needs the Go toolchain here; nothing on the target machines.')
    ap.add_argument('input', nargs='?', help='the .cryo to build')
    ap.add_argument('--target', '-t', action='append', metavar='T', default=[],
                    help='windows | linux | macos | all | host | '
                         'linux-arm64 ... (repeatable, or comma-separated). '
                         'Default: host')
    ap.add_argument('--list-targets', action='store_true',
                    help='print the target names and exit')
    ap.add_argument('-o', '--out', metavar='DIR', default='dist',
                    help='directory for the binaries (default: dist)')
    ap.add_argument('--name', help='base name of the binaries '
                                   '(default: the source file name)')
    ap.add_argument('--via', choices=('auto', 'native', 'bundle'),
                    default='auto',
                    help="'native' compiles through the Go backend; 'bundle' "
                         "ships the Pyro VM carrying the bytecode; 'auto' "
                         "(default) is native, falling back to bundle for a "
                         "program the Go backend does not cover")
    ap.add_argument('--unsafe', action='store_true',
                    help='turn off safety instrumentation')
    ap.add_argument('--sandbox', action='store_true',
                    help='refuse network/machine natives by policy')
    ap.add_argument('--assets', metavar='DIR',
                    help='bundle route: embed every file under DIR, readable '
                         'at runtime with asset("name")')
    ap.add_argument('--sign', metavar='KEY',
                    help='bundle route: sign the bytecode (path, or env:NAME)')
    ap.add_argument('--debug', action='store_true',
                    help='keep symbols and DWARF in the binaries')
    ap.add_argument('--no-sums', action='store_true',
                    help='do not write SHA256SUMS')
    ap.add_argument('--no-cache', action='store_true',
                    help='do not read or write the incremental cache')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    if a.list_targets:
        print(describe_targets())
        return 0
    if not a.input:
        ap.error('an input .cryo is required (or --list-targets)')

    import compiler
    try:
        targets = resolve_targets(a.target)
        if a.verbose and targets:
            print(f"building {a.input} for "
                  f"{', '.join(t.name for t in targets)}")
        results, via = build(
            a.input, targets, out_dir=a.out, name=a.name, via=a.via,
            safe=not a.unsafe, sandbox=a.sandbox,
            use_cache=not a.no_cache,
            assets=compiler.collect_assets(a.assets) if a.assets else None,
            sign_key=compiler.load_sign_key(a.sign) if a.sign else None,
            strip=not a.debug, sums=not a.no_sums, verbose=a.verbose)
    except BuildError as e:
        print(f"\n[build] {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"\n[build] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # A program that does not compile is the common failure here, and its
        # own diagnostic — rendered with the source line and a caret — is far
        # better than anything a build wrapper could add on top.
        if not _print_frontend_error(e, a.input):
            raise
        return 1
    return 1 if report(results, a.out, via) else 0


def _print_frontend_error(e, path):
    """Render a front-end/codegen error the way `cryoc` does. True if it was one."""
    import compiler
    from lexer import LexerError
    from parser import ParseError
    pairs = [
        (LexerError, '[Lexical Error]'),
        (ParseError, '[Syntax Error]'),
    ]
    for cls, prefix in pairs:
        if isinstance(e, cls):
            errs = getattr(e, 'errors', None) or [str(e)]
            print("", file=sys.stderr)
            for m in errs:
                print(compiler._point_at(getattr(e, 'path', None) or path,
                                         m, prefix) + "\n", file=sys.stderr)
            return True
    for cls, prefix in (
            (compiler.SemanticError, '[Semantic Error]'),
            (compiler.ModuleError, '[Module Error]'),
            (compiler.ForeignError, '[Foreign Error]'),
            (compiler.CodeGenGoError, '[CodeGen Go Error]'),
            (compiler.CodeGenPyroError, '[CodeGen Pyro Error]'),
            (compiler.CodeGenError, '[CodeGen Error]')):
        if isinstance(e, cls):
            msg = str(e)
            print(f"\n{prefix} {msg}" if not msg.startswith(prefix)
                  else f"\n{msg}", file=sys.stderr)
            return True
    return False


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
