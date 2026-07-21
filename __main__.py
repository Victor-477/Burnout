"""Allows invoking the compiler as a module: ``python -m burnout app.cryo --run``.

Equivalent to ``python burnout/cryoc.py app.cryo --run`` — same CLI, same
argumentos. Veja ``python -m burnout --help``.
"""
from . import main

if __name__ == "__main__":
    main()
