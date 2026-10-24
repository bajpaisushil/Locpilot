"""Reviewable, validated code edits.

The model proposes SEARCH/REPLACE blocks. LocPilot — not the model — turns
them into concrete file changes: each SEARCH must match exactly one location in
the *real* file (exact, then whitespace-tolerant matching), the result must
still parse, and the unified diff shown to the user is computed with difflib
from actual file contents. Applying a change verifies the file still has the
content the diff was computed from (unexpected-change detection) and writes
atomically. Rollback restores originals only for files nobody touched since.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from locpilot.security.paths import PathSecurityError, normalize_relpath, safe_join
from locpilot.tools.files import is_excluded, is_test_path, sha256_text
from locpilot.tools.symbols import syntax_errors

MAX_FILES_PER_PATCH = 8
MAX_FILE_BYTES = 1_000_000

_HEAD = re.compile(r"^\s*<{5,9}\s*SEARCH\b.*$")
_DIVIDER = re.compile(r"^\s*={5,9}\s*$")
_TAIL = re.compile(r"^\s*>{5,9}\s*REPLACE\b.*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


class EditError(ValueError):
    pass


class UnexpectedChangeError(RuntimeError):
    def __init__(self, path: str) -> None:
        super().__init__(f"{path} changed since the patch was generated; refusing to overwrite it")
        self.path = path


@dataclass
class EditBlock:
    path: str
    search: str
    replace: str


@dataclass
class FileChange:
    path: str
    change: str  # "modify" | "create"
    kind: str  # "fix" | "test"
    before: str | None
    after: str
    before_sha: str | None
    after_sha: str
    diff: str
    additions: int
    deletions: int

    def summary(self) -> dict:
        return {"path": self.path, "change": self.change, "kind": self.kind,
                "additions": self.additions, "deletions": self.deletions}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> FileChange:
        return cls(**data)


@dataclass
class PatchBuild:
    changes: list[FileChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.changes)

    @property
    def diff(self) -> str:
        return "".join(c.diff for c in self.changes)


# --- Parsing -------------------------------------------------------------------------

def _clean_filename(line: str) -> str | None:
    text = line.strip()
    fence = _FENCE.match(text)
    if fence:
        rest = text[len(fence.group(1)):].strip().split()
        text = rest[-1] if len(rest) >= 2 else ""
    text = re.sub(r"^(#+\s*|\*\*|File:\s*|Path:\s*|Filename:\s*|filepath:\s*)", "", text, flags=re.IGNORECASE)
    text = text.strip().strip("`*'\":").strip()
    if not text or " " in text or len(text) > 300:
        return None
    if "." not in text and "/" not in text:
        return None
    return text


def parse_edit_blocks(text: str) -> tuple[list[EditBlock], list[str]]:
    """Parse Aider-style SEARCH/REPLACE blocks. Returns (blocks, problems)."""
    lines = text.splitlines()
    blocks: list[EditBlock] = []
    problems: list[str] = []
    last_path: str | None = None
    i = 0
    while i < len(lines):
        if not _HEAD.match(lines[i]):
            i += 1
            continue
        path = None
        for back in range(i - 1, max(-1, i - 4), -1):
            candidate = lines[back]
            if not candidate.strip() or (_FENCE.match(candidate) and _clean_filename(candidate) is None):
                continue
            path = _clean_filename(candidate)
            break
        path = path or last_path
        j = i + 1
        search_lines: list[str] = []
        while j < len(lines) and not _DIVIDER.match(lines[j]):
            search_lines.append(lines[j])
            j += 1
        if j >= len(lines):
            problems.append("unterminated SEARCH block (missing =======)")
            break
        k = j + 1
        replace_lines: list[str] = []
        while k < len(lines) and not _TAIL.match(lines[k]):
            if _HEAD.match(lines[k]):
                break
            replace_lines.append(lines[k])
            k += 1
        if k >= len(lines) or not _TAIL.match(lines[k]):
            problems.append("unterminated REPLACE block (missing >>>>>>> REPLACE)")
            break
        if path is None:
            problems.append("edit block without a file path")
        else:
            blocks.append(EditBlock(path, "\n".join(search_lines), "\n".join(replace_lines)))
            last_path = path
        i = k + 1
    return blocks, problems


def parse_fenced_new_files(text: str, allowed_new: set[str]) -> list[EditBlock]:
    """Accept ```fenced``` whole-file content for files the step is allowed to create.

    Small models often answer "create tests/test_x.py" with a path line followed by a
    code block instead of a SEARCH/REPLACE block. This is only accepted for
    explicitly allowed new files, as a file *creation* (empty SEARCH).
    """
    if not allowed_new:
        return []
    lines = text.splitlines()
    blocks: list[EditBlock] = []
    i = 0
    while i < len(lines):
        if _FENCE.match(lines[i]):
            fence = _FENCE.match(lines[i]).group(1)
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(fence):
                j += 1
            body = lines[i + 1:j]
            path = _clean_filename(lines[i])
            for back in range(i - 1, max(-1, i - 3), -1):
                if path:
                    break
                if lines[back].strip():
                    path = _clean_filename(lines[back])
                    break
            if path is None and body and body[0].lstrip().startswith(("#", "//")):
                path = _clean_filename(body[0].lstrip("#/ ").strip())
            if path is None and len(allowed_new) == 1:
                path = next(iter(allowed_new))
            if path:
                norm = path.replace("\\", "/").lstrip("./")
                match = next((a for a in allowed_new if a == norm or a.rsplit("/", 1)[-1] == norm.rsplit("/", 1)[-1]),
                             None)
                if match and body:
                    blocks.append(EditBlock(match, "", "\n".join(body)))
            i = j + 1
            continue
        i += 1
    return blocks


# --- Matching ------------------------------------------------------------------------

def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _ensure_newline(text: str) -> str:
    return text if text.endswith("\n") or not text else text + "\n"


def apply_edit(content: str | None, search: str, replace: str) -> str:
    """Apply one SEARCH/REPLACE edit to ``content`` (None = file does not exist)."""
    if not search.strip():
        if content is None or not content.strip():
            return _ensure_newline(replace)
        return _ensure_newline(content) + ("\n" if not content.endswith("\n\n") else "") + _ensure_newline(replace)
    if content is None:
        raise EditError("file does not exist, so SEARCH must be empty to create it")

    count = content.count(search)
    if count == 1:
        return content.replace(search, replace, 1)
    if count > 1:
        raise EditError(f"SEARCH text matches {count} locations; include more surrounding lines to make it unique")

    c_lines = content.splitlines(keepends=True)
    s_lines = search.splitlines()
    while s_lines and not s_lines[0].strip():
        s_lines.pop(0)
    while s_lines and not s_lines[-1].strip():
        s_lines.pop()
    if not s_lines:
        raise EditError("SEARCH block is empty after trimming whitespace")
    r_lines = replace.splitlines()

    def windows(normalize) -> list[int]:
        target = [normalize(s) for s in s_lines]
        n = len(target)
        hits = []
        for start in range(0, len(c_lines) - n + 1):
            if all(normalize(c_lines[start + k].rstrip("\n").rstrip("\r")) == target[k] for k in range(n)):
                hits.append(start)
        return hits

    for normalize, reindent in ((str.rstrip, False), (str.strip, True)):
        hits = windows(normalize)
        if len(hits) > 1:
            raise EditError(f"SEARCH text matches {len(hits)} locations (ignoring whitespace); add more context")
        if len(hits) == 1:
            start = hits[0]
            n = len(s_lines)
            new_lines = list(r_lines)
            if reindent:
                actual = _indent_of(c_lines[start])
                proposed = _indent_of(s_lines[0])
                if actual != proposed:
                    adjusted = []
                    for line in new_lines:
                        if line.startswith(proposed):
                            adjusted.append(actual + line[len(proposed):])
                        elif not line.strip():
                            adjusted.append(line)
                        else:
                            adjusted.append(actual + line.lstrip())
                    new_lines = adjusted
            replacement = "".join(line + "\n" for line in new_lines)
            tail = c_lines[start + n - 1]
            if not tail.endswith("\n") and replacement.endswith("\n"):
                replacement = replacement[:-1]
            return "".join(c_lines[:start]) + replacement + "".join(c_lines[start + n:])

    # No match: give the model a useful hint for its repair attempt.
    best_ratio, best_start = 0.0, 0
    n = len(s_lines)
    joined_search = "\n".join(s.strip() for s in s_lines)
    for start in range(0, max(1, len(c_lines) - n + 1)):
        window = "\n".join(c_lines[k].strip() for k in range(start, min(len(c_lines), start + n)))
        ratio = difflib.SequenceMatcher(None, joined_search, window).quick_ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, start
    hint = ""
    if best_ratio > 0.5:
        snippet = "".join(c_lines[best_start: best_start + n])[:800]
        hint = f" The closest text in the file (line {best_start + 1}) is:\n{snippet}"
    raise EditError("SEARCH text was not found in the file." + hint)


# --- Diffs ---------------------------------------------------------------------------

def unified_diff(path: str, before: str | None, after: str) -> str:
    a = (before or "").splitlines(keepends=True)
    b = after.splitlines(keepends=True)
    fromfile = f"a/{path}" if before is not None else "/dev/null"
    body: list[str] = []
    for line in difflib.unified_diff(a, b, fromfile, f"b/{path}", n=3):
        body.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    if not body:
        return ""
    header = f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if before is None else "")
    return header + "".join(body)


def diff_stats(diff: str) -> tuple[int, int]:
    adds = dels = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            dels += 1
    return adds, dels


# --- Building and applying -------------------------------------------------------------

def _read_current(full: Path) -> str | None:
    if not full.exists():
        return None
    if not full.is_file():
        raise EditError("target is not a regular file")
    raw = full.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise EditError("file is too large to edit safely")
    if b"\x00" in raw[:8192]:
        raise EditError("refusing to edit a binary file")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError("file is not valid UTF-8; refusing to edit") from exc


def build_patch(root: Path, blocks: list[EditBlock], *, allowed_new_files: set[str] | None = None,
                base_contents: dict[str, str | None] | None = None) -> PatchBuild:
    """Validate edit blocks against the workspace and compute file changes + diffs.

    ``base_contents`` lets callers stack a second set of edits (e.g. tests) on
    top of pending, not-yet-applied changes.
    """
    result = PatchBuild()
    order: list[str] = []
    grouped: dict[str, list[EditBlock]] = {}
    for block in blocks:
        try:
            rel = normalize_relpath(block.path)
        except PathSecurityError as exc:
            result.errors.append(f"{block.path}: {exc}")
            continue
        if rel not in grouped:
            order.append(rel)
        grouped.setdefault(rel, []).append(block)
    if len(order) > MAX_FILES_PER_PATCH:
        result.errors.append(f"patch touches {len(order)} files; the limit is {MAX_FILES_PER_PATCH}")
        return result

    for rel in order:
        try:
            full = safe_join(root, rel)
        except PathSecurityError as exc:
            result.errors.append(f"{rel}: {exc}")
            continue
        if is_excluded(rel):
            result.errors.append(f"{rel}: generated, dependency or secret files cannot be edited")
            continue
        try:
            if base_contents is not None and rel in base_contents:
                original_disk = _read_current(full)
                current = base_contents[rel]
            else:
                original_disk = current = _read_current(full)
        except EditError as exc:
            result.errors.append(f"{rel}: {exc}")
            continue
        if original_disk is None and allowed_new_files is not None and rel not in allowed_new_files:
            result.errors.append(f"{rel}: creating new files is only allowed for planned files")
            continue
        uses_crlf = current is not None and "\r\n" in current
        working = current.replace("\r\n", "\n") if current is not None else None
        try:
            for block in grouped[rel]:
                working = apply_edit(working, block.search.replace("\r\n", "\n"), block.replace.replace("\r\n", "\n"))
        except EditError as exc:
            result.errors.append(f"{rel}: {exc}")
            continue
        assert working is not None
        after = working.replace("\n", "\r\n") if uses_crlf else working
        if original_disk is not None and after == original_disk:
            result.warnings.append(f"{rel}: edit produced no change")
            continue
        before_err = syntax_errors(rel, original_disk) if original_disk else None
        after_err = syntax_errors(rel, after)
        if after_err and not before_err:
            result.errors.append(f"{rel}: the edited file no longer parses — {after_err}")
            continue
        if after_err:
            result.warnings.append(f"{rel}: file had syntax errors before the edit ({after_err})")
        diff = unified_diff(rel, original_disk, after)
        adds, dels = diff_stats(diff)
        result.changes.append(FileChange(
            path=rel, change="modify" if original_disk is not None else "create",
            kind="test" if is_test_path(rel) else "fix", before=original_disk, after=after,
            before_sha=sha256_text(original_disk) if original_disk is not None else None,
            after_sha=sha256_text(after), diff=diff, additions=adds, deletions=dels,
        ))
    return result


def current_sha(root: Path, rel: str) -> str | None:
    full = safe_join(root, rel)
    if not full.exists():
        return None
    return sha256_text(full.read_bytes().decode("utf-8", errors="surrogateescape"))


def atomic_write(full: Path, text: str) -> None:
    """Write via a temp file and rename, keeping the file's permissions."""
    full.parent.mkdir(parents=True, exist_ok=True)
    mode = full.stat().st_mode if full.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(full.parent), prefix=".locpilot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, full)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _chains(changes: list[FileChange]) -> dict[str, list[FileChange]]:
    chains: dict[str, list[FileChange]] = {}
    for ch in changes:
        chains.setdefault(ch.path, []).append(ch)
    return chains


