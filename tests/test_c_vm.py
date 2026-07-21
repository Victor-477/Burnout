#!/usr/bin/env python3
# ============================================================
#  Burnout — Teste de Paridade Go VM vs C VM (Pyro)
# ============================================================
import os
import sys
import subprocess
import tempfile

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

_here = os.path.dirname(os.path.abspath(__file__))   # Burnout/tests/
_root = os.path.dirname(os.path.dirname(_here))       # raiz do projeto

CRYO_EXAMPLES = os.path.join(_root, "Cryo", "examples")
CRYOC = os.path.join(_root, "Burnout", "cryoc.py")
GO_VM = os.path.join(_root, "Pyro", "vm", "pyrovm_go.exe")

def compile_c_vm():
    print("Compilando Go VM...")
    go_build = ["go", "build", "-o", GO_VM, os.path.join(_root, "Pyro", "vm", "main.go")]
    res_go = subprocess.run(go_build, capture_output=True, text=True)
    if res_go.returncode != 0:
        print("Erro de compilacao da Go VM:")
        print(res_go.stderr)
        sys.exit(1)
        
    print("Compilando C VM...")
    cmd = 'call "C:\\Program Files\\Microsoft Visual Studio\\2022\\Community\\VC\\Auxiliary\\Build\\vcvars64.bat" && cl /O2 /Fe:Pyro\\vm\\pyrovm.exe Pyro\\vm\\main.c'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        print("Erro de compilacao da C VM:")
        print(res.stdout)
        print(res.stderr)
        sys.exit(1)
    print("VMs compiladas com sucesso!")

def run_command(args, stdin=""):
    try:
        res = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=5)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -999, "", "Timeout"

def test_parity():
    compile_c_vm()
    
    examples = sorted([f for f in os.listdir(CRYO_EXAMPLES) if f.startswith("example_") and f.endswith(".cryo")])
    
    passed = 0
    failed = 0
    skipped = 0
    
    # Adicionalmente, podemos desativar temporariamente a rede se nao quisermos testar HTTP real,
    # mas ambos Go VM e C VM devem fazer a requisicao identica.
    
    for filename in examples:
        filepath = os.path.join(CRYO_EXAMPLES, filename)
        
        # Testamos se o arquivo e compilavel com pyro backend
        with tempfile.NamedTemporaryFile(suffix=".pyro", delete=False) as tmp:
            tmp_pyro = tmp.name
        
        # Compila usando cryoc.py
        comp_args = [sys.executable, CRYOC, filepath, "--backend", "pyro", "-o", tmp_pyro, "--no-banner"]
        c_code, c_out, c_err = run_command(comp_args)
        
        if c_code != 0:
            # Se nao compilar com backend pyro, apenas pulamos (ex: asm, node, etc.)
            skipped += 1
            os.remove(tmp_pyro)
            continue
            
        print(f"Testando {filename}...")
        
        # Executa na Go VM
        go_args = [GO_VM, tmp_pyro]
        go_code, go_out, go_err = run_command(go_args)
        
        # Executa na C VM
        C_VM = os.path.join(_root, "Pyro", "vm", "pyrovm.exe")
        c_args = [C_VM, tmp_pyro]
        cvm_code, cvm_out, cvm_err = run_command(c_args)
        
        # Limpar arquivo temporario
        try:
            os.remove(tmp_pyro)
        except OSError:
            pass
            
        # Comparar saídas
        # O output do 'go run' pode incluir algumas warnings ou mensagens de go compilando, entao
        # devemos filtrar ou garantir que o stderr e limpo.
        # No caso do Go VM, se for run direto no main.go, o output e limpo.
        
        # Normaliza quebras de linha
        go_out_norm = go_out.replace("\r\n", "\n")
        cvm_out_norm = cvm_out.replace("\r\n", "\n")
        
        go_err_norm = go_err.replace("\r\n", "\n").strip()
        cvm_err_norm = cvm_err.replace("\r\n", "\n").strip()
        
        # Para fins de comparacao do stderr, Go compiler pode colocar avisos ou mensagens na stderr se go run recompilar.
        # Mas o stderr do programa executando em si deve ser comparado.
        
        # Vamos verificar se as saidas padrao sao identicas
        if go_code != cvm_code or go_out_norm != cvm_out_norm:
            print(f"[FAIL] Falha de paridade em {filename}")
            print(f"Exit Codes: Go={go_code}, C={cvm_code}")
            if go_out_norm != cvm_out_norm:
                print("--- Output Go ---")
                print(repr(go_out_norm))
                print("--- Output C ---")
                print(repr(cvm_out_norm))
            if go_err_norm != cvm_err_norm:
                print("--- Stderr Go ---")
                print(repr(go_err_norm))
                print("--- Stderr C ---")
                print(repr(cvm_err_norm))
            failed += 1
        else:
            print(f"[OK] {filename} ok")
            passed += 1
            
    print(f"\nResumo: {passed} passaram, {failed} falharam, {skipped} pulados.")
    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    test_parity()
