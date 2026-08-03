/* ============================================================
   Cryo Language — C Runtime Implementation  (v0.3)
   ============================================================ */
#include "cryo_runtime.h"
#include <ctype.h>
#include <inttypes.h>   /* PRId64: the portable int64_t format specifier */

/* ---------- Portability: POSIX functions are NOT ISO C ----------
   The c backend compiles with -std=c11, which defines __STRICT_ANSI__ and makes
   MinGW hide its POSIX/MSVCRT extensions. A hidden function is then implicitly
   declared as returning `int`, which TRUNCATES a returned pointer on 64-bit
   hosts — silent corruption, not a mere warning. `getline`/`ssize_t` are worse
   still: genuinely absent in some MinGW configurations.

   So: supply our own `strdup`, and read lines with a portable `fgets` loop
   instead of `getline`. (The same class of bug, and the same remedy, is
   documented in pyro/vm/pyro_runtime.c.) */
static char* cryo_strdup(const char* s) {
    if (!s) s = "";
    size_t n = strlen(s) + 1;
    char* p = (char*)malloc(n);
    if (!p) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    memcpy(p, s, n);
    return p;
}
#define strdup cryo_strdup

/* Reads one line from `f` without the trailing newline, growing as needed.
   Returns a malloc'd string (never NULL — "" at EOF), so callers can always
   free() and never have to null-check. */
