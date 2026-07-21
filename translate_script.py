import os
import re
import urllib.request
import urllib.parse
import json
import time

def translate_text(text):
    if not text.strip(): return text
    
    # We use Google Translate undocumented API
    url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=pt&tl=en&dt=t&q=" + urllib.parse.quote(text)
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode('utf-8'))
                translated = "".join([d[0] for d in data[0] if d[0]])
                return translated
        except Exception as e:
            time.sleep(1)
    return text

def process_file(filepath):
    print("Processing", filepath)
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    replacements = {
        "[Erro de Módulo]": "[Module Error]",
        "[Erro Semântico]": "[Semantic Error]",
        "[Erro de Sintaxe]": "[Syntax Error]",
        "[Cryo Seguranca]": "[Cryo Security]",
        "[Cryo Segurança]": "[Cryo Security]",
        "[Cryo Assert]": "[Cryo Assert]",
        "[Erro de Assert]": "[Cryo Assert]",
        "[Erro Léxico]": "[Lexical Error]",
        "[Erro Estrangeiro]": "[Foreign Error]",
        "[Erro CodeGen ASM]": "[CodeGen ASM Error]",
        "[Erro CodeGen Go]": "[CodeGen Go Error]",
        "[Erro CodeGen Pyro]": "[CodeGen Pyro Error]",
        "[Erro CodeGen Node]": "[CodeGen Node Error]",
        "[Erro CodeGen]": "[CodeGen Error]",
        "[Erro Interno]": "[Internal Error]",
        "[Erro]": "[Error]",
        "Erro ao compilar a VM Pyro": "Error compiling Pyro VM",
        "não encontrado — .pyro gerado, mas a VM não foi compilada/executada": "not found — .pyro generated, but VM was not compiled/executed",
    }
    for k, v in replacements.items():
        content = content.replace(k, v)

    is_py = filepath.endswith('.py') or filepath.endswith('.s')
    
    if is_py:
        pattern = re.compile(r'(\"\"\"(?:\\.|[^\\])*?\"\"\"|\'\'\'(?:\\.|[^\\])*?\'\'\')|(\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\')|(#.*)')
    else:
        pattern = re.compile(r'(/\*(?:.|\n)*?\*/)|(\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\')|(//.*)')
        
    pt_words = [' de ', ' da ', ' do ', ' em ', ' para ', ' não ', ' é ', ' com ', ' um ', ' uma ', ' o ', ' a ', ' os ', ' as ', ' que ', ' se ', ' na ', ' no ', ' ou ', ' falhou', ' linha', ' não ', ' inválido', ' esperado', ' indefinido', ' desconhecid', ' suportado', ' vazio', ' arquiv', ' gerado']
    
    def repl(m):
        if is_py:
            g1, g2, g3 = m.groups()
            if g1:
                # docstring
                lines = g1.split('\n')
                res = []
                for l in lines:
                    if len(l.strip()) > 3 and any(w in l.lower() for w in pt_words):
                        res.append(translate_text(l))
                    else:
                        res.append(l)
                return '\n'.join(res)
            elif g2:
                # string literal
                s_inner = g2[1:-1]
                if any(w in s_inner.lower() for w in pt_words) and len(s_inner.split()) > 1:
                    return g2[0] + translate_text(s_inner) + g2[-1]
                return g2
            elif g3:
                # comment
                c_inner = g3[1:].strip()
                if not c_inner.startswith('!') and 'coding:' not in c_inner:
                    return '# ' + translate_text(c_inner)
                return g3
        else:
            g1, g2, g3 = m.groups()
            if g1:
                # multiline comment /* ... */
                c_inner = g1[2:-2]
                return '/*' + translate_text(c_inner) + '*/'
            elif g2:
                # string literal
                s_inner = g2[1:-1]
                if any(w in s_inner.lower() for w in pt_words) and len(s_inner.split()) > 1:
                    return g2[0] + translate_text(s_inner) + g2[-1]
                return g2
            elif g3:
                # single line comment
                c_inner = g3[2:].strip()
                if not c_inner.startswith('go:'):
                    return '// ' + translate_text(c_inner)
                return g3
        return m.group(0)

    new_content = pattern.sub(repl, content)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)

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

for f in files:
    if os.path.exists(f):
        process_file(f)
    else:
        print("Not found:", f)
