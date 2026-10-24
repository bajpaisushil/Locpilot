"""Evidence retrieval: deterministic seeding plus bounded, model-requested read-only tools.

Evidence items are real file ranges with stable ids (E1, E2, …). Their exact
content (with line numbers) is what the model sees, and citations are later
validated against these items and the repository on disk.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from locpilot.agent.schemas import TaskUnderstanding, ToolRequest
from locpilot.security.paths import PathSecurityError, normalize_relpath
from locpilot.security.secrets import redact
from locpilot.tools import git
from locpilot.tools.files import RepoIndex, is_test_path, number_lines
from locpilot.tools.search import SearchError, search
from locpilot.tools.symbols import Symbol, SymbolIndex

MAX_ITEM_LINES = 110
MAX_READ_LINES = 160
STOPWORDS = set("""
a an the and or but if then else when while for to of in on at by with from into onto over under about as is are was
were be been being it its this that these those there here what which who whom whose why how where can could should
would will shall may might must do does did done doing have has had having not no yes all any some each every both
few more most other such only own same so than too very just also again once please explain show tell find look
investigate investigation analyse analyze root cause causes bug bugs issue issues problem problems error errors fix
fixes fixed fixing add adding added test tests testing code codebase repository repo project file files function
functions method methods class classes step steps works work working happen happens happening why returns return
returned get gets got make makes made use uses used using want need needs like seem seems wrong correct correctly
incorrect incorrectly sometimes always never smallest possible change changes minimal regression implement
implementation feature
""".split())
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_QUOTED = re.compile(r"\"([^\"\n]{3,120})\"|'([^'\n]{3,120})'|`([^`\n]{2,120})`")
_PATHISH = re.compile(r"(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}|[\w-]+\.(?:py|js|ts|tsx|jsx|go|java|rs|rb|php|cs|kt)\b")


@dataclass
class QueryTerms:
    identifiers: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    def all_terms(self) -> list[str]:
        return list(dict.fromkeys([*self.identifiers, *self.words]))


def split_identifier(name: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name.replace(".", " ").replace("_", " ")).split()
    return [p.lower() for p in parts if len(p) >= 3]


def _stem(word: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ies", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def extract_terms(request: str, understanding: TaskUnderstanding | None) -> QueryTerms:
    terms = QueryTerms()
    for m in _QUOTED.finditer(request):
        q = next(g for g in m.groups() if g)
        terms.quoted.append(q.strip())
    terms.paths = list(dict.fromkeys(_PATHISH.findall(request)))
    idents: list[str] = []
    for tok in _IDENT.findall(request):
        if ("_" in tok or re.search(r"[a-z][A-Z]", tok) or "." in tok) and len(tok) >= 3 and tok not in terms.paths:
            idents.append(tok)
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", request)]
    if understanding is not None:
        idents += [s for s in understanding.symbols if _IDENT.fullmatch(s.strip("()"))]
        for t in understanding.search_terms:
            t = t.strip()
            if not t:
                continue
            if _IDENT.fullmatch(t) and ("_" in t or re.search(r"[a-z][A-Z]", t)):
                idents.append(t)
            else:
                words += [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", t)]
        terms.quoted += [e for e in understanding.error_messages if 3 <= len(e) <= 160]
        terms.paths += [p for p in understanding.file_hints if p]
    terms.identifiers = list(dict.fromkeys(i.strip("()") for i in idents))[:16]
    stemmed = []
    for w in words:
        if w in STOPWORDS or w.isdigit():
            continue
        stemmed.append(_stem(w))
    terms.words = list(dict.fromkeys(stemmed))[:16]
    terms.quoted = list(dict.fromkeys(terms.quoted))[:6]
    terms.paths = list(dict.fromkeys(terms.paths))[:8]
    return terms


class EvidenceCollector:
    """Accumulates evidence items (dicts kept in graph state) within a size budget."""

    def __init__(self, index: RepoIndex, symbols: SymbolIndex, items: list[dict] | None = None,
                 max_chars: int = 26_000) -> None:
        self.index = index
        self.symbols = symbols
        self.items: list[dict] = list(items or [])
        self.max_chars = max_chars
        self.notes: list[str] = []

    @property
    def used_chars(self) -> int:
        return sum(len(i.get("content", "")) for i in self.items)

    def _next_id(self) -> str:
        nums = [int(i["id"][1:]) for i in self.items if re.fullmatch(r"E\d+", i["id"])]
        return f"E{max(nums, default=0) + 1}"

    def _overlaps(self, path: str, start: int, end: int) -> dict | None:
        for item in self.items:
            if item["path"] == path and not (end < item["start_line"] or start > item["end_line"]):
                covered = min(end, item["end_line"]) - max(start, item["start_line"]) + 1
                if covered >= 0.6 * (end - start + 1):
                    return item
        return None

    def add(self, path: str, start: int, end: int, *, kind: str, title: str, reason: str,
            symbol: str | None = None) -> dict | None:
        lines = self.index.read_lines(path)
        if not lines:
            return None
        start = max(1, start)
        end = min(len(lines), max(start, end))
        if end - start + 1 > MAX_ITEM_LINES:
            end = start + MAX_ITEM_LINES - 1
        existing = self._overlaps(path, start, end)
        if existing is not None:
            return existing
        content = redact(number_lines(lines, start, end))
        if self.used_chars + len(content) > self.max_chars:
            return None
        item = {"id": self._next_id(), "path": path, "start_line": start, "end_line": end, "kind": kind,
                "title": title[:160], "reason": reason[:200], "symbol": symbol, "content": content}
        self.items.append(item)
        return item

    def add_symbol(self, sym: Symbol, *, kind: str = "definition", reason: str) -> dict | None:
        return self.add(sym.path, sym.start_line, sym.end_line, kind=kind,
                        title=f"{sym.kind} {sym.qualname}", reason=reason, symbol=sym.qualname)

    def add_window(self, path: str, line: int, *, radius: int, kind: str, title: str, reason: str) -> dict | None:
        enclosing = self.symbols.enclosing(path, line)
        if enclosing is not None and enclosing.end_line - enclosing.start_line <= 70:
            return self.add_symbol(enclosing, kind=kind, reason=reason)
        return self.add(path, line - radius, line + radius, kind=kind, title=title, reason=reason)

    # seeding ---------------------------------------------------------------------------
    def seed(self, request: str, understanding: TaskUnderstanding | None, intent: str) -> dict:
        terms = extract_terms(request, understanding)
        log: dict = {"terms": terms.all_terms()[:20], "quoted": terms.quoted, "paths": terms.paths}

        # 1. Explicit paths
        for raw in terms.paths:
            path = self._match_path(raw)
            if not path:
                continue
            syms = self.symbols.symbols_in(path)
            if syms and len(self.index.read_lines(path) or []) > MAX_ITEM_LINES:
                for sym in [s for s in syms if s.parent is None][:4]:
                    self.add_symbol(sym, reason=f"defined in {path}, which the request mentions")
            else:
                self.add(path, 1, MAX_ITEM_LINES, kind="file", title=f"file {path}", reason="mentioned in the request")

        # 2. Symbols mentioned (definitions, then callers)
        primary: list[Symbol] = []
        for ident in terms.identifiers:
            defs = self.symbols.definitions(ident)
            for sym in defs[:2]:
                if self.add_symbol(sym, reason=f"definition of `{ident}`"):
                    primary.append(sym)
        if intent in ("explain", "root_cause", "fix", "question") and primary:
            for sym in primary[:2]:
                for site in self.symbols.callers(sym.name, limit=8)[:3]:
                    self.add_window(site.path, site.line, radius=5, kind="caller",
                                    title=f"call to {sym.name} in {site.caller.qualname if site.caller else site.path}",
                                    reason=f"caller of `{sym.name}`")

        # 3. Exact strings (error messages, quoted text)
        for quoted in terms.quoted:
            try:
                result = search(self.index, quoted, max_results=12, max_per_file=3)
            except SearchError:
                continue
            for hit in result.hits[:3]:
                self.add_window(hit.path, hit.line, radius=8, kind="search_hit",
                                title=f"'{quoted[:40]}' in {hit.path}", reason=f"contains the text '{quoted[:60]}'")

        # 4. Ranked keyword retrieval
        ranked = self.rank_files(terms)
        log["ranked_files"] = [p for p, _ in ranked[:8]]
        for path, _score in ranked[:6]:
            window = self.best_window(path, terms)
            if window is None:
                continue
            start, end = window
            enclosing = self.symbols.enclosing(path, (start + end) // 2)
            if enclosing is not None and enclosing.end_line - enclosing.start_line <= 90:
                self.add_symbol(enclosing, kind="search_hit", reason="matches the request's keywords")
            else:
                self.add(path, start, end, kind="search_hit", title=f"{path}:{start}-{end}",
                         reason="matches the request's keywords")

        # 5. Related tests for the main code
        targets = [s.name for s in primary[:2]] or [i["symbol"].rsplit(".", 1)[-1] for i in self.items
                                                     if i.get("symbol")][:2]
        for name in targets:
            self.find_tests(name, limit=1)
        log["evidence_count"] = len(self.items)
        return log

    def _match_path(self, raw: str) -> str | None:
        raw = raw.strip("`'\"").lstrip("./")
        try:
            raw = normalize_relpath(raw)
        except PathSecurityError:
            return None
        if self.index.has_file(raw):
            return raw
        matches = [f for f in self.index.files if f.endswith("/" + raw) or f == raw]
        return matches[0] if len(matches) == 1 else None

    def rank_files(self, terms: QueryTerms) -> list[tuple[str, float]]:
        vocab = [t.lower() for t in terms.identifiers] + terms.words
        vocab += [p for ident in terms.identifiers for p in split_identifier(ident)]
        vocab = [v for v in dict.fromkeys(vocab) if len(v) >= 3]
        if not vocab:
            return []
        files = [f for f in self.index.files if self.index.read(f) is not None]
        n = max(len(files), 1)
        df: Counter[str] = Counter()
        tf: dict[str, Counter[str]] = {}
        for rel in files:
            entry = self.index.read(rel)
            if entry is None:
                continue
            counts = Counter()
            low_path = rel.lower()
            for term in vocab:
                c = entry.lower.count(term)
                if c or term in low_path:
                    counts[term] = c
            if counts:
                tf[rel] = counts
                for term in counts:
                    df[term] += 1
        scores: list[tuple[str, float]] = []
        for rel, counts in tf.items():
            score = 0.0
            for term, c in counts.items():
                idf = math.log(1 + n / (1 + df[term]))
                score += idf * (1 + math.log(1 + c))
                if term in rel.lower():
                    score += 1.5 * idf
            score += 0.6 * len(counts)  # reward covering many distinct terms
            if is_test_path(rel):
                score *= 0.6
            symbols_here = {s.name.lower() for s in self.symbols.symbols_in(rel)}
            score += sum(2.0 for t in terms.identifiers if t.lower().rsplit(".", 1)[-1] in symbols_here)
            scores.append((rel, score))
        scores.sort(key=lambda kv: -kv[1])
        return scores

    def best_window(self, path: str, terms: QueryTerms, size: int = 36) -> tuple[int, int] | None:
        lines = self.index.read_lines(path)
        if not lines:
            return None
        vocab = [t.lower() for t in terms.identifiers] + terms.words
        hits = [len({t for t in vocab if t in line.lower()}) for line in lines]
        if not any(hits):
            return None
        best, best_start = -1, 0
        running = sum(hits[:size])
        for start in range(0, max(1, len(lines) - size + 1)):
            if start:
                running += (hits[start + size - 1] if start + size - 1 < len(hits) else 0) - hits[start - 1]
            if running > best:
                best, best_start = running, start
        return best_start + 1, min(len(lines), best_start + size)

    # model-requested tools ---------------------------------------------------------------
    def find_tests(self, name: str, limit: int = 2) -> list[dict]:
        tests = [f for f in self.index.files if is_test_path(f)]
        added = []
        try:
            result = search(self.index, name, word=True, files=tests, max_results=10, max_per_file=1)
        except SearchError:
            return added
        for hit in result.hits[:limit]:
            item = self.add_window(hit.path, hit.line, radius=10, kind="test", title=f"test referencing {name}",
                                   reason=f"existing test that references `{name}`")
            if item:
                added.append(item)
        return added

    def run_tool(self, req: ToolRequest) -> tuple[str, list[str]]:
        """Execute one read-only tool request. Returns (summary, new evidence ids)."""
        before = {i["id"] for i in self.items}
        q = req.query.strip()[:200]
        summary = ""
        try:
            if req.tool in ("search_text", "search_regex"):
                if not q:
                    return "empty query", []
                result = search(self.index, q, is_regex=req.tool == "search_regex", max_results=40, max_per_file=3)
                files = list(dict.fromkeys(h.path for h in result.hits))
                summary = f"{len(result.hits)} matches in {len(files)} files"
                for hit in result.hits[:4]:
                    self.add_window(hit.path, hit.line, radius=6, kind="search_hit", title=f"'{q[:40]}' in {hit.path}",
                                    reason=req.reason or f"matches '{q[:40]}'")
                if len(files) > 4:
                    self.notes.append(f"search '{q[:60]}' also matched: " + ", ".join(files[4:15]))
            elif req.tool == "find_definition":
                defs = self.symbols.definitions(q)
                summary = f"{len(defs)} definitions"
                for sym in defs[:3]:
                    self.add_symbol(sym, reason=req.reason or f"definition of `{q}`")
            elif req.tool == "find_callers":
                sites = self.symbols.callers(q, limit=12)
                summary = f"{len(sites)} call sites"
                for site in sites[:4]:
                    self.add_window(site.path, site.line, radius=5, kind="caller", title=f"call to {q} in {site.path}",
                                    reason=req.reason or f"caller of `{q}`")
                if len(sites) > 4:
                    self.notes.append(f"`{q}` is also called from: " +
                                      ", ".join(f"{s.path}:{s.line}" for s in sites[4:12]))
            elif req.tool == "read_file":
                path = self._match_path(req.path)
                if path is None:
                    return f"file not found: {req.path[:80]}", []
                start = max(1, req.start_line or 1)
                end = req.end_line if req.end_line >= start else start + 80
                end = min(end, start + MAX_READ_LINES - 1)
                self.add(path, start, end, kind="file", title=f"{path}:{start}-{end}", reason=req.reason or "requested")
                summary = f"read {path}:{start}-{end}"
            elif req.tool == "list_directory":
                rel = req.path.strip().strip("/")
                entries = self.index.list_dir(rel)
                listing = ", ".join(e["name"] + ("/" if e["type"] == "dir" else "") for e in entries[:60])
                self.notes.append(f"Directory `{rel or '.'}` contains: {listing}")
                summary = f"{len(entries)} entries"
            elif req.tool == "find_tests":
                added = self.find_tests(q or req.path, limit=2)
                summary = f"{len(added)} test snippets"
            elif req.tool == "git_log":
                path = self._match_path(req.path) if req.path else None
                commits = git.log(self.index.root, file=path, limit=8)
                self.notes.append(f"Recent commits{' for ' + path if path else ''}: " +
                                  "; ".join(f"{c['commit'][:8]} {c['date']} {c['subject'][:80]}" for c in commits))
                summary = f"{len(commits)} commits"
        except (SearchError, PathSecurityError, git.GitError) as exc:
            return f"error: {exc}", []
        new_ids = [i["id"] for i in self.items if i["id"] not in before]
        return summary or "no results", new_ids


def render_evidence(items: list[dict], *, max_lines_per_item: int | None = None) -> str:
    blocks = []
    for item in items:
        content = item["content"]
        if max_lines_per_item is not None:
            lines = content.splitlines()
            if len(lines) > max_lines_per_item:
                content = "\n".join(lines[:max_lines_per_item]) + f"\n… ({len(lines) - max_lines_per_item} more lines)"
        content = content.replace("</evidence>", "<\\/evidence>")
        blocks.append(f'<evidence id="{item["id"]}" file="{item["path"]}" lines="{item["start_line"]}-'
                      f'{item["end_line"]}" kind="{item["kind"]}">\n{content}\n</evidence>')
    return "\n".join(blocks)


def evidence_titles(items: list[dict]) -> str:
    return "\n".join(f"{i['id']} {i['path']}:{i['start_line']}-{i['end_line']} — {i['title']}" for i in items)
