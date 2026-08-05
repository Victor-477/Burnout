"""
Burnout — incremental compilation cache (roadmap 11.23)

Two caches, because profiling the self-hosted compiler showed two different
costs worth removing:

    resolve_modules (lex + parse of 7 files)   47 ms   28%
    everything else                           118 ms   72%
    ------------------------------------------------
    total                                     165 ms

  * a PARSE cache, one entry per module file, keyed by that file's content.
    Editing one module reparses one module.
  * an ARTIFACT cache, one entry per whole compilation, keyed by every input
    that can change the output. A rebuild with nothing changed becomes a file
    copy instead of 165 ms of work.

WHAT GOES IN THE KEY, AND WHY IT MATTERS MORE THAN THE SPEED
------------------------------------------------------------
A cache that returns a stale artifact is worse than no cache: the compiler
would report success and hand back the previous program. So the artifact key
covers everything that can change the result —

    every source file's content (entry plus all transitive imports)
    the backend, and the flags that reach code generation
    a fingerprint of the COMPILER'S OWN SOURCE

That last one is not paranoia. Without it, editing `codegen_pyro.py` and
recompiling would return the artifact built by the previous compiler, and the
change would appear to have done nothing — the single most confusing failure
this feature could produce, and the most likely one during development.

Entries are content-addressed, so a stale entry is never *wrong*, only unused;
`--no-cache` skips both caches, and `cryoc --clear-cache` empties the
directory.
"""
import hashlib
import os
import pickle
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

# Bumped when the cached representation itself changes shape (a new AST field,
# a different pickle protocol). Old entries then simply never match.
CACHE_FORMAT = 1

_DIRNAME = '.cryocache'


def cache_dir(root: Optional[str] = None) -> str:
    return os.path.join(root or os.getcwd(), _DIRNAME)


def _compiler_fingerprint() -> str:
    """Hash of the compiler's own sources.

    Editing the compiler must invalidate every entry. Without this, a change to
    a code generator would be masked by artifacts the previous version built —
    the compiler would look broken in a way nothing in the program explains.
    """
    global _FINGERPRINT
    if _FINGERPRINT is not None:
        return _FINGERPRINT
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    h = hashlib.sha256()
    h.update(f"format={CACHE_FORMAT}\n".encode())
    # Size and modification time rather than contents. Reading ~40 compiler
    # sources costs about 30 ms, and every `cryoc` run is a fresh process that
    # would pay it — enough to make a rebuild after an edit SLOWER than no
    # cache at all, which is the opposite of the point. An edit that preserves
    # both the size and the nanosecond mtime is not something an editor does.
    for d in (here, os.path.join(root, 'Cryo')):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith('.py'):
                continue
            try:
                st = os.stat(os.path.join(d, name))
                h.update(f'{name}:{st.st_size}:{st.st_mtime_ns}\x00'.encode())
            except OSError:
                pass
    _FINGERPRINT = h.hexdigest()[:16]
    return _FINGERPRINT


_FINGERPRINT: Optional[str] = None


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, 'rb') as f:
            return f.read()
    except OSError:
        return None


