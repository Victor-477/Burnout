#!/usr/bin/env python3
# ============================================================
#  Burnout — engine/CLI of the Cryo system
#
#  Orchestrates the front-end (CRYO) and the backends (PYRO).
#  Uso (a partir da project root):
#    python burnout/cryoc.py cryo/examples/example_pyro.cryo --run
#    python burnout/cryoc.py cryo/examples/app.cryo --backend c -v
#
#  The default backend is Go. See README.md and pyro/PYRO.md.
# ============================================================
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))   # burnout/
_root = os.path.dirname(_here)                        # project root
# CRYO (front-end) and PYRO (backends) enter the path; imports are flat.
for _p in ('cryo', 'pyro'):
    sys.path.insert(0, os.path.join(_root, _p))
sys.path.insert(0, _here)                             # burnout/ (compiler)

from compiler import main   # noqa: E402

if __name__ == '__main__':
    # subcommands: `--lsp` starts the Language Server; `fmt` formats files
    if len(sys.argv) > 1 and sys.argv[1] == '--lsp':
        import lsp
        lsp.main()
    elif len(sys.argv) > 1 and sys.argv[1] == 'fmt':
        import format as _fmt
        sys.exit(_fmt.main(sys.argv[1:]))
    else:
        main()
