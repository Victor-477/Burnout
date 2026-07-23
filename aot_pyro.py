# ============================================================
#  Burnout — Pyro AOT: .pyro bytecode -> C (Phase 9.5)
#
#  Translates a .pyro program into a standalone C file that runs WITHOUT the
#  VM and without the .pyro at runtime (the "zero-dependency" native route).
#
#  Model mirrors the VM (pyro/vm/main.c): a single global value stack and a
#  single global locals stack with a frame stack, so exceptions unwind — and
#  release — across C frames exactly as the VM unwinds its call stack. Each
#  Pyro function becomes a `void fn_i(void)` that sets up its frame, pops its
#  args off the value stack, runs the opcodes as straight-line C (jump targets
#  are `goto` labels), and on RET releases its locals and leaves the result on
#  the value stack. The value model and 28 native builtins come from the Pyro
#  C runtime (pyro/vm/pyro_runtime.{c,h}) — identical semantics.
#
#  Build:  gcc -O2 out.c pyro/vm/pyro_runtime.c -lm -o out   (+ -lws2_32 on Windows)
#  Usage:  python burnout/aot_pyro.py program.pyro > out.c
#
#  try/catch/throw: TRYPUSH = setjmp (records value-sp / frame-fp / locals-sp /
#  catch slot); THROW and failed asserts longjmp to the nearest handler, which
#  releases every value above the saved sp and the locals of every frame above
#  the saved fp before entering the catch body (leak-free), or fatal if none.
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

_OPNAME = {v: k[3:] for k, v in vars(bc).items()
           if k.startswith('OP_') and isinstance(v, int)}


def _c_bytes(b: bytes) -> str:
    """A C array-initializer of the raw bytes (avoids escaping surprises)."""
    return "{" + ",".join(str(x) for x in b) + "}"


def _func_range(funcs, i, codelen):
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
        if op in (bc.OP_JMP, bc.OP_JMPF, bc.OP_JMPT, bc.OP_TRYPUSH):
            rel = struct.unpack('<i', code[i+1:i+5])[0]
            targets.add(i + size + rel)
        i += size
    return targets


def _emit_func(out, code, consts, funcs, fi, codelen):
    f = funcs[fi]
    entry, end = _func_range(funcs, fi, codelen)
    targets = _jump_targets(code, entry, end)
    nloc = f['nlocals']
    nparams = f['nparams']

    out.append(f"static void fn_{fi}(void) {{  // {f['name']}  (params={nparams}, locals={nloc})")
    out.append("    int base = g_locsp; g_locsp += %d;" % nloc)
    out.append("    g_fbase[g_fp] = base; g_fnn[g_fp] = %d; g_fp++;" % nloc)
    out.append("    for (int i = 0; i < %d; i++) g_locals[base + i] = val_null();" % nloc)
    out.append("    for (int i = %d - 1; i >= 0; i--) g_locals[base + i] = g_stack[--g_sp];" % nparams)
    out.append("    (void)base;")

    i = entry
    while i < end:
        op = code[i]
        size = 1 + bc._OPERAND.get(op, 0)
        operand = code[i+1:i+size]
        if i in targets:
            out.append(f"  L{i}: ;")
        c = _emit_op(op, operand, i, size, consts, funcs, nloc)
        if c is None:
            raise ValueError(f"AOT: opcode {_OPNAME.get(op, hex(op))} not supported")
        out.append("    " + c)
        i += size
    # every function ends with an explicit RET in the bytecode; this is a guard
    out.append("    { for (int i = 0; i < %d; i++) release_value(g_locals[base + i]); "
               "g_locsp = base; g_fp--; g_stack[g_sp++] = val_null(); return; }" % nloc)
    out.append("}")
    out.append("")