static char* cryo_read_line(FILE* f) {
    size_t cap = 128, len = 0;
    char* buf = (char*)malloc(cap);
    if (!buf) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    buf[0] = '\0';
    for (;;) {
        if (!fgets(buf + len, (int)(cap - len), f)) break;   /* EOF or error */
        len += strlen(buf + len);
        if (len > 0 && buf[len - 1] == '\n') { buf[--len] = '\0'; break; }
        if (len + 1 < cap) break;                            /* short read: done */
        cap *= 2;                                            /* line continues */
        char* bigger = (char*)realloc(buf, cap);
        if (!bigger) { free(buf); fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
        buf = bigger;
    }
    if (len > 0 && buf[len - 1] == '\r') buf[len - 1] = '\0';  /* CRLF input */
    return buf;
}

/* Excecao global */
CryoException _cryo_exc = {.active = false};

/* ---------- Security: checked arithmetic ---------- */

static void _cryo_fatal(const char* kind, const char* detail) {
    fprintf(stderr, "[Cryo Security] %s: %s\n", kind, detail);
    abort();   /* aborts with core/backtrace instead of continuing corrupted */
}

int64_t cryo_add_ovf(int64_t a, int64_t b) {
    int64_t r;
    if (__builtin_add_overflow(a, b, &r))
        _cryo_fatal("Overflow", "integer addition overflow");
    return r;
}

int64_t cryo_sub_ovf(int64_t a, int64_t b) {
    int64_t r;
    if (__builtin_sub_overflow(a, b, &r))
        _cryo_fatal("Overflow", "integer subtraction overflow");
    return r;
}

int64_t cryo_mul_ovf(int64_t a, int64_t b) {
    int64_t r;
    if (__builtin_mul_overflow(a, b, &r))
        _cryo_fatal("Overflow", "integer multiplication overflow");
    return r;
}

int64_t cryo_idiv_chk(int64_t a, int64_t b) {
    if (b == 0)
        _cryo_fatal("DivByZero", "integer division by zero");
    if (a == INT64_MIN && b == -1)
        _cryo_fatal("Overflow", "INT64_MIN / -1 overflows");
    return a / b;
}

int64_t cryo_imod_chk(int64_t a, int64_t b) {
    if (b == 0)
        _cryo_fatal("DivByZero", "modulo by zero");
    if (a == INT64_MIN && b == -1)
        return 0;
    return a % b;
}

/* ---------- Security: assert ---------- */

void cryo_assert(bool cond, const char* msg) {
    if (!cond) {
        fprintf(stderr, "[Cryo Assert] %s\n", msg ? msg : "false condition");
        abort();
    }
}

/* ---------- Security: null pointer guard ---------- */

void* cryo_check_null(void* p, const char* what) {
    if (!p) {
        fprintf(stderr, "[Cryo Security] NullPointer: access to '%s' null\n",
                what ? what : "?");
        abort();
    }
    return p;
}

/* ---------- CryoArray ---------- */

CryoArray* cryo_array_new(void) {
    CryoArray* a = malloc(sizeof(CryoArray));
    if (!a) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    a->capacity = 8;
    a->length   = 0;
    a->data     = malloc(a->capacity * sizeof(uint64_t));
    return a;
}

void cryo_array_push(CryoArray* a, uint64_t v) {
    if (a->length >= a->capacity) {
        a->capacity *= 2;
        a->data = realloc(a->data, a->capacity * sizeof(uint64_t));
        if (!a->data) { fprintf(stderr, "[Cryo] realloc failed\n"); exit(1); }
    }
    a->data[a->length++] = v;
}

uint64_t cryo_array_get(CryoArray* a, int64_t i) {
    if (i < 0 || i >= a->length) {
        fprintf(stderr, "[Cryo] IndexError: indice %" PRId64
                        " fora dos limites (length=%" PRId64 ")\n", i, a->length);
        exit(1);
    }
    return a->data[i];
}

void cryo_array_set(CryoArray* a, int64_t i, uint64_t v) {
    if (i < 0 || i >= a->length) {
        fprintf(stderr, "[Cryo] IndexError: indice %" PRId64 " fora dos limites\n", i);
        exit(1);
    }
    a->data[i] = v;
}

CryoArray* cryo_array_slice(CryoArray* a, int64_t start, int64_t end) {
    if (start < 0) start = 0;
    if (end > a->length) end = a->length;
    CryoArray* out = cryo_array_new();
    for (int64_t i = start; i < end; i++)
        cryo_array_push(out, a->data[i]);
    return out;
}

void cryo_array_free(CryoArray* a) {
    if (a) { free(a->data); free(a); }
}

/* ── Phase 10.2 collection ops (ISSUES/09) ───────────────────
   CryoArray stores raw uint64_t, so the ELEMENT TYPE is a compile-time fact
   only. Anything needing equality, ordering or arithmetic therefore comes in
   per-type variants, exactly like cryo_push_i64/f64/str. Type-agnostic ops
   (reverse, concat, slice) need just one.

   All of these are NON-MUTATING: they return a fresh CryoArray and leave the
   source untouched, matching sort/reverse/slice/concat on the Pyro VM. */

static double _cryo_bits_to_f64(uint64_t u) { double d; memcpy(&d, &u, 8); return d; }

CryoArray* cryo_array_reverse(CryoArray* a) {
    CryoArray* out = cryo_array_new();
    for (int64_t i = a->length - 1; i >= 0; i--) cryo_array_push(out, a->data[i]);
    return out;
}

CryoArray* cryo_array_concat(CryoArray* a, CryoArray* b) {
    CryoArray* out = cryo_array_new();
    for (int64_t i = 0; i < a->length; i++) cryo_array_push(out, a->data[i]);
    for (int64_t i = 0; i < b->length; i++) cryo_array_push(out, b->data[i]);
    return out;
}

int64_t cryo_sum_i(CryoArray* a) {
    int64_t s = 0;
    for (int64_t i = 0; i < a->length; i++) s += (int64_t)a->data[i];
    return s;
}
double cryo_sum_f(CryoArray* a) {
    double s = 0;
    for (int64_t i = 0; i < a->length; i++) s += _cryo_bits_to_f64(a->data[i]);
    return s;
}

int64_t cryo_count_i(CryoArray* a, int64_t v) {
    int64_t n = 0;
    for (int64_t i = 0; i < a->length; i++) if ((int64_t)a->data[i] == v) n++;
    return n;
}
int64_t cryo_count_f(CryoArray* a, double v) {
    int64_t n = 0;
    for (int64_t i = 0; i < a->length; i++) if (_cryo_bits_to_f64(a->data[i]) == v) n++;
    return n;
}
int64_t cryo_count_s(CryoArray* a, const char* v) {
    int64_t n = 0;
    for (int64_t i = 0; i < a->length; i++) {
        const char* e = (const char*)(uintptr_t)a->data[i];
        if (e && v && strcmp(e, v) == 0) n++;
    }
    return n;
}

int64_t cryo_index_of_i(CryoArray* a, int64_t v) {
    for (int64_t i = 0; i < a->length; i++) if ((int64_t)a->data[i] == v) return i;
    return -1;
}
int64_t cryo_index_of_f(CryoArray* a, double v) {
    for (int64_t i = 0; i < a->length; i++) if (_cryo_bits_to_f64(a->data[i]) == v) return i;
    return -1;
}
int64_t cryo_index_of_s(CryoArray* a, const char* v) {
    for (int64_t i = 0; i < a->length; i++) {
        const char* e = (const char*)(uintptr_t)a->data[i];
        if (e && v && strcmp(e, v) == 0) return i;
    }
    return -1;
}

/* Stable insertion sort, matching the Pyro VM's stable ordering for equal keys
   (the Go VM uses SliceStable and the Pyro C runtime the same algorithm). */
#define _CRYO_SORT_BODY(CMP_LT)                                   \
    CryoArray* out = cryo_array_new();                            \
    for (int64_t i = 0; i < a->length; i++)                       \
        cryo_array_push(out, a->data[i]);                         \
    for (int64_t i = 1; i < out->length; i++) {                   \
        uint64_t key = out->data[i];                              \
        int64_t j = i - 1;                                        \
        while (j >= 0 && (CMP_LT)) { out->data[j+1] = out->data[j]; j--; } \
        out->data[j+1] = key;                                     \
    }                                                             \
    return out;

CryoArray* cryo_sort_i(CryoArray* a) {
    _CRYO_SORT_BODY((int64_t)key < (int64_t)out->data[j])
}
CryoArray* cryo_sort_f(CryoArray* a) {
    _CRYO_SORT_BODY(_cryo_bits_to_f64(key) < _cryo_bits_to_f64(out->data[j]))
}
CryoArray* cryo_sort_s(CryoArray* a) {
    _CRYO_SORT_BODY(strcmp((const char*)(uintptr_t)key,
                           (const char*)(uintptr_t)out->data[j]) < 0)
}
#undef _CRYO_SORT_BODY

/* ---------- Strings ---------- */

char* cryo_str_concat(const char* a, const char* b) {
    if (!a) a = "";
    if (!b) b = "";
    size_t len = strlen(a) + strlen(b) + 1;
    char* r = malloc(len);
    if (!r) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    strcpy(r, a); strcat(r, b);
    return r;
}

char* cryo_i64_to_str(int64_t n) {
    char* buf = malloc(32);
    /* int64_t is `long long` on Windows, where `long` is 32-bit: %ld would
       read the wrong width, and this MinGW's printf checker rejects %lld
       outright. PRId64 expands to whatever the target C library wants. */
    snprintf(buf, 32, "%" PRId64, n);
    return buf;
}

char* cryo_f64_to_str(double n) {
    char* buf = malloc(64);
    /* Remove zeros desnecessarios */
    snprintf(buf, 64, "%g", n);
    return buf;
}

char* cryo_bool_to_str(bool b) {
    /* Returns a static string — do not free */
    return b ? "true" : "false";
}

/* ---------- 11.27: rendering an array ----------
   One growable buffer rather than repeated cryo_str_concat: concatenating in a
   loop is quadratic and allocates a throwaway string per element, which for a
   large array is the difference between printing it and appearing to hang. */
typedef struct { char* p; size_t len, cap; } CryoSb;

static void cryo_sb_init(CryoSb* b) {
    b->cap = 64; b->len = 0; b->p = malloc(b->cap); if (b->p) b->p[0] = 0;
}

static void cryo_sb_add(CryoSb* b, const char* s) {
    if (!b->p || !s) return;
    size_t n = strlen(s);
    if (b->len + n + 1 > b->cap) {
        while (b->len + n + 1 > b->cap) b->cap *= 2;
        char* np = realloc(b->p, b->cap);
        if (!np) return;
        b->p = np;
    }
    memcpy(b->p + b->len, s, n + 1);
    b->len += n;
}

/* The shared shape. `one` renders element i; `owned` says whether it malloc'd,
   so a static "true"/"false" is not passed to free(). */
static char* cryo_arr_join(CryoArray* a, char* (*one)(CryoArray*, int64_t),
                           bool owned) {
    CryoSb b; cryo_sb_init(&b);
    cryo_sb_add(&b, "[");
    int64_t n = a ? a->length : 0;
    for (int64_t i = 0; i < n; i++) {
        if (i) cryo_sb_add(&b, ", ");
        char* s = one(a, i);
        cryo_sb_add(&b, s ? s : "null");
        if (owned && s) free(s);
    }
    cryo_sb_add(&b, "]");
    return b.p;
}

static char* cryo_arr_el_i(CryoArray* a, int64_t i) {
    return cryo_i64_to_str((int64_t)cryo_array_get(a, i));
}
static char* cryo_arr_el_f(CryoArray* a, int64_t i) {
    uint64_t u = cryo_array_get(a, i); double d; memcpy(&d, &u, 8);
    return cryo_f64_to_str(d);
}
static char* cryo_arr_el_s(CryoArray* a, int64_t i) {
    /* Strings are stored as pointers and are NOT quoted in the canonical
       form — "[a, b]", matching the VM and the go/node backends. */
    return (char*)(uintptr_t)cryo_array_get(a, i);
}
static char* cryo_arr_el_b(CryoArray* a, int64_t i) {
    return cryo_bool_to_str(cryo_array_get(a, i) != 0);
}

char* cryo_arr_to_str_i(CryoArray* a) { return cryo_arr_join(a, cryo_arr_el_i, true); }
char* cryo_arr_to_str_f(CryoArray* a) { return cryo_arr_join(a, cryo_arr_el_f, true); }
char* cryo_arr_to_str_s(CryoArray* a) { return cryo_arr_join(a, cryo_arr_el_s, false); }
char* cryo_arr_to_str_b(CryoArray* a) { return cryo_arr_join(a, cryo_arr_el_b, false); }

/* ---------- 11.27: optionals (T?) ---------- */

/* The exact text the Pyro VM prints. Not routed through _cryo_fatal, which
   formats as "kind: detail" — this message has no kind, and the two runtimes
   are compared byte for byte by test_c_vm.py. */
static void _cryo_unwrap_null(void) {
    fprintf(stderr, "[Cryo Security] unwrap of null value\n");
    abort();
}

int64_t* cryo_opt_i(int64_t v) {
    int64_t* p = malloc(sizeof(int64_t));
    if (!p) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    *p = v; return p;
}
double* cryo_opt_f(double v) {
    double* p = malloc(sizeof(double));
    if (!p) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    *p = v; return p;
}
bool* cryo_opt_b(bool v) {
    bool* p = malloc(sizeof(bool));
    if (!p) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    *p = v; return p;
}

int64_t cryo_unwrap_i(int64_t* p) { if (!p) _cryo_unwrap_null(); return *p; }
double  cryo_unwrap_f(double* p)  { if (!p) _cryo_unwrap_null(); return *p; }
bool    cryo_unwrap_b(bool* p)    { if (!p) _cryo_unwrap_null(); return *p; }
char*   cryo_unwrap_s(char* p)    { if (!p) _cryo_unwrap_null(); return p; }

int64_t cryo_str_len(const char* s) {
    return s ? (int64_t)strlen(s) : 0;
}

bool cryo_str_eq(const char* a, const char* b) {
    if (!a || !b) return a == b;
    return strcmp(a, b) == 0;
}

char* cryo_str_slice(const char* s, int64_t start, int64_t end) {
    int64_t slen = (int64_t)strlen(s);
    if (start < 0) start = 0;
    if (end > slen) end = slen;
    int64_t out_len = end - start;
    if (out_len < 0) out_len = 0;
    char* out = malloc(out_len + 1);
    strncpy(out, s + start, out_len);
    out[out_len] = '\0';
    return out;
}

char* cryo_str_upper(const char* s) {
    char* out = strdup(s);
    for (char* p = out; *p; p++) *p = (char)toupper((unsigned char)*p);
    return out;
}

char* cryo_str_lower(const char* s) {
    char* out = strdup(s);
    for (char* p = out; *p; p++) *p = (char)tolower((unsigned char)*p);
    return out;
}

/* ── Phase 10.4 strings (ISSUES/09) ──────────────────────────
   Semantics mirror the Pyro runtime (pyro/vm/pyro_runtime.c) so --backend c
   agrees with the VM.

   OWNERSHIP: every char*-returning helper here returns a freshly malloc'd
   string that the CALLER DOES NOT FREE — the generated C never frees, matching
   cryo_str_concat/upper/lower above. Short-lived programs trade the leak for a
   much simpler code generator; do not "fix" one of these in isolation. */

char* cryo_str_trim(const char* s) {
    if (!s) return strdup("");
    while (*s && isspace((unsigned char)*s)) s++;
    size_t len = strlen(s);
    while (len > 0 && isspace((unsigned char)s[len - 1])) len--;
    char* out = malloc(len + 1);
    if (!out) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    memcpy(out, s, len);
    out[len] = '\0';
    return out;
}

bool cryo_str_contains(const char* s, const char* sub) {
    if (!s || !sub) return false;
    return strstr(s, sub) != NULL;
}

int64_t cryo_str_find(const char* s, const char* sub) {
    if (!s || !sub) return -1;
    const char* at = strstr(s, sub);
    return at ? (int64_t)(at - s) : -1;
}

bool cryo_str_starts_with(const char* s, const char* p) {
    if (!s || !p) return false;
    size_t pl = strlen(p);
    return strlen(s) >= pl && strncmp(s, p, pl) == 0;
}

bool cryo_str_ends_with(const char* s, const char* p) {
    if (!s || !p) return false;
    size_t sl = strlen(s), pl = strlen(p);
    return sl >= pl && strcmp(s + sl - pl, p) == 0;
}

char* cryo_str_repeat(const char* s, int64_t n) {
    if (!s) s = "";
    if (n < 0) n = 0;
    size_t sl = strlen(s), total = sl * (size_t)n;
    char* out = malloc(total + 1);
    if (!out) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    for (int64_t i = 0; i < n; i++) memcpy(out + (size_t)i * sl, s, sl);
    out[total] = '\0';
    return out;
}

/* pad_start/pad_end: JS padStart/padEnd — the pad is repeated and TRUNCATED to
   fill exactly (width - len) bytes. */
static char* cryo_str_pad(const char* s, int64_t width, const char* pad, bool at_start) {
    if (!s) s = "";
    if (!pad) pad = "";
    size_t sl = strlen(s), pl = strlen(pad);
    if ((int64_t)sl >= width || pl == 0) return strdup(s);
    size_t need = (size_t)width - sl;
    char* out = malloc(need + sl + 1);
    if (!out) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    if (at_start) {
        for (size_t i = 0; i < need; i++) out[i] = pad[i % pl];
        memcpy(out + need, s, sl);
    } else {
        memcpy(out, s, sl);
        for (size_t i = 0; i < need; i++) out[sl + i] = pad[i % pl];
    }
    out[need + sl] = '\0';
    return out;
}
char* cryo_str_pad_start(const char* s, int64_t w, const char* p) { return cryo_str_pad(s, w, p, true); }
char* cryo_str_pad_end  (const char* s, int64_t w, const char* p) { return cryo_str_pad(s, w, p, false); }

/* replace ALL occurrences. Sized exactly up front (count the matches) instead of
   growing, so there is a single allocation and no realloc dance. An empty `old`
   inserts `rep` at all character boundaries — matching the Pyro runtime and Go. */
char* cryo_str_replace(const char* s, const char* old, const char* rep) {
    if (!s) s = "";
    if (!old) old = "";
    if (!rep) rep = "";
    size_t ol = strlen(old);
    if (ol == 0) {
        size_t sl = strlen(s), rl = strlen(rep);
        size_t total = (sl + 1) * rl + sl;
        char* out = malloc(total + 1);
        if (!out) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
        char* w = out;
        for (size_t i = 0; i < sl; i++) {
            memcpy(w, rep, rl); w += rl;
            *w++ = s[i];
        }
        memcpy(w, rep, rl); w += rl;
        *w = '\0';
        return out;
    }
    size_t rl = strlen(rep), n = 0;
    for (const char* p = s; (p = strstr(p, old)) != NULL; p += ol) n++;
    if (n == 0) return strdup(s);
    size_t out_len = strlen(s) + n * (rl > ol ? rl - ol : 0) - n * (ol > rl ? ol - rl : 0);
    char* out = malloc(out_len + 1);
    if (!out) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    char* w = out;
    for (const char* p = s;;) {
        const char* at = strstr(p, old);
        if (!at) { size_t t = strlen(p); memcpy(w, p, t); w += t; break; }
        size_t pre = (size_t)(at - p);
        memcpy(w, p, pre); w += pre;
        memcpy(w, rep, rl); w += rl;
        p = at + ol;
    }
    *w = '\0';
    return out;
}

/* split -> string[] (a CryoArray of char*). An empty separator splits into
   single characters, matching split_str() in the Pyro runtime. */
CryoArray* cryo_str_split(const char* s, const char* sep) {
    if (!s) s = "";
    if (!sep) sep = "";
    CryoArray* arr = cryo_array_new();
    size_t seplen = strlen(sep);
    if (seplen == 0) {
        for (const char* p = s; *p; p++) {
            char* ch = malloc(2);
            if (!ch) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
            ch[0] = *p; ch[1] = '\0';
            cryo_push_str(arr, ch);
        }
        return arr;
    }
    const char* curr = s;
    const char* next;
    while ((next = strstr(curr, sep)) != NULL) {
        size_t n = (size_t)(next - curr);
        char* part = malloc(n + 1);
        if (!part) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
        memcpy(part, curr, n); part[n] = '\0';
        cryo_push_str(arr, part);
        curr = next + seplen;
    }
    cryo_push_str(arr, strdup(curr));      /* trailing piece (may be "") */
    return arr;
}

char* cryo_str_join(CryoArray* a, const char* sep) {
    if (!sep) sep = "";
    size_t seplen = strlen(sep), total = 0;
    for (int64_t i = 0; i < a->length; i++) {
        const char* e = (const char*)(uintptr_t)a->data[i];
        total += e ? strlen(e) : 0;
    }
    if (a->length > 1) total += seplen * (size_t)(a->length - 1);
    char* out = malloc(total + 1);
    if (!out) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    char* w = out;
    for (int64_t i = 0; i < a->length; i++) {
        if (i > 0) { memcpy(w, sep, seplen); w += seplen; }
        const char* e = (const char*)(uintptr_t)a->data[i];
        size_t n = e ? strlen(e) : 0;
        if (n) { memcpy(w, e, n); w += n; }
    }
    *w = '\0';
    return out;
}

/* ---------- Print ---------- */

void cryo_print_str(const char* s)  { puts(s ? s : "(null)"); }
void cryo_print_i64(int64_t n)      { printf("%" PRId64 "\n", n); }
void cryo_print_f64(double n)       { printf("%g\n", n); }
void cryo_print_bool(bool b)        { puts(b ? "true" : "false"); }
void cryo_print_newline(void)       { putchar('\n'); }

/* ---------- Input ---------- */

char* cryo_input(const char* prompt) {
    if (prompt && *prompt) { printf("%s", prompt); fflush(stdout); }
    return cryo_read_line(stdin);   /* never NULL; "" at EOF */
}

int64_t cryo_input_int(const char* prompt) {
    char* s = cryo_input(prompt);
    int64_t v = strtoll(s, NULL, 10);
    free(s);
    return v;
}

double cryo_input_num(const char* prompt) {
    char* s = cryo_input(prompt);
    double v = strtod(s, NULL);
    free(s);
    return v;
}

/* ---------- Filesystem & Process ---------- */
#include <sys/stat.h>
#if defined(_WIN32)
#include <direct.h>
#include <windows.h>
#else
#include <unistd.h>
#include <dirent.h>
#endif

bool cryo_file_exists(const char* path) {
    if (!path) return false;
    struct stat st;
    return stat(path, &st) == 0;
}

bool cryo_is_dir(const char* path) {
    if (!path) return false;
    struct stat st;
    if (stat(path, &st) != 0) return false;
#if defined(_WIN32)
    return (st.st_mode & _S_IFDIR) != 0;
#else
    return S_ISDIR(st.st_mode);
#endif
}

static int _cryo_name_cmp(const void* a, const void* b) {
    return strcmp(*(const char**)a, *(const char**)b);
}

CryoArray* cryo_list_dir(const char* path) {
    CryoArray* out = cryo_array_new();
    if (!path) return out;
#if defined(_WIN32)
    char pattern[MAX_PATH];
    snprintf(pattern, sizeof(pattern), "%s\\*", path);
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pattern, &fd);
    if (h == INVALID_HANDLE_VALUE) return out;
    char** names = NULL;
    int n = 0, cap = 0;
    do {
        if (strcmp(fd.cFileName, ".") == 0 || strcmp(fd.cFileName, "..") == 0) continue;
        if (n == cap) {
            cap = cap ? cap * 2 : 16;
            names = (char**)realloc(names, (size_t)cap * sizeof(char*));
        }
        names[n++] = cryo_strdup(fd.cFileName);
    } while (FindNextFileA(h, &fd));
    FindClose(h);
    if (names) {
        qsort(names, (size_t)n, sizeof(char*), _cryo_name_cmp);
        for (int i = 0; i < n; i++) {
            cryo_push_str(out, names[i]);
        }
        free(names);
    }
#else
    DIR* d = opendir(path);
    if (!d) return out;
    char** names = NULL;
    int n = 0, cap = 0;
    struct dirent* e;
    while ((e = readdir(d)) != NULL) {
        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0) continue;
        if (n == cap) {
            cap = cap ? cap * 2 : 16;
            names = (char**)realloc(names, (size_t)cap * sizeof(char*));
        }
        names[n++] = cryo_strdup(e->d_name);
    }
    closedir(d);
    if (names) {
        qsort(names, (size_t)n, sizeof(char*), _cryo_name_cmp);
        for (int i = 0; i < n; i++) {
            cryo_push_str(out, names[i]);
        }
        free(names);
    }