def apply_changes(root: Path, changes: list[FileChange]) -> list[str]:
    """Apply changes after verifying every target is in an expected state. Idempotent.

    Changes to the same file are treated as an ordered chain (A→B, B→C); the file
    may currently be at any state of that chain (e.g. after a partial or repeated
    apply). Any other content means someone else edited the file, and nothing is
    written (:class:`UnexpectedChangeError`).
    """
    pending: list[tuple[str, str]] = []
    for path, chain in _chains(changes).items():
        sha = current_sha(root, path)
        states = [chain[0].before_sha] + [c.after_sha for c in chain]
        if sha not in states:
            raise UnexpectedChangeError(path)
        position = len(states) - 1 - states[::-1].index(sha)
        if position < len(chain):
            pending.append((path, chain[-1].after))
    for path, text in pending:
        atomic_write(safe_join(root, path), text)
    return [path for path, _ in pending]


def revert_changes(root: Path, changes: list[FileChange]) -> tuple[list[str], list[dict]]:
    """Restore originals for files still containing LocPilot's content. Never clobbers other edits."""
    restored: list[str] = []
    skipped: list[dict] = []
    for path, chain in _chains(changes).items():
        full = safe_join(root, path)
        sha = current_sha(root, path)
        states = [chain[0].before_sha] + [c.after_sha for c in chain]
        if sha == states[0]:
            continue
        if sha not in states[1:]:
            skipped.append({"path": path, "reason": "file was modified after LocPilot's change; left untouched"})
            continue
        if chain[0].before is None:
            full.unlink()
        else:
            atomic_write(full, chain[0].before)
        restored.append(path)
    return restored, skipped
