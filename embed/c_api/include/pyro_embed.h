/* ============================================================
 *  LibPyro — C API Embedding Header
 * ============================================================ */
#ifndef PYRO_EMBED_H
#define PYRO_EMBED_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32) || defined(__CYGWIN__)
  #ifdef BURNOUT_BUILD_DLL
    #define BURNOUT_API __declspec(dllexport)
  #else
    #define BURNOUT_API __declspec(dllimport)
  #endif
#else
  #define BURNOUT_API __attribute__((visibility("default")))
#endif

/* ── Value Types ───────────────────────────────────────── */
typedef enum pyro_type {
    PYRO_TYPE_NULL = 0,
    PYRO_TYPE_BOOL,
    PYRO_TYPE_INT,
    PYRO_TYPE_FLOAT,
    PYRO_TYPE_STRING,
    PYRO_TYPE_ARRAY,
    PYRO_TYPE_MAP,
    PYRO_TYPE_STRUCT,
    PYRO_TYPE_ERROR
} pyro_type_t;

typedef struct pyro_value {
    pyro_type_t type;
    union {
        bool     b;
        int64_t  i;
        double   f;
        char*    s;
        void*    ptr;
    } as;
} pyro_value_t;

typedef struct pyro_config {
    bool sandbox;
    bool debug;
} pyro_config_t;

typedef struct pyro_vm pyro_vm_t;

typedef pyro_value_t (*pyro_native_fn)(pyro_vm_t* vm, const pyro_value_t* args, size_t argc, void* user_data);

/* ── VM Lifecycle & Execution ──────────────────────────── */
BURNOUT_API pyro_vm_t* pyro_vm_create(const pyro_config_t* config);
BURNOUT_API void        pyro_vm_free(pyro_vm_t* vm);

BURNOUT_API int         pyro_vm_load_bytecode(pyro_vm_t* vm, const uint8_t* bytes, size_t size);
BURNOUT_API int         pyro_vm_eval(pyro_vm_t* vm, const char* source_code, pyro_value_t* result_out);
BURNOUT_API int         pyro_vm_call(pyro_vm_t* vm, const char* func_name, const pyro_value_t* args, size_t argc, pyro_value_t* result_out);

BURNOUT_API void        pyro_vm_register_native(pyro_vm_t* vm, const char* name, pyro_native_fn fn, void* user_data);
BURNOUT_API const char* pyro_vm_get_error(pyro_vm_t* vm);

/* ── Value Constructors & Extraction ───────────────────── */
BURNOUT_API pyro_value_t pyro_make_null(void);
BURNOUT_API pyro_value_t pyro_make_bool(bool v);
BURNOUT_API pyro_value_t pyro_make_int(int64_t v);
BURNOUT_API pyro_value_t pyro_make_float(double v);
BURNOUT_API pyro_value_t pyro_make_string(const char* str);
BURNOUT_API pyro_value_t pyro_make_error(const char* msg);
BURNOUT_API void         pyro_value_free(pyro_value_t val);

BURNOUT_API bool        pyro_get_bool(pyro_value_t val);
BURNOUT_API int64_t     pyro_get_int(pyro_value_t val);
BURNOUT_API double      pyro_get_float(pyro_value_t val);
BURNOUT_API const char* pyro_get_string(pyro_value_t val);

#ifdef __cplusplus
}
#endif

#endif /* PYRO_EMBED_H */