#endif
    return out;
}

bool cryo_make_dir(const char* path) {
    if (!path) return false;
#if defined(_WIN32)
    return _mkdir(path) == 0 || cryo_is_dir(path);
#else
    return mkdir(path, 0755) == 0 || cryo_is_dir(path);
#endif
}

bool cryo_delete_file(const char* path) {
    if (!path || cryo_is_dir(path)) return false;
    return remove(path) == 0;
}

int64_t cryo_file_size(const char* path) {
    if (!path) return -1;
    struct stat st;
    if (stat(path, &st) != 0) return -1;
    return (int64_t)st.st_size;
}

bool cryo_write_file(const char* path, const char* content) {
    if (!path) return false;
    if (!content) content = "";
    FILE* fp = fopen(path, "wb");
    if (!fp) return false;
    size_t len = strlen(content);
    size_t wrote = fwrite(content, 1, len, fp);
    fclose(fp);
    return wrote == len;
}

char* cryo_read_file(const char* path) {
    if (!path) return cryo_strdup("");
    FILE* fp = fopen(path, "rb");
    if (!fp) return cryo_strdup("");
    fseek(fp, 0, SEEK_END);
    long n = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (n < 0) { fclose(fp); return cryo_strdup(""); }
    char* buf = (char*)malloc((size_t)n + 1);
    if (!buf) { fclose(fp); return cryo_strdup(""); }
    size_t got = fread(buf, 1, (size_t)n, fp);
    fclose(fp);
    buf[got] = '\0';
    return buf;
}

