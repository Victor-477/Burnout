"""Permite invocar o compilador como módulo: ``python -m burnout app.cryo --run``.

Equivale a ``python burnout/cryoc.py app.cryo --run`` — mesma CLI, mesmos
argumentos. Veja ``python -m burnout --help``.
"""
from . import main

if __name__ == "__main__":
    main()
