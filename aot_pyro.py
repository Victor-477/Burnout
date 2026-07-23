# ============================================================
#  Burnout — Pyro AOT: .pyro bytecode -> C (Phase 9.5)
#
#  Translates a .pyro program into a standalone C file that runs WITHOUT the
#  VM and without the .pyro at runtime (the "zero-dependency" native route).
#
#  Each Pyro function becomes a C function; the stack machine is lowered to
#  straight-line C over a small per-call value stack, with jump targets as
#  `goto` labels. The value model and the 28 native builtins are reused from
#  the Pyro C runtime (pyro/vm/pyro_runtime.{c,h}) — identical semantics.
#
#  Build:  gcc -O2 out.c pyro/vm/pyro_runtime.c -lm -o out
#  Usage:  python burnout/aot_pyro.py program.pyro > out.c
#
#  try/catch/throw are lowered with setjmp/longjmp over a global handler
#  stack (TRYPUSH = setjmp; THROW / failed assert = longjmp to the nearest
#  handler, else fatal), so exceptions unwind across C function frames
#  exactly as the VM unwinds its call stack.
# ============================================================
import os
import struct
import sys

# locate sibling modules (codegen_pyro/disasm_pyro here; ast_nodes in ../Cryo)
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
for _p in (_here, os.path.join(_root, "Cryo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import codegen_pyro as bc      # OP_* constants, NATIVES, operand sizes
import disasm_pyro             # reuse the .pyro loader

# opcode -> mnemonic (for readable errors / comments)
_OPNAME = {v: k[3:] for k, v in vars(bc).items()
           if k.startswith('OP_') and isinstance(v, int)}

# static stack-depth delta per opcode (the generator emits stack-balanced code,
# so a linear walk yields a safe per-function upper bound)
def _delta(op, operand):
    if op in (bc.OP_CONST, bc.OP_TRUE, bc.OP_FALSE, bc.OP_NULL, bc.OP_LOAD):
        return +1
    if op in (bc.OP_POP, bc.OP_STORE, bc.OP_JMPF, bc.OP_JMPT, bc.OP_PRINT,
              bc.OP_HAS, bc.OP_INDEX, bc.OP_COALESCE, bc.OP_THROW):
        return -1
    if op in (bc.OP_ADD, bc.OP_SUB, bc.OP_MUL, bc.OP_DIV, bc.OP_MOD,
              bc.OP_BAND, bc.OP_BOR, bc.OP_BXOR, bc.OP_SHL, bc.OP_SHR,
              bc.OP_EQ, bc.OP_NE, bc.OP_LT, bc.OP_GT, bc.OP_LE, bc.OP_GE):
        return -1
    if op in (bc.OP_NEG, bc.OP_BNOT, bc.OP_NOT, bc.OP_LEN, bc.OP_KEYS,
              bc.OP_APPEND, bc.OP_UNWRAP, bc.OP_JMP, bc.OP_HALT,
              bc.OP_TRYPOP, bc.OP_PRINTLN):
        return 0
    if op == bc.OP_ASSERT:
        return -2
    if op == bc.OP_SETIDX:
        return -3
    if op == bc.OP_RET:
        return -1
    if op == bc.OP_NEWARR:
        return 1 - struct.unpack('<H', operand)[0]
    if op == bc.OP_NEWMAP:
        return 1 - 2 * struct.unpack('<H', operand)[0]
    if op == bc.OP_CALL:
        return 1 - operand[2]
    if op == bc.OP_NATIVE:
        return 1 - operand[1]
    return 0


def _c_bytes(b: bytes) -> str:
    """A C array-initializer of the raw bytes (avoids escaping surprises)."""
    return "{" + ",".join(str(x) for x in b) + "}"


def _func_range(funcs, i, codelen):
    """[entry, end) byte range of function i in the code section."""
    entry = funcs[i]['entry']
    ends = [f['entry'] for f in funcs if f['entry'] > entry]
    return entry, (min(ends) if ends else codelen)


def _jump_targets(code, entry, end):
    """Offsets in [entry, end) that are jump targets (need a C label)."""
    targets = set()
    i = entry
    while i < end:
        op = code[i]
        size = 1 + bc._OPERAND.get(op, 0)
        if op in (bc.OP_JMP, bc.OP_JMPF, bc.OP_JMPT):
            rel = struct.unpack('<i', code[i+1:i+5])[0]
            targets.add(i + size + rel)
        elif op == bc.OP_TRYPUSH:            # i32 catch-rel + u16 slot
            rel = struct.unpack('<i', code[i+1:i+5])[0]
            targets.add(i + size + rel)
        i += size
    return targets


def _emit_func(out, code, consts, funcs, fi, codelen):
    f = funcs[fi]
    entry, end = _func_range(funcs, fi, codelen)
    targets = _jump_targets(code, entry, end)

    # per-function max stack depth
    depth = 0; maxd = 0; i = entry
    while i < end:
        op = code[i]; size = 1 + bc._OPERAND.get(op, 0)
        depth += _delta(op, code[i+1:i+size])
        if depth > maxd: maxd = depth
        i += size
    stacksz = maxd + 4

    nloc = f['nlocals']
    out.append(f"static Value fn_{fi}(Value* args, int argc) {{  // {f['name']}")
    out.append(f"    Value L[{max(nloc,1)}]; Value S[{max(stacksz,1)}]; int sp = 0;")
    out.append(f"    for (int i = 0; i < {nloc}; i++) L[i] = (i < argc) ? args[i] : val_null();")
    out.append("    (void)argc; (void)S; (void)sp;")

    i = entry
    while i < end:
        op = code[i]
        size = 1 + bc._OPERAND.get(op, 0)
        operand = code[i+1:i+size]
        if i in targets:
            out.append(f"  L{i}: ;")
        c = _emit_op(op, operand, i, size, consts, funcs)
        if c is None:
            raise ValueError(f"AOT: opcode {_OPNAME.get(op,hex(op))} not supported "
                             f"(try/catch is not yet lowered by the AOT backend)")
        out.append("    " + c)
        i += size
    out.append("    return val_null();")
    out.append("}")
    out.append("")


def _emit_op(op, operand, off, size, consts, funcs):
    tgt = None
    if op in (bc.OP_JMP, bc.OP_JMPF, bc.OP_JMPT):
        rel = struct.unpack('<i', operand[:4])[0]
        tgt = off + size + rel
    if op == bc.OP_CONST:
        idx = struct.unpack('<H', operand)[0]
        return f"{{ retain_value(K[{idx}]); S[sp++] = K[{idx}]; }}"
    if op == bc.OP_TRUE:   return "S[sp++] = val_bool(true);"
    if op == bc.OP_FALSE:  return "S[sp++] = val_bool(false);"
    if op == bc.OP_NULL:   return "S[sp++] = val_null();"
    if op == bc.OP_POP:    return "release_value(S[--sp]);"
    if op == bc.OP_LOAD:
        s = struct.unpack('<H', operand)[0]
        return f"{{ retain_value(L[{s}]); S[sp++] = L[{s}]; }}"
    if op == bc.OP_STORE:
        s = struct.unpack('<H', operand)[0]
        return f"{{ release_value(L[{s}]); L[{s}] = S[--sp]; }}"
    if op in (bc.OP_ADD, bc.OP_SUB, bc.OP_MUL, bc.OP_DIV, bc.OP_MOD,
              bc.OP_BAND, bc.OP_BOR, bc.OP_BXOR, bc.OP_SHL, bc.OP_SHR,
              bc.OP_EQ, bc.OP_NE, bc.OP_LT, bc.OP_GT, bc.OP_LE, bc.OP_GE):
        return (f"{{ Value b = S[--sp]; Value a = S[--sp]; "
                f"S[sp++] = bin_op({op}, a, b); release_value(a); release_value(b); }}")
    if op == bc.OP_NEG:
        return ("{ Value a = S[sp-1]; S[sp-1] = (a.kind == VAL_FLOAT) ? "
                "val_float(-a.as.f) : val_int(-a.as.i); }")
    if op == bc.OP_BNOT:
        return "{ Value a = S[sp-1]; S[sp-1] = val_int(~a.as.i); }"
    if op == bc.OP_NOT:
        return ("{ Value a = S[sp-1]; bool t = value_truthy(a); "
                "release_value(a); S[sp-1] = val_bool(!t); }")
    if op == bc.OP_JMP:    return f"goto L{tgt};"
    if op == bc.OP_JMPF:
        return f"{{ Value a = S[--sp]; bool t = value_truthy(a); release_value(a); if (!t) goto L{tgt}; }}"
    if op == bc.OP_JMPT:
        return f"{{ Value a = S[--sp]; bool t = value_truthy(a); release_value(a); if (t) goto L{tgt}; }}"
    if op == bc.OP_CALL:
        fi = struct.unpack('<H', operand[:2])[0]; argc = operand[2]
        return f"{{ Value r = fn_{fi}(&S[sp-{argc}], {argc}); sp -= {argc}; S[sp++] = r; }}"
    if op == bc.OP_RET:
        return "{ Value r = S[--sp]; for (int i = 0; i < (int)(sizeof(L)/sizeof(L[0])); i++) release_value(L[i]); return r; }"
    if op == bc.OP_PRINT:
        return "{ Value v = S[--sp]; char* s = value_to_string(v); printf(\"%s\\n\", s); free(s); release_value(v); }"
    if op == bc.OP_PRINTLN: return "printf(\"\\n\");"
    if op == bc.OP_ASSERT:
        return ("{ Value cond = S[--sp]; Value msg = S[--sp]; if (!value_truthy(cond)) "
                "{ char* m = value_to_string(msg); char e[1100]; snprintf(e, sizeof(e), "
                "\"[Cryo Assert] %s\", m); free(m); Value ev = val_str(e, (int64_t)strlen(e)); "
                "release_value(cond); release_value(msg); aot_raise(ev); } "
                "release_value(cond); release_value(msg); }")
    if op == bc.OP_NEWARR:
        n = struct.unpack('<H', operand)[0]
        return (f"{{ RcArray* a = rc_array_new(); int base = sp - {n}; "
                f"for (int i = 0; i < {n}; i++) {{ rc_array_push(a, S[base+i]); release_value(S[base+i]); }} "
                f"sp = base; S[sp++] = val_array(a); }}")
    if op == bc.OP_NEWMAP:
        n = struct.unpack('<H', operand)[0]
        return (f"{{ RcMap* m = rc_map_new(); int base = sp - {2*n}; "
                f"for (int i = 0; i < {n}; i++) {{ Value k = S[base+2*i]; Value v = S[base+2*i+1]; "
                f"rc_map_set(m, k, v); release_value(k); release_value(v); }} "
                f"sp = base; S[sp++] = val_map(m); }}")
    if op == bc.OP_INDEX:
        return ("{ Value key = S[--sp]; Value cont = S[--sp]; S[sp++] = index_get(cont, key); "
                "release_value(key); release_value(cont); }")
    if op == bc.OP_SETIDX:
        return ("{ Value val = S[--sp]; Value key = S[--sp]; Value cont = S[--sp]; "
                "index_set(cont, key, val); release_value(val); release_value(key); release_value(cont); }")
    if op == bc.OP_LEN:
        return "{ Value v = S[sp-1]; int64_t n = value_length(v); release_value(v); S[sp-1] = val_int(n); }"
    if op == bc.OP_APPEND:
        return ("{ Value val = S[--sp]; Value arr = S[sp-1]; if (arr.kind != VAL_ARRAY) fatal(\"push on a non-array value\"); "
                "rc_array_push(arr.as.arr, val); release_value(val); S[sp++] = val_int(arr.as.arr->length); }")
    if op == bc.OP_HAS:
        return ("{ Value key = S[--sp]; Value mp = S[sp-1]; bool h = (mp.kind == VAL_MAP) && rc_map_has(mp.as.map, key); "
                "release_value(key); release_value(mp); S[sp-1] = val_bool(h); }")
    if op == bc.OP_KEYS:
        return ("{ Value mp = S[sp-1]; if (mp.kind != VAL_MAP) fatal(\"keys() applied to a non-map value\"); "
                "RcArray* k = rc_map_keys_sorted(mp.as.map); release_value(mp); S[sp-1] = val_array(k); }")
    if op == bc.OP_NATIVE:
        nid, argc = operand[0], operand[1]
        return (f"{{ Value r = native({nid}, &S[sp-{argc}], {argc}); "
                f"for (int i = 0; i < {argc}; i++) release_value(S[sp-{argc}+i]); sp -= {argc}; S[sp++] = r; }}")
    if op == bc.OP_COALESCE:
        return ("{ Value b = S[--sp]; Value a = S[--sp]; if (a.kind == VAL_NULL) { S[sp++] = b; release_value(a); } "
                "else { S[sp++] = a; release_value(b); } }")
    if op == bc.OP_UNWRAP:
        return ("{ Value a = S[sp-1]; if (a.kind == VAL_NULL) fatal(\"[Cryo Security] unwrap of null value\"); }")
    if op == bc.OP_TRYPUSH:
        rel = struct.unpack('<i', operand[:4])[0]
        slot = struct.unpack('<H', operand[4:6])[0]
        catch = off + size + rel
        return (f"{{ int _h = aot_hp++; aot_handlers[_h].saved_sp = sp; aot_handlers[_h].locals = L; "
                f"aot_handlers[_h].slot = {slot}; "
                f"if (setjmp(aot_handlers[_h].env)) {{ AotHandler* h = &aot_handlers[aot_hp - 1]; "
                f"sp = h->saved_sp; "
                f"if (h->slot != 0xFFFF) {{ release_value(L[h->slot]); L[h->slot] = aot_thrown; }} "
                f"else release_value(aot_thrown); aot_hp--; goto L{catch}; }} }}")
    if op == bc.OP_TRYPOP:
        return "if (aot_hp > 0) aot_hp--;"          # try completed normally
    if op == bc.OP_THROW:
        return "{ Value v = S[--sp]; aot_raise(v); }"
    if op == bc.OP_HALT:
        return ";"
    return None  # genuinely unsupported opcode


def compile_to_c(data: bytes) -> str:
    p = disasm_pyro.load(data)
    consts, funcs, code = p['consts'], p['funcs'], p['code']
    codelen = len(code)

    out = []
    out.append("// Generated by Burnout AOT (.pyro -> C).  Build:")
    out.append("//   gcc -O2 this.c pyro/vm/pyro_runtime.c -lm -o program")
    out.append("#include <stdio.h>")
    out.append("#include <stdlib.h>")
    out.append("#include <string.h>")
    out.append("#include <setjmp.h>")
    out.append('#include "pyro_runtime.h"')
    out.append("")
    out.append("bool pyro_sandboxed = false;")
    out.append("void fatal(const char* msg) { fprintf(stderr, \"[Pyro AOT] %s\\n\", msg); exit(70); }")
    out.append("")
    # try/catch: a global handler stack; THROW longjmps to the nearest handler
    # (works across C function frames, mirroring the VM's exception unwinding).
    out.append("typedef struct { jmp_buf env; int saved_sp; Value* locals; int slot; } AotHandler;")
    out.append("static AotHandler aot_handlers[256];")
    out.append("static int aot_hp = 0;")
    out.append("static Value aot_thrown;")
    out.append("static void aot_raise(Value v) {")
    out.append("    if (aot_hp > 0) { aot_thrown = v; longjmp(aot_handlers[aot_hp - 1].env, 1); }")
    out.append("    char* s = value_to_string(v); char e[1100];")
    out.append("    snprintf(e, sizeof(e), \"uncaught exception: %s\", s); free(s); fatal(e);")
    out.append("}")
    out.append("")
    out.append(f"static Value K[{max(len(consts),1)}];")
    out.append("static void setup_consts(void) {")
    for i, (kind, val) in enumerate(consts):
        if kind == 'int':
            out.append(f"    K[{i}] = val_int({val}LL);")
        elif kind == 'float':
            out.append(f"    K[{i}] = val_float({val!r});")
        elif kind == 'bool':
            out.append(f"    K[{i}] = val_bool({'true' if val else 'false'});")
        elif kind == 'str':
            b = val.encode('utf-8')
            out.append(f"    {{ static const char c[] = {_c_bytes(b + bytes(1))}; K[{i}] = val_str(c, {len(b)}); }}")
    out.append("}")
    out.append("")

    for i in range(len(funcs)):
        out.append(f"static Value fn_{i}(Value*, int);")
    out.append("")

    for i in range(len(funcs)):
        _emit_func(out, code, consts, funcs, i, codelen)

    out.append("int main(void) {")
    out.append("    setup_consts();")
    out.append(f"    Value r = fn_{p['entryfn']}(0, 0);")
    out.append("    release_value(r);")
    out.append("    return 0;")
    out.append("}")
    return "\n".join(out) + "\n"


def main():
    if len(sys.argv) < 2:
        print("usage: python burnout/aot_pyro.py program.pyro [-o out.c]", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], 'rb') as f:
        c = compile_to_c(f.read())
    if '-o' in sys.argv:
        with open(sys.argv[sys.argv.index('-o') + 1], 'w', encoding='utf-8') as f:
            f.write(c)
    else:
        sys.stdout.write(c)


if __name__ == '__main__':
    main()
