// ============================================================
//  Cryo runtime for the C# backend.
//
//  Kept as a real .cs file rather than a string inside the code
//  generator so it can be read, edited and compiled on its own —
//  the same reason cryo_runtime.c sits beside codegen_c.py. The
//  generator embeds this text into the file it emits, so a
//  generated program stays a single self-contained .cs.
//
//  What is here is ONLY what .NET does not already provide: Cryo's
//  output format, and the aborts whose wording has to match the
//  Pyro VM. Everything else (string, List<T>, Dictionary<K,V>) is
//  the standard library, which is why this is a few hundred lines
//  where the C runtime is a few thousand.
//
//  Every conversion pins InvariantCulture. Without it a `number`
//  prints "2,5" on a machine with a comma decimal separator and
//  the same program produces different output on two computers —
//  invariant 1 broken by the host's locale.
// ============================================================
// A Cryo `throw` / failed `assert`. Carries the message STRING, because
// `catch (string e)` binds the message and not an exception object — node
// bound an Error once and printing it gave "{}" (12.12).
public class CryoThrow : Exception {
    public string Value;
    public CryoThrow(string v) : base(v) { Value = v; }
}

public static class Cryo {
    // ── rendering (must match the Pyro VM exactly) ──

    public static string Num(double d) {
        if (double.IsNaN(d)) return "NaN";
        if (double.IsPositiveInfinity(d)) return "+Inf";
        if (double.IsNegativeInfinity(d)) return "-Inf";
        // 2.0 prints as "2", not "2.0": the VM renders a float with no
        // fractional part as an integer, and test_parity asserts
        // `print([1.5, 2.0])` as "[1.5, 2]".
        if (d == System.Math.Floor(d) && System.Math.Abs(d) < 1e15)
            return ((long)d).ToString(CultureInfo.InvariantCulture);
        return d.ToString("R", CultureInfo.InvariantCulture);
    }

    public static string Str(object v) {
        if (v == null) return "null";
        if (v is bool) return ((bool)v) ? "true" : "false";
        if (v is double) return Num((double)v);
        if (v is float) return Num((double)(float)v);
        if (v is string) return (string)v;
        if (v is System.Collections.IDictionary) {
            // Sorted by the key's own TEXT, so int keys come out 1, 10, 2.
            // That looks wrong at a glance and is exactly what the VM does;
            // ordering numerically here would make the same program print
            // differently on two backends.
            var map = (System.Collections.IDictionary)v;
            var keys = new List<string>();
            var byText = new Dictionary<string, object>();
            foreach (System.Collections.DictionaryEntry e in map) {
                string k = Str(e.Key);
                if (!byText.ContainsKey(k)) { keys.Add(k); byText[k] = e.Value; }
            }
            keys.Sort(StringComparer.Ordinal);
            var parts = new List<string>();
            foreach (var k in keys) parts.Add(k + ": " + Str(byText[k]));
            return "{" + string.Join(", ", parts) + "}";
        }
        if (v is System.Collections.IEnumerable) {
            var parts = new List<string>();
            foreach (var x in (System.Collections.IEnumerable)v) parts.Add(Str(x));
            return "[" + string.Join(", ", parts) + "]";
        }
        return Convert.ToString(v, CultureInfo.InvariantCulture);
    }

    public static void Print(object v) { Console.WriteLine(Str(v)); }

    // ── aborts ──
    //
    // The envelope is this engine's to choose — the VM prefixes "[Pyro VM] ",
    // go panics, node prints nothing. What has to agree across backends is the
    // message from the marker onwards (12.13).
    public static T Abort<T>(string msg) {
        Console.Out.Flush();
        Console.Error.WriteLine("[Cryo C#] " + msg);
        Environment.Exit(1);
        return default(T);
    }
    public static void AbortV(string msg) { Abort<int>(msg); }

    // ── integer arithmetic ──

    public static long IDiv(long a, long b) {
        if (b == 0) return Abort<long>("[Cryo Security] DivByZero: integer division");
        return a / b;
    }
    public static long IMod(long a, long b) {
        if (b == 0) return Abort<long>("[Cryo Security] DivByZero: integer division");
        return a % b;
    }

    // ── indexing, with the VM's bounds messages (12.13) ──

    public static T At<T>(List<T> xs, long i) {
        if (i < 0 || i >= xs.Count)
            return Abort<T>("[Cryo Security] IndexError: index " + i +
                            " out of bounds (len=" + xs.Count + ")");
        return xs[(int)i];
    }
    // The VM's SET message carries no (len=…). That asymmetry is the shape to
    // match, not to tidy up.
    public static void SetAt<T>(List<T> xs, long i, T v) {
        if (i < 0 || i >= xs.Count) {
            AbortV("[Cryo Security] IndexError: index " + i + " out of bounds");
            return;
        }
        xs[(int)i] = v;
    }
    public static string CharAt(string s, long i) {
        if (s == null || i < 0 || i >= s.Length)
            return Abort<string>("[Cryo Security] IndexError: string index out of bounds");
        return s[(int)i].ToString();
    }

    // ── optionals ──

    public static T Unwrap<T>(Nullable<T> v) where T : struct {
        if (!v.HasValue) return Abort<T>("[Cryo Security] unwrap of null value");
        return v.Value;
    }
    public static string UnwrapS(string v) {
        if (v == null) return Abort<string>("[Cryo Security] unwrap of null value");
        return v;
    }

    // ── conversions ──

