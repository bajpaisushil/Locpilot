"""Fast text and regular-expression search over the repository inventory."""

from __future__ import annotations

import bisect
import fnmatch
import re
import time
from dataclasses import dataclass, field

import regex as safe_regex

from locpilot.tools.files import RepoIndex

MAX_LINE_CHARS = 300


@dataclass
class SearchHit:
    path: str
    line: int
    text: str


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    files_scanned: int = 0
    files_matched: int = 0
    truncated: bool = False
    error: str | None = None


class SearchError(ValueError):
    pass


def compile_pattern(query: str, *, is_regex: bool, case_sensitive: bool, word: bool):
    if not query or len(query) > 500:
        raise SearchError("query must be 1-500 characters")
    flags = 0 if case_sensitive else safe_regex.IGNORECASE
    body = query if is_regex else safe_regex.escape(query)
    if word:
        body = rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])"
    try:
        return safe_regex.compile(body, flags | safe_regex.MULTILINE)
    except safe_regex.error as exc:
        raise SearchError(f"invalid regular expression: {exc}") from exc


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def search(
    index: RepoIndex,
    query: str,
    *,
    is_regex: bool = False,
    case_sensitive: bool = False,
    word: bool = False,
    path_glob: str | None = None,
    files: list[str] | None = None,
    max_results: int = 200,
    max_per_file: int = 20,
    time_budget_s: float = 8.0,
) -> SearchResult:
    pattern = compile_pattern(query, is_regex=is_regex, case_sensitive=case_sensitive, word=word)
    result = SearchResult()
    deadline = time.monotonic() + time_budget_s
    literal = None if (is_regex or case_sensitive) else query.lower()
    candidates = files if files is not None else index.files
    for rel in candidates:
        if path_glob and not fnmatch.fnmatch(rel, path_glob):
            continue
        if time.monotonic() > deadline:
            result.truncated = True
            result.error = "search time budget exhausted"
            break
        entry = index.read(rel)
        if entry is None:
            continue
        result.files_scanned += 1
        if literal is not None and literal not in entry.lower:
            continue
        starts: list[int] | None = None
        per_file = 0
        seen_lines: set[int] = set()
        try:
            for match in pattern.finditer(entry.text, timeout=0.5):
                if starts is None:
                    starts = _line_starts(entry.text)
                line_no = bisect.bisect_right(starts, match.start())
                if line_no in seen_lines:
                    continue
                seen_lines.add(line_no)
                text = entry.lines[line_no - 1] if line_no - 1 < len(entry.lines) else ""
                result.hits.append(SearchHit(rel, line_no, text.strip()[:MAX_LINE_CHARS]))
                per_file += 1
                if per_file >= max_per_file or len(result.hits) >= max_results:
                    break
        except TimeoutError:
            result.error = f"regex timed out on {rel}; skipped"
            continue
        if per_file:
            result.files_matched += 1
        if len(result.hits) >= max_results:
            result.truncated = True
            break
    return result
