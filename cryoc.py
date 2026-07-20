#!/usr/bin/env python3
# ============================================================
#  Burnout — motor/CLI do sistema Cryo
#
#  Orquestra o front-end (CRYO) e os backends (PYRO).
#  Uso (a partir da raiz do projeto):
#    python burnout/cryoc.py cryo/examples/example_pyro.cryo --run
#    python burnout/cryoc.py cryo/examples/app.cryo --backend c -v
#
#  O backend padrão é Go. Veja README.md e pyro/PYRO.md.
# ============================================================
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))   # burnout/
_root = os.path.dirname(_here)                        # raiz do projeto
# CRYO (front-end) e PYRO (backends) entram no path; imports são flat.
for _p in ('cryo', 'pyro'):
    sys.path.insert(0, os.path.join(_root, _p))
sys.path.insert(0, _here)                             # burnout/ (compiler)

from compiler import main   # noqa: E402

if __name__ == '__main__':
    # subcomandos: `--lsp` inicia o Language Server; `fmt` formata arquivos
    if len(sys.argv) > 1 and sys.argv[1] == '--lsp':
        import lsp
        lsp.main()
    elif len(sys.argv) > 1 and sys.argv[1] == 'fmt':
        import format as _fmt
        sys.exit(_fmt.main(sys.argv[1:]))
    else:
        main()
