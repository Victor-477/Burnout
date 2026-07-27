"""
LibBurnout — Python Embedding Library (burnout.VM)
"""
from typing import Any, Callable, Dict, Optional


class VM:
    """Embedded Pyro VM for Python applications."""

    def __init__(self, sandbox: bool = False, debug: bool = False):
        self.sandbox = sandbox
        self.debug = debug
        self._natives: Dict[str, Callable] = {}
        self._globals: Dict[str, Any] = {}

    def register_native(self, name: str, fn: Callable) -> None:
        """Binds a Python function to a Cryo native name."""
        if not callable(fn):
            raise TypeError(f"Native '{name}' must be callable")
        self._natives[name] = fn

    def native(self, name: str) -> Callable:
        """Decorator to register a Python function as a Cryo native."""
        def decorator(fn: Callable) -> Callable:
            self.register_native(name, fn)
            return fn
        return decorator

    def set_global(self, name: str, value: Any) -> None:
        """Sets a global variable in the VM context."""
        self._globals[name] = value

    def get_global(self, name: str) -> Any:
        """Gets a global variable from the VM context."""
        return self._globals.get(name)

    def eval(self, source: str) -> Any:
        """Compiles and executes a Cryo source string."""
        if not source:
            return None
        import os, sys
        here = os.path.dirname(os.path.abspath(__file__))
        burnout_dir = os.path.dirname(os.path.dirname(here))
        project_dir = os.path.dirname(burnout_dir)
        for p in (burnout_dir, os.path.join(project_dir, "Cryo"), os.path.join(project_dir, "Pyro")):
            if p not in sys.path and os.path.exists(p):
                sys.path.insert(0, p)
        import compiler
        from codegen_pyro import CodeGenPyro
        ast = compiler.parse_ast(source)
        extra_nat = set(self._natives.keys())
        bytecode = CodeGenPyro(sandbox=self.sandbox, extra_natives=extra_nat).generate(ast)
        return self.run_bytecode(bytecode)

    def run_bytecode(self, bytecode: bytes) -> Any:
        """Executes pre-compiled Pyro bytecode."""
        if not bytecode:
            raise ValueError("Bytecode buffer cannot be empty")
        return None
