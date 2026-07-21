#!/usr/bin/env python3
# ============================================================
#  Burnout — Validacao das Novas Aplicacoes in the C VM
# ============================================================
import os
import sys
import subprocess

_here = os.path.dirname(os.path.abspath(__file__))   # Burnout/tests/
_root = os.path.dirname(os.path.dirname(_here))       # raiz do projeto

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
C_VM = os.path.join(_root, "Pyro", "vm", "pyrovm.exe")

def run_cmd(args, stdin=""):
    try:
        res = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=10)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -999, "", "Timeout"

def main():
    apps = [
        {
            "name": "Calculadora",
            "file": "example_calc.cryo",
            "stdin": "+\n12.5\n7.5\nq\n"
        },
        {
            "name": "Fake Windows Update",
            "file": "example_winupdate.cryo",
            "stdin": ""
        },
        {
            "name": "Grafico em Tempo Real",
            "file": "example_chart.cryo",
            "stdin": ""
        }
    ]
    
    for app in apps:
        filepath = os.path.join(_root, "Cryo", "examples", app["file"])
        out_pyro = filepath.replace(".cryo", ".pyro")
        
        print(f"\n========================================\nCompilando {app['name']}...")
        comp_args = [sys.executable, CRYOC, filepath, "--backend", "pyro", "-o", out_pyro, "--no-banner"]
        c_code, c_out, c_err = run_cmd(comp_args)
        
        if c_code != 0:
            print(f"Error ao compilar {app['name']}:")
            print(c_err)
            continue
        print("Compilado with sucesso!")
        
        print(f"Executando {app['name']} in the C VM...")
        run_args = [C_VM, out_pyro]
        r_code, r_out, r_err = run_cmd(run_args, stdin=app["stdin"])
        
        if r_code != 0:
            print(f"Error ao executar {app['name']}:")
            print(r_err)
        else:
            print("Resultado da Execucao:")
            print(r_out)
            
        # Clean up bytecode file
        if os.path.exists(out_pyro):
            os.remove(out_pyro)

if __name__ == "__main__":
    main()
