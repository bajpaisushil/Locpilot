"""The model's own tools.

Agent-tuned models (MiMo, Qwen3-Coder, Devstral, …) are trained to work by calling tools — ``read``, ``grep``,
``edit``, ``write`` — and to see each result before the next step. Asked to print SEARCH/REPLACE text instead they
often say "I'll make the edit" and then call a tool anyway; ignoring that call loses a correct change.

This module defines those tools for Ollama's tool-calling API and turns the calls into what LocPilot already has:
lookups become evidence, edits become the same validated edit blocks the text protocol produces. Nothing here
writes to disk: edits are staged in memory and end up in a patch the user approves.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from locpilot.agent.schemas import ToolRequest
from locpilot.llm.ollama import LLMResult
from locpilot.security.paths import PathSecurityError, normalize_relpath
from locpilot.security.secrets import redact
from locpilot.tools.files import RepoIndex, is_test_path, number_lines
from locpilot.tools.patch import EditBlock, PatchBuild, build_patch
from locpilot.tools.search import SearchError, search

if TYPE_CHECKING:
    from locpilot.agent.evidence import EvidenceCollector

MAX_READ_LINES = 200
DEFAULT_READ_LINES = 120
MAX_GREP_HITS = 30
MAX_READ_ITEM = 110  # lines one read adds to the evidence while exploring
READ_TAIL = 30  # a read that ends this close to the end of the file takes the rest along
REPEAT_LINES = 40  # lines the model already has are sent again only when it asks for this few


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_PATH = {"type": "string", "description": "path relative to the repository root, for example src/app/main.py"}
READ = _tool("read", "Read lines of a file in the repository. Returns numbered lines.", {
    "file_path": _PATH,
    "offset": {"type": "integer", "description": "first line to read (1-based, default 1)"},
    "limit": {"type": "integer", "description": f"how many lines (default {DEFAULT_READ_LINES}, at most "
                                                f"{MAX_READ_LINES})"}}, ["file_path"])
GREP = _tool("grep", "Search the repository for a text or regular expression. Returns matches as path:line: text.", {
    "pattern": {"type": "string", "description": "text or regular expression to find"},
    "path": {"type": "string", "description": "optional: only search this file or folder"}}, ["pattern"])
FIND_DEFINITION = _tool("find_definition", "Show the definition of a function, method or class.", {
    "name": {"type": "string", "description": "the name, for example parseConfig"}}, ["name"])
FIND_CALLERS = _tool("find_callers", "Show the places that call a function or method.", {
    "name": {"type": "string", "description": "the name, for example parseConfig"}}, ["name"])
LIST = _tool("list", "List the files and folders in a directory of the repository.", {
    "path": {"type": "string", "description": "directory relative to the repository root ('' for the root)"}}, [])
EDIT = _tool("edit", "Replace text in a file. old_string must be copied exactly from the file (same indentation) and "
                     "occur exactly once: include a few unchanged lines around the change to make it unique.", {
    "file_path": _PATH,
    "old_string": {"type": "string", "description": "the exact existing text to replace"},
    "new_string": {"type": "string", "description": "the text that replaces it"}},
    ["file_path", "old_string", "new_string"])
WRITE = _tool("write", "Create a new file. content is the whole file.", {
    "file_path": _PATH, "content": {"type": "string", "description": "the complete content of the file"}},
    ["file_path", "content"])

EXPLORE_TOOLS = [READ, GREP, FIND_DEFINITION, FIND_CALLERS, LIST]
EDIT_TOOLS = [READ, GREP, EDIT, WRITE]

# Names models use for the same things (their training harnesses differ).
_NAMES = {
    "read": ("read", "read_file", "view", "view_file", "cat", "open", "open_file"),
    "search": ("grep", "search", "search_text", "search_regex", "search_files", "rg", "ripgrep", "find_in_files"),
    "definition": ("find_definition", "definition", "go_to_definition", "find_symbol"),
    "callers": ("find_callers", "callers", "find_references", "references", "find_usages"),
    "list": ("list", "ls", "list_dir", "list_directory", "list_files", "glob"),
    "edit": ("edit", "edit_file", "str_replace", "replace", "replace_in_file", "search_replace", "apply_edit",
             "multiedit", "multi_edit"),
    "write": ("write", "write_file", "create_file", "create", "write_to_file", "new_file"),
    "run": ("bash", "shell", "run", "run_command", "execute", "run_tests", "terminal"),
}
_EDITOR_COMMANDS = {"view": "read", "create": "write", "str_replace": "edit"}
PATH_ARGS = ("file_path", "path", "filePath", "filename", "file", "target_file")
_OLD_ARGS = ("old_string", "old_str", "oldString", "search", "old_text", "old")
_NEW_ARGS = ("new_string", "new_str", "newString", "replace", "new_text", "new")
_NUMBERED = re.compile(r"^\s*\d+\s\|\s?")


def kind_of(call: dict) -> str:
    """What a tool call means: read, search, definition, callers, list, edit, write, run or unknown."""
    name = str(call.get("name") or "").lower().replace("-", "_")
    name = re.split(r"[.:/]", name)[-1]
    if name in ("str_replace_editor", "str_replace_based_edit_tool", "text_editor"):
        return _EDITOR_COMMANDS.get(str((call.get("arguments") or {}).get("command") or ""), "unknown")
    return next((kind for kind, names in _NAMES.items() if name in names), "unknown")


def arg(args: dict, *names: str) -> str | None:
    for name in names:
        value = args.get(name)
        if isinstance(value, str):
            return value
    return None


def int_arg(args: dict, *names: str) -> int | None:
    for name in names:
        value = args.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def assistant_message(result: LLMResult) -> dict:
    """The model's turn, as it goes back into the conversation (Ollama's message format)."""
    return {"role": "assistant", "content": result.content,
            "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in result.tool_calls]}


def tool_message(call: dict, output: str) -> dict:
    return {"role": "tool", "tool_name": call["name"], "content": output}


def repo_path(raw: str | None, workspace: Path) -> str | None:
    """A path as the model gave it → relative to the repository (absolute paths inside it are accepted)."""
    text = (raw or "").strip().strip("`'\"")
    if not text:
        return None
    for root in {str(workspace), str(workspace.resolve())}:
        if text.startswith(root + "/"):
            text = text[len(root) + 1:]
    while text.startswith("./"):
        text = text[2:]
    try:
        return normalize_relpath(text)
    except PathSecurityError:
        return None


def _find_file(index: RepoIndex, raw: str | None, workspace: Path) -> str | None:
    """The indexed file a model-given path means (exact, or the only file ending with it)."""
    rel = repo_path(raw, workspace)
    if rel is None:
        return None
    if index.has_file(rel):
        return rel
    matches = [f for f in index.files if f.endswith("/" + rel)]
    return matches[0] if len(matches) == 1 else None


def _read_range(args: dict, total: int) -> tuple[int, int]:
    """Lines [start, end] a read call asks for (several argument styles), clamped to the file."""
    view = args.get("view_range")
    if isinstance(view, list) and len(view) == 2 and all(isinstance(v, int) for v in view):
        start, end = view[0], view[1] if view[1] > 0 else total
    else:
        start = int_arg(args, "offset", "start_line", "start", "line") or 1
        limit = int_arg(args, "limit", "num_lines", "max_lines")
        end_line = int_arg(args, "end_line", "end")
        end = start + limit - 1 if limit else (end_line or start + DEFAULT_READ_LINES - 1)
    start = max(1, start)
    end = min(total, max(start, end), start + MAX_READ_LINES - 1)
    return start, end


def numbered(path: str, text: str, args: dict) -> str:
    lines = text.splitlines()
    if not lines:
        return f"{path} is empty."
    start, end = _read_range(args, len(lines))
    more = f"\n… {len(lines) - end} more lines (read again with offset={end + 1})" if end < len(lines) else ""
    return f"{path} (lines {start}-{end} of {len(lines)}):\n{redact(number_lines(lines, start, end))}{more}"


def grep(index: RepoIndex, pattern: str, where: str | None, workspace: Path) -> tuple[str, list]:
    """Search as the model's ``grep``: a regular expression if it is one, else plain text. Returns (reply, hits)."""
    pattern = pattern.strip()[:300]
    if not pattern:
        return "grep needs a pattern.", []
    files: list[str] | None = None
    glob: str | None = None
    if where and where.strip() not in ("", ".", "./", "/"):
        rel = _find_file(index, where, workspace)
        if rel:
            files = [rel]
        else:
            folder = (repo_path(where.rstrip("/*"), workspace) or where).strip("/")
            glob = where if any(ch in where for ch in "*?[") else f"{folder}/*"
    hits: list = []
    for is_regex in (True, False):
        try:
            result = search(index, pattern, is_regex=is_regex, files=files, path_glob=glob,
                            max_results=MAX_GREP_HITS, max_per_file=8)
        except SearchError:
            continue
        hits = result.hits
        if hits:
            break
    if not hits:
        return f"No matches for {pattern!r}" + (f" in {where}" if where else "") + ".", []
    return redact("\n".join(f"{h.path}:{h.line}: {h.text}" for h in hits)), hits