char* cryo_env(const char* name) {
    if (!name) return cryo_strdup("");
    char* val = getenv(name);
    return cryo_strdup(val ? val : "");
}

char* cryo_exec(const char* cmd) {
    if (!cmd || !*cmd) return cryo_strdup("");
    FILE* fp = NULL;
#if defined(_WIN32)
    fp = _popen(cmd, "r");
#else
    fp = popen(cmd, "r");
#endif
    if (!fp) return cryo_strdup("");
    char* res = cryo_read_line(fp);
#if defined(_WIN32)
    _pclose(fp);
#else
    pclose(fp);
#endif
    return res;
}

/* ---------- 11.27: maps (map<K,V>) ----------
   Open addressing with linear probing. Small, and the whole table is one
   allocation, which matters more here than the theoretical wins of chaining:
   Cryo maps are usually tens of entries, not millions. */

static uint64_t _cryo_map_hash(CryoMap* m, uint64_t k) {
    if (m->str_keys) {
        const char* s = (const char*)(uintptr_t)k;
        uint64_t h = 1469598103934665603ULL;      /* FNV-1a */
        if (s) for (; *s; s++) { h ^= (unsigned char)*s; h *= 1099511628211ULL; }
        return h;
    }
    /* integer keys: a mix, so that consecutive ints do not all probe together */
    uint64_t h = k;
    h ^= h >> 33; h *= 0xff51afd7ed558ccdULL;
    h ^= h >> 33; h *= 0xc4ceb9fe1a85ec53ULL;
    h ^= h >> 33;
    return h;
}