def _write_atomic(path: str, data: bytes) -> None:
    """Write via a temp file and rename.

    A cache entry half-written by an interrupted build would be read back as a
    valid one. The rename makes an entry appear complete or not at all.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f'.{os.getpid()}.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── parse cache: one entry per module file ───────────────────

class ParseCache:
    """Caches the AST of a single .cryo file, keyed by its content."""

    def __init__(self, root: Optional[str] = None, enabled: bool = True):
        self.dir = os.path.join(cache_dir(root), 'parse')
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def _key(self, src: str) -> str:
        h = hashlib.sha256()
        h.update(_compiler_fingerprint().encode())
        h.update(b'\x00parse\x00')
        h.update(src.encode('utf-8'))
        return h.hexdigest()

    def get(self, src: str):
        if not self.enabled:
            return None
        data = _read(os.path.join(self.dir, self._key(src) + '.ast'))
        if data is None:
            self.misses += 1
            return None
        try:
            ast = pickle.loads(data)
        except Exception:
            # A corrupt entry is a miss, never an error: the source is right
            # there and reparsing it costs milliseconds.
            self.misses += 1
            return None
        self.hits += 1
        return ast

    def put(self, src: str, ast) -> None:
        if not self.enabled:
            return
        try:
            data = pickle.dumps(ast, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            return          # unpicklable AST: skip the cache, never fail
        _write_atomic(os.path.join(self.dir, self._key(src) + '.ast'), data)


# ── artifact cache: one entry per whole compilation ──────────

class ArtifactCache:
    """Caches a compiled output, keyed by every input that can change it."""

    def __init__(self, root: Optional[str] = None, enabled: bool = True):
        self.dir = os.path.join(cache_dir(root), 'artifact')
        self.enabled = enabled

    def key(self, sources: List[Tuple[str, str]], settings: Dict[str, Any]) -> str:
        """sources: (path, content) for the entry and every import it pulled in."""
        h = hashlib.sha256()
        h.update(_compiler_fingerprint().encode())
        h.update(b'\x00artifact\x00')
        for name, content in sorted(sources):
            h.update(os.path.basename(name).encode())
            h.update(b'\x00')
            h.update(content.encode('utf-8'))
            h.update(b'\x00')
        for k in sorted(settings):
            h.update(f'{k}={settings[k]!r}\x00'.encode())
        return h.hexdigest()

    # The filename is a hash of the INPUTS, which says nothing about whether
    # the bytes on disk are still the ones that were written. A truncated or
    # damaged entry would otherwise be handed back as a valid program — the
    # compiler reporting success over a corrupt artifact. Each entry therefore
    # carries a checksum of its own contents, and a mismatch is simply a miss.
    # An entry stores the WARNINGS the compilation produced as well as its
    # output. Compiling is not a pure function — it prints diagnostics — and a
    # cache that skips the work also skips those, so the second build of a page
    # whose javascript calls `cryo.…` fell silent about it. Anything the cache
    # cannot replay, it must not hide.
    _MAGIC = b'CRYOART2'

    def get(self, key: str) -> Optional[Tuple[bytes, str]]:
        """(artifact, warnings) or None."""
        if not self.enabled:
            return None
        raw = _read(os.path.join(self.dir, key + '.out'))
        head = len(self._MAGIC) + 32 + 4
        if raw is None or len(raw) < head or raw[:len(self._MAGIC)] != self._MAGIC:
            return None
        digest = raw[len(self._MAGIC):len(self._MAGIC) + 32]
        wlen = int.from_bytes(raw[len(self._MAGIC) + 32:head], 'little')
        body = raw[head:]
        if len(body) < wlen or hashlib.sha256(body).digest() != digest:
            return None          # damaged entry: a miss, never a wrong answer
        try:
            warnings = body[:wlen].decode('utf-8')
        except UnicodeDecodeError:
            return None
        return body[wlen:], warnings

    def put(self, key: str, data: bytes, warnings: str = '') -> None:
        if not self.enabled or data is None:
            return
        w = warnings.encode('utf-8')
        body = w + data
        blob = (self._MAGIC + hashlib.sha256(body).digest()
                + len(w).to_bytes(4, 'little') + body)
        _write_atomic(os.path.join(self.dir, key + '.out'), blob)


# ── housekeeping ─────────────────────────────────────────────

def clear(root: Optional[str] = None) -> Tuple[int, int]:
    """Remove the cache. Returns (entries, bytes) removed."""
    d = cache_dir(root)
    n = size = 0
    for sub in ('parse', 'artifact'):
        p = os.path.join(d, sub)
        if not os.path.isdir(p):
            continue
        for name in os.listdir(p):
            f = os.path.join(p, name)
            try:
                size += os.path.getsize(f)
                n += 1
            except OSError:
                pass
    shutil.rmtree(d, ignore_errors=True)
    return n, size


def stats(root: Optional[str] = None) -> Dict[str, Any]:
    d = cache_dir(root)
    out = {'dir': d, 'parse': 0, 'artifact': 0, 'bytes': 0}
    for sub in ('parse', 'artifact'):
        p = os.path.join(d, sub)
        if not os.path.isdir(p):
            continue
        for name in os.listdir(p):
            try:
                out['bytes'] += os.path.getsize(os.path.join(p, name))
                out[sub] += 1
            except OSError:
                pass
    return out