def _unnumber(text: str) -> str:
    """Text copied from a numbered listing (``12 | code``) without the line numbers."""
    lines = text.split("\n")
    if lines and all(_NUMBERED.match(line) for line in lines if line.strip()):
        return "\n".join(_NUMBERED.sub("", line, count=1) for line in lines)
    return text


class EditSession:
    """Edits a model makes through tool calls, staged in memory until they become a patch.

    Each edit is validated against the file as it would be after the edits before it. A rejected edit tells the
    model why (text not found, not unique, file not allowed) in the tool's reply, so it can try again.
    """

    def __init__(self, workspace: Path, index: RepoIndex, *, planned: set[str], allowed_new: set[str],
                 only_tests: bool = False, protected: set[str] | None = None,
                 staged: list[EditBlock] | None = None, tests_later: bool = False) -> None:
        self.workspace = workspace
        self.index = index
        self.planned = set(planned)  # files the plan names
        self.allowed_new = set(allowed_new)  # files that may be created
        self.only_tests = only_tests
        self.tests_later = tests_later  # the code change of a fix: its regression test is a step of its own
        self.protected = set(protected or ())  # existing tests: may grow, never shrink
        self.staged: list[EditBlock] = list(staged or [])  # edits of an earlier round this one continues
        self.problems: list[str] = []  # edits rejected in the latest round of calls
        self.unplanned: list[str] = []  # files edited although the plan did not name them
        self.can_read = True  # set to False when the context window is nearly full
        self.tokens = 0  # tokens in the conversation these edits are made in (kept by the caller)
        self.new_reads = 0  # reads, in the latest round of calls, of code this conversation had not shown yet
        self._seen: dict[str, set[int]] = {}  # the lines of each file that reads have returned
        self._build: PatchBuild | None = None

    # state ------------------------------------------------------------------------------
    def build(self) -> PatchBuild:
        if self._build is None:
            self._build = build_patch(self.workspace, self.staged, allowed_new_files=self.allowed_new)
        return self._build

    def _current(self, rel: str) -> str | None:
        """The file as it stands with the staged edits (None: it does not exist)."""
        for change in self.build().changes:
            if change.path == rel:
                return change.after
        return self.index.read_text(rel) if self.index.has_file(rel) else None

    def _may_edit(self) -> str:
        return "You may edit: " + ", ".join(sorted(self.planned | self.allowed_new)) + "."

    def _reject(self, problem: str) -> str:
        self.problems.append(problem)
        return "ERROR: " + problem

    def _target(self, raw: str | None) -> tuple[str | None, str]:
        """The file an edit may change, or why it may not."""
        rel = repo_path(raw, self.workspace)
        if rel is None:
            return None, f"{raw!r} is not a path inside the repository."
        known = self.planned | self.allowed_new
        if rel in known:
            return rel, ""
        if not self.index.has_file(rel):
            name = rel.rsplit("/", 1)[-1]
            same = [p for p in sorted(known) if p.rsplit("/", 1)[-1] == name]
            if len(same) == 1:  # a sloppy path that clearly means a planned file
                return same[0], ""
            return None, (f"{rel} does not exist, and creating it was not planned (nothing can be run here, so a "
                          f"scratch script is of no use). {self._may_edit()}")
        if self.only_tests:
            return None, f"Only the regression test may be edited in this step. {self._may_edit()}"
        if is_test_path(rel) and self.tests_later:
            return None, (f"{rel} is an existing test: tests are not written or changed in this step. The "
                          "regression test is the next step and goes in a file of its own; an existing test that "
                          "the requested change makes outdated is updated after the tests have run. When the code "
                          f"change is complete, reply DONE. {self._may_edit()}")
        if is_test_path(rel):
            return None, f"{rel} is an existing test: do not modify it. {self._may_edit()}"
        return rel, ""  # another source file: allowed, and pointed out with the patch

    def _stage(self, block: EditBlock) -> str:
        """Try one edit on top of the staged ones; returns the tool's reply."""
        candidate = build_patch(self.workspace, [*self.staged, block], allowed_new_files=self.allowed_new)
        if candidate.errors:
            return self._reject(candidate.errors[0].replace("SEARCH text", "old_string")
                                .replace("SEARCH block", "old_string").replace("SEARCH", "old_string"))
        change = next((c for c in candidate.changes if c.path == block.path), None)
        before = next((c.after for c in self.build().changes if c.path == block.path), None)
        if change is None or change.after == before:
            return self._reject(f"{block.path}: this edit changes nothing — the file already has that text. If "
                                "your edits are complete, reply DONE.")
        if block.path in self.protected and change.deletions:
            return self._reject(f"{block.path}: existing tests must not be deleted or rewritten; add a new test.")
        self.staged.append(block)
        self._build = candidate
        if block.path not in self.planned | self.allowed_new and block.path not in self.unplanned:
            self.unplanned.append(block.path)
        self._seen[block.path] = set(range(1, change.after.count("\n") + 2))  # its own text: nothing new to read
        return (f"OK: {block.path} updated in the pending patch (+{change.additions}/-{change.deletions}). "
                "If the change needs more edits to be complete, make them; when it is complete, reply DONE.")

    # tools ------------------------------------------------------------------------------
    def handle(self, call: dict) -> str:
        """Run one tool call; returns the text the model gets back."""
        kind = kind_of(call)
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if kind == "edit":
            return self._edit(args)
        if kind == "write":
            return self._write(args)
        if kind in ("read", "search") and not self.can_read:
            return "The context window is nearly full: no more reading. Make the edit now with what you know."
        if kind == "read":
            return self._read(args)
        if kind == "search":
            return grep(self.index, arg(args, "pattern", "query", "regex", "text") or "",
                        arg(args, "path", "include", "glob", "directory"), self.workspace)[0]
        if kind == "run":
            return ("There is no shell here: commands cannot be run. LocPilot runs the tests itself after your "
                    "edits. If your edits are complete, reply DONE.")
        return f"There is no tool named {call.get('name')!r} here. Use read, grep, edit or write."

    def _read(self, args: dict) -> str:
        raw = arg(args, *PATH_ARGS)
        rel = repo_path(raw, self.workspace)
        text = self._current(rel) if rel else None
        if text is None:
            rel = _find_file(self.index, raw, self.workspace)
            text = self._current(rel) if rel else None
        if rel is None or text is None:
            return f"{raw!r}: no such file in the repository."
        start, end = _read_range(args, len(text.splitlines()) or 1)
        seen = self._seen.setdefault(rel, set())
        tests_come_later = self.tests_later and is_test_path(rel) and rel not in self.planned | self.allowed_new
        if not tests_come_later and not seen.issuperset(range(start, end + 1)):
            self.new_reads += 1  # still finding its way; a model re-reading what it has is done, in effect
        seen.update(range(start, end + 1))
        reminder = ("\n(Tests are not written in this step: the regression test comes next, in a file of its own. "
                    "If the code change is complete, reply DONE.)") if tests_come_later and self.staged else ""
        return numbered(rel, text, args) + reminder

    def _edit(self, args: dict) -> str:
        rel, problem = self._target(arg(args, *PATH_ARGS))
        if rel is None:
            return self._reject(problem)
        edits = [e for e in args["edits"] if isinstance(e, dict)] if isinstance(args.get("edits"), list) else [args]
        replies = []
        for edit in edits:
            old, new = arg(edit, *_OLD_ARGS), arg(edit, *_NEW_ARGS)
            if old is None or new is None:
                replies.append(self._reject("edit needs file_path, old_string and new_string."))
                continue
            current = self._current(rel)
            if current is None:
                replies.append(self._reject(f"{rel} does not exist yet: create it with write."))
                continue
            if not old.strip():
                replies.append(self._reject(f"old_string is empty: give the exact text of {rel} to replace."))
                continue
            if old not in current and _unnumber(old) in current:  # copied with the listing's line numbers
                old, new = _unnumber(old), _unnumber(new)
            if edit.get("replace_all") is True and current.count(old) > 1:
                replies.append(self._stage(EditBlock(rel, current, current.replace(old, new))))
            else:
                replies.append(self._stage(EditBlock(rel, old, new)))
        return "\n".join(replies)

    def _write(self, args: dict) -> str:
        rel, problem = self._target(arg(args, *PATH_ARGS))
        if rel is None:
            return self._reject(problem)
        content = arg(args, "content", "file_text", "contents", "text")
        if content is None or not content.strip():
            return self._reject("write needs file_path and the complete content of the file.")
        on_disk = self.index.has_file(rel)
        if not on_disk:
            # A new file (written again: the latest version replaces the earlier one).
            self.staged = [b for b in self.staged if b.path != rel]
            self._build = None
            return self._stage(EditBlock(rel, "", content))
        if rel in self.protected:
            return self._reject(f"{rel}: existing tests must not be rewritten; add a new test.")
        current = self._current(rel) or ""
        return self._stage(EditBlock(rel, current, content))