    public static long ToInt(string s) {
        long r;
        if (long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out r))
            return r;
        double d;
        if (double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out d))
            return (long)d;
        return Abort<long>("[Cryo Security] to_int: '" + s + "' is not a valid integer");
    }
    public static double ToNum(string s) {
        double d;
        if (double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out d))
            return d;
        return Abort<double>("[Cryo Security] to_number: '" + s + "' is not a valid number");
    }

    // ── strings ──

    public static string Substr(string s, long start, long n) {
        if (s == null) return "";
        if (start < 0) start = 0;
        if (start > s.Length) start = s.Length;
        long end = start + (n < 0 ? 0 : n);
        if (end > s.Length) end = s.Length;
        return s.Substring((int)start, (int)(end - start));
    }
    public static string SliceStr(string s, long a, long b) {
        if (s == null) return "";
        if (a < 0) a = 0;
        if (b > s.Length) b = s.Length;
        if (b < a) b = a;
        return s.Substring((int)a, (int)(b - a));
    }
    public static long Find(string s, string sub) {
        return s.IndexOf(sub, StringComparison.Ordinal);
    }
    public static string Repeat(string s, long n) {
        if (n <= 0) return "";
        var sb = new StringBuilder();
        for (long i = 0; i < n; i++) sb.Append(s);
        return sb.ToString();
    }
    public static string PadStart(string s, long w, string p) {
        if (p == null || p.Length == 0 || s.Length >= w) return s;
        var sb = new StringBuilder();
        while (sb.Length + s.Length < w) sb.Append(p);
        return sb.ToString().Substring(0, (int)(w - s.Length)) + s;
    }
    public static string PadEnd(string s, long w, string p) {
        if (p == null || p.Length == 0 || s.Length >= w) return s;
        var sb = new StringBuilder(s);
        while (sb.Length < w) sb.Append(p);
        return sb.ToString().Substring(0, (int)w);
    }
    public static string ReplaceAll(string s, string old, string rep) {
        // ISSUES/17 — an EMPTY needle inserts the replacement at every
        // position boundary: replace("abc","","-") is "-a-b-c-" and
        // replace("","","-") is "-". .NET's Replace throws on an empty needle,
        // which would abort where the VM returns a string.
        if (s == null) s = "";
        if (old == null) old = "";
        if (rep == null) rep = "";
        if (old.Length == 0) {
            var sb = new StringBuilder();
            foreach (var ch in s) { sb.Append(rep); sb.Append(ch); }
            sb.Append(rep);
            return sb.ToString();
        }
        return s.Replace(old, rep);
    }
    public static List<string> Split(string s, string sep) {
        var outv = new List<string>();
        if (s == null) s = "";
        // An empty separator splits into characters, matching split_str() in
        // the Pyro runtime — which is what chars() relies on.
        if (sep == null || sep.Length == 0) {
            foreach (var ch in s) outv.Add(ch.ToString());
            return outv;
        }
        foreach (var part in s.Split(new string[] { sep }, StringSplitOptions.None))
            outv.Add(part);
        return outv;
    }
    public static string Join(List<string> xs, string sep) {
        return string.Join(sep, xs);
    }

    // ── math ──

    public static long Gcd(long a, long b) {
        a = System.Math.Abs(a); b = System.Math.Abs(b);
        while (b != 0) { long t = a % b; a = b; b = t; }
        return a;
    }
    public static long SignI(long v) { return v > 0 ? 1L : (v < 0 ? -1L : 0L); }
    public static long ClampI(long v, long lo, long hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }
    public static double ClampF(double v, double lo, double hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    // ── collections ──

    public static List<T> Sorted<T>(List<T> xs) {
        var c = new List<T>(xs);
        // Numbers order numerically, everything else by its text — the VM's
        // rule, and the reason this is not a bare c.Sort().
        if (typeof(T) == typeof(long) || typeof(T) == typeof(double)) c.Sort();
        else c.Sort((x, y) => string.CompareOrdinal(Str(x), Str(y)));
        return c;
    }
    public static List<T> Reversed<T>(List<T> xs) {
        var c = new List<T>(xs); c.Reverse(); return c;
    }
    public static List<T> Slice<T>(List<T> xs, long a, long b) {
        if (a < 0) a = 0;
        if (b > xs.Count) b = xs.Count;
        if (b < a) b = a;
        return xs.GetRange((int)a, (int)(b - a));
    }
    public static List<T> Concat<T>(List<T> a, List<T> b) {
        var c = new List<T>(a); c.AddRange(b); return c;
    }
    // Compared by rendered text so equality means the same thing here as it
    // does in the VM's index_of, which compares values rather than references.
    public static long IndexOf<T>(List<T> xs, T v) {
        string want = Str(v);
        for (int i = 0; i < xs.Count; i++) if (Str(xs[i]) == want) return i;
        return -1;
    }
    public static long CountOf<T>(List<T> xs, T v) {
        string want = Str(v);
        long n = 0;
        foreach (var x in xs) if (Str(x) == want) n++;
        return n;
    }
    public static long SumI(List<long> xs) { long n = 0; foreach (var x in xs) n += x; return n; }
    public static double SumF(List<double> xs) { double n = 0; foreach (var x in xs) n += x; return n; }

    // keys() returns them sorted by text, which is what the VM guarantees and
    // what `print(keys(m))` is asserted on.
    public static List<K> Keys<K, V>(Dictionary<K, V> m) {
        var ks = new List<K>(m.Keys);
        ks.Sort((x, y) => string.CompareOrdinal(Str(x), Str(y)));
        return ks;
    }
    // A missing key reads as the zero value, matching the VM's `m[k]` -> null
    // rather than throwing the way Dictionary's indexer would.
    public static V Get<K, V>(Dictionary<K, V> m, K k) {
        V v;
        if (m.TryGetValue(k, out v)) return v;
        return default(V);
    }
    public static bool Has<K, V>(Dictionary<K, V> m, K k) { return m.ContainsKey(k); }
    public static void Remove<K, V>(Dictionary<K, V> m, K k) { m.Remove(k); }
}
