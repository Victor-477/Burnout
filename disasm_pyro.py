# ============================================================
#  Burnout — Pyro bytecode disassembler (.pyro -> text)
#
#  Reads a .pyro file (the proper target language) and prints a
#  listing legível: cabeçalho, pool de constants, tabela de
#  functions and disassembled code (with jump targets and names of
#  function resolved). Useful for inspection and as a textual form
#  readable by AI than what the machine executes.
#
#  Usage:  python burnout/disasm_pyro.py program.pyro
# ============================================================
import struct
import sys

import codegen_pyro as bc

# opcode -> name  (from the generator's OP_* constants)
_OPNAMES = {v: k[3:] for k, v in vars(bc).items()
            if k.startswith('OP_') and isinstance(v, int)}

_TAGNAME = {bc.TAG_INT: 'int', bc.TAG_FLT: 'float',
            bc.TAG_STR: 'str', bc.TAG_BOOL: 'bool'}


def _xor_decode(code: bytes) -> bytes:
    """Inverse of the generator's XOR rolling."""
    out = bytearray(len(code))
    k = 0x5A
    for i, e in enumerate(code):
        b = e ^ k
        out[i] = b
        k = (k * 31 + 7 + b) & 0xFF
    return bytes(out)


def load(data: bytes) -> dict:
    if data[:4] != b'PYRO':
        raise ValueError("invalid .pyro file (magic)")
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
            raise ValueError(f"unknown constant tag: {tag}")

    funcs = []
    nfuncs = rd('<H')
    for _ in range(nfuncs):
        nameidx = rd('<H'); entry = rd('<I'); nparams = data[pos]; pos += 1; nlocals = rd('<H')
        funcs.append({'name': consts[nameidx][1], 'entry': entry,
                      'nparams': nparams, 'nlocals': nlocals})
    entryfn = rd('<H')
    codelen = rd('<I')
    code = data[pos:pos+codelen]
    pos += codelen
    if flags & 0x01:
        code = _xor_decode(code)
    dbg = {}
    if flags & 0x02:
        ndbg = rd('<I')
        for _ in range(ndbg):
            off = rd('<I'); line = rd('<I')
            dbg[off] = line
    return {'version': version, 'flags': flags, 'consts': consts,
            'funcs': funcs, 'entryfn': entryfn, 'code': code, 'dbg': dbg}


def _const_str(consts, idx):
    kind, val = consts[idx]
    if kind == 'str':
        return f'"{val}"'
    return str(val)


def disassemble(data: bytes) -> str:
    p = load(data)
    consts, funcs, code = p['consts'], p['funcs'], p['code']
    # maps entry-offset -> function name, for labels
    entry_at = {f['entry']: f['name'] for f in funcs}

    out = []
    _fl = [('encoded' if p['flags'] & 1 else 'plaintext')]
    if p['flags'] & 0x02:
        _fl.append('debug')
    if p['flags'] & 0x04:
        _fl.append('sandbox')
    out.append(f"; Pyro bytecode  (version {p['version']}, {', '.join(_fl)})")
    out.append(f"; entry = {funcs[p['entryfn']]['name']}  |  "
               f"{len(consts)} consts, {len(funcs)} funcs, {len(code)} bytes of code")
    out.append("")
    out.append("; ── constants ──")
    for i, (kind, val) in enumerate(consts):
        shown = f'"{val}"' if kind == 'str' else val
        out.append(f";   [{i}] {kind:<5} {shown}")
    out.append("")
    out.append("; ── functions ──")
    for i, f in enumerate(funcs):
        out.append(f";   #{i} {f['name']}  entry={f['entry']} "
                   f"params={f['nparams']} locals={f['nlocals']}")
    out.append("")

    dbg = p.get('dbg', {})
    i = 0
    while i < len(code):
        if i in entry_at:
            out.append(f"\n{entry_at[i]}:")
        if i in dbg:
            out.append(f"  ; line {dbg[i]}")
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
            rel = struct.unpack('<i', operand)[0]
            text += f" {rel:+d}   ; -> {i + size + rel}"
        elif op == bc.OP_CALL:
            fi = struct.unpack('<H', operand[:2])[0]
            argc = operand[2]
            fname = funcs[fi]['name'] if fi < len(funcs) else '?'
            text += f" {fi} {argc}  ; {fname}(argc={argc})"
        elif op == bc.OP_NATIVE:
            nid, argc = operand[0], operand[1]
            nname = next((k for k, v in bc.NATIVES.items() if v[0] == nid), '?')
            text += f" {nid} {argc}  ; {nname}(argc={argc})"
        elif op == bc.OP_TRYPUSH:
            rel = struct.unpack('<i', operand[:4])[0]
            slot = struct.unpack('<H', operand[4:6])[0]
            slot_s = 'sem var' if slot == 0xFFFF else f'slot {slot}'
            text += f" {rel:+d} {slot}  ; catch -> {i + size + rel} ({slot_s})"
        out.append(text)
        i += size
    return '\n'.join(out) + '\n'


def main():
    if len(sys.argv) < 2:
        print("usage: python burnout/disasm_pyro.py program.pyro", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], 'rb') as f:
        print(disassemble(f.read()))


if __name__ == '__main__':
    main()
