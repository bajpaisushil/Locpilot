"""Repository file inventory with ignore rules, secret exclusion and a content cache."""

from __future__ import annotations

import fnmatch
import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from locpilot.security.paths import PathSecurityError, safe_join
from locpilot.security.secrets import is_secret_path
from locpilot.tools import git

# Dependency/cache directories excluded at any depth.
EXCLUDED_DIRS = {
    ".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".nox", ".next", ".nuxt", ".svelte-kit", ".gradle", ".idea", "bower_components", ".terraform", ".cache",
    ".parcel-cache", ".turbo", ".yarn", "Pods", "DerivedData", ".dart_tool", "site-packages", ".eggs",
    ".serverless", ".angular", ".expo",
}
# Ambiguous names (may be real source folders deeper in a tree) excluded only at the top level.
TOP_LEVEL_EXCLUDED_DIRS = {
    "venv", "env", "dist", "build", "out", "target", "coverage", "htmlcov", "vendor", "tmp", ".tmp", ".output",
    ".coverage",
}
EXCLUDED_FILE_GLOBS = [
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll", "*.exe", "*.class", "*.jar", "*.war", "*.o", "*.a", "*.obj",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.icns", "*.pdf", "*.zip", "*.gz", "*.tgz", "*.bz2",
    "*.xz", "*.7z", "*.rar", "*.mp3", "*.mp4", "*.mov", "*.avi", "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.sqlite", "*.sqlite3", "*.db", "*.parquet", "*.pkl", "*.pickle", "*.npy", "*.npz", "*.h5", "*.onnx",
    "*.pt", "*.bin", "*.wasm", "*.DS_Store", "*.snap",
]
MAX_INDEX_FILES = 60_000
MAX_SEARCH_BYTES = 1_000_000
MAX_VIEW_BYTES = 2_000_000
CACHE_BUDGET_BYTES = 96 * 1024 * 1024

LANGUAGE_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".mts": "typescript", ".cts": "typescript",
    ".go": "go", ".java": "java", ".rs": "rust", ".rb": "ruby", ".php": "php", ".cs": "csharp", ".kt": "kotlin",
    ".kts": "kotlin", ".swift": "swift", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".scala": "scala", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".md": "markdown", ".mdx": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".less": "less", ".sql": "sql", ".vue": "vue", ".svelte": "svelte", ".xml": "xml", ".gradle": "groovy",
    ".graphql": "graphql", ".proto": "proto", ".dart": "dart", ".lua": "lua", ".r": "r", ".ex": "elixir",
    ".exs": "elixir", ".erl": "erlang", ".hs": "haskell", ".clj": "clojure", ".tf": "hcl",
}
_SPECIAL_NAMES = {"dockerfile": "dockerfile", "makefile": "makefile", "gemfile": "ruby", "rakefile": "ruby"}
# Monaco editor language ids for the UI viewer.
_MONACO_IDS = {"tsx": "typescript", "toml": "ini", "makefile": "plaintext", "vue": "html", "svelte": "html",
               "groovy": "plaintext", "proto": "plaintext", "hcl": "plaintext", "erlang": "plaintext",
               "haskell": "plaintext", "elixir": "plaintext", "clojure": "clojure", "dart": "dart"}

CODE_LANGUAGES = {"python", "javascript", "typescript", "tsx", "go", "java", "rust", "ruby", "php", "csharp",
                  "kotlin", "swift", "c", "cpp", "scala", "shell", "vue", "svelte", "dart", "lua", "elixir"}


def language_for(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[name]
    dot = name.rfind(".")
    return LANGUAGE_BY_EXT.get(name[dot:]) if dot != -1 else None


def monaco_language(path: str) -> str:
    lang = language_for(path) or "plaintext"
    return _MONACO_IDS.get(lang, lang)


def is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    if len(parts) > 1 and parts[0] in TOP_LEVEL_EXCLUDED_DIRS:
        return True
    if any(part.endswith(".egg-info") for part in parts[:-1]):
        return True
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name.lower(), g.lower()) for g in EXCLUDED_FILE_GLOBS):
        return True
    return is_secret_path(rel)


def is_test_path(rel: str) -> bool:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{low}" or "/test/" in f"/{low}" or "__tests__/" in low or "/spec/" in f"/{low}"
        or name.startswith("test_") or name.endswith(("_test.py", "_test.go", "_spec.rb"))
        or ".test." in name or ".spec." in name or name.endswith("test.java") or name == "conftest.py"
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


@dataclass
class CachedFile:
    mtime_ns: int
    size: int
    text: str
    lower: str
    lines: list[str]


