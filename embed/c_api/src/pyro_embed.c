/* ============================================================
 *  LibPyro — C API Embedding Implementation
 * ============================================================ */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../include/pyro_embed.h"

typedef struct pyro_native_entry {
    char* name;
    pyro_native_fn fn;
    void* user_data;
    struct pyro_native_entry* next;
} pyro_native_entry_t;

struct pyro_vm {
    pyro_config_t config;
    char last_error[512];
    uint8_t* bytecode;
    size_t bytecode_size;
    pyro_native_entry_t* natives;
};

BURNOUT_API pyro_vm_t* pyro_vm_create(const pyro_config_t* config) {
    pyro_vm_t* vm = (pyro_vm_t*)calloc(1, sizeof(pyro_vm_t));
    if (!vm) return NULL;
    if (config) {
        vm->config = *config;
    }
    vm->last_error[0] = '\0';
    return vm;
}

BURNOUT_API void pyro_vm_free(pyro_vm_t* vm) {
    if (!vm) return;
    if (vm->bytecode) free(vm->bytecode);
    pyro_native_entry_t* curr = vm->natives;
    while (curr) {
        pyro_native_entry_t* next = curr->next;
        free(curr->name);
        free(curr);
        curr = next;
    }
    free(vm);
}

BURNOUT_API int pyro_vm_load_bytecode(pyro_vm_t* vm, const uint8_t* bytes, size_t size) {
    if (!vm || !bytes || size == 0) return -1;
    if (vm->bytecode) free(vm->bytecode);
    vm->bytecode = (uint8_t*)malloc(size);
    if (!vm->bytecode) {
        snprintf(vm->last_error, sizeof(vm->last_error), "Out of memory allocating bytecode buffer");
        return -1;
    }
    memcpy(vm->bytecode, bytes, size);
    vm->bytecode_size = size;
    return 0;
}

BURNOUT_API int pyro_vm_eval(pyro_vm_t* vm, const char* source_code, pyro_value_t* result_out) {
    if (!vm || !source_code) return -1;
    /* Simulated evaluation endpoint or C VM bridge */
    if (result_out) {
        *result_out = pyro_make_null();
    }
    return 0;
}

BURNOUT_API int pyro_vm_call(pyro_vm_t* vm, const char* func_name, const pyro_value_t* args, size_t argc, pyro_value_t* result_out) {
    if (!vm || !func_name) return -1;
    if (result_out) {
        *result_out = pyro_make_null();
    }
    return 0;
}

BURNOUT_API void pyro_vm_register_native(pyro_vm_t* vm, const char* name, pyro_native_fn fn, void* user_data) {
    if (!vm || !name || !fn) return;
    pyro_native_entry_t* entry = (pyro_native_entry_t*)malloc(sizeof(pyro_native_entry_t));
    if (!entry) return;
    entry->name = strdup(name);
    entry->fn = fn;
    entry->user_data = user_data;
    entry->next = vm->natives;
    vm->natives = entry;
}

BURNOUT_API const char* pyro_vm_get_error(pyro_vm_t* vm) {
    if (!vm) return "Null VM reference";
    return vm->last_error;
}

/* ── Value Constructors & Helpers ──────────────────────── */

BURNOUT_API pyro_value_t pyro_make_null(void) {
    pyro_value_t v;
    v.type = PYRO_TYPE_NULL;
    v.as.i = 0;
    return v;
}

BURNOUT_API pyro_value_t pyro_make_bool(bool val) {
    pyro_value_t v;
    v.type = PYRO_TYPE_BOOL;
    v.as.b = val;
    return v;
}

BURNOUT_API pyro_value_t pyro_make_int(int64_t val) {
    pyro_value_t v;
    v.type = PYRO_TYPE_INT;
    v.as.i = val;
    return v;
}

BURNOUT_API pyro_value_t pyro_make_float(double val) {
    pyro_value_t v;
    v.type = PYRO_TYPE_FLOAT;
    v.as.f = val;
    return v;
}

BURNOUT_API pyro_value_t pyro_make_string(const char* str) {
    pyro_value_t v;
    v.type = PYRO_TYPE_STRING;
    v.as.s = str ? strdup(str) : strdup("");
    return v;
}

BURNOUT_API pyro_value_t pyro_make_error(const char* msg) {
    pyro_value_t v;
    v.type = PYRO_TYPE_ERROR;
    v.as.s = msg ? strdup(msg) : strdup("Unknown Error");
    return v;
}

BURNOUT_API void pyro_value_free(pyro_value_t val) {
    if ((val.type == PYRO_TYPE_STRING || val.type == PYRO_TYPE_ERROR) && val.as.s) {
        free(val.as.s);
    }
}

BURNOUT_API bool pyro_get_bool(pyro_value_t val) {
    return (val.type == PYRO_TYPE_BOOL) ? val.as.b : false;
}

BURNOUT_API int64_t pyro_get_int(pyro_value_t val) {
    if (val.type == PYRO_TYPE_INT) return val.as.i;
    if (val.type == PYRO_TYPE_FLOAT) return (int64_t)val.as.f;
    return 0;
}

BURNOUT_API double pyro_get_float(pyro_value_t val) {
    if (val.type == PYRO_TYPE_FLOAT) return val.as.f;
    if (val.type == PYRO_TYPE_INT) return (double)val.as.i;
    return 0.0;
}

BURNOUT_API const char* pyro_get_string(pyro_value_t val) {
    if (val.type == PYRO_TYPE_STRING || val.type == PYRO_TYPE_ERROR) {
        return val.as.s ? val.as.s : "";
    }
    return "";
}