static int _cryo_map_keyeq(CryoMap* m, uint64_t a, uint64_t b) {
    if (!m->str_keys) return a == b;
    const char* x = (const char*)(uintptr_t)a;
    const char* y = (const char*)(uintptr_t)b;
    if (!x || !y) return x == y;
    return strcmp(x, y) == 0;
}

CryoMap* cryo_map_new(int str_keys) {
    CryoMap* m = malloc(sizeof(CryoMap));
    if (!m) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    m->cap = 16; m->len = 0; m->str_keys = str_keys;
    m->e = calloc((size_t)m->cap, sizeof(CryoMapEntry));
    if (!m->e) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    return m;
}

static int64_t _cryo_map_slot(CryoMap* m, uint64_t k) {
    int64_t mask = m->cap - 1;
    int64_t i = (int64_t)(_cryo_map_hash(m, k) & (uint64_t)mask);
    while (m->e[i].used && !_cryo_map_keyeq(m, m->e[i].key, k)) {
        i = (i + 1) & mask;
    }
    return i;
}

static void _cryo_map_grow(CryoMap* m) {
    int64_t oldcap = m->cap;
    CryoMapEntry* old = m->e;
    m->cap *= 2;
    m->e = calloc((size_t)m->cap, sizeof(CryoMapEntry));
    if (!m->e) { fprintf(stderr, "[Cryo] malloc failed\n"); exit(1); }
    m->len = 0;
    for (int64_t i = 0; i < oldcap; i++) {
        if (old[i].used) {
            int64_t j = _cryo_map_slot(m, old[i].key);
            m->e[j] = old[i];
            m->len++;
        }
    }
    free(old);
}