class RepoIndex:
    """Inventory of searchable files in a repository or worktree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.files: list[str] = []
        self._file_set: set[str] = set()
        self._cache: OrderedDict[str, CachedFile] = OrderedDict()
        self._cache_bytes = 0
        self._lock = threading.RLock()
        self.refreshed_at = 0.0
        self.truncated = False

    # inventory --------------------------------------------------------------------
    def refresh(self, force: bool = False, max_age: float = 3.0) -> None:
        with self._lock:
            if not force and time.monotonic() - self.refreshed_at < max_age:
                return
            try:
                candidates = git.list_files(self.root)
            except git.GitError:
                candidates = self._walk()
            files = []
            for rel in candidates:
                if is_excluded(rel):
                    continue
                full = self.root / rel
                if not full.is_file():
                    continue
                if full.is_symlink() and not full.resolve().is_relative_to(self.root):
                    continue
                files.append(rel)
                if len(files) >= MAX_INDEX_FILES:
                    self.truncated = True
                    break
            self.files = files
            self._file_set = set(files)
            self.refreshed_at = time.monotonic()

    def _walk(self) -> list[str]:
        out: list[str] = []
        for path in self.root.rglob("*"):
            rel = path.relative_to(self.root).as_posix()
            if path.is_file() and not is_excluded(rel):
                out.append(rel)
            if len(out) >= MAX_INDEX_FILES:
                break
        return sorted(out)

    def has_file(self, rel: str) -> bool:
        self.refresh()
        return rel in self._file_set

    def code_files(self) -> list[str]:
        self.refresh()
        return [f for f in self.files if (language_for(f) or "") in CODE_LANGUAGES]

    # content ----------------------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        return safe_join(self.root, rel)

    def _load(self, rel: str, limit: int) -> CachedFile | None:
        try:
            full = self.resolve(rel)
        except PathSecurityError:
            return None
        try:
            stat = full.stat()
        except OSError:
            return None
        if not full.is_file() or stat.st_size > limit:
            return None
        with self._lock:
            cached = self._cache.get(rel)
            if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
                self._cache.move_to_end(rel)
                return cached
        try:
            raw = full.read_bytes()
        except OSError:
            return None
        if b"\x00" in raw[:8192]:
            return None
        text = raw.decode("utf-8", errors="replace")
        entry = CachedFile(stat.st_mtime_ns, stat.st_size, text, text.lower(), text.splitlines())
        with self._lock:
            old = self._cache.pop(rel, None)
            if old:
                self._cache_bytes -= old.size
            self._cache[rel] = entry
            self._cache_bytes += entry.size
            while self._cache_bytes > CACHE_BUDGET_BYTES and self._cache:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= evicted.size
        return entry

    def read(self, rel: str, limit: int = MAX_SEARCH_BYTES) -> CachedFile | None:
        """Cached text content of a searchable file (None for binary/huge/blocked files)."""
        return self._load(rel, limit)

    def read_text(self, rel: str) -> str | None:
        entry = self._load(rel, MAX_SEARCH_BYTES)
        return entry.text if entry else None

    def read_lines(self, rel: str) -> list[str] | None:
        entry = self._load(rel, MAX_SEARCH_BYTES)
        return entry.lines if entry else None

    def invalidate(self, rel: str | None = None) -> None:
        with self._lock:
            if rel is None:
                self._cache.clear()
                self._cache_bytes = 0
            else:
                old = self._cache.pop(rel, None)
                if old:
                    self._cache_bytes -= old.size
            self.refreshed_at = 0.0

    # views ------------------------------------------------------------------------
    def list_dir(self, rel_dir: str = "") -> list[dict]:
        self.refresh()
        prefix = rel_dir.strip("/")
        if prefix:
            safe_join(self.root, prefix)  # validates the directory path
            prefix += "/"
        dirs: dict[str, None] = {}
        files: list[dict] = []
        for rel in self.files:
            if not rel.startswith(prefix):
                continue
            rest = rel[len(prefix):]
            if "/" in rest:
                dirs.setdefault(rest.split("/", 1)[0], None)
            else:
                try:
                    size = (self.root / rel).stat().st_size
                except OSError:
                    size = None
                files.append({"name": rest, "path": rel, "type": "file", "size": size})
        entries = [{"name": d, "path": prefix + d, "type": "dir", "size": None} for d in sorted(dirs, key=str.lower)]
        return entries + sorted(files, key=lambda e: e["name"].lower())

    def language_stats(self) -> list[dict]:
        self.refresh()
        counts: dict[str, int] = {}
        for rel in self.files:
            lang = language_for(rel)
            if lang and lang not in ("markdown", "json", "yaml", "toml", "ini", "xml"):
                counts[lang] = counts.get(lang, 0) + 1
        return [{"name": k, "files": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def number_lines(lines: list[str], start: int, end: int) -> str:
    """Render lines [start, end] (1-based, inclusive) with right-aligned line numbers."""
    width = len(str(end))
    return "\n".join(f"{i:>{width}} | {lines[i - 1]}" for i in range(start, end + 1) if 0 < i <= len(lines))


_INDEXES: dict[str, RepoIndex] = {}
_INDEX_LOCK = threading.Lock()


def get_index(root: Path) -> RepoIndex:
    key = str(Path(root).resolve())
    with _INDEX_LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = RepoIndex(Path(key))
            _INDEXES[key] = index
            if len(_INDEXES) > 24:
                _INDEXES.pop(next(iter(_INDEXES)))
    index.refresh()
    return index


def drop_index(root: Path) -> None:
    with _INDEX_LOCK:
        _INDEXES.pop(str(Path(root).resolve()), None)
