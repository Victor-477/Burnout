// ============================================================
//  Cryo runtime for the C++ backend.  Header-only, C++14.
//
//  C++14 and not 17 on purpose: the oldest toolchain this has been
//  built with is MinGW GCC 6.3, which predates std::optional. A
//  nullable value is a shared_ptr instead — which is also what the
//  go backend uses for `T?`, so the three agree on representation
//  as well as on behaviour.
//
//  REFERENCE SEMANTICS. PYRO_RUNTIME.md §2 says arrays, maps and
//  structs are reference-counted objects and passing one shares it.
//  A bare std::vector would copy on assignment, so `int[] b = a;
//  b.push(1);` would leave `a` untouched here and changed on every
//  other backend. Every container is therefore a shared_ptr, and
//  that is the reason the generated code is full of them.
//
//  What is here is only what the standard library does not give:
//  Cryo's output format, and aborts whose wording matches the VM.
// ============================================================
#ifndef CRYO_RUNTIME_HPP
#define CRYO_RUNTIME_HPP

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace cryo {

template <class T> using Arr = std::shared_ptr<std::vector<T> >;
template <class K, class V> using Map = std::shared_ptr<std::map<K, V> >;
template <class T> using Opt = std::shared_ptr<T>;

// ── aborts ──
//
// The envelope is this engine's to choose — the VM prefixes "[Pyro VM] ",
// go panics, node prints nothing. What has to agree across backends is the
// message from the marker onwards (12.13).
inline void abort_with(const std::string& msg) {
    std::cout.flush();
    std::cerr << "[Cryo C++] " << msg << std::endl;
    std::exit(1);
}

// ── rendering (must match the Pyro VM exactly) ──

inline std::string str(bool v) { return v ? "true" : "false"; }

inline std::string str(int64_t v) {
    std::ostringstream o;
    o << v;
    return o.str();
}
inline std::string str(int v) { return str(static_cast<int64_t>(v)); }

inline std::string str(double d) {
    if (std::isnan(d)) return "NaN";
    if (std::isinf(d)) return d > 0 ? "+Inf" : "-Inf";
    // 2.0 prints as "2", not "2.0": the VM renders a float with no fractional
    // part as an integer, and test_parity asserts `print([1.5, 2.0])` as
    // "[1.5, 2]".
    if (d == std::floor(d) && std::fabs(d) < 1e15) {
        std::ostringstream o;
        o << static_cast<int64_t>(d);
        return o.str();
    }
    // Shortest form that round-trips, which is what §3.1 specifies. 17 digits
    // always round-trips; trailing zeros are then trimmed so 1.5 does not come
    // out as 1.5000000000000000.
    std::ostringstream o;
    o.precision(17);
    o << d;
    std::string s = o.str();
    if (s.find('.') != std::string::npos && s.find('e') == std::string::npos &&
        s.find('E') == std::string::npos) {
        // Try shorter precisions and keep the first that reads back equal.
        for (int p = 1; p <= 17; ++p) {
            std::ostringstream t;
            t.precision(p);
            t << d;
            double back = std::atof(t.str().c_str());
            if (back == d) return t.str();
        }
    }
    return s;
}

inline std::string str(const std::string& v) { return v; }
inline std::string str(const char* v) { return std::string(v); }
inline std::string str(std::nullptr_t) { return "null"; }

template <class T> std::string str(const Arr<T>& a);
template <class K, class V> std::string str(const Map<K, V>& m);
template <class T> std::string str(const std::shared_ptr<T>& p);

template <class T> std::string str(const Arr<T>& a) {
    if (!a) return "null";
    std::string out = "[";
    for (size_t i = 0; i < a->size(); ++i) {
        if (i) out += ", ";
        out += str((*a)[i]);
    }
    return out + "]";
}

template <class K, class V> std::string str(const Map<K, V>& m) {
    if (!m) return "null";
    // Ordered by the key's own TEXT, so int keys come out 1, 10, 2. That looks
    // wrong at a glance and is exactly what the VM does; std::map's own order
    // is by key VALUE, which would print the same program differently here.
    std::vector<std::pair<std::string, std::string> > pairs;
    for (typename std::map<K, V>::const_iterator it = m->begin();
         it != m->end(); ++it) {
        pairs.push_back(std::make_pair(str(it->first), str(it->second)));
    }
    std::sort(pairs.begin(), pairs.end(),
              [](const std::pair<std::string, std::string>& a,
                 const std::pair<std::string, std::string>& b) {
                  return a.first < b.first;
              });
    std::string out = "{";
    for (size_t i = 0; i < pairs.size(); ++i) {
        if (i) out += ", ";
        out += pairs[i].first + ": " + pairs[i].second;
    }
    return out + "}";
}

// An optional, or a struct handle. A struct gets its own str() emitted by the
// code generator (it has to name the fields), which is more specialised than
// this and so wins overload resolution.
template <class T> std::string str(const std::shared_ptr<T>& p) {
    if (!p) return "null";
    return str(*p);
}

template <class T> void print(const T& v) { std::cout << str(v) << std::endl; }

// ── throw / catch ──
//
// The value a catch binds is the message STRING, not an exception object —
// node bound an Error once and printing it gave "{}" (12.12).
struct Thrown {
    std::string value;
    explicit Thrown(const std::string& v) : value(v) {}
};

// ── integer arithmetic ──

inline int64_t idiv(int64_t a, int64_t b) {
    if (b == 0) abort_with("[Cryo Security] DivByZero: integer division");
    return a / b;
}
inline int64_t imod(int64_t a, int64_t b) {
    if (b == 0) abort_with("[Cryo Security] DivByZero: integer division");
    return a % b;
}

// ── indexing, with the VM's bounds messages (12.13) ──

template <class T> T& at(const Arr<T>& a, int64_t i) {
    if (!a || i < 0 || static_cast<size_t>(i) >= a->size()) {
        std::ostringstream o;
        o << "[Cryo Security] IndexError: index " << i << " out of bounds (len="
          << (a ? a->size() : 0) << ")";
        abort_with(o.str());
    }
    return (*a)[static_cast<size_t>(i)];
}

// The VM's SET message carries no (len=…). That asymmetry is the shape to
// match, not to tidy up.
template <class T> void set_at(const Arr<T>& a, int64_t i, const T& v) {
    if (!a || i < 0 || static_cast<size_t>(i) >= a->size()) {
        std::ostringstream o;
        o << "[Cryo Security] IndexError: index " << i << " out of bounds";
        abort_with(o.str());
    }
    (*a)[static_cast<size_t>(i)] = v;
}

inline std::string char_at(const std::string& s, int64_t i) {
    if (i < 0 || static_cast<size_t>(i) >= s.size())
        abort_with("[Cryo Security] IndexError: string index out of bounds");
    return std::string(1, s[static_cast<size_t>(i)]);
}

template <class T> T unwrap(const std::shared_ptr<T>& p) {
    if (!p) abort_with("[Cryo Security] unwrap of null value");
    return *p;
}

template <class T> std::shared_ptr<T> opt(const T& v) {
    return std::make_shared<T>(v);
}

// `a ?? b` — the left side is evaluated ONCE, which a plain conditional in the
// generated code would not guarantee (11.27 hit exactly this in C).
template <class T> T or_else(const std::shared_ptr<T>& a, const T& b) {
    return a ? *a : b;
}

// ── constructors ──

template <class T> Arr<T> arr() { return std::make_shared<std::vector<T> >(); }
template <class T> Arr<T> arr(const std::vector<T>& xs) {
    return std::make_shared<std::vector<T> >(xs);
}
template <class K, class V> Map<K, V> mapof() {
    return std::make_shared<std::map<K, V> >();
}
template <class K, class V> Map<K, V> mapof(const std::map<K, V>& m) {
    return std::make_shared<std::map<K, V> >(m);
}

template <class T> int64_t len(const Arr<T>& a) {
    return a ? static_cast<int64_t>(a->size()) : 0;
}
template <class K, class V> int64_t len(const Map<K, V>& m) {
    return m ? static_cast<int64_t>(m->size()) : 0;
}
inline int64_t len(const std::string& s) {
    return static_cast<int64_t>(s.size());
}

template <class T> int64_t push(const Arr<T>& a, const T& v) {
    a->push_back(v);
    return static_cast<int64_t>(a->size());
}

// ── conversions ──

inline int64_t to_int(const std::string& s) {
    char* end = nullptr;
    long long v = std::strtoll(s.c_str(), &end, 10);
    if (end == s.c_str() || *end != '\0') {
        char* e2 = nullptr;
        double d = std::strtod(s.c_str(), &e2);
        if (e2 != s.c_str() && *e2 == '\0') return static_cast<int64_t>(d);
        abort_with("[Cryo Security] to_int: '" + s + "' is not a valid integer");
    }
    return static_cast<int64_t>(v);
}
inline double to_num(const std::string& s) {
    char* end = nullptr;
    double d = std::strtod(s.c_str(), &end);
    if (end == s.c_str() || *end != '\0')
        abort_with("[Cryo Security] to_number: '" + s + "' is not a valid number");
    return d;
}

// ── strings ──

inline std::string upper(std::string s) {
    for (size_t i = 0; i < s.size(); ++i) s[i] = std::toupper((unsigned char)s[i]);
    return s;
}
inline std::string lower(std::string s) {
    for (size_t i = 0; i < s.size(); ++i) s[i] = std::tolower((unsigned char)s[i]);
    return s;
}
inline bool is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}
inline std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && is_space(s[a])) ++a;
    while (b > a && is_space(s[b - 1])) --b;
    return s.substr(a, b - a);
}
inline bool contains(const std::string& s, const std::string& sub) {
    return s.find(sub) != std::string::npos;
}
inline int64_t find(const std::string& s, const std::string& sub) {
    size_t p = s.find(sub);
    return p == std::string::npos ? -1 : static_cast<int64_t>(p);
}
inline bool starts_with(const std::string& s, const std::string& p) {
    return s.size() >= p.size() && s.compare(0, p.size(), p) == 0;
}
inline bool ends_with(const std::string& s, const std::string& p) {
    return s.size() >= p.size() &&
           s.compare(s.size() - p.size(), p.size(), p) == 0;
}
inline std::string repeat(const std::string& s, int64_t n) {
    std::string out;
    for (int64_t i = 0; i < n; ++i) out += s;
    return out;
}
inline std::string pad_start(const std::string& s, int64_t w, const std::string& p) {
    if (p.empty() || static_cast<int64_t>(s.size()) >= w) return s;
    std::string fill;
    while (static_cast<int64_t>(fill.size() + s.size()) < w) fill += p;
    return fill.substr(0, static_cast<size_t>(w - (int64_t)s.size())) + s;
}
inline std::string pad_end(const std::string& s, int64_t w, const std::string& p) {
    if (p.empty() || static_cast<int64_t>(s.size()) >= w) return s;
    std::string out = s;
    while (static_cast<int64_t>(out.size()) < w) out += p;
    return out.substr(0, static_cast<size_t>(w));
}
inline std::string substr(const std::string& s, int64_t start, int64_t n) {
    if (start < 0) start = 0;
    if (start > (int64_t)s.size()) start = (int64_t)s.size();
    int64_t end = start + (n < 0 ? 0 : n);
    if (end > (int64_t)s.size()) end = (int64_t)s.size();
    return s.substr((size_t)start, (size_t)(end - start));
}
inline std::string slice_str(const std::string& s, int64_t a, int64_t b) {
    if (a < 0) a = 0;
    if (b > (int64_t)s.size()) b = (int64_t)s.size();
    if (b < a) b = a;
    return s.substr((size_t)a, (size_t)(b - a));
}
inline std::string replace_all(const std::string& s, const std::string& old,
                               const std::string& rep) {
    // ISSUES/17 — an EMPTY needle inserts the replacement at every position
    // boundary: replace("abc","","-") is "-a-b-c-" and replace("","","-") is
    // "-". Looping on find() with an empty needle would never terminate.
    if (old.empty()) {
        std::string out;
        for (size_t i = 0; i < s.size(); ++i) { out += rep; out += s[i]; }
        return out + rep;
    }
    std::string out;
    size_t pos = 0;
    while (true) {
        size_t at = s.find(old, pos);
        if (at == std::string::npos) { out += s.substr(pos); break; }
        out += s.substr(pos, at - pos);
        out += rep;
        pos = at + old.size();
    }
    return out;
}
inline Arr<std::string> split(const std::string& s, const std::string& sep) {
    Arr<std::string> out = arr<std::string>();
    // An empty separator splits into characters, matching split_str() in the
    // Pyro runtime — which is what chars() relies on.
    if (sep.empty()) {
        for (size_t i = 0; i < s.size(); ++i) out->push_back(std::string(1, s[i]));
        return out;
    }
    size_t pos = 0;
    while (true) {
        size_t at = s.find(sep, pos);
        if (at == std::string::npos) { out->push_back(s.substr(pos)); break; }
        out->push_back(s.substr(pos, at - pos));
        pos = at + sep.size();
    }
    return out;
}
inline std::string join(const Arr<std::string>& xs, const std::string& sep) {
    std::string out;
    for (size_t i = 0; i < xs->size(); ++i) {
        if (i) out += sep;
        out += (*xs)[i];
    }
    return out;
}

