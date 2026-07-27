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
