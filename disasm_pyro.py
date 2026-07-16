# ============================================================
#  Burnout — Desassemblador de bytecode Pyro (.pyro -> texto)
#
#  Lê um arquivo .pyro (a linguagem-alvo própria) e imprime um
#  listing legível: cabeçalho, pool de constantes, tabela de
#  funções e o código desmontado (com alvos de salto e nomes de
#  função resolvidos). Útil para inspeção e como forma textual
#  legível por IA do que a máquina executa.
#
#  Uso:  python burnout/disasm_pyro.py programa.pyro
# ============================================================
import struct
import sys

import codegen_pyro as bc

# opcode -> nome  (a partir das constantes OP_* do gerador)
_OPNAMES = {v: k[3:] for k, v in vars(bc).items()
            if k.startswith('OP_') and isinstance(v, int)}

_TAGNAME = {bc.TAG_INT: 'int', bc.TAG_FLT: 'float',
            bc.TAG_STR: 'str', bc.TAG_BOOL: 'bool'}


def _xor_decode(code: bytes) -> bytes:
    """Inverso do XOR rolling do gerador."""
    out = bytearray(len(code))
    k = 0x5A
    for i, e in enumerate(code):
        b = e ^ k
        out[i] = b
        k = (k * 31 + 7 + b) & 0xFF
    return bytes(out)


def load(data: bytes) -> dict:
    if data[:4] != b'PYRO':
        raise ValueError("arquivo .pyro inválido (magic)")
    version = data[4]
    flags = data[5]
    pos = 6

    def rd(fmt):
        nonlocal pos
        n = struct.calcsize(fmt)
        v = struct.unpack_from(fmt, data, pos)[0]
        pos += n
        return v

    consts = []
    nconsts = rd('<H')
    for _ in range(nconsts):
        tag = data[pos]; pos += 1
        if tag == bc.TAG_INT:   consts.append(('int', rd('<q')))
        elif tag == bc.TAG_FLT: consts.append(('float', rd('<d')))
        elif tag == bc.TAG_BOOL: consts.append(('bool', bool(data[pos]))); pos += 1
        elif tag == bc.TAG_STR:
            ln = rd('<H'); consts.append(('str', data[pos:pos+ln].decode('utf-8'))); pos += ln
        else:
            raise ValueError(f"tag de constante desconhecida: {tag}")

    funcs = []
    nfuncs = rd('<H')
    for _ in range(nfuncs):
        nameidx = rd('<H'); entry = rd('<I'); nparams = data[pos]; pos += 1; nlocals = rd('<H')
        funcs.append({'name': consts[nameidx][1], 'entry': entry,
                      'nparams': nparams, 'nlocals': nlocals})
    entryfn = rd('<H')
    codelen = rd('<I')
    code = data[pos:pos+codelen]
    if flags & 0x01:
        code = _xor_decode(code)
    return {'version': version, 'flags': flags, 'consts': consts,
            'funcs': funcs, 'entryfn': entryfn, 'code': code}


def _const_str(consts, idx):
    kind, val = consts[idx]
    if kind == 'str':
        return f'"{val}"'
    return str(val)


def disassemble(data: bytes) -> str:
    p = load(data)
    consts, funcs, code = p['consts'], p['funcs'], p['code']
    # mapeia entry-offset -> nome de função, para rótulos
    entry_at = {f['entry']: f['name'] for f in funcs}

    out = []
    out.append(f"; Pyro bytecode  (versão {p['version']}, "
               f"{'codificado' if p['flags'] & 1 else 'texto-claro'})")
    out.append(f"; entry = {funcs[p['entryfn']]['name']}  |  "
               f"{len(consts)} consts, {len(funcs)} funcs, {len(code)} bytes de código")
    out.append("")
    out.append("; ── constantes ──")
    for i, (kind, val) in enumerate(consts):
        shown = f'"{val}"' if kind == 'str' else val
        out.append(f";   [{i}] {kind:<5} {shown}")
    out.append("")
    out.append("; ── funções ──")
    for i, f in enumerate(funcs):
        out.append(f";   #{i} {f['name']}  entry={f['entry']} "
                   f"params={f['nparams']} locals={f['nlocals']}")
    out.append("")

    i = 0
    while i < len(code):
        if i in entry_at:
            out.append(f"\n{entry_at[i]}:")
        op = code[i]
        name = _OPNAMES.get(op, f"?0x{op:02X}")
        size = 1 + bc._OPERAND.get(op, 0)
        operand = code[i+1:i+size]
        text = f"  {i:>5}: {name}"
        if op in (bc.OP_CONST,):
            idx = struct.unpack('<H', operand)[0]
            text += f" {idx}    ; {_const_str(consts, idx)}"
        elif op in (bc.OP_LOAD, bc.OP_STORE):
            text += f" {struct.unpack('<H', operand)[0]}"
        elif op in (bc.OP_NEWARR, bc.OP_NEWMAP):
            text += f" {struct.unpack('<H', operand)[0]}"
        elif op in (bc.OP_JMP, bc.OP_JMPF, bc.OP_JMPT):
            rel = struct.unpack('<h', operand)[0]
            text += f" {rel:+d}   ; -> {i + size + rel}"
        elif op == bc.OP_CALL:
            fi = struct.unpack('<H', operand[:2])[0]
            argc = operand[2]
            fname = funcs[fi]['name'] if fi < len(funcs) else '?'
            text += f" {fi} {argc}  ; {fname}(argc={argc})"
        out.append(text)
        i += size
    return '\n'.join(out) + '\n'


def main():
    if len(sys.argv) < 2:
        print("uso: python burnout/disasm_pyro.py programa.pyro", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], 'rb') as f:
        print(disassemble(f.read()))


if __name__ == '__main__':
    main()