// ── math ──

inline int64_t gcd(int64_t a, int64_t b) {
    if (a < 0) a = -a;
    if (b < 0) b = -b;
    while (b != 0) { int64_t t = a % b; a = b; b = t; }
    return a;
}
inline int64_t sign_i(int64_t v) { return v > 0 ? 1 : (v < 0 ? -1 : 0); }
inline int64_t clamp_i(int64_t v, int64_t lo, int64_t hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
inline double clamp_f(double v, double lo, double hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
// Away from zero, not C's rint: round(2.5) is 3 on every other backend.
inline double round_half_up(double d) {
    return d < 0 ? -std::floor(-d + 0.5) : std::floor(d + 0.5);
}

// ── collections ──

template <class T> Arr<T> sorted(const Arr<T>& xs) {
    Arr<T> c = std::make_shared<std::vector<T> >(*xs);
    std::sort(c->begin(), c->end());
    return c;
}
// Strings order by their text, which is std::sort's default for std::string —
// numbers order numerically, also the default. The VM's rule falls out.
template <class T> Arr<T> reversed(const Arr<T>& xs) {
    Arr<T> c = std::make_shared<std::vector<T> >(*xs);
    std::reverse(c->begin(), c->end());
    return c;
}
template <class T> Arr<T> slice(const Arr<T>& xs, int64_t a, int64_t b) {
    int64_t n = (int64_t)xs->size();
    if (a < 0) a = 0;
    if (b > n) b = n;
    if (b < a) b = a;
    Arr<T> c = arr<T>();
    for (int64_t i = a; i < b; ++i) c->push_back((*xs)[(size_t)i]);
    return c;
}
template <class T> Arr<T> concat(const Arr<T>& a, const Arr<T>& b) {
    Arr<T> c = std::make_shared<std::vector<T> >(*a);
    c->insert(c->end(), b->begin(), b->end());
    return c;
}
template <class T> int64_t index_of(const Arr<T>& xs, const T& v) {
    for (size_t i = 0; i < xs->size(); ++i)
        if ((*xs)[i] == v) return (int64_t)i;
    return -1;
}
template <class T> int64_t count_of(const Arr<T>& xs, const T& v) {
    int64_t n = 0;
    for (size_t i = 0; i < xs->size(); ++i) if ((*xs)[i] == v) ++n;
    return n;
}
template <class T> T sum(const Arr<T>& xs) {
    T n = T();
    for (size_t i = 0; i < xs->size(); ++i) n += (*xs)[i];
    return n;
}

// keys() returns them sorted by TEXT, which is what the VM guarantees and what
// `print(keys(m))` is asserted on — not std::map's order by key value.
template <class K, class V> Arr<K> keys(const Map<K, V>& m) {
    std::vector<std::pair<std::string, K> > ks;
    for (typename std::map<K, V>::const_iterator it = m->begin();
         it != m->end(); ++it)
        ks.push_back(std::make_pair(str(it->first), it->first));
    std::sort(ks.begin(), ks.end(),
              [](const std::pair<std::string, K>& a,
                 const std::pair<std::string, K>& b) {
                  return a.first < b.first;
              });
    Arr<K> out = arr<K>();
    for (size_t i = 0; i < ks.size(); ++i) out->push_back(ks[i].second);
    return out;
}
// A missing key reads as the zero value, matching the VM's `m[k]` -> null
// rather than inserting the way std::map's operator[] would.
template <class K, class V> V map_get(const Map<K, V>& m, const K& k) {
    typename std::map<K, V>::const_iterator it = m->find(k);
    return it == m->end() ? V() : it->second;
}
template <class K, class V> void map_set(const Map<K, V>& m, const K& k, const V& v) {
    (*m)[k] = v;
}
template <class K, class V> bool has(const Map<K, V>& m, const K& k) {
    return m->find(k) != m->end();
}
template <class K, class V> void remove(const Map<K, V>& m, const K& k) {
    m->erase(k);
}

}  // namespace cryo

#endif  // CRYO_RUNTIME_HPP
