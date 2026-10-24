"""Symbol extraction (definitions, call sites, imports) and static call-chain analysis.

Python uses the standard-library ``ast`` module; JavaScript, TypeScript, Go,
Java and Rust use Tree-sitter grammars; other languages fall back to regular
expressions. Everything here is *static, name-based* analysis: callers and
callees are matched by identifier, so results for very common names can include
false positives. Reports label these findings accordingly.
"""

from __future__ import annotations

import ast
import importlib
import re
import threading
from dataclasses import dataclass, field
from functools import cache

from locpilot.tools.files import RepoIndex, is_test_path, language_for

MAX_PARSE_BYTES = 400_000
MAX_SYMBOL_FILES = 8_000


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

@dataclass
class CallSite:
    path: str
    line: int
    text: str
    caller: Symbol | None


_FILE_CACHE: dict[tuple[str, str, int, int], FileSymbols] = {}
_FILE_CACHE_LOCK = threading.Lock()


class SymbolIndex:
    def __init__(self, index: RepoIndex) -> None:
        self.index = index
        self.by_file: dict[str, FileSymbols] = {}
        self.by_name: dict[str, list[Symbol]] = {}
        self.by_lower: dict[str, list[Symbol]] = {}
        self.callsites: dict[str, list[tuple[str, int]]] = {}
        self.truncated = False

    def build(self) -> SymbolIndex:
        files = self.index.code_files()
        if len(files) > MAX_SYMBOL_FILES:
            files = files[:MAX_SYMBOL_FILES]
            self.truncated = True
        for rel in files:
            entry = self.index.read(rel, limit=MAX_PARSE_BYTES)
            if entry is None:
                continue
            key = (str(self.index.root), rel, entry.mtime_ns, entry.size)
            with _FILE_CACHE_LOCK:
                parsed = _FILE_CACHE.get(key)
            if parsed is None:
                try:
                    parsed = extract_file_symbols(rel, entry.text)
                except Exception:  # noqa: BLE001 - a single unparsable file must not break indexing
                    parsed = FileSymbols(parse_error=True)
                with _FILE_CACHE_LOCK:
                    if len(_FILE_CACHE) > 50_000:
                        _FILE_CACHE.clear()
                    _FILE_CACHE[key] = parsed
            self.by_file[rel] = parsed
            for sym in parsed.symbols:
                self.by_name.setdefault(sym.name, []).append(sym)
                self.by_lower.setdefault(sym.name.lower(), []).append(sym)
            for name, line in parsed.calls:
                self.callsites.setdefault(name, []).append((rel, line))
        return self

    @property
    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.by_file.values())

    def definitions(self, name: str) -> list[Symbol]:
        name = name.strip().strip("`'\"()")
        if "." in name:
            parent, _, short = name.rpartition(".")
            parent_short = parent.rsplit(".", 1)[-1]
            exact = [s for s in self.by_name.get(short, []) if s.parent == parent_short]
            if exact:
                return exact
            name = short
        found = self.by_name.get(name) or self.by_lower.get(name.lower(), [])
        # Prefer non-test definitions first.
        return sorted(found, key=lambda s: (is_test_path(s.path), s.path, s.start_line))

    def symbols_in(self, path: str) -> list[Symbol]:
        parsed = self.by_file.get(path)
        return list(parsed.symbols) if parsed else []

    def enclosing(self, path: str, line: int) -> Symbol | None:
        best: Symbol | None = None
        for sym in self.symbols_in(path):
            if sym.start_line <= line <= sym.end_line and sym.kind not in ("module",):
                if best is None or (sym.end_line - sym.start_line) <= (best.end_line - best.start_line):
                    best = sym
        return best

    def callers(self, name: str, limit: int = 30) -> list[CallSite]:
        short = name.rsplit(".", 1)[-1]
        definitions = {(s.path, s.start_line) for s in self.by_name.get(short, [])}
        out: list[CallSite] = []
        for path, line in self.callsites.get(short, []):
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
        parsed = self.by_file.get(symbol.path)
        if parsed is None:
            return []
        out: list[tuple[str, int, list[Symbol]]] = []
        seen: set[str] = set()
        for name, line in parsed.calls:
            if symbol.start_line <= line <= symbol.end_line and name != symbol.name and name not in seen:
                seen.add(name)
                out.append((name, line, self.by_name.get(name, [])[:3]))
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
        parsed = self.by_file.get(path)
        return list(parsed.imports) if parsed else []

    def importers_of(self, path: str, limit: int = 40) -> list[tuple[str, int, str]]:
        """Files whose import specifiers plausibly resolve to ``path``."""
        stem = path.rsplit(".", 1)[0]
        module_dotted = stem.replace("/", ".")
        if module_dotted.endswith(".__init__"):
            module_dotted = module_dotted[: -len(".__init__")]
        tail = stem.rsplit("/", 1)[-1]
        out: list[tuple[str, int, str]] = []
        for rel, parsed in self.by_file.items():
            if rel == path:
                continue
            for spec, line in parsed.imports:
                s = spec.strip()
                if not s:
                    continue
                dotted_hit = module_dotted.endswith(s.lstrip(".")) and s.lstrip(".") and (
                    s.lstrip(".").split(".")[-1] == module_dotted.split(".")[-1] or s.lstrip(".") == module_dotted)
                rel_hit = (s.startswith(".") and "/" in s and s.rsplit("/", 1)[-1].split(".")[0] == tail)
                if dotted_hit or rel_hit or s == module_dotted:
                    out.append((rel, line, s))
                    break
            if len(out) >= limit:
                break
        return out


_SYMBOL_INDEXES: dict[str, tuple[float, SymbolIndex]] = {}
_SYMBOL_LOCK = threading.Lock()


def get_symbol_index(index: RepoIndex) -> SymbolIndex:
    """Build (or reuse, if the inventory has not been refreshed) the symbol index for a repo."""
    key = str(index.root)
    with _SYMBOL_LOCK:
        cached = _SYMBOL_INDEXES.get(key)
        if cached and cached[0] == index.refreshed_at:
            return cached[1]
    built = SymbolIndex(index).build()
    with _SYMBOL_LOCK:
        _SYMBOL_INDEXES[key] = (index.refreshed_at, built)
    return built
