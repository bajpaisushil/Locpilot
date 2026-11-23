"""Symbol extraction (definitions, call sites, imports) and static call-chain analysis.

Python uses the standard-library ``ast`` module; JavaScript, TypeScript, Go,
Java and Rust use Tree-sitter grammars; other languages fall back to regular
expressions. Everything here is *static, name-based* analysis: callers and
callees are matched by identifier, so results for very common names can include
false positives. Reports label these findings accordingly.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from locpilot.tools.files import RepoIndex, is_test_path, language_for

MAX_PARSE_BYTES = 400_000
MAX_SYMBOL_FILES = 60_000  # as many as the inventory holds: symbols are on disk, not in RAM


@dataclass
class Symbol:
    name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    parent: str | None = None
    signature: str = ""

    @property
    def qualname(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name

    def to_dict(self) -> dict:
        return {"name": self.name, "qualname": self.qualname, "kind": self.kind, "path": self.path,
                "start_line": self.start_line, "end_line": self.end_line, "signature": self.signature}


@dataclass
class FileSymbols:
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)  # (callee name, line)
    imports: list[tuple[str, int]] = field(default_factory=list)  # (module specifier, line)
    parse_error: bool = False


# --- Tree-sitter -------------------------------------------------------------------

_TS_QUERIES: dict[str, tuple[str, str, str]] = {
    # language: (python module, language function, query)
    "javascript": ("tree_sitter_javascript", "language", """
        (function_declaration name: (identifier) @name) @def.function
        (generator_function_declaration name: (identifier) @name) @def.function
        (class_declaration name: (identifier) @name) @def.class
        (method_definition name: (property_identifier) @name) @def.method
        (variable_declarator name: (identifier) @name value: [(arrow_function) (function_expression)]) @def.function
        (call_expression function: [(identifier) @call (member_expression property: (property_identifier) @call)])
        (new_expression constructor: (identifier) @call)
        (import_statement source: (string) @import)
        (call_expression function: (identifier) @_req arguments: (arguments (string) @import) (#eq? @_req "require"))
    """),
    "typescript": ("tree_sitter_typescript", "language_typescript", """
        (function_declaration name: (identifier) @name) @def.function
        (class_declaration name: (type_identifier) @name) @def.class
        (abstract_class_declaration name: (type_identifier) @name) @def.class
        (interface_declaration name: (type_identifier) @name) @def.interface
        (type_alias_declaration name: (type_identifier) @name) @def.type
        (enum_declaration name: (identifier) @name) @def.enum
        (method_definition name: (property_identifier) @name) @def.method
        (variable_declarator name: (identifier) @name value: [(arrow_function) (function_expression)]) @def.function
        (call_expression function: [(identifier) @call (member_expression property: (property_identifier) @call)])
        (new_expression constructor: (identifier) @call)
        (import_statement source: (string) @import)
    """),
    "go": ("tree_sitter_go", "language", """
        (function_declaration name: (identifier) @name) @def.function
        (method_declaration name: (field_identifier) @name) @def.method
        (type_spec name: (type_identifier) @name) @def.type
        (call_expression function: [(identifier) @call (selector_expression field: (field_identifier) @call)])
        (import_spec path: (interpreted_string_literal) @import)
    """),
    "java": ("tree_sitter_java", "language", """
        (class_declaration name: (identifier) @name) @def.class
        (interface_declaration name: (identifier) @name) @def.interface
        (enum_declaration name: (identifier) @name) @def.enum
        (record_declaration name: (identifier) @name) @def.class
        (method_declaration name: (identifier) @name) @def.method
        (constructor_declaration name: (identifier) @name) @def.method
        (method_invocation name: (identifier) @call)
        (object_creation_expression type: (type_identifier) @call)
        (import_declaration (scoped_identifier) @import)
    """),
    "rust": ("tree_sitter_rust", "language", """
        (function_item name: (identifier) @name) @def.function
        (struct_item name: (type_identifier) @name) @def.struct
        (enum_item name: (type_identifier) @name) @def.enum
        (trait_item name: (type_identifier) @name) @def.trait
        (mod_item name: (identifier) @name) @def.module
        (call_expression function: [(identifier) @call (field_expression field: (field_identifier) @call)
                                    (scoped_identifier name: (identifier) @call)])
        (use_declaration argument: (_) @import)
    """),
}
_TS_LANG_ALIAS = {"tsx": "tsx"}
_CLASS_KINDS = {"class", "interface", "struct", "trait", "enum", "type"}


@cache
def _ts_query(language: str):
    """Return (parser, query) for a language, or None if the grammar is unavailable."""
    try:
        from tree_sitter import Language, Parser, Query
    except ImportError:
        return None
    key = "typescript" if language == "tsx" else language
    spec = _TS_QUERIES.get(key)
    if spec is None:
        return None
    module_name, func_name, query_src = spec
    if language == "tsx":
        func_name = "language_tsx"
    try:
        module = importlib.import_module(module_name)
        lang = Language(getattr(module, func_name)())
        return Parser(lang), Query(lang, query_src)
    except Exception:  # noqa: BLE001 - grammar missing or query incompatible: fall back to regex
        return None


_TS_LOCK = threading.Lock()


def _extract_tree_sitter(path: str, source: str, language: str) -> FileSymbols | None:
    compiled = _ts_query(language)
    if compiled is None:
        return None
    from tree_sitter import QueryCursor

    parser, query = compiled
    data = source.encode("utf-8", errors="replace")
    with _TS_LOCK:
        tree = parser.parse(data)
        matches = QueryCursor(query).matches(tree.root_node)
    out = FileSymbols(parse_error=tree.root_node.has_error)
    lines = source.splitlines()
    for _pattern, caps in matches:
        defs = [(k, v) for k, v in caps.items() if k.startswith("def.")]
        if defs and "name" in caps:
            kind = defs[0][0].split(".", 1)[1]
            node = defs[0][1][0]
            name = caps["name"][0].text.decode("utf-8", errors="replace")
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            if node.type == "variable_declarator" and node.parent is not None:
                start = node.parent.start_point[0] + 1
            sig = lines[start - 1].strip()[:200] if start - 1 < len(lines) else ""
            out.symbols.append(Symbol(name, kind, path, start, end, None, sig))
        elif "call" in caps:
            for node in caps["call"]:
                out.calls.append((node.text.decode("utf-8", errors="replace"), node.start_point[0] + 1))
        elif "import" in caps:
            for node in caps["import"]:
                spec = node.text.decode("utf-8", errors="replace").strip("\"'`")
                out.imports.append((spec, node.start_point[0] + 1))
    _assign_parents(out.symbols)
    return out


def _assign_parents(symbols: list[Symbol]) -> None:
    symbols.sort(key=lambda s: (s.start_line, -s.end_line))
    stack: list[Symbol] = []
    for sym in symbols:
        while stack and stack[-1].end_line < sym.start_line:
            stack.pop()
        containers = [s for s in stack if s.kind in _CLASS_KINDS]
        if sym.kind in ("method", "function") and containers:
            sym.parent = containers[-1].name
            if sym.kind == "function":
                sym.kind = "method"
        stack.append(sym)


# --- Python (ast) ------------------------------------------------------------------

class _PyVisitor(ast.NodeVisitor):
    def __init__(self, path: str, lines: list[str]) -> None:
        self.path = path
        self.lines = lines
        self.out = FileSymbols()
        self.stack: list[tuple[str, str]] = []  # (kind, name)

    def _add(self, node: ast.AST, name: str, kind: str) -> None:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        end = getattr(node, "end_lineno", None) or node.lineno
        parent = next((n for k, n in reversed(self.stack) if k == "class"), None)
        if kind == "function" and self.stack and self.stack[-1][0] == "class":
            kind = "method"
        sig = self.lines[node.lineno - 1].strip()[:200] if node.lineno - 1 < len(self.lines) else ""
        self.out.symbols.append(Symbol(name, kind, self.path, start, end, parent, sig))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node, node.name, "class")
        self.stack.append(("class", node.name))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add(node, node.name, "function")
        self.stack.append(("function", node.name))
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name:
            self.out.calls.append((name, node.lineno))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.out.imports.append((alias.name, node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        self.out.imports.append((module, node.lineno))
        for alias in node.names:
            if alias.name != "*":
                self.out.imports.append((f"{module}.{alias.name}" if module.strip(".") else module + alias.name,
                                         node.lineno))


def _extract_python(path: str, source: str) -> FileSymbols:
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        out = _extract_regex(path, source)
        out.parse_error = True
        return out
    visitor = _PyVisitor(path, lines)
    visitor.visit(tree)
    return visitor.out


# --- Regex fallback ------------------------------------------------------------------

_REGEX_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+|abstract\s+|final\s+|override\s+)*"
    r"(?P<kw>def|function|func|fun|fn|class|interface|struct|enum|trait|module|object)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_REGEX_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CALL_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "def", "function", "func", "fn", "class",
                  "elif", "with", "print", "and", "or", "not", "in", "lambda", "sizeof", "typeof", "await"}


def _extract_regex(path: str, source: str) -> FileSymbols:
    out = FileSymbols()
    lines = source.splitlines()
    kinds = {"def": "function", "function": "function", "func": "function", "fun": "function", "fn": "function",
             "class": "class", "interface": "interface", "struct": "struct", "enum": "enum", "trait": "trait",
             "module": "module", "object": "class"}
    defs = []
    for m in _REGEX_DEF.finditer(source):
        line = source.count("\n", 0, m.start()) + 1
        defs.append((line, kinds[m.group("kw")], m.group("name")))
    for line, kind, name in defs:
        header = lines[line - 1]
        indent = len(header) - len(header.lstrip())
        end = line
        for j in range(line, len(lines)):
            text = lines[j]
            stripped = text.strip()
            if not stripped:
                continue
            if len(text) - len(text.lstrip()) <= indent:
                if stripped[0] in "})]" or stripped == "end":
                    end = j + 1
                break
            end = j + 1
        out.symbols.append(Symbol(name, kind, path, line, end, None, header.strip()[:200]))
    for idx, text in enumerate(lines, start=1):
        for m in _REGEX_CALL.finditer(text):
            if m.group(1) not in _CALL_KEYWORDS:
                out.calls.append((m.group(1), idx))
    _assign_parents(out.symbols)
    return out


def extract_file_symbols(path: str, source: str) -> FileSymbols:
    language = language_for(path)
    if language == "python":
        return _extract_python(path, source)
    if language in ("javascript", "typescript", "tsx", "go", "java", "rust"):
        result = _extract_tree_sitter(path, source, language)
        if result is not None:
            return result
    return _extract_regex(path, source)


def syntax_errors(path: str, source: str) -> str | None:
    """Return a description of syntax errors without executing anything, or None if it parses."""
    language = language_for(path)
    if language == "python":
        try:
            ast.parse(source, filename=path)
        except SyntaxError as exc:
            return f"Python syntax error at line {exc.lineno}: {exc.msg}"
        return None
    if language == "json":
        import json

        try:
            json.loads(source)
        except json.JSONDecodeError as exc:
            return f"JSON error at line {exc.lineno}: {exc.msg}"
        return None
    if language in ("javascript", "typescript", "tsx", "go", "java", "rust"):
        compiled = _ts_query(language)
        if compiled is None:
            return None
        parser, _ = compiled
        with _TS_LOCK:
            tree = parser.parse(source.encode("utf-8", errors="replace"))
        if tree.root_node.has_error:
            line = _first_error_line(tree.root_node)
            return f"{language} syntax error near line {line}" if line else f"{language} syntax error"
    return None


def _first_error_line(node) -> int | None:
    if node.type == "ERROR" or node.is_missing:
        return node.start_point[0] + 1
    for child in node.children:
        if child.has_error:
            found = _first_error_line(child)
            if found:
                return found
    return None


# --- Index -------------------------------------------------------------------------
#
# Parsed symbols live in one SQLite file (``<data dir>/index/symbols.sqlite``), not in Python memory, so a big
# repository costs disk rather than RAM, and nothing is parsed twice:
#
# * a file's parse result is stored once per *content* (language + hash), so the task worktrees of a repository —
#   copies of the same files — reuse the parses of the main checkout instead of parsing everything again;
# * each indexed folder ("root") records which content each of its files had (with mtime and size), so after a
#   restart only files that changed since are read and parsed.
#
# Without a configured file (tests, scripts) the same store lives in memory for the life of the process.

PARSER_VERSION = "1"  # bump whenever extract_file_symbols() output changes: stored parses are then discarded

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, parse_error INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS defs (blob INTEGER NOT NULL, name TEXT NOT NULL, lower TEXT NOT NULL, kind TEXT NOT NULL,
                                 start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, parent TEXT,
                                 signature TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS defs_name ON defs(name);
CREATE INDEX IF NOT EXISTS defs_lower ON defs(lower);
CREATE INDEX IF NOT EXISTS defs_blob ON defs(blob);
CREATE TABLE IF NOT EXISTS calls (blob INTEGER NOT NULL, name TEXT NOT NULL, line INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS calls_name ON calls(name);
CREATE INDEX IF NOT EXISTS calls_blob ON calls(blob);
CREATE TABLE IF NOT EXISTS imports (blob INTEGER NOT NULL, spec TEXT NOT NULL, line INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS imports_blob ON imports(blob);
CREATE TABLE IF NOT EXISTS roots (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS files (root INTEGER NOT NULL, rel TEXT NOT NULL, blob INTEGER NOT NULL,
                                  mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, PRIMARY KEY (root, rel))
                                  WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS files_blob ON files(blob, root);
"""
_PARSE_TABLES = ("defs", "calls", "imports")


class SymbolStore:
    """The SQLite file behind every SymbolIndex. Thread-safe: one connection, one lock."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        target = str(self.path) if self.path is not None else ":memory:"
        try:
            conn = self._connect(target)
        except sqlite3.DatabaseError:
            if self.path is None:
                raise
            # A damaged index is only a cache: start over rather than fail every task.
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            conn = self._connect(target)
        return conn

    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key = 'parser_version'").fetchone()
        if row is None or row[0] != PARSER_VERSION:
            conn.execute("BEGIN")
            for table in ("files", "blobs", *_PARSE_TABLES):
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('parser_version', ?)", (PARSER_VERSION,))
            conn.execute("COMMIT")
        return conn

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def root_id(self, root: str) -> int:
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO roots(path) VALUES (?)", (root,))
            return self.conn.execute("SELECT id FROM roots WHERE path = ?", (root,)).fetchone()[0]

    def drop_root(self, root: str) -> None:
        """Forget an indexed folder (e.g. a deleted task worktree). Its parses stay until collect_garbage()."""
        with self.lock:
            row = self.conn.execute("SELECT id FROM roots WHERE path = ?", (root,)).fetchone()
            if row:
                self.conn.execute("BEGIN")
                self.conn.execute("DELETE FROM files WHERE root = ?", (row[0],))
                self.conn.execute("DELETE FROM roots WHERE id = ?", (row[0],))
                self.conn.execute("COMMIT")

    def collect_garbage(self) -> dict:
        """Forget folders that no longer exist and parses no folder refers to any more."""
        with self.lock:
            gone = [(rid, p) for rid, p in self.conn.execute("SELECT id, path FROM roots") if not Path(p).is_dir()]
            self.conn.execute("BEGIN")
            for rid, _ in gone:
                self.conn.execute("DELETE FROM files WHERE root = ?", (rid,))
                self.conn.execute("DELETE FROM roots WHERE id = ?", (rid,))
            orphans = "SELECT id FROM blobs WHERE id NOT IN (SELECT blob FROM files)"
            for table in _PARSE_TABLES:
                self.conn.execute(f"DELETE FROM {table} WHERE blob IN ({orphans})")  # noqa: S608
            removed = self.conn.execute(f"DELETE FROM blobs WHERE id IN ({orphans})").rowcount  # noqa: S608
            self.conn.execute("COMMIT")
            return {"roots_removed": len(gone), "parses_removed": removed}

    def compact(self) -> dict:
        """Forget what is no longer needed, then give the space back to the disk."""
        result = self.collect_garbage()
        if self.path is not None:
            with self.lock:
                self.conn.execute("VACUUM")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return result

    def size_bytes(self) -> int:
        if self.path is None:
            return 0
        return sum(Path(f"{self.path}{s}").stat().st_size for s in ("", "-wal") if Path(f"{self.path}{s}").exists())


_STORE: SymbolStore | None = None
_STORE_LOCK = threading.Lock()


def configure_store(path: Path | None) -> SymbolStore:
    """Use the index file at ``path`` (None: in memory) from now on, and tidy it up. Called once at startup."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None or _STORE.path != path:
            _STORE = SymbolStore(path)
        store = _STORE
    with _SYMBOL_LOCK:
        _SYMBOL_INDEXES.clear()
    try:
        store.collect_garbage()
    except sqlite3.Error:
        pass
    return store


def symbol_store() -> SymbolStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SymbolStore(None)
        return _STORE


def _content_key(rel: str, text: str) -> str:
    digest = hashlib.blake2b(text.encode("utf-8", errors="replace"), digest_size=16).hexdigest()
    return f"{language_for(rel) or ''}:{digest}"


@dataclass
class CallSite:
    path: str
    line: int
    text: str
    caller: Symbol | None


ProgressFn = Callable[[int, int], None]  # (files indexed so far, files to index)

_BATCH = 400  # files parsed per transaction: progress survives an interruption, the lock is released in between


class SymbolIndex:
    """Definitions, call sites and imports of one folder's code files, answered from the SQLite store."""

    def __init__(self, index: RepoIndex, store: SymbolStore | None = None) -> None:
        self.index = index
        self.store = store or symbol_store()
        self.root = str(index.root)
        self.truncated = False
        self.parsed_now = 0  # files parsed by the last build() (the rest came from the store)
        self._root_id: int | None = None
        self._symbols_cache: dict[str, list[Symbol]] = {}

    @property
    def root_id(self) -> int:
        if self._root_id is None:
            self._root_id = self.store.root_id(self.root)
        return self._root_id

    # building ---------------------------------------------------------------------
    def build(self, progress: ProgressFn | None = None) -> SymbolIndex:
        files = self.index.code_files()
        if len(files) > MAX_SYMBOL_FILES:
            files = files[:MAX_SYMBOL_FILES]
            self.truncated = True
        root_id = self.root_id
        known = {rel: (blob, mtime, size) for rel, blob, mtime, size in
                 self.store.query("SELECT rel, blob, mtime_ns, size FROM files WHERE root = ?", (root_id,))}
        changed: list[str] = []
        current: set[str] = set()
        for rel in files:
            try:
                st = self.index.resolve(rel).stat()
            except (OSError, ValueError):
                continue
            if st.st_size > MAX_PARSE_BYTES:
                continue
            current.add(rel)
            row = known.get(rel)
            if row is None or row[1] != st.st_mtime_ns or row[2] != st.st_size:
                changed.append(rel)
        stale = [rel for rel in known if rel not in current]
        if stale:
            with self.store.lock:
                self.store.conn.execute("BEGIN")
                self.store.conn.executemany("DELETE FROM files WHERE root = ? AND rel = ?",
                                            [(root_id, rel) for rel in stale])
                self.store.conn.execute("COMMIT")
        for start in range(0, len(changed), _BATCH):
            self._index_batch(changed[start:start + _BATCH])
            if progress is not None:
                progress(min(start + _BATCH, len(changed)), len(changed))
        self._symbols_cache.clear()
        return self

    def _index_batch(self, rels: list[str]) -> None:
        entries = []
        for rel in rels:
            entry = self.index.read(rel, limit=MAX_PARSE_BYTES)
            entries.append((rel, entry, _content_key(rel, entry.text) if entry is not None else None))
        keys = list({key for _, _, key in entries if key})
        with self.store.lock:
            have = dict(self._blob_ids(keys))
        parsed: dict[str, FileSymbols] = {}
        for rel, entry, key in entries:
            if key and key not in have and key not in parsed:
                try:
                    parsed[key] = extract_file_symbols(rel, entry.text)
                except Exception:  # noqa: BLE001 - a single unparsable file must not break indexing
                    parsed[key] = FileSymbols(parse_error=True)
        conn = self.store.conn
        with self.store.lock:
            conn.execute("BEGIN")
            try:
                have.update(self._blob_ids(list(parsed)))  # another build may have stored them meanwhile
                for key, fs in parsed.items():
                    if key in have:
                        continue
                    blob = conn.execute("INSERT INTO blobs(key, parse_error) VALUES (?, ?)",
                                        (key, int(fs.parse_error))).lastrowid
                    have[key] = blob
                    conn.executemany("INSERT INTO defs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     [(blob, s.name, s.name.lower(), s.kind, s.start_line, s.end_line, s.parent,
                                       s.signature) for s in fs.symbols])
                    conn.executemany("INSERT INTO calls VALUES (?, ?, ?)", [(blob, n, ln) for n, ln in fs.calls])
                    conn.executemany("INSERT INTO imports VALUES (?, ?, ?)", [(blob, sp, ln) for sp, ln in fs.imports])
                rows, gone = [], []
                for rel, entry, key in entries:
                    if entry is None:
                        gone.append((self.root_id, rel))
                    else:
                        rows.append((self.root_id, rel, have[key], entry.mtime_ns, entry.size))
                conn.executemany("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)", rows)
                conn.executemany("DELETE FROM files WHERE root = ? AND rel = ?", gone)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        self.parsed_now += len(parsed)

    def _blob_ids(self, keys: list[str]) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            marks = ",".join("?" * len(chunk))
            out += self.store.conn.execute(f"SELECT key, id FROM blobs WHERE key IN ({marks})",  # noqa: S608
                                           chunk).fetchall()
        return out

    # queries ----------------------------------------------------------------------
    _DEF_COLUMNS = "f.rel, d.name, d.kind, d.start_line, d.end_line, d.parent, d.signature"

    @staticmethod
    def _symbol(row: tuple) -> Symbol:
        rel, name, kind, start, end, parent, sig = row
        return Symbol(name, kind, rel, start, end, parent, sig)

    def _defs_where(self, where: str, params: tuple, limit: int | None = None) -> list[Symbol]:
        sql = (f"SELECT {self._DEF_COLUMNS} FROM defs d JOIN files f ON f.blob = d.blob AND f.root = ? "
               f"WHERE {where} ORDER BY f.rel, d.rowid")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [self._symbol(r) for r in self.store.query(sql, (self.root_id, *params))]

    @property
    def symbol_count(self) -> int:
        return self.store.query("SELECT count(*) FROM defs d JOIN files f ON f.blob = d.blob WHERE f.root = ?",
                                (self.root_id,))[0][0]

    @property
    def file_count(self) -> int:
        return self.store.query("SELECT count(*) FROM files WHERE root = ?", (self.root_id,))[0][0]

    def definitions(self, name: str) -> list[Symbol]:
        name = name.strip().strip("`'\"()")
        if "." in name:
            parent, _, short = name.rpartition(".")
            parent_short = parent.rsplit(".", 1)[-1]
            exact = self._defs_where("d.name = ? AND d.parent = ?", (short, parent_short))
            if exact:
                return exact
            name = short
        found = self._defs_where("d.name = ?", (name,)) or self._defs_where("d.lower = ?", (name.lower(),))
        # Prefer non-test definitions first.
        return sorted(found, key=lambda s: (is_test_path(s.path), s.path, s.start_line))

    def symbols_in(self, path: str) -> list[Symbol]:
        cached = self._symbols_cache.get(path)
        if cached is None:
            cached = self._defs_where("f.rel = ?", (path,))
            if len(self._symbols_cache) > 2000:
                self._symbols_cache.clear()
            self._symbols_cache[path] = cached
        return list(cached)

    def enclosing(self, path: str, line: int) -> Symbol | None:
        best: Symbol | None = None
        for sym in self.symbols_in(path):
            if sym.start_line <= line <= sym.end_line and sym.kind not in ("module",):
                if best is None or (sym.end_line - sym.start_line) <= (best.end_line - best.start_line):
                    best = sym
        return best

    def callers(self, name: str, limit: int = 30) -> list[CallSite]:
        short = name.rsplit(".", 1)[-1]
        definitions = {(s.path, s.start_line) for s in self._defs_where("d.name = ?", (short,))}
        out: list[CallSite] = []
        with self.store.lock:
            cursor = self.store.conn.execute(
                "SELECT f.rel, c.line FROM calls c JOIN files f ON f.blob = c.blob AND f.root = ? "
                "WHERE c.name = ? ORDER BY f.rel, c.rowid", (self.root_id, short))
            sites = cursor.fetchmany(limit * 20 + 100)
        for path, line in sites:
            if (path, line) in definitions:
                continue
            lines = self.index.read_lines(path) or []
            text = lines[line - 1].strip()[:200] if 0 < line <= len(lines) else ""
            caller = self.enclosing(path, line)
            if caller is not None and caller.name == short and caller.start_line == line:
                continue
            out.append(CallSite(path, line, text, caller))
            if len(out) >= limit:
                break
        out.sort(key=lambda c: (is_test_path(c.path), c.path, c.line))
        return out

    def callees(self, symbol: Symbol, limit: int = 40) -> list[tuple[str, int, list[Symbol]]]:
        calls = self.store.query(
            "SELECT c.name, c.line FROM calls c JOIN files f ON f.blob = c.blob AND f.root = ? "
            "WHERE f.rel = ? AND c.line BETWEEN ? AND ? ORDER BY c.rowid",
            (self.root_id, symbol.path, symbol.start_line, symbol.end_line))
        out: list[tuple[str, int, list[Symbol]]] = []
        seen: set[str] = set()
        for name, line in calls:
            if name != symbol.name and name not in seen:
                seen.add(name)
                out.append((name, line, self._defs_where("d.name = ?", (name,), limit=3)))
                if len(out) >= limit:
                    break
        return out

    def call_chain(self, name: str, depth: int = 3, fanout: int = 4) -> list[dict]:
        """Upward chains of callers: [{symbol, path, line, callers: [...]}] (static, name-based)."""
        visited: set[str] = set()

        def expand(target: str, level: int) -> list[dict]:
            if level >= depth or target in visited:
                return []
            visited.add(target)
            nodes = []
            for site in self.callers(target, limit=fanout * 3)[:fanout]:
                caller_name = site.caller.qualname if site.caller else "<module>"
                node = {"caller": caller_name, "path": site.path, "line": site.line, "text": site.text,
                        "callers": expand(site.caller.name, level + 1) if site.caller else []}
                nodes.append(node)
            return nodes

        return expand(name.rsplit(".", 1)[-1], 0)

    def imports_of(self, path: str) -> list[tuple[str, int]]:
        return [(spec, line) for spec, line in self.store.query(
            "SELECT i.spec, i.line FROM imports i JOIN files f ON f.blob = i.blob AND f.root = ? "
            "WHERE f.rel = ? ORDER BY i.rowid", (self.root_id, path))]

    def importers_of(self, path: str, limit: int = 40) -> list[tuple[str, int, str]]:
        """Files whose import specifiers plausibly resolve to ``path``."""
        stem = path.rsplit(".", 1)[0]
        module_dotted = stem.replace("/", ".")
        if module_dotted.endswith(".__init__"):
            module_dotted = module_dotted[: -len(".__init__")]
        tail = stem.rsplit("/", 1)[-1]
        last = module_dotted.split(".")[-1]
        # Every kind of match below contains the module's last name or the file's stem: let SQLite pre-filter.
        rows = self.store.query(
            "SELECT f.rel, i.spec, i.line FROM imports i JOIN files f ON f.blob = i.blob AND f.root = ? "
            "WHERE f.rel != ? AND (instr(i.spec, ?) > 0 OR instr(i.spec, ?) > 0) ORDER BY f.rel, i.rowid",
            (self.root_id, path, last, tail))
        out: list[tuple[str, int, str]] = []
        done: set[str] = set()
        for rel, spec, line in rows:
            if rel in done:
                continue
            s = spec.strip()
            if not s:
                continue
            dotted_hit = module_dotted.endswith(s.lstrip(".")) and s.lstrip(".") and (
                s.lstrip(".").split(".")[-1] == module_dotted.split(".")[-1] or s.lstrip(".") == module_dotted)
            rel_hit = (s.startswith(".") and "/" in s and s.rsplit("/", 1)[-1].split(".")[0] == tail)
            if dotted_hit or rel_hit or s == module_dotted:
                out.append((rel, line, s))
                done.add(rel)
                if len(out) >= limit:
                    break
        return out


_SYMBOL_INDEXES: dict[str, tuple[float, SymbolIndex]] = {}
_SYMBOL_LOCK = threading.Lock()


def get_symbol_index(index: RepoIndex, progress: ProgressFn | None = None) -> SymbolIndex:
    """Update (or reuse, if the inventory has not been refreshed) the symbol index for a folder."""
    key = str(index.root)
    with _SYMBOL_LOCK:
        cached = _SYMBOL_INDEXES.get(key)
        if cached and cached[0] == index.refreshed_at:
            return cached[1]
    built = SymbolIndex(index).build(progress)
    with _SYMBOL_LOCK:
        _SYMBOL_INDEXES[key] = (index.refreshed_at, built)
        while len(_SYMBOL_INDEXES) > 24:
            _SYMBOL_INDEXES.pop(next(iter(_SYMBOL_INDEXES)))
    return built


def forget_symbols(root: Path) -> None:
    """Drop a folder from the index (its worktree was removed)."""
    key = str(Path(root).resolve())
    with _SYMBOL_LOCK:
        _SYMBOL_INDEXES.pop(key, None)
    try:
        symbol_store().drop_root(key)
    except sqlite3.Error:
        pass