class ExploreSession:
    """Read-only lookups during the investigation; whatever they show becomes evidence the analysis can cite.

    A lookup shows code once. Asked for lines it has already shown, it points at them instead of sending them
    again: the model reads a file in overlapping pieces, and every repeated line costs time and context.
    """

    def __init__(self, collector: EvidenceCollector, workspace: Path, *, shown_lines: int | None = None) -> None:
        self.collector = collector
        self.index = collector.index
        self.workspace = workspace
        self.asked: set[str] = set()  # lookups already answered
        self.repeats = 0  # how often the model asked for something it already has
        # Evidence the model has in full: what a lookup returned, and the snippets found before it started —
        # unless the prompt cut them short (``shown_lines`` is how many lines of each it shows).
        self.shown: set[str] = {i["id"] for i in collector.items
                                if shown_lines is None or len(i["content"].splitlines()) <= shown_lines}

    def _render(self, ids: list[str]) -> str:
        from locpilot.agent.evidence import render_evidence

        self.shown.update(ids)
        return render_evidence([i for i in self.collector.items if i["id"] in ids])

    def _have(self, rel: str, start: int, end: int) -> list[dict]:
        """Evidence the model has in full that shows lines of start..end."""
        return [i for i in self.collector.items if i["path"] == rel and i["id"] in self.shown
                and not (end < i["start_line"] or start > i["end_line"])]

    @staticmethod
    def _where(items: list[dict]) -> str:
        return ", ".join(f"lines {i['start_line']}-{i['end_line']} are {i['id']}" for i in items)

    def _read(self, rel: str, lines: list[str], args: dict, asked: dict) -> str:
        total = len(lines)
        start, end = _read_range(args, total)
        end = min(end, start + MAX_READ_ITEM - 1)
        if total - end <= READ_TAIL:  # a few lines short of the end: take them along instead of a second read
            end = total
        asked.update(start_line=start, end_line=end)
        have = self._have(rel, start, end)
        known = {n for i in self._have(rel, 1, total) for n in range(i["start_line"], i["end_line"] + 1)}
        unseen = [n for n in range(start, end + 1) if n not in known]
        if not unseen:
            self.repeats += 1
            reply = f"You already have these lines of {rel} above ({self._where(have)}). Nothing new here."
            if end - start + 1 <= REPEAT_LINES:
                reply += "\n" + redact(number_lines(lines, start, end))
        else:
            first, last = unseen[0], unseen[-1]
            item = self.collector.add(rel, first, last, kind="file", title=f"{rel}:{first}-{last}",
                                      reason="requested", exact=True)
            if item is None:
                return "The evidence budget is full: nothing more can be read. Finish with what you have."
            reply = self._render([item["id"]])
            known.update(range(item["start_line"], item["end_line"] + 1))
            if first > start or last < end:
                reply += f"\nThe other lines you asked for are above ({self._where(have)})."
            if item["end_line"] < last:  # longer than one item holds: the rest is still to read
                end = item["end_line"]
        after = [n for n in range(end + 1, total + 1) if n not in known]
        if after:
            return f"{reply}\n… {len(after)} more lines in {rel} (read again with offset={after[0]})"
        if len(known) >= total:
            return f"{reply}\nYou now have the whole of {rel} ({total} lines)."
        return reply

    def _located(self, kind: str, name: str) -> list[dict]:
        """Evidence the model already has for a definition or the callers of ``name``."""
        symbols = self.collector.symbols
        spots = [(s.path, s.start_line, s.end_line) for s in symbols.definitions(name)[:3]] if kind == "definition" \
            else [(s.path, s.line, s.line) for s in symbols.callers(name, limit=12)[:4]]
        found: dict[str, dict] = {}
        for path, first, last in spots:
            for item in self._have(path, first, last):
                found[item["id"]] = item
        return list(found.values())

    def handle(self, call: dict) -> tuple[str, dict, list[str]]:
        """Run one call. Returns (reply for the model, what was asked — for the timeline, new evidence ids)."""
        kind = kind_of(call)
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        before = {i["id"] for i in self.collector.items}
        asked: dict = {"tool": kind, "query": "", "path": ""}
        key = kind + json.dumps(args, sort_keys=True, default=str)
        if key in self.asked:  # the same lookup again: the model is going round in circles
            self.repeats += 1
            return ("You already did exactly this; the result is above. If the evidence is enough, stop calling "
                    "tools and say what you found; otherwise look at something you have not seen yet.", asked, [])
        self.asked.add(key)
        if kind == "read":
            raw = arg(args, *PATH_ARGS)
            rel = _find_file(self.index, raw, self.workspace)
            asked.update(tool="read_file", path=rel or str(raw))
            lines = self.index.read_lines(rel) if rel else None
            if rel is None or lines is None:
                return f"{raw!r}: no such file in the repository.", asked, []
            reply = self._read(rel, lines, args, asked)
        elif kind == "search":
            pattern = arg(args, "pattern", "query", "regex", "text") or ""
            asked.update(tool="search_text", query=pattern[:200])
            reply, hits = grep(self.index, pattern, arg(args, "path", "include", "glob", "directory"), self.workspace)
            for hit in [h for h in hits if not is_test_path(h.path)][:2] or hits[:1]:
                self.collector.add_window(hit.path, hit.line, radius=5, kind="search_hit",
                                          title=f"'{pattern[:40]}' in {hit.path}", reason=f"matches '{pattern[:40]}'")
        elif kind in ("definition", "callers"):
            name = (arg(args, "name", "symbol", "query", "function") or "").strip()
            tool = "find_definition" if kind == "definition" else "find_callers"
            asked.update(tool=tool, query=name[:200])
            have = self._located(kind, name)
            reply = self.collector.run_tool(ToolRequest(tool=tool, query=name))[0]
            if have:
                reply += " — already shown above: " + ", ".join(
                    f"{i['id']} ({i['path']}:{i['start_line']}-{i['end_line']})" for i in have)
        elif kind == "list":
            rel = (repo_path(arg(args, "path", "directory", "dir") or "", self.workspace) or "").strip("/")
            asked.update(tool="list_directory", path=rel)
            entries = self.index.list_dir(rel)
            reply = ", ".join(e["name"] + ("/" if e["type"] == "dir" else "") for e in entries[:80]) or \
                f"{rel or '.'}: empty or not a directory."
        elif kind in ("edit", "write"):
            return ("Not yet: this step only investigates. Finish looking; you will make the change next.",
                    asked | {"tool": kind}, [])
        elif kind == "run":
            return "Commands cannot be run here. Read the code instead.", asked | {"tool": kind}, []
        else:
            return (f"There is no tool named {call.get('name')!r} here. Use read, grep, find_definition, "
                    "find_callers or list.", asked | {"tool": "unknown"}, [])
        new_ids = [i["id"] for i in self.collector.items if i["id"] not in before]
        fresh = [i for i in new_ids if i not in self.shown]  # a read has shown its lines already
        if fresh:
            reply = f"{reply}\n\n{self._render(fresh)}"
        return reply, asked, new_ids