def _emit_op(op, operand, off, size, consts, funcs, nloc):
    tgt = None
    if op in (bc.OP_JMP, bc.OP_JMPF, bc.OP_JMPT):
        rel = struct.unpack('<i', operand[:4])[0]
        tgt = off + size + rel
    if op == bc.OP_CONST:
        idx = struct.unpack('<H', operand)[0]
        return f"{{ retain_value(K[{idx}]); g_stack[g_sp++] = K[{idx}]; }}"
    if op == bc.OP_TRUE:   return "g_stack[g_sp++] = val_bool(true);"
    if op == bc.OP_FALSE:  return "g_stack[g_sp++] = val_bool(false);"
    if op == bc.OP_NULL:   return "g_stack[g_sp++] = val_null();"
    if op == bc.OP_POP:    return "release_value(g_stack[--g_sp]);"
    if op == bc.OP_LOAD:
        s = struct.unpack('<H', operand)[0]
        return f"{{ retain_value(g_locals[base+{s}]); g_stack[g_sp++] = g_locals[base+{s}]; }}"
    if op == bc.OP_STORE:
        s = struct.unpack('<H', operand)[0]
        return f"{{ release_value(g_locals[base+{s}]); g_locals[base+{s}] = g_stack[--g_sp]; }}"
    if op in (bc.OP_ADD, bc.OP_SUB, bc.OP_MUL, bc.OP_DIV, bc.OP_MOD,
              bc.OP_BAND, bc.OP_BOR, bc.OP_BXOR, bc.OP_SHL, bc.OP_SHR,
              bc.OP_EQ, bc.OP_NE, bc.OP_LT, bc.OP_GT, bc.OP_LE, bc.OP_GE):
        return (f"{{ Value b = g_stack[--g_sp]; Value a = g_stack[--g_sp]; "
                f"g_stack[g_sp++] = bin_op({op}, a, b); release_value(a); release_value(b); }}")
    if op == bc.OP_NEG:
        return ("{ Value a = g_stack[g_sp-1]; g_stack[g_sp-1] = (a.kind == VAL_FLOAT) ? "
                "val_float(-a.as.f) : val_int(-a.as.i); }")
    if op == bc.OP_BNOT:
        return "{ Value a = g_stack[g_sp-1]; g_stack[g_sp-1] = val_int(~a.as.i); }"
    if op == bc.OP_NOT:
        return ("{ Value a = g_stack[g_sp-1]; bool t = value_truthy(a); "
                "release_value(a); g_stack[g_sp-1] = val_bool(!t); }")
    if op == bc.OP_JMP:    return f"goto L{tgt};"
    if op == bc.OP_JMPF:
        return f"{{ Value a = g_stack[--g_sp]; bool t = value_truthy(a); release_value(a); if (!t) goto L{tgt}; }}"
    if op == bc.OP_JMPT:
        return f"{{ Value a = g_stack[--g_sp]; bool t = value_truthy(a); release_value(a); if (t) goto L{tgt}; }}"
    if op == bc.OP_CALL:
        fi = struct.unpack('<H', operand[:2])[0]
        return f"fn_{fi}();"                       # callee pops its args, leaves result on g_stack
    if op == bc.OP_RET:
        return ("{ Value r = g_stack[--g_sp]; "
                "for (int i = 0; i < %d; i++) release_value(g_locals[base + i]); "
                "g_locsp = base; g_fp--; g_stack[g_sp++] = r; return; }" % nloc)
    if op == bc.OP_PRINT:
        return "{ Value v = g_stack[--g_sp]; char* s = value_to_string(v); printf(\"%s\\n\", s); free(s); release_value(v); }"
    if op == bc.OP_PRINTLN: return "printf(\"\\n\");"
    if op == bc.OP_ASSERT:
        return ("{ Value cond = g_stack[--g_sp]; Value msg = g_stack[--g_sp]; if (!value_truthy(cond)) "
                "{ char* m = value_to_string(msg); char e[1100]; snprintf(e, sizeof(e), "
                "\"[Cryo Assert] %s\", m); free(m); Value ev = val_str(e, (int64_t)strlen(e)); "
                "release_value(cond); release_value(msg); aot_raise(ev); } "
                "release_value(cond); release_value(msg); }")
    if op == bc.OP_NEWARR:
        n = struct.unpack('<H', operand)[0]
        return (f"{{ RcArray* a = rc_array_new(); int b0 = g_sp - {n}; "
                f"for (int i = 0; i < {n}; i++) {{ rc_array_push(a, g_stack[b0+i]); release_value(g_stack[b0+i]); }} "
                f"g_sp = b0; g_stack[g_sp++] = val_array(a); }}")
    if op == bc.OP_NEWMAP:
        n = struct.unpack('<H', operand)[0]
        return (f"{{ RcMap* m = rc_map_new(); int b0 = g_sp - {2*n}; "
                f"for (int i = 0; i < {n}; i++) {{ Value k = g_stack[b0+2*i]; Value v = g_stack[b0+2*i+1]; "
                f"rc_map_set(m, k, v); release_value(k); release_value(v); }} "
                f"g_sp = b0; g_stack[g_sp++] = val_map(m); }}")
    if op == bc.OP_INDEX:
        return ("{ Value key = g_stack[--g_sp]; Value cont = g_stack[--g_sp]; "
                "g_stack[g_sp++] = index_get(cont, key); release_value(key); release_value(cont); }")
    if op == bc.OP_SETIDX:
        return ("{ Value val = g_stack[--g_sp]; Value key = g_stack[--g_sp]; Value cont = g_stack[--g_sp]; "
                "index_set(cont, key, val); release_value(val); release_value(key); release_value(cont); }")
    if op == bc.OP_LEN:
        return "{ Value v = g_stack[g_sp-1]; int64_t n = value_length(v); release_value(v); g_stack[g_sp-1] = val_int(n); }"
    if op == bc.OP_APPEND:
        # contract: pop val, pop arr -> push new size (net -1). Peeking arr
        # would strand a slot and desync paths that merge after a conditional push.
        return ("{ Value val = g_stack[--g_sp]; Value arr = g_stack[--g_sp]; if (arr.kind != VAL_ARRAY) fatal(\"push on a non-array value\"); "
                "rc_array_push(arr.as.arr, val); release_value(val); int64_t n = arr.as.arr->length; "
                "release_value(arr); g_stack[g_sp++] = val_int(n); }")
    if op == bc.OP_HAS:
        return ("{ Value key = g_stack[--g_sp]; Value mp = g_stack[g_sp-1]; bool h = (mp.kind == VAL_MAP) && rc_map_has(mp.as.map, key); "
                "release_value(key); release_value(mp); g_stack[g_sp-1] = val_bool(h); }")
    if op == bc.OP_KEYS:
        return ("{ Value mp = g_stack[g_sp-1]; if (mp.kind != VAL_MAP) fatal(\"keys() applied to a non-map value\"); "
                "RcArray* k = rc_map_keys_sorted(mp.as.map); release_value(mp); g_stack[g_sp-1] = val_array(k); }")
    if op == bc.OP_NATIVE:
        nid, argc = operand[0], operand[1]
        return (f"{{ Value r = native({nid}, &g_stack[g_sp-{argc}], {argc}); "
                f"for (int i = 0; i < {argc}; i++) release_value(g_stack[g_sp-{argc}+i]); g_sp -= {argc}; g_stack[g_sp++] = r; }}")
    if op == bc.OP_COALESCE:
        return ("{ Value b = g_stack[--g_sp]; Value a = g_stack[--g_sp]; if (a.kind == VAL_NULL) { g_stack[g_sp++] = b; release_value(a); } "
                "else { g_stack[g_sp++] = a; release_value(b); } }")
    if op == bc.OP_UNWRAP:
        return "{ Value a = g_stack[g_sp-1]; if (a.kind == VAL_NULL) fatal(\"[Cryo Security] unwrap of null value\"); }"
    if op == bc.OP_TRYPUSH:
        rel = struct.unpack('<i', operand[:4])[0]
        slot = struct.unpack('<H', operand[4:6])[0]
        catch = off + size + rel
        return (f"{{ int _h = g_hp++; g_handlers[_h].saved_sp = g_sp; g_handlers[_h].saved_fp = g_fp; "
                f"g_handlers[_h].saved_locsp = g_locsp; g_handlers[_h].slot = {slot}; "
                f"if (setjmp(g_handlers[_h].env)) {{ AotHandler* h = &g_handlers[g_hp - 1]; "
                f"while (g_sp > h->saved_sp) release_value(g_stack[--g_sp]); "
                f"while (g_fp > h->saved_fp) {{ int b = g_fbase[g_fp-1]; int n = g_fnn[g_fp-1]; "
                f"for (int i = 0; i < n; i++) release_value(g_locals[b+i]); g_fp--; }} "
                f"g_locsp = h->saved_locsp; base = g_fbase[g_fp-1]; "
                f"if (h->slot != 0xFFFF) {{ release_value(g_locals[base + h->slot]); g_locals[base + h->slot] = aot_thrown; }} "
                f"else release_value(aot_thrown); g_hp--; goto L{catch}; }} }}")
    if op == bc.OP_TRYPOP:
        return "if (g_hp > 0) g_hp--;"
    if op == bc.OP_THROW:
        return "{ Value v = g_stack[--g_sp]; aot_raise(v); }"
    if op == bc.OP_HALT:
        return ";"
    return None


