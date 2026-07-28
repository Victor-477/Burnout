"""
Burnout Lib Builder — Compiles C/C++ shared/static libraries & packages header distributions.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EMBED_DIR = os.path.dirname(HERE)
BURNOUT_DIR = os.path.dirname(EMBED_DIR)
PROJECT_ROOT = os.path.dirname(BURNOUT_DIR)
DIST_DIR = os.path.join(PROJECT_ROOT, "dist")


def build_c_library():
    print("[LibBurnout] Building C shared library (libpyro)...")
    inc_dir = os.path.join(EMBED_DIR, "c_api", "include")
    src_file = os.path.join(EMBED_DIR, "c_api", "src", "pyro_embed.c")
    runtime_src = os.path.join(BURNOUT_DIR, "runtime", "cryo_runtime.c")

    os.makedirs(os.path.join(DIST_DIR, "include"), exist_ok=True)
    os.makedirs(os.path.join(DIST_DIR, "lib"), exist_ok=True)

    # Copy headers
    shutil.copy(os.path.join(inc_dir, "pyro_embed.h"), os.path.join(DIST_DIR, "include"))
    cpp_header = os.path.join(EMBED_DIR, "cpp", "include", "pyro.hpp")
    if os.path.exists(cpp_header):
        shutil.copy(cpp_header, os.path.join(DIST_DIR, "include"))

    out_name = "pyro.dll" if sys.platform == "win32" else "libpyro.so"
    out_path = os.path.join(DIST_DIR, "lib", out_name)

    cmd = ["gcc", "-O2", "-shared", "-DBURNOUT_BUILD_DLL", f"-I{inc_dir}", src_file, "-o", out_path]
    print(f"Executing: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print(f"✓ Built shared library: {out_path}")
        else:
            print(f"Warning: gcc shared build returned {res.returncode}: {res.stderr}")
    except FileNotFoundError:
        print("[LibBurnout] Note: gcc toolchain not found on PATH. Shared headers exported to dist/include.")


if __name__ == "__main__":
    build_c_library()
