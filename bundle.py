#!/usr/bin/env python3
# ============================================================
#  pyro bundle — one executable that needs no toolchain
#
#  The VM binary already depended on nothing but the operating system's own
#  libraries: no Go, no C, no Python. What it lacked was a way to CARRY its
#  program, so running anything meant shipping the VM, a .pyro beside it,
#  and in practice the Python CLI that produced the .pyro.
#
#  This copies a VM binary and appends the bytecode plus a 16-byte trailer.
#  The VM looks for that trailer in its own image at startup (see
#  embeddedProgram in Pyro/vm/main.go) and runs the payload instead of
#  asking for a file argument.
#
#  THIS TOOL IS BUILD-TIME. It is written in Python because the rest of the
#  toolchain is; the executable it produces has no Python dependency, which
#  is the whole point. Nothing here runs on the target machine.
#
#  Format — kept here and in the VM, and nowhere else:
#
#      [ … VM executable … ][ bytecode (n bytes) ][ uint64be n ][ "PYROBND1" ]
#
#  The trailer sits at the very END so that detection is one read at a known
#  offset, and so the original PE/ELF image is untouched: both loaders ignore
#  trailing bytes after the last section, which is what lets this work with
#  no linker involved.
# ============================================================
import os
import struct
import sys

MAGIC = b'PYROBND1'
TRAILER = 8 + len(MAGIC)


def bundle(vm_path: str, pyro_path: str, out_path: str) -> None:
    with open(vm_path, 'rb') as f:
        vm = f.read()
    with open(pyro_path, 'rb') as f:
        code = f.read()
    if not code:
        raise SystemExit(f"[bundle] {pyro_path} is empty")

    # Refuse to bundle a bundle. Appending to an already-bundled executable
    # would leave the first payload stranded in the middle of the file and
    # the second one readable, so it would APPEAR to work while shipping two
    # copies of the program — and the file would grow every time.
    if len(vm) >= TRAILER and vm[-len(MAGIC):] == MAGIC:
        raise SystemExit(
            f"[bundle] {vm_path} already carries a bundled program; "
            f"bundle from the plain VM binary instead")

    with open(out_path, 'wb') as f:
        f.write(vm)
        f.write(code)
        f.write(struct.pack('>Q', len(code)))
        f.write(MAGIC)

    # The executable bit matters on POSIX and is harmless on Windows; without
    # it the output of a successful bundle cannot be run, which would be a
    # confusing way to finish.
    try:
        os.chmod(out_path, os.stat(vm_path).st_mode)
    except OSError:
        pass


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: pyro bundle <program.pyro> [-o out] [--vm path]\n"
              "  Produces one executable that runs with no Go, C or Python\n"
              "  installed on the target machine.", file=sys.stderr)
        return 2

    pyro_path = argv[0] if not argv[0].startswith('-') else None
    out_path = None
    vm_path = None
    rest = argv[1:] if pyro_path else argv
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ('-o', '--output') and i + 1 < len(rest):
            out_path = rest[i + 1]
            i += 2
        elif a == '--vm' and i + 1 < len(rest):
            vm_path = rest[i + 1]
            i += 2
        elif pyro_path is None and not a.startswith('-'):
            pyro_path = a
            i += 1
        else:
            print(f"[bundle] unknown option {a}", file=sys.stderr)
            return 2

    if not pyro_path:
        print("[bundle] no .pyro given", file=sys.stderr)
        return 2
    if not os.path.isfile(pyro_path):
        print(f"[bundle] not found: {pyro_path}", file=sys.stderr)
        return 1

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exe = '.exe' if sys.platform == 'win32' else ''
    if vm_path is None:
        # The Go VM by default: it is the reference engine, and it links its
        # runtime statically, so the bundle it produces needs nothing beyond
        # the operating system. The C VM reads the same trailer once it
        # implements it (ISSUES/34) and can be selected with --vm.
        vm_path = os.path.join(root, 'Pyro', 'vm', 'pyrovm_go' + exe)
    if not os.path.isfile(vm_path):
        print(f"[bundle] VM binary not found: {vm_path}\n"
              f"          build it first: cd Pyro/vm && go build -o "
              f"pyrovm_go{exe} .", file=sys.stderr)
        return 1

    if out_path is None:
        base = os.path.splitext(os.path.basename(pyro_path))[0]
        out_path = base + exe

    bundle(vm_path, pyro_path, out_path)
    size = os.path.getsize(out_path)
    print(f"✓ bundled: {out_path}  ({size:,} bytes, "
          f"{os.path.getsize(pyro_path):,} of it bytecode)")
    print("  runs with no Go, C or Python installed")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