void cryo_map_set(CryoMap* m, uint64_t k, uint64_t v) {
    /* Grown at half full. Linear probing degrades badly past ~70%, and this
       keeps the probe runs short without much memory. */
    if ((m->len + 1) * 2 > m->cap) _cryo_map_grow(m);
    int64_t i = _cryo_map_slot(m, k);
    if (!m->e[i].used) { m->e[i].used = 1; m->e[i].key = k; m->len++; }
    m->e[i].val = v;
}

bool cryo_map_has(CryoMap* m, uint64_t k) {
    if (!m) return false;
    int64_t i = _cryo_map_slot(m, k);
    return m->e[i].used ? true : false;
}

uint64_t cryo_map_get(CryoMap* m, uint64_t k) {
    if (!m) return 0;
    int64_t i = _cryo_map_slot(m, k);
    return m->e[i].used ? m->e[i].val : 0;
}

void cryo_map_remove(CryoMap* m, uint64_t k) {
    if (!m) return;
    int64_t i = _cryo_map_slot(m, k);
    if (!m->e[i].used) return;
    m->e[i].used = 0;
    m->len--;
    /* Re-insert the rest of this probe run. A tombstone would be simpler, but
       leaving a hole here breaks lookups for any key that probed PAST this
       slot — the classic open-addressing deletion bug. */
    int64_t mask = m->cap - 1;
    int64_t j = (i + 1) & mask;
    while (m->e[j].used) {
        CryoMapEntry moved = m->e[j];
        m->e[j].used = 0;
        m->len--;
        cryo_map_set(m, moved.key, moved.val);
        j = (j + 1) & mask;
    }
}

