// ============================================================
//  LibPyro — C# (.NET) Embedding Library
// ============================================================
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace Pyro.Embedding;

public enum PyroType {
    Null = 0,
    Bool = 1,
    Int = 2,
    Float = 3,
    String = 4,
    Array = 5,
    Map = 6,
    Func = 7,
    Error = 8
}

public class PyroValue {
    public PyroType Type { get; set; } = PyroType.Null;
    public long IntValue { get; set; }
    public double FloatValue { get; set; }
    public bool BoolValue { get; set; }
    public string StringValue { get; set; }
    public List<PyroValue> ArrayValue { get; set; }
    public Dictionary<string, PyroValue> MapValue { get; set; }

    public static PyroValue FromNull() => new() { Type = PyroType.Null };
    public static PyroValue FromBool(bool b) => new() { Type = PyroType.Bool, BoolValue = b };
    public static PyroValue FromInt(long i) => new() { Type = PyroType.Int, IntValue = i };
    public static PyroValue FromFloat(double f) => new() { Type = PyroType.Float, FloatValue = f };
    public static PyroValue FromString(string s) => new() { Type = PyroType.String, StringValue = s };
    public static PyroValue FromArray(List<PyroValue> a) => new() { Type = PyroType.Array, ArrayValue = a };
    public static PyroValue FromMap(Dictionary<string, PyroValue> m) => new() { Type = PyroType.Map, MapValue = m };

    public bool IsNull => Type == PyroType.Null;
    public bool AsBool => Type == PyroType.Bool ? BoolValue : (Type == PyroType.Int ? IntValue != 0 : !string.IsNullOrEmpty(StringValue));
    public long AsInt => Type == PyroType.Int ? IntValue : (long)FloatValue;
    public double AsFloat => Type == PyroType.Float ? FloatValue : (double)IntValue;
    public string AsString => StringValue ?? ToString();

    public override string ToString() {
        return Type switch {
            PyroType.Null => "null",
            PyroType.Bool => BoolValue ? "true" : "false",
            PyroType.Int => IntValue.ToString(),
            PyroType.Float => FloatValue.ToString(System.Globalization.CultureInfo.InvariantCulture),
            PyroType.String => StringValue,
            PyroType.Array => "[" + string.Join(", ", ArrayValue ?? new List<PyroValue>()) + "]",
            PyroType.Map => "{ ... }",
            _ => "null"
        };
    }
}

public class PyroVM {
    public bool Sandbox { get; set; }
    public bool Debug { get; set; }

    private readonly Dictionary<string, Delegate> _natives = new();
    private readonly Dictionary<string, object> _globals = new();

    public PyroVM(bool sandbox = false, bool debug = false) {
        Sandbox = sandbox;
        Debug = debug;
    }

    public void RegisterNative(string name, Delegate fn) {
        _natives[name] = fn;
    }

    public void SetGlobal(string name, object value) {
        _globals[name] = value;
    }

    public object GetGlobal(string name) {
        return _globals.TryGetValue(name, out var val) ? val : null;
    }

    public PyroValue RunBytecode(byte[] bytecode) {
        if (bytecode == null || bytecode.Length == 0) {
            throw new ArgumentException("Bytecode buffer cannot be empty", nameof(bytecode));
        }

        string tempPath = Path.Combine(Path.GetTempPath(), "pyro_embed_" + Guid.NewGuid().ToString("N") + ".pyro");
        try {
            File.WriteAllBytes(tempPath, bytecode);
            string projectRoot = FindProjectRoot();
            string vmExe = Path.Combine(projectRoot, "build", "pyrovm_cs.exe");
            if (!File.Exists(vmExe)) {
                vmExe = Path.Combine(projectRoot, "build", "pyrovm_cpp.exe");
            }
            if (!File.Exists(vmExe)) {
                vmExe = Path.Combine(projectRoot, "build", "pyrovm.exe");
            }

            var psi = new ProcessStartInfo {
                FileName = vmExe,
                Arguments = $"\"{tempPath}\"",
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };

            if (Sandbox) {
                psi.EnvironmentVariables["PYRO_SANDBOX"] = "1";
            }

            using var proc = Process.Start(psi);
            string stdout = proc.StandardOutput.ReadToEnd();
            string stderr = proc.StandardError.ReadToEnd();
            proc.WaitForExit();

            if (proc.ExitCode != 0) {
                throw new InvalidOperationException($"Pyro execution failed with exit code {proc.ExitCode}: {stderr}");
            }

            if (!string.IsNullOrEmpty(stdout)) {
                Console.Write(stdout);
            }
            return PyroValue.FromNull();
        } finally {
            if (File.Exists(tempPath)) {
                try { File.Delete(tempPath); } catch { }
            }
        }
    }

    public PyroValue Eval(string source) {
        if (string.IsNullOrWhiteSpace(source)) return PyroValue.FromNull();

        string projectRoot = FindProjectRoot();
        string tempCryo = Path.Combine(Path.GetTempPath(), "embed_" + Guid.NewGuid().ToString("N") + ".cryo");
        string tempPyro = Path.ChangeExtension(tempCryo, ".pyro");

        try {
            File.WriteAllText(tempCryo, source);

            var psi = new ProcessStartInfo {
                FileName = "python",
                Arguments = $"Burnout/cryoc.py \"{tempCryo}\" --backend pyro -o \"{tempPyro}\" --no-banner",
                WorkingDirectory = projectRoot,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };

            using var proc = Process.Start(psi);
            string err = proc.StandardError.ReadToEnd();
            proc.WaitForExit();

            if (proc.ExitCode != 0 || !File.Exists(tempPyro)) {
                throw new InvalidOperationException($"Compilation failed: {err}");
            }

            byte[] bytecode = File.ReadAllBytes(tempPyro);
            return RunBytecode(bytecode);
        } finally {
            if (File.Exists(tempCryo)) try { File.Delete(tempCryo); } catch { }
            if (File.Exists(tempPyro)) try { File.Delete(tempPyro); } catch { }
        }
    }

    private static string FindProjectRoot() {
        string current = AppDomain.CurrentDomain.BaseDirectory;
        while (!string.IsNullOrEmpty(current)) {
            if (File.Exists(Path.Combine(current, "Burnout", "cryoc.py"))) {
                return current;
            }
            string parent = Path.GetDirectoryName(current);
            if (parent == current) break;
            current = parent;
        }
        return Directory.GetCurrentDirectory();
    }
}
