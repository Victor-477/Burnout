/* ============================================================
   Cryo Language — C Runtime  (v0.3)
   Include in every generated .pyro.
   Compile: gcc -O2 programa.c cryo_runtime.c -lm -o programa
   ============================================================ */
#pragma once
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <math.h>
#include <setjmp.h>

/* ---------- null ---------- */
#define null NULL

/* ---------- CryoArray ----------
   Array dinamico generico — elementos sao uint64_t (8 bytes).
   int64_t, double and pointers fit in 8 bytes on x86-64.      */
typedef struct CryoArray {
    uint64_t* data;
    int64_t   length;
    int64_t   capacity;
} CryoArray;

CryoArray* cryo_array_new(void);
void       cryo_array_push(CryoArray* a, uint64_t v);
uint64_t   cryo_array_get(CryoArray* a, int64_t i);
void       cryo_array_set(CryoArray* a, int64_t i, uint64_t v);
CryoArray* cryo_array_slice(CryoArray* a, int64_t start, int64_t end);
/* Phase 10.2 collection ops (ISSUES/09). All NON-MUTATING: a fresh array is
   returned and the source is untouched. CryoArray is untyped (raw uint64_t), so
   anything needing equality/ordering/arithmetic has per-element-type variants. */
CryoArray* cryo_array_reverse(CryoArray* a);
CryoArray* cryo_array_concat(CryoArray* a, CryoArray* b);
int64_t    cryo_sum_i(CryoArray* a);
double     cryo_sum_f(CryoArray* a);
int64_t    cryo_count_i(CryoArray* a, int64_t v);
int64_t    cryo_count_f(CryoArray* a, double v);
int64_t    cryo_count_s(CryoArray* a, const char* v);
int64_t    cryo_index_of_i(CryoArray* a, int64_t v);
int64_t    cryo_index_of_f(CryoArray* a, double v);
int64_t    cryo_index_of_s(CryoArray* a, const char* v);
CryoArray* cryo_sort_i(CryoArray* a);
CryoArray* cryo_sort_f(CryoArray* a);
CryoArray* cryo_sort_s(CryoArray* a);
void       cryo_array_free(CryoArray* a);

/* Helpers for push/get by type */
static inline void cryo_push_i64(CryoArray* a, int64_t v)  { cryo_array_push(a, (uint64_t)v); }
static inline void cryo_push_f64(CryoArray* a, double v)   { uint64_t u; memcpy(&u,&v,8); cryo_array_push(a,u); }
static inline void cryo_push_str(CryoArray* a, char* v)    { cryo_array_push(a, (uint64_t)(uintptr_t)v); }
static inline void cryo_push_bool(CryoArray* a, bool v)    { cryo_array_push(a, (uint64_t)v); }

static inline int64_t cryo_get_i64(CryoArray* a, int64_t i)  { return (int64_t)cryo_array_get(a,i); }
static inline double  cryo_get_f64(CryoArray* a, int64_t i)  { uint64_t u=cryo_array_get(a,i); double v; memcpy(&v,&u,8); return v; }
static inline char*   cryo_get_str(CryoArray* a, int64_t i)  { return (char*)(uintptr_t)cryo_array_get(a,i); }
static inline bool    cryo_get_bool(CryoArray* a, int64_t i) { return (bool)cryo_array_get(a,i); }

/* 11.27 — element ASSIGNMENT, `a[i] = v`. cryo_array_set existed but had no
   typed wrappers, so the C backend refused IndexAssignment outright. */
static inline void cryo_set_i64(CryoArray* a, int64_t i, int64_t v)  { cryo_array_set(a,i,(uint64_t)v); }
static inline void cryo_set_f64(CryoArray* a, int64_t i, double v)   { uint64_t u; memcpy(&u,&v,8); cryo_array_set(a,i,u); }
static inline void cryo_set_str(CryoArray* a, int64_t i, char* v)    { cryo_array_set(a,i,(uint64_t)(uintptr_t)v); }
static inline void cryo_set_bool(CryoArray* a, int64_t i, bool v)    { cryo_array_set(a,i,(uint64_t)v); }

/* ---------- Strings ---------- */
char*   cryo_str_concat(const char* a, const char* b);
char*   cryo_i64_to_str(int64_t n);
char*   cryo_f64_to_str(double n);
char*   cryo_bool_to_str(bool b);
/* 11.27 — rendering an ARRAY. CryoArray stores raw uint64_t and does not know
   what its elements are, so the element type comes from the code generator and
   picks the function. The output is the canonical VM form (PYRO_RUNTIME.md
   §3.1): "[a, b, c]", elements separated by ", ", strings NOT quoted — so
   print(a) reads identically on every backend, which is the whole point. The
   returned buffer is malloc'd. */
char*   cryo_arr_to_str_i(CryoArray* a);
char*   cryo_arr_to_str_f(CryoArray* a);
char*   cryo_arr_to_str_s(CryoArray* a);
char*   cryo_arr_to_str_b(CryoArray* a);

