import os
import re
import json

files = [
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\compiler.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\codegen_pyro.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\codegen_go.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\codegen_node.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\codegen_c.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\codegen_asm.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\lsp.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\disasm_pyro.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\__init__.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\__main__.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\cryoc.py",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\runtime\cryo_runtime.c",
    r"c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\runtime\cryo_runtime.h"
]

pt_words = [' de ', ' da ', ' do ', ' em ', ' para ', ' não ', ' é ', ' com ', ' um ', ' uma ', ' o ', ' a ', ' os ', ' as ', ' que ', ' se ', ' na ', ' no ', ' ou ', ' falhou', ' linha', ' não ', ' inválido', ' esperado', ' indefinido', ' desconhecid', ' suportado', ' vazio', ' arquiv', ' gerado']

extracted = set()

for filepath in files:
    if not os.path.exists(filepath): continue
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    is_py = filepath.endswith('.py') or filepath.endswith('.s')
    if is_py:
        pattern = re.compile(r'(\"\"\"(?:\\.|[^\\])*?\"\"\"|\'\'\'(?:\\.|[^\\])*?\'\'\')|(\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\')|(#.*)')
    else:
        pattern = re.compile(r'(/\*(?:.|\n)*?\*/)|(\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\')|(//.*)')
        
    for m in pattern.finditer(content):
        g1, g2, g3 = m.groups()
        if g1:
            lines = g1.split('\n')
            for l in lines:
                if len(l.strip()) > 3 and any(w in l.lower() for w in pt_words):
                    extracted.add(l.strip())
        elif g2:
            s_inner = g2[1:-1]
            if any(w in s_inner.lower() for w in pt_words) and len(s_inner.split()) > 1:
                extracted.add(s_inner)
        elif g3:
            c_inner = g3[1:].strip() if is_py else g3[2:].strip()
            if not c_inner.startswith('!') and not c_inner.startswith('go:') and 'coding:' not in c_inner:
                if len(c_inner) > 0:
                    extracted.add(c_inner)

with open(r'c:\Users\senai\Desktop\Projetos\Pyro_Cryo\Burnout\extracted.json', 'w', encoding='utf-8') as f:
    json.dump(list(extracted), f, ensure_ascii=False, indent=2)