def compile_to_c(data: bytes) -> str:
    p = disasm_pyro.load(data)
    consts, funcs, code = p['consts'], p['funcs'], p['code']
    codelen = len(code)

    out = []
    out.append("// Generated by Burnout AOT (.pyro -> C).  Build:")
    out.append("//   gcc -O2 this.c pyro/vm/pyro_runtime.c -lm -o program   (+ -lws2_32 on Windows)")
    out.append("#include <stdio.h>")
    out.append("#include <stdlib.h>")
    out.append("#include <string.h>")
    out.append("#include <setjmp.h>")
    out.append('#include "pyro_runtime.h"')
    out.append("")
    # pyro_sandboxed lives in pyro_runtime.c; the host only supplies fatal().
    out.append("void fatal(const char* msg) { fprintf(stderr, \"[Pyro AOT] %s\\n\", msg); exit(70); }")
    out.append("")
    # global machine state (single value stack, single locals stack, frame stack)
    out.append("static Value g_stack[65536]; static int g_sp = 0;")
    out.append("static Value g_locals[65536]; static int g_locsp = 0;")
    out.append("static int g_fbase[8192]; static int g_fnn[8192]; static int g_fp = 0;")
    out.append("typedef struct { jmp_buf env; int saved_sp; int saved_fp; int saved_locsp; int slot; } AotHandler;")
    out.append("static AotHandler g_handlers[1024]; static int g_hp = 0;")
    out.append("static Value aot_thrown;")
    out.append("static void aot_raise(Value v) {")
    out.append("    if (g_hp > 0) { aot_thrown = v; longjmp(g_handlers[g_hp - 1].env, 1); }")
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
        out.append(f"static void fn_{i}(void);")
    out.append("")

    for i in range(len(funcs)):
        _emit_func(out, code, consts, funcs, i, codelen)

    out.append("int main(int argc, char** argv) {")
    out.append("    pyro_argc = argc - 1; pyro_argv = argv + 1;   // program args, argv[0] dropped")
    # sandbox policy mirrors the VM: baked-in flag from the .pyro, plus PYRO_SANDBOX=1.
    if p['flags'] & 0x04:
        out.append("    pyro_sandboxed = true;   // compiled from a sandboxed .pyro")
    out.append('    { const char* e = getenv("PYRO_SANDBOX"); '
               'if (e && strcmp(e, "1") == 0) pyro_sandboxed = true; }')
    out.append("    setup_consts();")
    out.append(f"    fn_{p['entryfn']}();")
    out.append("    if (g_sp > 0) release_value(g_stack[--g_sp]);   // discard entry return")
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