/* ---------- 11.27: optionals (T?) ----------
   T? is a POINTER, the same representation the go backend uses: NULL is null,
   anything else points at the value. `string?` needs no wrapper at all — a
   char* is already nullable — which is why there is no cryo_opt_s.

   cryo_unwrap_* implements `x!`, and aborts with the SAME message the Pyro VM
   prints, byte for byte: the two are compared in test_c_vm.py, and a runtime
   that words its failures differently is a different language. */
int64_t* cryo_opt_i(int64_t v);
double*  cryo_opt_f(double v);
bool*    cryo_opt_b(bool v);
int64_t  cryo_unwrap_i(int64_t* p);
double   cryo_unwrap_f(double* p);
bool     cryo_unwrap_b(bool* p);
char*    cryo_unwrap_s(char* p);

/* ---------- 11.27: maps (map<K,V>) ----------
   Open-addressed hash table. Keys and values are uint64_t, as in CryoArray, so
   int64_t / double / char* all fit; `str_keys` says how to hash and compare
   them and is fixed at creation, because a map<K,V> has one key type for life.

   cryo_map_keys returns the keys SORTED BY THEIR OWN TEXT, and rendering does
   the same, because that is what the Pyro VM and the go/node backends do
   (PYRO_RUNTIME.md §4). Iterating in bucket order would make the same program
   print its map differently here than everywhere else — and differently again
   after an insertion resized the table. */
typedef struct { uint64_t key; uint64_t val; int used; } CryoMapEntry;
typedef struct CryoMap {
    CryoMapEntry* e;
    int64_t cap, len;
    int     str_keys;
} CryoMap;

CryoMap*   cryo_map_new(int str_keys);
void       cryo_map_set(CryoMap* m, uint64_t k, uint64_t v);
uint64_t   cryo_map_get(CryoMap* m, uint64_t k);
bool       cryo_map_has(CryoMap* m, uint64_t k);
void       cryo_map_remove(CryoMap* m, uint64_t k);
int64_t    cryo_map_len(CryoMap* m);
CryoArray* cryo_map_keys(CryoMap* m);
/* val_kind: 0 int, 1 number, 2 string, 3 bool — the generator knows it */
char*      cryo_map_to_str(CryoMap* m, int val_kind);

/* Keys and values travel as uint64_t, so each end needs one conversion. Six
   inlines rather than a helper per (key kind x value kind) pair, which would
   be sixteen functions saying the same thing. */
static inline uint64_t cryo_u64_i(int64_t v)     { return (uint64_t)v; }
static inline uint64_t cryo_u64_f(double v)      { uint64_t u; memcpy(&u,&v,8); return u; }
static inline uint64_t cryo_u64_s(const char* v) { return (uint64_t)(uintptr_t)v; }
static inline int64_t  cryo_of_u64_i(uint64_t u) { return (int64_t)u; }
static inline double   cryo_of_u64_f(uint64_t u) { double d; memcpy(&d,&u,8); return d; }
static inline char*    cryo_of_u64_s(uint64_t u) { return (char*)(uintptr_t)u; }
static inline bool     cryo_of_u64_b(uint64_t u) { return u != 0; }
int64_t cryo_str_len(const char* s);
bool    cryo_str_eq(const char* a, const char* b);
char*   cryo_str_slice(const char* s, int64_t start, int64_t end);
char*   cryo_str_upper(const char* s);
/* Phase 10.4 strings (ISSUES/09). Every char* returned is malloc'd and NOT
   freed by the generated code — see the ownership note in cryo_runtime.c. */
char*   cryo_str_trim(const char* s);
bool    cryo_str_contains(const char* s, const char* sub);
int64_t cryo_str_find(const char* s, const char* sub);
bool    cryo_str_starts_with(const char* s, const char* p);
bool    cryo_str_ends_with(const char* s, const char* p);
char*   cryo_str_repeat(const char* s, int64_t n);
char*   cryo_str_pad_start(const char* s, int64_t w, const char* p);
char*   cryo_str_pad_end(const char* s, int64_t w, const char* p);
char*   cryo_str_replace(const char* s, const char* old, const char* rep);
CryoArray* cryo_str_split(const char* s, const char* sep);   /* -> string[] */
char*   cryo_str_join(CryoArray* a, const char* sep);
char*   cryo_str_lower(const char* s);

/* ---------- Print ---------- */
void cryo_print_str(const char* s);
void cryo_print_i64(int64_t n);
void cryo_print_f64(double n);
void cryo_print_bool(bool b);
void cryo_print_newline(void);

/* ---------- Input ---------- */
char*   cryo_input(const char* prompt);
int64_t cryo_input_int(const char* prompt);
double  cryo_input_num(const char* prompt);