int64_t cryo_map_len(CryoMap* m) { return m ? m->len : 0; }

/* Keys as text, for ordering. Both the VM and the go backend order a map by
   the key's own textual form, so this has to agree with them. */
static char* _cryo_key_text(CryoMap* m, uint64_t k) {
    if (m->str_keys) {
        const char* s = (const char*)(uintptr_t)k;
        return s ? (char*)s : (char*)"null";
    }
    return cryo_i64_to_str((int64_t)k);
}

static int _cryo_key_cmp(CryoMap* m, uint64_t a, uint64_t b) {
    char* x = _cryo_key_text(m, a);
    char* y = _cryo_key_text(m, b);
    int r = strcmp(x, y);
    if (!m->str_keys) { free(x); free(y); }
    return r;
}

CryoArray* cryo_map_keys(CryoMap* m) {
    CryoArray* out = cryo_array_new();
    if (!m) return out;
    for (int64_t i = 0; i < m->cap; i++) {
        if (m->e[i].used) cryo_array_push(out, m->e[i].key);
    }
    /* insertion sort: key counts here are small, and it keeps the comparator
       (which may allocate) out of qsort's context-free callback */
    for (int64_t i = 1; i < out->length; i++) {
        uint64_t v = out->data[i];
        int64_t j = i - 1;
        while (j >= 0 && _cryo_key_cmp(m, out->data[j], v) > 0) {
            out->data[j + 1] = out->data[j];
            j--;
        }
        out->data[j + 1] = v;
    }
    return out;
}

char* cryo_map_to_str(CryoMap* m, int val_kind) {
    CryoSb b; cryo_sb_init(&b);
    cryo_sb_add(&b, "{");
    CryoArray* ks = cryo_map_keys(m);
    for (int64_t i = 0; i < ks->length; i++) {
        if (i) cryo_sb_add(&b, ", ");
        uint64_t k = ks->data[i];
        char* kt = _cryo_key_text(m, k);
        cryo_sb_add(&b, kt);
        if (!m->str_keys) free(kt);
        cryo_sb_add(&b, ": ");
        uint64_t v = cryo_map_get(m, k);
        if (val_kind == 0) { char* s = cryo_i64_to_str((int64_t)v); cryo_sb_add(&b, s); free(s); }
        else if (val_kind == 1) { double d; memcpy(&d, &v, 8); char* s = cryo_f64_to_str(d); cryo_sb_add(&b, s); free(s); }
        else if (val_kind == 2) { const char* s = (const char*)(uintptr_t)v; cryo_sb_add(&b, s ? s : "null"); }
        else { cryo_sb_add(&b, v ? "true" : "false"); }
    }
    cryo_sb_add(&b, "}");
    cryo_array_free(ks);
    return b.p;
}