/* ---------- Filesystem & Process ---------- */
bool       cryo_file_exists(const char* path);
bool       cryo_is_dir(const char* path);
CryoArray* cryo_list_dir(const char* path);
bool       cryo_make_dir(const char* path);
bool       cryo_delete_file(const char* path);
int64_t    cryo_file_size(const char* path);
bool       cryo_write_file(const char* path, const char* content);
char*      cryo_read_file(const char* path);
char*      cryo_env(const char* name);
char*      cryo_exec(const char* cmd);

/* ---------- Conversoes ---------- */
static inline int64_t cryo_to_int(double v)  { return (int64_t)v; }
static inline double  cryo_to_num(int64_t v) { return (double)v; }
static inline char*   cryo_to_str_i(int64_t v) { return cryo_i64_to_str(v); }
static inline char*   cryo_to_str_f(double v)  { return cryo_f64_to_str(v); }
static inline char*   cryo_to_str_b(bool v)    { return cryo_bool_to_str(v); }

/* ---------- Math ---------- */
static inline double  cryo_sqrt(double x)          { return sqrt(x); }
static inline double  cryo_pow(double b, double e)  { return pow(b, e); }
static inline double  cryo_abs_f(double x)          { return fabs(x); }
static inline int64_t cryo_abs_i(int64_t x)         { return llabs(x); }
static inline int64_t cryo_min_i(int64_t a, int64_t b) { return a < b ? a : b; }
static inline int64_t cryo_max_i(int64_t a, int64_t b) { return a > b ? a : b; }
static inline double  cryo_min_f(double a, double b)   { return a < b ? a : b; }
static inline double  cryo_max_f(double a, double b)   { return a > b ? a : b; }
static inline double  cryo_floor(double x)           { return floor(x); }
static inline double  cryo_ceil(double x)            { return ceil(x); }
static inline double  cryo_round(double x)           { return round(x); }
static inline double  cryo_log(double x)             { return log(x); }
static inline double  cryo_log10(double x)           { return log10(x); }
static inline double  cryo_sin(double x)             { return sin(x); }
static inline double  cryo_cos(double x)             { return cos(x); }
static inline double  cryo_tan(double x)             { return tan(x); }

/* ── Phase 10.4 stdlib: same semantics as the Pyro runtime (ISSUES/09) ──
   clamp keeps the argument's type, so there are int and float variants, exactly
   like min/max above. sign and gcd always return int. */
static inline int64_t cryo_clamp_i(int64_t x, int64_t lo, int64_t hi) {
    return x < lo ? lo : (x > hi ? hi : x);
}
static inline double  cryo_clamp_f(double x, double lo, double hi) {
    return x < lo ? lo : (x > hi ? hi : x);
}
static inline int64_t cryo_sign_f(double x)   { return x < 0 ? -1 : (x > 0 ? 1 : 0); }
static inline int64_t cryo_sign_i(int64_t x)  { return x < 0 ? -1 : (x > 0 ? 1 : 0); }
static inline int64_t cryo_gcd(int64_t a, int64_t b) {
    if (a < 0) a = -a;
    if (b < 0) b = -b;
    while (b != 0) { int64_t t = a % b; a = b; b = t; }
    return a;
}
static inline double  cryo_hypot(double a, double b) { return hypot(a, b); }

#define CRYO_PI 3.14159265358979323846
#define CRYO_E  2.71828182845904523536

/* ---------- Security: arithmetic with verification ----------
   Under --safe mode the compiler routes +,-,* of integers through
   estas functions, que abortam em overflow. Divisao/modulo por
   zero sao always protecteds.                                  */
int64_t cryo_add_ovf(int64_t a, int64_t b);
int64_t cryo_sub_ovf(int64_t a, int64_t b);
int64_t cryo_mul_ovf(int64_t a, int64_t b);
int64_t cryo_idiv_chk(int64_t a, int64_t b);
int64_t cryo_imod_chk(int64_t a, int64_t b);

/* ---------- Security: assert ---------- */
void cryo_assert(bool cond, const char* msg);

/* ---------- Security: null pointer guard ---------- */
void* cryo_check_null(void* p, const char* what);

/* ---------- Excecao via setjmp ---------- */
typedef struct {
    jmp_buf  buf;
    char     message[512];
    bool     active;
} CryoException;

extern CryoException _cryo_exc;

#define CRYO_TRY     if (!setjmp(_cryo_exc.buf)) { _cryo_exc.active = true;
#define CRYO_CATCH(var) _cryo_exc.active = false; } else { char* var = _cryo_exc.message;
#define CRYO_END_CATCH   }
#define CRYO_FINALLY  /* finally always runs */
#define CRYO_THROW(msg) do { \
    strncpy(_cryo_exc.message, (msg), 511); \
    if (_cryo_exc.active) longjmp(_cryo_exc.buf, 1); \
    else { fprintf(stderr, "[Cryo Exception] %s\n", (msg)); exit(1); } \
} while(0)
