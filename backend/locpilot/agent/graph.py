"""The LocPilot LangGraph state machine.

One graph run handles one conversation turn (thread id ``<task>#<turn>``).
Persistent context (report, plan, evidence, approved commands, last test runs,
history) is carried into the next turn's input by the runner.

    start_turn → understand ─┬─ review ───────────────────────────────┐
                             ├─ diagnose (retry_fix) ──────────┐      │
                             ├─ plan (follow-up fix) ──┐       │      │
                             ├─ clarify ───────────────┼───────┼──► finalize → END
                             └─ inspect → seed_evidence ⇄ plan_evidence (bounded)
                                  → analyze → validate ─┬─ (ask / explain) ─► finalize
                                                        └─ (code + fix) ─► plan
    plan → generate_patch → approval (interrupt) ─┬─ rejected ─► finalize | generate_patch (with feedback)
                                                  └─ approved → apply → [verify(before_fix) → apply]
                                                       → verify(after_fix) → evaluate
    evaluate ─┬─ passed / partial / no retries / stalled ─► finalize
              └─ failed & retries left ─► diagnose ─► generate_patch (repair) ─► approval …
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from locpilot.agent import prompts
from locpilot.agent.citations import CitationResolver, build_explanation_report, build_rca_report
from locpilot.agent.evidence import EvidenceCollector, extract_terms, render_evidence
from locpilot.agent.schemas import (
    EvidencePlan,
    InvestigationReport,
    LLMAnalysis,
    LLMChangeReview,
    LLMEditList,
    LLMExplanation,
    LLMFailureAnalysis,
    LLMPatchCheck,
    LLMPlan,
    LLMTestSpec,
    LLMTracedFailure,
    LLMWalkThrough,
    TaskUnderstanding,
)
from locpilot.agent.toolcalls import (
    EDIT_TOOLS,
    EXPLORE_TOOLS,
    PATH_ARGS,
    EditSession,
    ExploreSession,
    arg,
    assistant_message,
    kind_of,
    tool_message,
)
from locpilot.agent.workspace import USER_STAGE, WorkspaceError, prepare_worktree, run_verification_command
from locpilot.config import Settings
from locpilot.llm.ollama import LLMClient, LLMResponseError, chat_json
from locpilot.security.commands import CommandPolicyError, validate_command
from locpilot.security.paths import PathSecurityError, normalize_relpath
from locpilot.security.sandbox import detect_sandbox
from locpilot.security.secrets import redact
from locpilot.storage.db import Database, now_iso
from locpilot.tools import git
from locpilot.tools.files import RepoIndex, get_index, is_excluded, is_test_path, language_for
from locpilot.tools.manifests import inspect_project
from locpilot.tools.patch import (
    EditBlock,
    FileChange,
    UnexpectedChangeError,
    apply_changes,
    build_patch,
    parse_edit_blocks,
    parse_fenced_new_files,
    revert_changes,
)
from locpilot.tools.symbols import get_symbol_index

# Model turns one tool loop may take (each is short: a tool call, or a few of them).
MAX_EDIT_TURNS = 10
EDIT_TURN_TOKENS = 3200  # one reply of the edit loop: room for a new file of about 250 lines
MAX_EXPLORE_TURNS = 10
EXPLORE_SNIPPET_LINES = 45  # lines of each snippet found beforehand that the exploring prompt shows
# Every tool call gets a reply (a model left without one waits for it); calls past the per-step limit get this.
SKIPPED_CALL = "Not run: too many calls in one step. Ask again if you still need it."
# Share of the evidence budget the keyword search may fill when the model can explore by itself afterwards, and
# how far its own lookups may go past the budget (what it chose to read matters most).
SEED_SHARE = 0.65
EXPLORE_SHARE = 1.2
CHARS_PER_TOKEN = 3.2  # code with line numbers, estimated low so prompts are not cut by the context window

# Steps where a thinking model reasons before answering when Settings.thinking is "auto": finding the root cause
# and explaining a failed verification. The other steps format, classify or copy and gain little from it.
THINKING_STEPS = frozenset({"analyze", "diagnose"})


def new_test_file(path: str, tools: bool = False) -> str:
    """The regression test file doesn't exist yet: how to create it, with the exact block to write.

    Without this, models dropped the imports or swapped the SEARCH and REPLACE halves. Only this step shows the
    template: in the edit rules every step shares, it changed how a 7B model wrote ordinary fixes.
    """
    if tools:
        return (f"### {path}\n(new file) Create it with the write tool: the complete file, starting with the "
                "imports of the test framework and of the code under test.")
    return (f"### {path}\n(new file) Create it with one block: an empty SEARCH section, then the complete file after "
            "the ======= line, starting with the imports of the test framework and of the code under test:\n"
            f"{path}\n<<<<<<< SEARCH\n=======\n<the complete test file>\n>>>>>>> REPLACE")


# Keys that survive from one turn to the next.
PERSISTENT_KEYS = ("task_id", "repo_id", "repo_path", "worktree_path", "base_commit", "history", "report",
                   "plan", "evidence", "notes", "overview", "approved_commands", "last_runs", "applied_patch_ids",
                   "proposed_commands")


# What earlier turns found, said and planned: the part of the carried-over state "Clear context" drops, so the next
# message starts fresh. The worktree, the changes LocPilot applied and the commands you approved stay.
CONTEXT_KEYS = ("history", "report", "plan", "evidence", "notes", "last_runs", "proposed_commands")


class AgentState(TypedDict, total=False):
    task_id: str
    repo_id: str
    repo_path: str
    project_notes: str  # LOCPILOT.md, read again at the start of every turn
    turn: int
    mode: str
    request: str
    history: list[dict]
    workspace_path: str
    worktree_path: str | None
    base_commit: str | None
    overview: dict | None
    understanding: dict | None
    intent: str
    evidence: list[dict]
    notes: list[str]
    tool_log: list[dict]
    evidence_rounds: int
    evidence_done: bool
    # One conversation across steps (tool-capable models): the analysis continues the exploration and the plan
    # continues the analysis, so the model server does not process the same context again for every step.
    explore_messages: list[dict]
    analysis_messages: list[dict]
    conversation_tokens: int  # tokens in that conversation so far (to know whether a follow-up still fits)
    conversation_tools: bool  # that conversation was offered the explore tools (later calls must repeat them)
    # The conversation in which the model last wrote tests or code, and the diagnosis that continued it: a failed
    # verification is diagnosed and repaired where the model can see its own work and the code it read.
    work_messages: list[dict]
    work_tokens: int
    diagnose_messages: list[dict]
    diagnose_tokens: int
    raw_analysis: dict | None
    report: dict | None
    report_turn: int
    plan: dict | None
    proposed_commands: list[dict]
    approved_commands: list[list[str]]
    patch_id: str | None
    patch_mode: str
    patch_attempts: int
    patch_error: str | None
    approval_decision: str | None
    feedback: str | None
    apply_stage: str
    verify_stage: str
    before_runs: list[dict]
    runs: list[dict]
    last_runs: list[dict]
    user_runs: list[dict]
    applied_patch_ids: list[str]
    verification: dict | None
    failure_signatures: list[str]
    stalled: bool
    repair_attempts: int
    diagnosis: dict | None
    retry_planned: bool
    repair_target: str
    repair_tests: list[str]  # existing test files a test repair may update (their assertions are outdated)
    test_repair_needed: bool
    before_checked_for: str | None
    review: dict | None
    clarifying_question: str | None
    unresolved: list[str]
    warnings: list[str]
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    tool_calls: int
    turn_started: float
    final_status: str
    task_status: str


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class AgentDeps:
    settings: Settings
    db: Database
    llm: Any  # Callable[[], LLMClient]
    emitter: Any  # Callable[[task_id, turn, type, data, persist], Awaitable[dict]]


PROJECT_NOTES_FILE = "LOCPILOT.md"


def load_project_notes(repo_path: Path, limit: int) -> tuple[str, str | None]:
    """The repository owner's LOCPILOT.md (secrets redacted, at most ``limit`` characters) and a line for the
    timeline saying it is used; ("", None) when there is none."""
    path = repo_path / PROJECT_NOTES_FILE
    if limit <= 0 or not path.is_file() or path.is_symlink():
        return "", None
    try:
        raw = path.read_bytes()[:200_000].decode("utf-8", errors="replace")
    except OSError:
        return "", None
    text = redact(raw).strip()
    if not text:
        return "", None
    if len(text) <= limit:
        return text, f"Using your project notes from {PROJECT_NOTES_FILE} ({len(text):,} characters)."
    cut = text[:limit]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return (cut.rstrip() + "\n[…]",
            f"Using the first {len(cut):,} of {len(text):,} characters of {PROJECT_NOTES_FILE}; keep it short "
            "(about 1,500 characters) so it fits small models.")


def _gb(n: int | float) -> str:
    return f"{n / 2**30:.1f} GB"


def memory_warning(mem: dict | None) -> str | None:
    """The text shown when the model would not fit in the RAM that is free (see OllamaClient.memory_check)."""
    if not mem or not mem.get("short"):
        return None
    context = f", of which {_gb(mem['context_bytes'])} for the context" if mem.get("context_bytes") else ""
    return (f"Only {_gb(mem['available_bytes'])} of memory is free, but the model needs about "
            f"{_gb(mem['needed_bytes'])}{context}. Your computer may start swapping, which makes answers much slower. "
            "Close other apps, or pick a smaller model or a smaller context window in Settings.")


def turn_input(carry: dict, *, task_id: str, repo: dict, turn: int, mode: str, request: str,
               user_runs: list[dict] | None = None) -> dict:
    """Initial state for a new turn: persistent context + fresh per-turn fields.

    ``user_runs`` are command records the user ran in the worktree since the previous turn.
    """
    state = {k: carry[k] for k in PERSISTENT_KEYS if k in carry and carry[k] is not None}
    state.update({
        "task_id": task_id, "repo_id": repo["id"], "repo_path": repo["path"], "turn": turn, "mode": mode,
        "request": request, "understanding": None, "intent": "", "tool_log": [], "evidence_rounds": 0,
        "evidence_done": False, "explore_messages": [], "analysis_messages": [], "conversation_tokens": 0,
        "conversation_tools": False, "work_messages": [], "work_tokens": 0, "diagnose_messages": [],
        "diagnose_tokens": 0,
        "raw_analysis": None, "patch_id": None, "patch_mode": "initial",
        "patch_attempts": 0, "patch_error": None, "approval_decision": None, "feedback": None, "apply_stage": "",
        "verify_stage": "", "before_runs": [], "runs": [], "verification": None, "failure_signatures": [],
        "stalled": False, "repair_attempts": 0, "diagnosis": None, "retry_planned": False,
        "repair_target": "code", "repair_tests": [], "test_repair_needed": False, "before_checked_for": None, "review": None, "clarifying_question": None,
        "unresolved": [], "warnings": [], "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "tool_calls": 0, "turn_started": time.time(), "final_status": "", "task_status": "running",
        "user_runs": [run_entry(r) for r in user_runs or []],
    })
    state.setdefault("history", [])
    state.setdefault("evidence", [])
    state.setdefault("notes", [])
    state.setdefault("approved_commands", [])
    state.setdefault("applied_patch_ids", [])
    state.setdefault("last_runs", [])
    return state


_FIX_VERB = re.compile(r"\b(fix|repair|patch|resolve|correct)(es|ed|ing)?\b", re.IGNORECASE)
_NO_FIX = re.compile(r"\b(do not|don't|dont|without|no need to|not)\s+(\w+\s+){0,2}(fix|change|modify|edit)",
                     re.IGNORECASE)


def _asks_for_fix(request: str) -> bool:
    return bool(_FIX_VERB.search(request)) and not _NO_FIX.search(request)


def test_files_in(test_ids: list[str], files: list[str]) -> list[str]:
    """The test files that failing test ids name (as a path, or dotted like a Python module)."""
    found = []
    for path in files:
        if is_test_path(path) and any(path in t or path.rsplit(".", 1)[0].replace("/", ".") in t for t in test_ids):
            found.append(path)
    return found


def new_source_path(index: RepoIndex, raw: str) -> str | None:
    """A file the plan wants to create: accepted when it would sit next to source files of the same kind."""
    try:
        path = normalize_relpath((raw or "").strip().strip("`'\"").removeprefix("./"))
    except PathSecurityError:
        return None
    if "/" not in path or "." not in path.rsplit("/", 1)[-1] or index.has_file(path) or is_test_path(path):
        return None
    if is_excluded(path) or language_for(path) is None:
        return None
    folder, suffix = path.rsplit("/", 1)[0] + "/", "." + path.rsplit(".", 1)[-1]
    siblings = [f for f in index.files if f.startswith(folder) and "/" not in f[len(folder):]]
    return path if any(f.endswith(suffix) and not is_test_path(f) for f in siblings) else None


def _remap_path(path: str, allowed: set[str]) -> str | None:
    """Map an obviously-meant path (placeholder prefix or same file name) onto the single allowed file."""
    name = path.rsplit("/", 1)[-1]
    candidates = [a for a in allowed if a.rsplit("/", 1)[-1] == name]
    if len(candidates) == 1:
        return candidates[0]
    if len(allowed) == 1 and (path.startswith("path/to/") or path in ("file.ext", "path/to/file.ext")):
        return next(iter(allowed))
    return None


def _resolve_import(index: RepoIndex, from_file: str, spec: str) -> str | None:
    """Resolve a Python/JS import specifier to a repository file, if it is local."""
    spec = spec.strip()
    base_dir = from_file.rsplit("/", 1)[0] if "/" in from_file else ""
    if from_file.endswith(".py"):
        level = len(spec) - len(spec.lstrip("."))
        mod = spec.lstrip(".").replace(".", "/")
        if level:
            parts = base_dir.split("/") if base_dir else []
            parts = parts[: len(parts) - (level - 1)] if level > 1 else parts
            mod = "/".join([*parts, mod]) if mod else "/".join(parts)
        candidates = [f"{mod}.py", f"{mod}/__init__.py", f"src/{mod}.py"]
        # "from pkg.module import Name" is recorded as "pkg.module.Name" too: try dropping the last segment.
        if "/" in mod:
            parent = mod.rsplit("/", 1)[0]
            candidates += [f"{parent}.py", f"{parent}/__init__.py"]
    elif spec.startswith("."):
        joined = f"{base_dir}/{spec}" if base_dir else spec
        parts: list[str] = []
        for part in joined.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        mod = "/".join(parts)
        candidates = [mod] + [mod + ext for ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs")] + \
                     [f"{mod}/index{ext}" for ext in (".js", ".ts", ".tsx")]
    else:
        return None
    for c in candidates:
        if c and index.has_file(c) and c != from_file:
            return c
    return None


def _lang_fence(path: str) -> str:
    return {"python": "python", "javascript": "javascript", "typescript": "typescript", "tsx": "tsx", "go": "go",
            "java": "java", "rust": "rust", "ruby": "ruby"}.get(language_for(path) or "", "")


def _tail_lines(text: str, n: int, max_chars: int) -> str:
    lines = (text or "").splitlines()
    return "\n".join(lines[-n:])[-max_chars:]


def _focus_output(stdout: str, stderr: str, max_chars: int = 3500) -> str:
    """Pick the most informative part of test output: failure sections and the summary."""
    text = f"{stdout}\n{stderr}".strip()
    if len(text) <= max_chars:
        return text
    markers = [m.start() for m in re.finditer(r"(?m)^(FAIL|ERROR|Traceback|=+ FAILURES|AssertionError|not ok|●|---)",
                                              text)]
    if markers:
        start = max(0, markers[0] - 200)
        chunk = text[start:start + max_chars - 600]
        return chunk + "\n…\n" + text[-600:]
    return text[-max_chars:]


def run_entry(row: dict) -> dict:
    """A finished command record as the agent sees it, with worktree paths made repository-relative."""
    output = _focus_output(row["stdout"] or "", row["stderr"] or "")
    if row.get("cwd"):
        output = output.replace(row["cwd"].rstrip("/\\") + "/", "")
    return {"stage": row["stage"], "command_id": row["id"], "argv": row["argv"], "exit_code": row["exit_code"],
            "status": row["status"], "summary": row["summary"], "output": output}


class LocPilotGraph:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps
        self.settings = deps.settings
        self.db = deps.db

    # helpers ------------------------------------------------------------------------
    async def emit(self, state: AgentState, type_: str, data: dict, persist: bool = True) -> None:
        await self.deps.emitter(state["task_id"], state["turn"], type_, data, persist)

    async def phase(self, state: AgentState, phase: str, label: str, status: str = "started", detail: str = "") -> None:
        if status == "started":
            self.db.update_task(state["task_id"], phase=phase)
        await self.emit(state, "phase", {"phase": phase, "label": label, "state": status, "detail": detail})

    async def log(self, state: AgentState, text: str, level: str = "info") -> None:
        await self.emit(state, "log", {"level": level, "text": text})

    def index(self, state: AgentState) -> RepoIndex:
        return get_index(Path(state["workspace_path"]))

    def _debug(self, state: AgentState, step: str, messages: list[dict], outputs: list[str],
               thinking: list[str] | None = None, tool_calls: list[dict] | None = None) -> None:
        if not self.settings.debug_llm:
            return
        record = {"ts": now_iso(), "task": state["task_id"], "turn": state["turn"], "step": step,
                  "prompt": messages[-1]["content"], "outputs": outputs}
        if thinking and any(thinking):
            record["thinking"] = thinking
        if tool_calls:
            record["tool_calls"] = tool_calls
        with open(self.settings.logs_dir / "llm-debug.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def _budget(self, state: AgentState, needed: int = 1) -> None:
        if state.get("llm_calls", 0) + needed > self.settings.max_llm_calls:
            raise BudgetExceeded(f"LLM call budget of {self.settings.max_llm_calls} reached for this turn")

    def _usage(self, state: AgentState, results: list) -> dict:
        return {"llm_calls": state.get("llm_calls", 0) + len(results),
                "prompt_tokens": state.get("prompt_tokens", 0) + sum(r.prompt_tokens for r in results),
                "completion_tokens": state.get("completion_tokens", 0) + sum(r.completion_tokens for r in results)}

    def _note_context(self, state: AgentState, client: LLMClient, prompt_tokens: int) -> None:
        """Remember how much of the context window the last model call filled (shown next to the memory meter)."""
        if not prompt_tokens:
            return
        task = self.db.get_task(state["task_id"])
        if task is None:
            return
        stats = task["stats"] or {}
        stats["context_tokens"] = int(prompt_tokens)
        stats["context_window"] = int(getattr(client, "num_ctx", None) or self.settings.num_ctx)
        self.db.update_task(state["task_id"], stats=stats)

    def _think(self, step: str) -> bool:
        """Whether a thinking model reasons before answering in this step (see Settings.thinking)."""
        mode = self.settings.thinking
        return mode == "on" or (mode == "auto" and step in THINKING_STEPS)

    async def llm_json(self, state: AgentState, step: str, user: str, model_cls, max_tokens: int,
                       messages: list[dict] | None = None, keep: list | None = None,
                       tools: list[dict] | None = None):
        """One structured answer. ``messages`` continues a conversation instead of starting with ``user`` (then
        ``tools`` must be the tools that conversation was offered); ``keep`` receives the raw model results."""
        self._budget(state)
        client: LLMClient = self.deps.llm()
        await self.emit(state, "llm", {"step": step, "state": "started", "model": client.model})

        async def progress(tokens: int) -> None:
            await self.emit(state, "llm_progress", {"step": step, "tokens": tokens}, persist=False)

        messages = messages or [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": user}]
        try:
            obj, results = await chat_json(client, messages, model_cls, max_tokens=max_tokens, on_progress=progress,
                                           think=self._think(step), tools=tools)
        except LLMResponseError as exc:
            self._debug(state, step, messages, [f"ERROR: {exc}"])
            await self.emit(state, "llm", {"step": step, "state": "failed", "model": client.model})
            raise
        self._debug(state, step, messages, [r.content for r in results], [r.thinking for r in results])
        self._note_context(state, client, max((r.prompt_tokens for r in results), default=0))
        if keep is not None:
            keep.extend(results)
        await self.emit(state, "llm", {
            "step": step, "state": "completed", "model": client.model,
            "prompt_tokens": sum(r.prompt_tokens for r in results),
            "completion_tokens": sum(r.completion_tokens for r in results),
            "duration_ms": sum(r.duration_ms for r in results)})
        return obj, self._usage(state, results)

    async def _tools_ok(self) -> bool:
        """Whether the model is offered real tools (it can call them, and the setting allows it)."""
        check = getattr(self.deps.llm(), "supports_tools", None)
        return bool(self.settings.native_tools and check is not None and await check())

    async def llm_text(self, state: AgentState, step: str, messages: list[dict], max_tokens: int,
                       tools: list[dict] | None = None):
        self._budget(state)
        client: LLMClient = self.deps.llm()
        await self.emit(state, "llm", {"step": step, "state": "started", "model": client.model})

        async def progress(tokens: int) -> None:
            await self.emit(state, "llm_progress", {"step": step, "tokens": tokens}, persist=False)

        extra = {"tools": tools} if tools else {}
        result = await client.chat(messages, max_tokens=max_tokens, on_progress=progress, think=self._think(step),
                                   **extra)
        self._debug(state, step, messages, [result.content], [result.thinking], result.tool_calls)
        self._note_context(state, client, result.prompt_tokens)
        await self.emit(state, "llm", {"step": step, "state": "completed", "model": client.model,
                                       "prompt_tokens": result.prompt_tokens,
                                       "completion_tokens": result.completion_tokens,
                                       "duration_ms": result.duration_ms})
        return result

    def _window(self) -> int:
        """The model's context window in tokens."""
        return int(getattr(self.deps.llm(), "num_ctx", None) or self.settings.num_ctx)

    def _follow_up(self, convo: list[dict], tokens: int, job: str, reserve: int) -> list[dict] | None:
        """``convo`` continued with ``job`` — if that and ``reserve`` answer tokens still fit the context window."""
        if not convo or tokens + len(job) / CHARS_PER_TOKEN + reserve + 300 > self._window():
            return None
        return [*convo, {"role": "user", "content": job}]

    def _fit(self, build: Callable[[int | None], str], reserve: int) -> str:
        """The prompt with as much of each evidence item as fits the context window next to ``reserve`` answer
        tokens (a prompt that does not fit is cut by the model server, silently and from the top)."""
        window = self._window()
        text = build(None)
        for lines in (90, 70, 50, 35):
            if len(text) / CHARS_PER_TOKEN + reserve + 400 <= window:
                break
            text = build(lines)
        return text

    def _context(self, state: AgentState, *, evidence_lines: int | None = None, with_evidence: bool = True,
                 with_user_runs: bool = True) -> str:
        overview = state.get("overview") or {}
        summary = overview.get("prompt_summary") or ""
        history = "\n".join(f"- Turn {h['turn']}: user asked \"{h['request'][:200]}\" → {h['headline'][:200]}"
                            for h in (state.get("history") or [])[-4:])
        evidence = render_evidence(state.get("evidence") or [], max_lines_per_item=evidence_lines) if with_evidence else ""
        user_runs = self._runs_text(state["user_runs"])[:3500] if with_user_runs and state.get("user_runs") else ""
        return prompts.context_block(summary, evidence, state.get("notes") or [], history, user_runs,
                                     state.get("project_notes") or "")

    # nodes --------------------------------------------------------------------------
    async def start_turn(self, state: AgentState) -> dict:
        updates: dict = {}
        if state["mode"] == "code" and not state.get("worktree_path"):
            await self.phase(state, "inspect", "Creating isolated Git worktree")
            try:
                ws = await asyncio.to_thread(prepare_worktree, self.settings, Path(state["repo_path"]), state["task_id"])
            except (WorkspaceError, git.GitError) as exc:
                await self.phase(state, "inspect", "Creating isolated Git worktree", "failed", str(exc))
                return {"workspace_path": state["repo_path"], "unresolved": [f"Could not create a worktree: {exc}"],
                        "final_status": "failed", "mode": "ask"}
            self.db.update_task(state["task_id"], worktree_path=ws["worktree_path"], base_commit=ws["base_commit"],
                                workspace_path=ws["worktree_path"])
            for notice in ws["notices"]:
                await self.log(state, notice)
            await self.phase(state, "inspect", "Creating isolated Git worktree", "completed",
                             f"Worktree at {ws['worktree_path']}")
            updates.update(worktree_path=ws["worktree_path"], base_commit=ws["base_commit"])
        workspace = updates.get("worktree_path") or state.get("worktree_path") or state["repo_path"]
        updates["workspace_path"] = workspace
        if not state.get("worktree_path") and not updates.get("worktree_path"):
            self.db.update_task(state["task_id"], workspace_path=workspace)
        notes, notice = await asyncio.to_thread(load_project_notes, Path(state["repo_path"]),
                                                self.settings.project_notes_chars)
        updates["project_notes"] = notes
        if notice:
            await self.log(state, notice)
        warning = await self._memory_warning()
        if warning:
            await self.log(state, warning, level="warning")
        return updates

    async def _memory_warning(self) -> str | None:
        """A warning when loading the model would not fit in the free RAM (the machine would swap: very slow)."""
        check = getattr(self.deps.llm(), "memory_check", None)
        if check is None:
            return None
        try:
            mem = await check()
        except Exception:  # noqa: BLE001 - only advice: never fail a turn over it
            return None
        return memory_warning(mem)

    def route_after_start(self, state: AgentState) -> str:
        return "finalize" if state.get("final_status") == "failed" else "understand"

    async def understand(self, state: AgentState) -> dict:
        await self.phase(state, "understand", "Understanding the request")
        index = await asyncio.to_thread(self.index, state)
        overview = state.get("overview")
        if not overview or state.get("turn", 1) == 1:
            ov = await asyncio.to_thread(inspect_project, index, Path(state["repo_path"]))
            overview = ov.to_dict() | {"prompt_summary": ov.prompt_summary()}
        state = {**state, "overview": overview}
        user = self._context(state, with_evidence=False) + "\n\n" + prompts.UNDERSTAND.format(request=state["request"])
        u, usage = await self.llm_json(state, "understand", user, TaskUnderstanding, max_tokens=500)
        intent = u.intent
        if state["mode"] == "code" and intent == "root_cause" and _asks_for_fix(state["request"]):
            intent = "fix"  # explicit "fix it" in Code mode: don't stop at the analysis
        has_changes = bool(self._applied_patches(state))
        if intent in ("review_changes", "explain_changes", "retry_fix") and not has_changes:
            intent = "root_cause" if intent == "retry_fix" else "question"
        updates: dict = {"understanding": u.model_dump(), "intent": intent, "overview": overview, **usage}
        if state.get("turn", 1) > 1 and intent not in ("review_changes", "explain_changes", "retry_fix"):
            if u.relates_to_previous:
                updates["evidence"] = (state.get("evidence") or [])[-8:]
                updates["notes"] = (state.get("notes") or [])[-6:]
            else:
                updates["evidence"] = []
                updates["notes"] = []
        await self.phase(state, "understand", "Understanding the request", "completed",
                         f"Intent: {intent.replace('_', ' ')} — {u.summary}")
        return updates

    def route_after_understand(self, state: AgentState) -> str:
        intent = state["intent"]
        u = TaskUnderstanding.model_validate(state["understanding"])
        has_changes = bool(self._applied_patches(state))
        if intent in ("review_changes", "explain_changes") and has_changes:
            return "review"
        if intent == "retry_fix" and state["mode"] == "code" and has_changes:
            return "diagnose"
        if intent in ("fix", "add_tests") and state["mode"] == "code" and state.get("report") \
                and u.relates_to_previous and (state["report"] or {}).get("kind") == "rca":
            return "plan"
        if u.needs_clarification:
            terms = extract_terms(state["request"], None)
            if not terms.identifiers and not terms.quoted and not terms.paths and len(terms.words) <= 1:
                return "clarify"
        return "inspect"

    async def inspect(self, state: AgentState) -> dict:
        await self.phase(state, "inspect", "Inspecting repository structure")

        loop = asyncio.get_running_loop()
        last_report = [0.0]

        def progress(done: int, total: int) -> None:
            # Only a first look at a big repository takes long enough to be worth reporting; parses are stored.
            now = time.monotonic()
            if total < 400 or (now - last_report[0] < 1.5 and done < total):
                return
            last_report[0] = now
            detail = f"Indexing code: {done:,} of {total:,} files (stored, so this is faster next time)"
            asyncio.run_coroutine_threadsafe(self.emit(state, "phase", {
                "phase": "inspect", "label": "Inspecting repository structure", "state": "started",
                "detail": detail}, persist=False), loop)

        def work() -> dict:
            index = self.index(state)
            index.refresh(force=True)
            symbols = get_symbol_index(index, progress)
            return {"files": len(index.files), "symbols": symbols.symbol_count,
                    "truncated": index.truncated or symbols.truncated}

        info = await asyncio.to_thread(work)
        ov = state.get("overview") or {}
        langs = ", ".join(f"{lang['name']} ({lang['files']})" for lang in (ov.get("languages") or [])[:4]) or "unknown"
        summary = (f"{info['files']} files indexed, {info['symbols']} symbols; languages: {langs}; "
                   f"tests: {', '.join(ov.get('test_frameworks') or []) or 'none detected'}")
        await self.emit(state, "tool_call", {"tool": "inspect_project", "args": {}, "summary": summary,
                                             "evidence_ids": []})
        await self.phase(state, "inspect", "Inspecting repository structure", "completed", summary)
        return {"tool_calls": state.get("tool_calls", 0) + 1}

    async def seed_evidence(self, state: AgentState) -> dict:
        await self.phase(state, "evidence", "Gathering evidence")
        u = TaskUnderstanding.model_validate(state["understanding"]) if state.get("understanding") else None
        share = SEED_SHARE if await self._tools_ok() else 1.0

        def work():
            index = self.index(state)
            collector = EvidenceCollector(index, get_symbol_index(index), state.get("evidence") or [],
                                          max_chars=int(self.settings.max_evidence_chars * share))
            log = collector.seed(state["request"], u, state["intent"])
            return collector, log

        collector, log = await asyncio.to_thread(work)
        new_ids = [i["id"] for i in collector.items if i["id"] not in {e["id"] for e in state.get("evidence") or []}]
        await self.emit(state, "tool_call", {
            "tool": "retrieve", "args": {"terms": log.get("terms", [])[:12], "quoted": log.get("quoted", [])},
            "summary": f"{len(new_ids)} snippets from {len({i['path'] for i in collector.items})} files "
                       f"(ranked: {', '.join(log.get('ranked_files', [])[:4]) or 'none'})",
            "evidence_ids": new_ids})
        await self.emit(state, "evidence", {"items": [self._ev_summary(i) for i in collector.items
                                                      if i["id"] in new_ids]})
        return {"evidence": collector.items, "notes": (state.get("notes") or []) + collector.notes,
                "tool_log": (state.get("tool_log") or []) + [{"tool": "retrieve", "query": " ".join(log.get("terms", []))}],
                "tool_calls": state.get("tool_calls", 0) + 1}

    @staticmethod
    def _ev_summary(item: dict) -> dict:
        return {k: item[k] for k in ("id", "path", "start_line", "end_line", "kind", "title")}

    def route_after_seed(self, state: AgentState) -> str:
        if self.settings.max_evidence_rounds > 0 and state.get("llm_calls", 0) + 3 <= self.settings.max_llm_calls:
            return "plan_evidence"
        return "analyze"

    async def _explore(self, state: AgentState) -> dict:
        """Evidence gathering as a tool loop: the model reads and searches, seeing each result before the next.

        Whatever it looks at becomes evidence (with ids the analysis can cite); its closing summary is kept as a
        note for the analysis.
        """
        label = "Exploring the code"
        await self.phase(state, "evidence", label)
        u = state.get("understanding") or {}
        user = self._context(state, evidence_lines=EXPLORE_SNIPPET_LINES) + "\n\n" + prompts.EXPLORE.format(
            request=state["request"], summary=u.get("summary", ""))
        messages = [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": user}]
        understanding = TaskUnderstanding.model_validate(state["understanding"]) if state.get("understanding") else None
        terms = extract_terms(state["request"], understanding)

        def open_session() -> ExploreSession:
            index = self.index(state)
            # What the model chooses to read may go past the keyword search's budget, further in a larger window.
            budget = max(self.settings.max_evidence_chars * EXPLORE_SHARE, window * 0.5 * CHARS_PER_TOKEN)
            collector = EvidenceCollector(index, get_symbol_index(index), state.get("evidence") or [],
                                          max_chars=int(budget))
            return ExploreSession(collector, Path(state["workspace_path"]), shown_lines=EXPLORE_SNIPPET_LINES)

        window = self._window()
        session = await asyncio.to_thread(open_session)
        collector = session.collector
        before = {e["id"] for e in state.get("evidence") or []}
        limit = min(int(0.8 * window), window - 3800)  # the analysis continues this conversation: leave it room
        used = 0
        results = []
        tool_log = list(state.get("tool_log") or [])
        lookups, conclusion = 0, ""
        local = dict(state)
        for _turn in range(MAX_EXPLORE_TURNS):
            if local.get("llm_calls", 0) + 3 > self.settings.max_llm_calls:
                break
            result = await self.llm_text(local, "explore", messages, max_tokens=700, tools=EXPLORE_TOOLS)
            results.append(result)
            local["llm_calls"] = local.get("llm_calls", 0) + 1
            used = result.prompt_tokens + result.completion_tokens
            if not result.tool_calls:
                conclusion = result.content.strip()
                messages = messages + [{"role": "assistant", "content": result.content}]
                break
            replies = []
            for call in result.tool_calls[:4]:
                reply, asked, new_ids = await asyncio.to_thread(session.handle, call)
                fresh = [i for i in collector.items if i["id"] in new_ids]
                followed = await asyncio.to_thread(collector.add_callees, fresh, terms) if fresh else []
                if followed:  # what the code just read calls, in other files
                    reply += "\n\nAlso shown, because that code calls them:\n" + render_evidence(followed)
                    new_ids = new_ids + [i["id"] for i in followed]
                    session.shown.update(i["id"] for i in followed)
                replies.append(tool_message(call, reply))
                lookups += 1
                await self.emit(state, "tool_call", {
                    "tool": asked["tool"], "args": {k: v for k, v in asked.items() if k != "tool" and v},
                    "summary": f"{len(new_ids)} new snippets" if new_ids else reply.split("\n", 1)[0][:160],
                    "evidence_ids": new_ids})
                tool_log.append({"tool": asked["tool"], "query": asked.get("query", ""), "path": asked.get("path", "")})
            replies += [tool_message(call, SKIPPED_CALL) for call in result.tool_calls[4:]]
            messages = messages + [assistant_message(result), *replies]
            used += int(sum(len(r["content"]) for r in replies) / CHARS_PER_TOKEN)
            if used > limit or session.repeats >= 2:  # no room left for the analysis, or going round in circles
                break
            if any(kind_of(c) in ("edit", "write") for c in result.tool_calls):
                break  # it has started on the change: it has seen enough (the call was answered "not yet")
        new_items = [i for i in collector.items if i["id"] not in before]
        if new_items:
            await self.emit(state, "evidence", {"items": [self._ev_summary(i) for i in new_items]})
        await self.phase(state, "evidence", label, "completed", f"{lookups} lookups, {len(new_items)} new snippets")
        notes = (state.get("notes") or []) + collector.notes
        if conclusion:
            notes.append("From exploring the code: " + " ".join(conclusion.split())[:700])
        return {"evidence": collector.items, "notes": notes, "tool_log": tool_log,
                "tool_calls": state.get("tool_calls", 0) + lookups,
                "evidence_rounds": state.get("evidence_rounds", 0) + 1, "evidence_done": True,
                "explore_messages": messages, "conversation_tokens": used,
                **self._usage(state, results)}

    async def plan_evidence(self, state: AgentState) -> dict:
        if await self._tools_ok():
            return await self._explore(state)
        rounds = state.get("evidence_rounds", 0)
        label = "Deciding what else to inspect" + (f" (round {rounds + 1})" if rounds else "")
        await self.phase(state, "evidence", label)
        done = "; ".join(f"{t['tool']}({t.get('query', '')[:60]})" for t in state.get("tool_log") or []) or "none"
        u = state.get("understanding") or {}
        user = self._context(state, evidence_lines=45) + "\n\n" + prompts.EVIDENCE_PLAN.format(
            request=state["request"], summary=u.get("summary", ""), done=done)
        plan, usage = await self.llm_json(state, "plan_evidence", user, EvidencePlan, max_tokens=500)
        updates: dict = {**usage, "evidence_rounds": rounds + 1}
        if plan.sufficient or not plan.requests:
            await self.phase(state, "evidence", label, "completed",
                             "Evidence judged sufficient" if plan.sufficient else "No further tool calls requested")
            updates["evidence_done"] = True
            return updates
        seen = {(t["tool"], t.get("query", ""), t.get("path", "")) for t in state.get("tool_log") or []}
        before = {e["id"] for e in state.get("evidence") or []}

        def work():
            index = self.index(state)
            collector = EvidenceCollector(index, get_symbol_index(index), state.get("evidence") or [],
                                          max_chars=self.settings.max_evidence_chars)
            outcomes = []
            for req in plan.requests[:4]:
                key = (req.tool, req.query.strip(), req.path.strip())
                if key in seen:
                    continue
                seen.add(key)
                summary, ids = collector.run_tool(req)
                outcomes.append((req, summary, ids))
            # Follow calls out of what was just read into the other files they lead to.
            fresh = [i for i in collector.items if i["id"] not in before]
            u = TaskUnderstanding.model_validate(state["understanding"]) if state.get("understanding") else None
            callees = collector.add_callees(fresh, extract_terms(state["request"], u))
            if callees:
                outcomes.append((None, f"{len(callees)} called functions: "
                                       + ", ".join(i["symbol"] or i["path"] for i in callees),
                                 [i["id"] for i in callees]))
            return collector, outcomes

        collector, outcomes = await asyncio.to_thread(work)
        tool_log = list(state.get("tool_log") or [])
        for req, summary, ids in outcomes:
            if req is None:  # functions the new evidence calls, followed automatically
                await self.emit(state, "tool_call", {"tool": "find_definition", "args": {}, "summary": summary,
                                                     "reason": "followed calls into other files", "evidence_ids": ids})
                continue
            args = {k: v for k, v in {"query": req.query, "path": req.path, "start_line": req.start_line or None,
                                      "end_line": req.end_line or None}.items() if v}
            await self.emit(state, "tool_call", {"tool": req.tool, "args": args, "summary": summary,
                                                 "reason": req.reason, "evidence_ids": ids})
            tool_log.append({"tool": req.tool, "query": req.query, "path": req.path})
        new_items = [i for i in collector.items if i["id"] not in before]
        if new_items:
            await self.emit(state, "evidence", {"items": [self._ev_summary(i) for i in new_items]})
        await self.phase(state, "evidence", label, "completed",
                         f"{len(outcomes)} tool calls, {len(new_items)} new snippets"
                         + (f"; still missing: {plan.missing[:160]}" if plan.missing else ""))
        updates.update(evidence=collector.items, notes=(state.get("notes") or []) + collector.notes,
                       tool_log=tool_log, tool_calls=state.get("tool_calls", 0) + len(outcomes),
                       evidence_done=not new_items)
        return updates

    def route_after_plan_evidence(self, state: AgentState) -> str:
        if state.get("evidence_done") or state.get("evidence_rounds", 0) >= self.settings.max_evidence_rounds:
            return "analyze"
        if state.get("llm_calls", 0) + 3 > self.settings.max_llm_calls:
            return "analyze"
        return "plan_evidence"

    async def analyze(self, state: AgentState) -> dict:
        rca = state["intent"] in ("root_cause", "fix", "retry_fix", "add_tests")
        label = "Forming and checking hypotheses" if rca else "Analyzing the code"
        await self.phase(state, "analyze", label)
        if not state.get("evidence"):
            await self.log(state, "No relevant code was found for this request; the answer will say so.", "warning")
        template = prompts.ANALYZE_RCA if rca else prompts.ANALYZE_EXPLAIN
        job = template.format(request=state["request"])
        if rca:
            job += prompts.pitfalls(i["path"] for i in state.get("evidence") or [])
        model_cls = LLMAnalysis if rca else LLMExplanation
        results: list = []
        # After exploring, the analysis continues that conversation (what the model read is all there); otherwise
        # it gets the evidence in a prompt of its own.
        messages = self._follow_up(state.get("explore_messages") or [], state.get("conversation_tokens", 0), job,
                                   reserve=3000)
        continued = messages is not None
        raw = None
        if continued:
            try:
                raw, usage = await self.llm_json(state, "analyze", "", model_cls, max_tokens=3000, messages=messages,
                                                 keep=results, tools=EXPLORE_TOOLS)
            except LLMResponseError as exc:  # not worth failing the task over: ask again the plain way
                await self.log(state, f"The analysis could not continue the exploration ({str(exc)[:160]}); "
                                      "starting it from the evidence instead.", "warning")
                continued, results = False, []
        if raw is None:
            user = self._fit(lambda lines: self._context(state, evidence_lines=lines) + "\n\n" + job, reserve=3000)
            messages = [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": user}]
            raw, usage = await self.llm_json(state, "analyze", "", model_cls, max_tokens=3000, messages=messages,
                                             keep=results)
        await self.phase(state, "analyze", label, "completed")
        updates = {"raw_analysis": {"kind": "rca" if rca else "explanation", "data": raw.model_dump()},
                   "explore_messages": [], "analysis_messages": [], **usage}
        plan_next = state["mode"] == "code" and state["intent"] in ("fix", "retry_fix", "add_tests")
        if plan_next and len(results) == 1 and await self._tools_ok():  # the plan continues from here
            updates.update(analysis_messages=[*messages, {"role": "assistant", "content": results[0].content}],
                           conversation_tokens=results[0].prompt_tokens + results[0].completion_tokens,
                           conversation_tools=continued)
        return updates

    async def validate(self, state: AgentState) -> dict:
        await self.phase(state, "validate", "Validating citations against the repository")
        raw = state["raw_analysis"]

        def work() -> InvestigationReport:
            index = self.index(state)
            if raw["kind"] == "rca":
                return build_rca_report(LLMAnalysis.model_validate(raw["data"]), intent=state["intent"], index=index,
                                        evidence=state.get("evidence") or [], tool_log=state.get("tool_log") or [])
            return build_explanation_report(LLMExplanation.model_validate(raw["data"]), intent=state["intent"],
                                            index=index, evidence=state.get("evidence") or [],
                                            tool_log=state.get("tool_log") or [])

        report = await asyncio.to_thread(work)
        cited = len({(c.path, c.start_line) for s in report.execution_flow for c in s.citations}
                    | {(c.path, c.start_line) for c in report.root_cause_citations})
        detail = f"{cited} distinct citations verified"
        if report.removed_citations:
            detail += f"; {len(report.removed_citations)} unverifiable citation(s) removed"
        await self.emit(state, "report", {"report": report.model_dump()})
        await self.phase(state, "validate", "Validating citations against the repository", "completed", detail)
        return {"report": report.model_dump(), "report_turn": state["turn"]}

    def route_after_validate(self, state: AgentState) -> str:
        if state["mode"] == "code" and state["intent"] in ("fix", "retry_fix", "add_tests"):
            return "plan"
        return "finalize"

    async def clarify(self, state: AgentState) -> dict:
        u = state.get("understanding") or {}
        question = (u.get("clarifying_question") or "").strip() or \
            "Which file, function, endpoint or error message should I focus on?"
        await self.phase(state, "clarify", "Asking for clarification", "completed", question)
        return {"clarifying_question": question}

    # --- Code mode -----------------------------------------------------------------------
    def _existing_path(self, index: RepoIndex, raw: str) -> str | None:
        raw = (raw or "").strip().strip("`'\"").lstrip("./")
        if not raw:
            return None
        try:
            raw = normalize_relpath(raw)
        except PathSecurityError:
            return None
        if index.has_file(raw):
            return raw
        matches = [f for f in index.files if f.endswith("/" + raw)]
        return matches[0] if len(matches) == 1 else None

    def _default_test_path(self, index: RepoIndex, fix_files: list[str], near: str | None = None) -> str | None:
        if not fix_files:
            return None
        target = fix_files[0]
        stem = target.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        ext = target.rsplit(".", 1)[-1] if "." in target else "py"

        def closeness(test: str) -> int:  # shared leading folders: tests of the same package come first
            shared = 0
            for a, b in zip(test.split("/")[:-1], target.split("/")[:-1], strict=False):
                if a != b:
                    break
                shared += 1
            return -shared

        tests = [near] if near else sorted((f for f in index.files if is_test_path(f) and f.endswith("." + ext)),
                                           key=closeness)
        if ext == "py":
            base = tests[0].rsplit("/", 1)[0] if tests and "/" in tests[0] else ("tests" if any(
                f.startswith("tests/") for f in index.files) else "")
            return f"{base + '/' if base else ''}test_{stem}_regression.py"
        if tests:
            base = tests[0].rsplit("/", 1)[0] if "/" in tests[0] else ""
            return f"{base + '/' if base else ''}{stem}.regression.test.{ext}"
        return f"{stem}.regression.test.{ext}"

    def _proposed_commands(self, state: AgentState) -> tuple[list[dict], list[str]]:
        overview = state.get("overview") or {}
        workspace = Path(state["workspace_path"])
        origin = Path(state["repo_path"])
        proposed, warnings = [], []
        frameworks = ", ".join(overview.get("test_frameworks") or []) or "detected test runner"
        for argv in (overview.get("test_commands") or [])[:2]:
            try:
                validated = validate_command(list(argv), workspace, origin)
            except CommandPolicyError as exc:
                warnings.append(f"Skipped test command {' '.join(argv)}: {exc}")
                continue
            proposed.append({"argv": list(argv), "display": validated.display,
                             "reason": f"Run the project's test suite ({frameworks})", "trusted": False,
                             "notes": validated.notes})
        if not proposed:
            warnings.append("No test command was detected for this repository, so verification will be skipped.")
        return proposed, warnings

    async def plan(self, state: AgentState) -> dict:
        await self.phase(state, "plan", "Planning the change")
        report = state.get("report") or {}
        overview = state.get("overview") or {}
        job = prompts.PLAN.format(
            problem=report.get("problem_summary", ""), root_cause=report.get("most_likely_root_cause") or "unknown",
            minimal_fix=report.get("minimal_fix", ""), request=state["request"],
            test_files=", ".join((overview.get("test_files") or [])[:12]) or "none",
            frameworks=", ".join(overview.get("test_frameworks") or []) or "unknown")
        messages = self._follow_up(state.get("analysis_messages") or [], state.get("conversation_tokens", 0), job,
                                   reserve=800)
        raw = None
        if messages is not None:
            try:
                raw, usage = await self.llm_json(state, "plan", "", LLMPlan, max_tokens=800, messages=messages,
                                                 tools=EXPLORE_TOOLS if state.get("conversation_tools") else None)
            except LLMResponseError:
                pass  # ask again the plain way
        if raw is None:
            messages = [{"role": "system", "content": prompts.SYSTEM},
                        {"role": "user", "content": self._context(state) + "\n\n" + job}]
            raw, usage = await self.llm_json(state, "plan", "", LLMPlan, max_tokens=800, messages=messages)
        index = await asyncio.to_thread(self.index, state)
        warnings: list[str] = []
        files = []
        for f in raw.files_to_modify:
            path = self._existing_path(index, f.path) or new_source_path(index, f.path)
            if path is None:
                warnings.append(f"Plan referenced {f.path!r}, which does not exist; ignored.")
            elif is_test_path(path):
                continue
            elif path not in [x["path"] for x in files]:
                files.append({"path": path, "change": f.change})
        if not files:
            for c in report.get("root_cause_citations") or []:
                if not is_test_path(c["path"]) and c["path"] not in [x["path"] for x in files]:
                    files.append({"path": c["path"], "change": report.get("minimal_fix", "")})
        test_file = None
        if raw.test_file.strip():
            existing = self._existing_path(index, raw.test_file)
            if existing and is_test_path(existing):
                # Regression tests go in a new file so existing tests are never rewritten.
                test_file = self._default_test_path(index, [f["path"] for f in files] or [existing],
                                                    near=existing)
            else:
                try:
                    candidate = normalize_relpath(raw.test_file.strip().lstrip("./"))
                except PathSecurityError:
                    candidate = None
                js_family = {"javascript", "typescript", "tsx"}
                cand_lang = language_for(candidate) if candidate else None
                code_lang = language_for(files[0]["path"]) if files else cand_lang
                if candidate and is_test_path(candidate) and cand_lang and (
                        cand_lang == code_lang or {cand_lang, code_lang} <= js_family):
                    test_file = candidate
        if test_file is None and files:
            test_file = self._default_test_path(index, [f["path"] for f in files])
        commands, cmd_warnings = self._proposed_commands(state)
        warnings += cmd_warnings
        plan_out = {"summary": raw.summary, "approach": raw.approach, "files_to_modify": files,
                    "test_file": test_file, "test_description": raw.test_description, "risks": raw.risks,
                    "commands": commands}
        await self.emit(state, "plan", {"plan": plan_out})
        for w in warnings:
            await self.log(state, w, "warning")
        if not files:
            await self.phase(state, "plan", "Planning the change", "failed", "No file to change could be identified")
            return {"plan": plan_out, "proposed_commands": commands, **usage,
                    "patch_error": "Could not identify which existing file to change from the verified evidence.",
                    "warnings": (state.get("warnings") or []) + warnings}
        await self.phase(state, "plan", "Planning the change", "completed",
                         f"Change {', '.join(f['path'] for f in files)}; regression test in {test_file or 'n/a'}")
        updates = {"plan": plan_out, "proposed_commands": commands, **usage, "analysis_messages": [],
                   "warnings": (state.get("warnings") or []) + warnings}
        # "Add X, with tests" is easily read as a request for tests. Tests alone are written for code that exists:
        # when the plan creates the code, it is a change with tests.
        if state["intent"] == "add_tests" and any(not index.has_file(f["path"]) for f in files):
            await self.log(state, "The request needs new code, not only tests: writing the code first, then its tests.")
            updates["intent"] = "fix"
        return updates

    def route_after_plan(self, state: AgentState) -> str:
        return "finalize" if state.get("patch_error") else "generate_patch"

    def _file_block(self, index: RepoIndex, path: str, max_lines: int = 500) -> str:
        lines = index.read_lines(path)
        if lines is None:
            return f"### {path}\n(new file — does not exist yet)"
        body = "\n".join(lines[:max_lines])
        more = f"\n… ({len(lines) - max_lines} more lines not shown)" if len(lines) > max_lines else ""
        return f"### {path}\n```{_lang_fence(path)}\n{redact(body)}\n```{more}"

    async def _tool_edits(self, state: AgentState, local_state: dict, step: str, messages: list[dict],
                          usage: dict, session: EditSession, walk: bool = False):
        """The edit step as a tool loop: the model reads, edits, sees each result, and stops when it is done.

        With ``walk`` the model is asked once, when it has finished, to walk the request's example through its
        changed code with real values and to correct what that shows: a small model that declares a no-op fix
        done finds the mistake as soon as it has to compute a result.
        Returns the conversation and the model's last reply without tool calls (None if the turn limit ended it).
        """
        window = self._window()
        previous, stuck, cut_off = "", 0, False

        async def walk_through(convo: list[dict]) -> list[dict] | None:
            """The conversation to go on with if the model's own walk-through shows its change is wrong."""
            nonlocal walk
            if not walk or not session.staged or local_state["llm_calls"] + 2 > self.settings.max_llm_calls:
                return None
            walk = False
            asked = [*convo, {"role": "user", "content": prompts.WALK_THROUGH}]
            kept: list = []
            try:
                # A JSON answer, so it reasons it out instead of reaching for a tool; the tools stay declared
                # because this conversation was offered them (the model server reuses only an identical start).
                checked, _totals = await self.llm_json(local_state, step, "", LLMWalkThrough, max_tokens=900,
                                                       messages=asked, keep=kept, tools=EDIT_TOOLS)
            except LLMResponseError:
                return None
            for k, v in (("llm_calls", len(kept)), ("prompt_tokens", sum(r.prompt_tokens for r in kept)),
                         ("completion_tokens", sum(r.completion_tokens for r in kept))):
                usage[k] += v
            local_state["llm_calls"] = state.get("llm_calls", 0) + usage["llm_calls"]
            if checked.correct or not kept:
                return None
            await self.log(state, "Walking the request's example through the change showed that it does not do what "
                                  f"was asked (it returns: {' '.join(checked.result.split())[:160]}); correcting it.")
            return [*asked, {"role": "assistant", "content": kept[-1].content},
                    {"role": "user", "content": prompts.WALK_FAILED.format(result=checked.result.strip(),
                                                                           expected=checked.expected.strip())}]
        for _turn in range(MAX_EDIT_TURNS):
            if local_state["llm_calls"] >= self.settings.max_llm_calls:
                break
            result = await self.llm_text(local_state, step, messages, max_tokens=EDIT_TURN_TOKENS, tools=EDIT_TOOLS)
            for k, v in (("llm_calls", 1), ("prompt_tokens", result.prompt_tokens),
                         ("completion_tokens", result.completion_tokens)):
                usage[k] += v
            local_state["llm_calls"] = state.get("llm_calls", 0) + usage["llm_calls"]
            session.tokens = result.prompt_tokens + result.completion_tokens
            if not result.tool_calls and result.truncated and not cut_off:
                # The reply ran into the length limit. Inside a tool call (a long new file, usually) the call is
                # incomplete and the model server drops it: say so, or minutes of writing vanish without a trace.
                # In prose the model was working something out at length — with edits made, it is done.
                prose = len(result.content) > 1500
                if prose and session.staged:
                    return messages, None
                cut_off = True
                await self.log(state, "The model's reply ran to the length limit without an edit; asking again."
                               if prose else "The model's edit was longer than one reply allows and was cut off; "
                                             "asking for a shorter one.", "warning")
                messages = messages + [{"role": "assistant", "content": result.content[:600] or "(cut off)"},
                                       {"role": "user",
                                        "content": prompts.CUT_OFF_PROSE if prose else prompts.CUT_OFF_CALL}]
                continue
            if not result.tool_calls:
                checked = await walk_through([*messages, {"role": "assistant", "content": result.content or "DONE"}])
                if checked is None:
                    return messages, result
                messages = checked
                continue
            # A model that keeps asking for something it cannot have (a shell, the same call again), or whose
            # edits are all refused, is stuck: two such turns in a row end the loop with what is staged.
            asked = json.dumps(result.tool_calls, sort_keys=True)
            useful = asked != previous and any(kind_of(c) in ("read", "search", "edit", "write")
                                               for c in result.tool_calls)
            previous = asked
            if not useful and stuck >= 1:
                break
            session.problems, session.new_reads = [], 0
            session.can_read = result.prompt_tokens + result.completion_tokens < 0.72 * window
            replies = []
            staged_before = len(session.staged)
            for call in result.tool_calls[:8]:
                reply = await asyncio.to_thread(session.handle, call)
                replies.append(tool_message(call, reply))
                args = call.get("arguments") or {}
                shown = {k: v for k, v in {"path": arg(args, *PATH_ARGS), "query": arg(args, "pattern", "query")}.items()
                         if v}
                tool = {"read": "read_file", "search": "search_text"}.get(kind_of(call), kind_of(call))
                await self.emit(state, "tool_call", {"tool": tool, "args": shown,
                                                     "summary": reply.split("\n", 1)[0][:200], "evidence_ids": []})
            replies += [tool_message(call, SKIPPED_CALL) for call in result.tool_calls[8:]]
            messages = messages + [assistant_message(result), *replies]
            session.tokens += int(sum(len(r["content"]) for r in replies) / CHARS_PER_TOKEN)
            kinds = {kind_of(c) for c in result.tool_calls}
            staged_now = len(session.staged) > staged_before
            refused = bool(session.problems) and not staged_now and kinds <= {"edit", "write"}
            # With edits made, a turn that only looks again at code it has already seen is the model checking its
            # work with nothing left to do: it rarely says DONE by itself, and each such turn costs time.
            idle = bool(session.staged) and not staged_now and "read" in kinds and not session.new_reads
            if staged_now or session.new_reads:
                stuck = 0
            elif refused or not useful or idle:
                stuck += 1  # (a search alone neither counts nor clears the count)
            if stuck >= 2:
                checked = await walk_through(messages)
                if checked is None:
                    break
                messages, stuck = checked, 0
        return messages, None

    async def _edit_loop(self, state: AgentState, step: str, prompt: str, *, workspace: Path, allowed: set[str],
                         allowed_new: set[str], only_tests: bool = False, attempts: int = 2,
                         protected: set[str] | None = None, tools: bool = False,
                         base: list[dict] | None = None, convo: dict | None = None,
                         seed: list[EditBlock] | None = None):
        """Ask for edits, validate them, and feed errors back (bounded).

        With ``tools`` the model first works through its edit tools (see agent/toolcalls.py). A model without
        them — or one that answers in text anyway — uses SEARCH/REPLACE blocks, and JSON as the last resort.
        ``base`` is a conversation this step continues and ``seed`` the edits already staged in it; ``convo``
        receives the conversation afterwards ("messages", "tokens", "staged") so a later step can continue it.
        """
        if base:
            messages = [*base, {"role": "user", "content": prompt}]
        else:
            if state.get("project_notes"):  # the owner's conventions matter most when writing code
                prompt = prompts.project_notes_block(state["project_notes"]) + "\n\n" + prompt
            messages = [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": prompt}]
        usage = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        build = None
        errors: list[str] = []
        local_state = dict(state)
        local_state.setdefault("llm_calls", 0)
        pending = None  # a text reply that ended the tool loop, read by the text protocol below
        if tools:
            session = EditSession(workspace, get_index(workspace), planned=allowed, allowed_new=allowed_new,
                                  only_tests=only_tests, protected=protected, staged=seed,
                                  tests_later=step == "fix_patch")
            messages, pending = await self._tool_edits(state, local_state, step, messages, usage, session,
                                                       walk=step == "fix_patch" and not base)
            if session.staged:
                if convo is not None:
                    done = [{"role": "assistant", "content": pending.content}] if pending is not None else []
                    convo.update(messages=[*messages, *done], tokens=session.tokens, staged=list(session.staged))
                notes = [f"{path} was edited although the plan did not name it (the model found the change belonged "
                         "there)." for path in session.unplanned]
                return session.build(), usage, notes + session.problems
            if pending is None:
                return None, usage, session.problems or ["the model made no edits"]
        as_json = False
        for attempt in range(attempts):
            if as_json:
                # The last reply held no edits at all: often a sentence like "I'll make the edits" and nothing
                # else. Constrained JSON can't stop there.
                await self.log(state, "The model described the change instead of writing it; asking for the "
                                      "edits as structured output.", "warning")
                try:
                    edit_list, json_usage = await self.llm_json(local_state, step, "", LLMEditList, max_tokens=2400,
                                                                messages=messages)
                except LLMResponseError as exc:
                    return build, usage, [*errors, str(exc)[:300]]
                for k in ("llm_calls", "prompt_tokens", "completion_tokens"):
                    usage[k] += json_usage[k] - local_state.get(k, 0)
                local_state.update(json_usage)
                content = json.dumps(edit_list.model_dump())[:6000]
                blocks = [EditBlock(e.path, e.search, e.replace) for e in edit_list.edits]
                problems: list[str] = []
            else:
                if pending is not None:
                    result, pending = pending, None
                else:
                    result = await self.llm_text(local_state, step, messages, max_tokens=1800)
                    for k, v in (("llm_calls", 1), ("prompt_tokens", result.prompt_tokens),
                                 ("completion_tokens", result.completion_tokens)):
                        usage[k] += v
                    local_state["llm_calls"] = state.get("llm_calls", 0) + usage["llm_calls"]
                content = result.content
                blocks, problems = parse_edit_blocks(result.content)
            if not blocks:
                creatable = {a for a in allowed_new if not (workspace / a).exists()}
                blocks = parse_fenced_new_files(content, creatable)
                if blocks:
                    problems = []
            errors = list(problems)
            kept = []
            for b in blocks:
                try:
                    norm = normalize_relpath(b.path.strip().lstrip("./"))
                except PathSecurityError as exc:
                    errors.append(f"{b.path}: {exc}")
                    continue
                if norm not in allowed:
                    remapped = _remap_path(norm, allowed)
                    if remapped is None:
                        errors.append(f"{norm}: not one of the files you may edit ({', '.join(sorted(allowed))})")
                        continue
                    norm = remapped
                if only_tests and not is_test_path(norm):
                    errors.append(f"{norm}: only test files may be edited in this step")
                    continue
                b.path = norm
                kept.append(b)
            if not kept and not errors:
                errors.append("no SEARCH/REPLACE blocks found in the reply")
                as_json = not as_json  # next: the same edits as structured output
            if kept:
                build = await asyncio.to_thread(build_patch, workspace, kept, allowed_new_files=allowed_new)
                errors += build.errors
                for ch in [c for c in build.changes if c.path in (protected or set()) and c.deletions]:
                    errors.append(f"{ch.path}: existing tests must not be deleted or rewritten; add a new test "
                                  "instead")
                    build.changes.remove(ch)
                if not build.changes and build.warnings and not build.errors:
                    errors.append("the edit did not change anything (REPLACE is identical to the current code)")
                if build.ok and not errors:
                    return build, usage, []
                if build.ok and build.changes:
                    # Some blocks were invalid but others applied cleanly; keep the valid part.
                    return build, usage, errors
            if attempt + 1 < attempts and local_state["llm_calls"] < self.settings.max_llm_calls:
                await self.log(state, f"Edit validation failed ({'; '.join(errors)[:300]}); asking the model to "
                                      "correct it.", "warning")
                new_files = sorted(a for a in allowed_new if not (workspace / a).exists())
                create_hint = "".join(f"\nTo create {p}, the block must be exactly:\n{p}\n<<<<<<< SEARCH\n=======\n"
                                      "<the whole file content>\n>>>>>>> REPLACE" for p in new_files[:2])
                if as_json and not kept:
                    request = ("You described the change but did not write it. Give the edits now as JSON: "
                               '{"edits": [{"path": ..., "search": ..., "replace": ...}]} — search: the exact existing '
                               "lines (copied from the file contents shown, with indentation), replace: the new "
                               "lines; an empty search creates the file or appends to it.")
                else:
                    as_json = False
                    request = ("Those edits could not be applied:\n- " + "\n- ".join(errors[:8]) +
                               "\nReply again with corrected SEARCH/REPLACE blocks only. Copy SEARCH lines exactly "
                               "from the file contents shown." + create_hint)
                messages = messages + [{"role": "assistant", "content": content[:6000] or "(no reply)"},
                                       {"role": "user", "content": request}]
            else:
                break
        return build, usage, errors

    async def generate_patch(self, state: AgentState) -> dict:
        repair = state.get("patch_mode") == "repair"
        attempt_no = state.get("patch_attempts", 0) + 1
        label = "Generating repair patch" if repair else "Generating patch"
        if attempt_no > 1:
            label += f" (attempt {attempt_no})"
        await self.phase(state, "patch", label)
        workspace = Path(state["worktree_path"] or state["workspace_path"])
        index = get_index(workspace)
        index.invalidate()
        plan = state.get("plan") or {}
        fix_files = [f["path"] for f in plan.get("files_to_modify") or []]
        test_path = plan.get("test_file")
        usage_total = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        warnings: list[str] = []
        changes: list[FileChange] = []

        def add_usage(u: dict) -> None:
            for k in usage_total:
                usage_total[k] += u[k]

        plan_text = (f"Summary: {plan.get('summary', '')}\nApproach: {plan.get('approach', '')}\n" +
                     "\n".join(f"- {f['path']}: {f['change']}" for f in plan.get("files_to_modify") or []))
        if state.get("feedback"):
            plan_text += f"\n\nThe user rejected the previous patch with this feedback: {state['feedback']}"

        protected = self._protected_tests(state, index)
        tooled = await self._tools_ok()
        work: dict = {}  # the conversation of this step's last edits, for a diagnosis to continue
        diagnosed = state.get("diagnose_messages") or [] if tooled else []
        if repair and state.get("repair_target") in ("test", "test_fix") and test_path:
            report = state.get("report") or {}
            test_files = {test_path}
            base = None
            if state["repair_target"] == "test":  # it passed on the unfixed code: it does not reproduce the bug
                step = "test_repair_patch"
                prompt = prompts.TEST_REPAIR.format(
                    request=state["request"], root_cause=report.get("most_likely_root_cause") or "",
                    fix_diff=self._current_diff(state), original_code=self._original_code(state, index),
                    test_file=self._file_block(index, test_path), test_path=test_path,
                    runs=self._runs_text((state.get("before_runs") or []) + (state.get("last_runs") or []))[:4000],
                    framework=self._framework_label(state, test_path),
                    rules=prompts.edit_rules(test_path, tooled))
            else:  # the tests are wrong, not the change: the new test's expectation, or outdated existing tests
                step = "test_fix_patch"
                diag = state.get("diagnosis") or {}
                sources = [f for f in self._touched_files(state) if not is_test_path(f)] or fix_files
                outdated = [p for p in state.get("repair_tests") or [] if index.has_file(p) and p != test_path]
                prompt = prompts.TEST_FIX.format(
                    request=state["request"], root_cause=report.get("most_likely_root_cause") or "",
                    fix_diff="".join(c["diff"] for p in self._applied_patches(state) for c in p["changes"]
                                     if c["kind"] != "test")[:5000] or "(no code change)",
                    code_context=self._code_context(index, sources),
                    test_file="\n\n".join(self._file_block(index, p) for p in [test_path, *outdated]),
                    test_path=test_path, runs=self._runs_text(state.get("last_runs") or [])[:4000],
                    diagnosis=f"{diag.get('diagnosis', '')}\nNext step: {diag.get('next_step', '')}",
                    framework=self._framework_label(state, test_path),
                    rules=prompts.edit_rules(test_path, tooled))
                test_files |= set(outdated)
                protected = protected - set(outdated)  # these may change: their assertions are outdated
                follow_up = prompts.TEST_FIX_FOLLOW_UP.format(
                    test_path=test_path, framework=self._framework_label(state, test_path),
                    outdated="\n" + "\n\n".join(self._file_block(index, p) for p in outdated) if outdated else "none",
                    rules=prompts.edit_rules(test_path, tooled))
                if self._follow_up(diagnosed, state.get("diagnose_tokens", 0), follow_up, reserve=2400):
                    base, prompt = diagnosed, follow_up  # the test run and its diagnosis are already there
            build, usage, errors = await self._edit_loop({**state, **self._merge_usage(state, usage_total)},
                                                         step, prompt, workspace=workspace,
                                                         allowed=test_files, allowed_new={test_path},
                                                         only_tests=True, protected=protected, tools=tooled,
                                                         base=base, convo=work)
            add_usage(usage)
            if build is None or not build.changes:
                return await self._patch_failed(state, usage_total, errors)
            changes = build.changes
            warnings += build.warnings + errors
            warnings += [f"Updates the existing test {c.path}: the diagnosis is that the requested change makes its "
                         "assertions outdated. Review this change carefully." for c in changes if c.path != test_path]
        elif repair:
            touched = self._touched_files(state)
            allowed = set(touched) | set(fix_files) | ({test_path} if test_path else set())
            files_text = "\n\n".join(self._file_block(index, p) for p in sorted(allowed))
            diag = state.get("diagnosis") or {}
            prompt = prompts.REPAIR.format(runs=self._runs_text((state.get("user_runs") or [])
                                                                + (state.get("last_runs") or state.get("runs") or [])),
                                           diagnosis=f"{diag.get('diagnosis', '')}\nNext step: {diag.get('next_step', '')}",
                                           files=files_text, scope=prompts.edit_scope(sorted(allowed), tooled),
                                           how="with the edit tools" if tooled else "with SEARCH/REPLACE edits",
                                           rewrite=prompts.REWRITE if tooled else "",
                                           rules=prompts.edit_rules(sorted(allowed)[0], tooled))
            follow_up = prompts.REPAIR_FOLLOW_UP.format(scope=prompts.edit_scope(sorted(allowed), tooled),
                                                        rewrite=prompts.REWRITE,
                                                        rules=prompts.edit_rules(sorted(allowed)[0], tooled))
            base = None
            if self._follow_up(diagnosed, state.get("diagnose_tokens", 0), follow_up, reserve=2400):
                base, prompt = diagnosed, follow_up  # the test run and its diagnosis are already there
            build, usage, errors = await self._edit_loop({**state, **self._merge_usage(state, usage_total)},
                                                         "repair_patch", prompt, workspace=workspace, allowed=allowed,
                                                         allowed_new=set(), protected=protected, tools=tooled,
                                                         base=base, convo=work)
            add_usage(usage)
            if build is None or not build.changes:
                return await self._patch_failed(state, usage_total, errors)
            changes = build.changes
            warnings += build.warnings + errors
        else:
            tests_only = state.get("intent") == "add_tests"
            fix_diff = "(no code change in this step — the code under test is shown below)"
            fix_changes: list[FileChange] = []
            test_changes: list[FileChange] = []
            fix_work: dict = {}
            test_work: dict = {}
            new_sources: set[str] = set()
            if not tests_only:
                files_text = "\n\n".join(self._file_block(index, p) for p in fix_files)
                report = state.get("report") or {}
                prompt = prompts.EDIT_FIX.format(request=state["request"],
                                                 root_cause=report.get("most_likely_root_cause") or "(see plan)",
                                                 minimal_fix=report.get("minimal_fix") or plan.get("approach", ""),
                                                 plan=plan_text, files=files_text,
                                                 scope=prompts.edit_scope(fix_files, tooled),
                                                 rules=prompts.edit_rules(fix_files[0], tooled))
                new_sources = {p for p in fix_files if not index.has_file(p)}  # files the plan creates
                build, usage, errors = await self._edit_loop({**state, **self._merge_usage(state, usage_total)},
                                                             "fix_patch", prompt, workspace=workspace,
                                                             allowed=set(fix_files), allowed_new=new_sources,
                                                             tools=tooled, convo=fix_work)
                add_usage(usage)
                if build is None or not build.changes:
                    return await self._patch_failed(state, usage_total, errors)
                fix_changes = list(build.changes)
                warnings += build.warnings + errors
                fix_diff = build.diff[:5000]
            else:
                fix_diff += "\n\n" + self._code_context(index, fix_files)
            if tests_only and not test_path:
                return await self._patch_failed(state, usage_total, ["no test file could be determined"])
            if test_path:
                exists = index.has_file(test_path)
                example = self._example_test(index, test_path, fix_files)
                code_context = self._code_context(index, fix_files)
                report = state.get("report") or {}
                framework = self._framework_label(state, test_path)
                shared = {"request": state["request"], "code_context": code_context, "test_path": test_path,
                          "test_file_state": "(existing — append to it)" if exists else "(does not exist yet)",
                          "test_file": self._file_block(index, test_path) if exists
                          else new_test_file(test_path, tooled),
                          "example_test": example or "(no existing tests found)", "framework": framework,
                          "rules": prompts.edit_rules(test_path, tooled)}
                if tests_only:  # tests for code that is taken to be correct: they must pass as it is
                    test_prompt = prompts.ADD_TESTS.format(**shared)
                else:
                    test_prompt = prompts.EDIT_TEST.format(
                        **{**shared, "test_file_state": "(existing — append a new test)" if exists
                           else "(does not exist yet)"},
                        problem=report.get("problem_summary", ""),
                        root_cause=report.get("most_likely_root_cause") or "", fix_diff=fix_diff, spec="{spec}")
                spec_text = "(no specification)"
                if not tests_only and \
                        state.get("llm_calls", 0) + usage_total["llm_calls"] + 2 < self.settings.max_llm_calls:
                    spec_user = prompts.TEST_SPEC.format(
                        request=state["request"], root_cause=report.get("most_likely_root_cause") or "",
                        minimal_fix=report.get("minimal_fix") or plan.get("approach", ""), code_context=code_context)
                    try:
                        spec, spec_usage = await self.llm_json({**state, **self._merge_usage(state, usage_total)},
                                                               "test_spec", spec_user, LLMTestSpec, max_tokens=500)
                        add_usage({k: spec_usage[k] - self._merge_usage(state, usage_total)[k]
                                   for k in usage_total})
                        spec_text = (f"- Scenario: {spec.scenario}\n- Inputs: {spec.inputs}\n"
                                     f"- Correct behavior to assert: {spec.expected_correct_behavior}\n"
                                     f"- Current buggy behavior (must make the test fail): {spec.buggy_behavior}\n"
                                     f"- Assertion: {spec.assertion}")
                    except LLMResponseError:
                        pass
                test_prompt = test_prompt.replace("{spec}", spec_text)
                if tooled:
                    test_prompt += "\n" + prompts.TEST_TOOL_HINT
                if state.get("llm_calls", 0) + usage_total["llm_calls"] < self.settings.max_llm_calls:
                    tbuild, tusage, terrors = await self._edit_loop(
                        {**state, **self._merge_usage(state, usage_total)}, "test_patch", test_prompt,
                        workspace=workspace, allowed={test_path}, allowed_new={test_path}, only_tests=True,
                        protected=protected, tools=tooled, convo=test_work)
                    add_usage(tusage)
                    if tbuild is not None and tbuild.changes:
                        test_changes = list(tbuild.changes)
                        warnings += tbuild.warnings + terrors
                    elif tests_only:
                        return await self._patch_failed(state, usage_total, terrors)
                    else:
                        warnings.append("Could not generate a valid regression test: " + "; ".join(terrors)[:400])

            # A second look before the user sees it: does the change do what the request asks? A wrong fix and a
            # test written from the same misunderstanding would otherwise "verify" each other.
            if fix_changes and self.settings.check_changes and \
                    state.get("llm_calls", 0) + usage_total["llm_calls"] + 2 < self.settings.max_llm_calls:
                label_check = "Checking the change against your request"
                await self.phase(state, "patch", label_check)
                check = None
                try:
                    shown = "".join(c.diff for c in [*fix_changes, *test_changes])[:12000]
                    check, check_usage = await self.llm_json(
                        {**state, **self._merge_usage(state, usage_total)}, "check",
                        prompts.CHECK_PATCH.format(request=state["request"], diff=shown), LLMPatchCheck, max_tokens=1300)
                    add_usage({k: check_usage[k] - self._merge_usage(state, usage_total)[k] for k in usage_total})
                except LLMResponseError:
                    pass
                failed = [r for r in check.requirements if not r.holds] if check else []
                findings = "\n".join(f"- {r.requirement}: {r.why}" for r in failed) or \
                    (f"- {check.correction.strip()}" if check and check.correction.strip() else "")
                if check is None or check.verdict == "ok" or not findings:
                    await self.phase(state, "patch", label_check, "completed", "Nothing found" if check else "Skipped")
                else:
                    await self.phase(state, "patch", label_check, "completed", f"Found: {findings[:300]}")
                    revised = False

                    async def revise(step: str, job: str, held: dict, **scope):
                        """One more round in the conversation that made the edits, on top of them."""
                        if not (tooled and held.get("messages")
                                and self._follow_up(held["messages"], held.get("tokens", 0), job, reserve=2400)):
                            return None
                        rbuild, rusage, _errors = await self._edit_loop(
                            {**state, **self._merge_usage(state, usage_total)}, step, job, workspace=workspace,
                            tools=True, base=held["messages"], seed=held.get("staged"), convo=held, **scope)
                        add_usage(rusage)
                        return rbuild if rbuild is not None and rbuild.changes else None

                    if check.verdict in ("code_wrong", "both_wrong"):
                        rbuild = await revise("fix_patch", prompts.REVISE_FIX.format(findings=findings), fix_work,
                                              allowed=set(fix_files), allowed_new=new_sources)
                        if rbuild is not None and rbuild.diff != "".join(c.diff for c in fix_changes):
                            fix_changes, fix_diff, revised = list(rbuild.changes), rbuild.diff[:5000], True
                    # A test written for the first version of the fix is looked at again when the fix changed.
                    if (check.verdict in ("test_wrong", "both_wrong") or revised) and test_changes and test_path:
                        rbuild = await revise("test_patch",
                                              prompts.REVISE_TEST.format(findings=findings, fix_diff=fix_diff),
                                              test_work, allowed={test_path}, allowed_new={test_path},
                                              only_tests=True, protected=protected)
                        if rbuild is not None and rbuild.diff != "".join(c.diff for c in test_changes):
                            test_changes, revised = list(rbuild.changes), True
                    warnings.append(("LocPilot checked this change against your request, found a problem and revised "
                                     "it. What the check found:\n" if revised else
                                     "A check of this change against your request found a possible problem that was "
                                     "not resolved:\n") + findings[:600])
            changes = [*fix_changes, *test_changes]
            work = test_work or fix_work  # the test-writing conversation has the code, the fix and the test
        diff = "".join(c.diff for c in changes)
        summary = plan.get("summary", "") if not repair else (state.get("diagnosis") or {}).get("next_step", "Repair")
        if repair and state.get("repair_target") == "test":
            summary = "Rewrite the regression test so it reproduces the bug"
        elif repair and state.get("repair_target") == "test_fix":
            summary = "Correct the regression test (it failed on the fixed code)"
        patch = self.db.add_patch(state["task_id"], turn=state["turn"], attempt=attempt_no, summary=summary[:500],
                                  diff=diff, changes=[c.to_dict() for c in changes], warnings=warnings)
        public = {k: v for k, v in patch.items() if k != "changes"}
        await self.emit(state, "patch", {"patch": public})
        await self.phase(state, "patch", label, "completed",
                         f"{len(changes)} file(s): " + ", ".join(f"{c.path} (+{c.additions}/-{c.deletions})"
                                                                for c in changes))
        merged = self._merge_usage(state, usage_total)
        return {"patch_id": patch["id"], "patch_attempts": attempt_no, "patch_error": None, **merged,
                "work_messages": work.get("messages") or [], "work_tokens": work.get("tokens") or 0,
                "diagnose_messages": [], "diagnose_tokens": 0,
                "warnings": (state.get("warnings") or []) + warnings}

    @staticmethod
    def _merge_usage(state: AgentState, usage: dict) -> dict:
        return {"llm_calls": state.get("llm_calls", 0) + usage["llm_calls"],
                "prompt_tokens": state.get("prompt_tokens", 0) + usage["prompt_tokens"],
                "completion_tokens": state.get("completion_tokens", 0) + usage["completion_tokens"]}

    async def _patch_failed(self, state: AgentState, usage: dict, errors: list[str]) -> dict:
        message = "The model could not produce a valid patch: " + ("; ".join(errors)[:600] or "no edits returned")
        await self.phase(state, "patch", "Generating patch", "failed", message)
        return {"patch_id": None, "patch_error": message, **self._merge_usage(state, usage),
                "patch_attempts": state.get("patch_attempts", 0) + 1}

    def _example_test(self, index: RepoIndex, test_path: str, fix_files: list[str] | None = None) -> str:
        """A real existing test file (prefer one that exercises the code being fixed) as a style example."""
        ext = test_path.rsplit(".", 1)[-1]
        test_markers = ("def test", "test(", "it(", "func Test", "#[test]", "@Test")
        candidates = []
        for f in index.files:
            name = f.rsplit("/", 1)[-1]
            if not is_test_path(f) or not f.endswith("." + ext) or f == test_path or name in ("__init__.py",
                                                                                            "conftest.py"):
                continue
            text = index.read_text(f) or ""
            if not any(m in text for m in test_markers):
                continue
            stems = [x.rsplit("/", 1)[-1].rsplit(".", 1)[0] for x in fix_files or []]
            score = (2 if any(st and st in text for st in stems) else 0) + \
                    (1 if f.rsplit("/", 1)[0] == test_path.rsplit("/", 1)[0] else 0)
            candidates.append((-score, len(text), f))
        if not candidates:
            return ""
        pick = sorted(candidates)[0][2]
        lines = index.read_lines(pick) or []
        return f"### {pick}\n```{_lang_fence(pick)}\n" + redact("\n".join(lines[:80])) + "\n```"

    def _code_context(self, index: RepoIndex, files: list[str], max_files: int = 4) -> str:
        """Full code under test plus the local modules it imports (so tests use the real interfaces)."""
        symbols = get_symbol_index(index)
        ordered: list[str] = []
        for f in files:
            if f not in ordered:
                ordered.append(f)
            for spec, _line in symbols.imports_of(f):
                target = _resolve_import(index, f, spec)
                if target and target not in ordered and not is_test_path(target):
                    ordered.append(target)
        return "\n\n".join(self._file_block(index, p, max_lines=220) for p in ordered[:max_files])

    def _applied_patches(self, state: AgentState) -> list[dict]:
        """Patches currently applied in the worktree (rolled-back patches are excluded)."""
        return [p for p in self.db.list_patches(state["task_id"]) if p["status"] == "applied"]

    def _protected_tests(self, state: AgentState, index: RepoIndex) -> set[str]:
        """Test files that existed before LocPilot touched the task (append-only for the agent)."""
        created = {c["path"] for p in self.db.list_patches(state["task_id"]) for c in p["changes"]
                   if c["change"] == "create"}
        return {f for f in index.files if is_test_path(f)} - created

    def _original_code(self, state: AgentState, index: RepoIndex) -> str:
        blocks = []
        seen: set[str] = set()
        for patch in self._applied_patches(state):
            for c in patch["changes"]:
                if c["kind"] != "test" and c["path"] not in seen and c["before"] is not None:
                    seen.add(c["path"])
                    blocks.append(f"### {c['path']} (before the fix)\n```{_lang_fence(c['path'])}\n"
                                  f"{redact(c['before'])[:6000]}\n```")
        return "\n\n".join(blocks) or "(unavailable)"

    def _framework_label(self, state: AgentState, test_path: str) -> str:
        frameworks = (state.get("overview") or {}).get("test_frameworks") or []
        framework = next((f for f in frameworks if f in ("pytest", "unittest", "node:test", "jest", "vitest",
                                                          "go test", "cargo test")), None) or \
            (frameworks[0] if frameworks else "the existing test framework")
        if framework == "unittest" and test_path.endswith(".py"):
            framework = "unittest (unittest.TestCase classes, runnable with python -m unittest)"
        return framework

    def _touched_files(self, state: AgentState) -> list[str]:
        files: list[str] = []
        for patch in self._applied_patches(state):
            for c in patch["changes"]:
                if c["path"] not in files:
                    files.append(c["path"])
        return files

    def route_after_patch(self, state: AgentState) -> str:
        return "approval" if state.get("patch_id") else "finalize"

    async def approval(self, state: AgentState) -> dict:
        patch = self.db.get_patch(state["patch_id"])
        assert patch is not None
        # Idempotent: this node re-runs from the top when resumed after interrupt().
        approval = next((a for a in self._approvals_for_patch(state["task_id"], patch["id"])), None)
        if approval is None:
            plan = state.get("plan") or {}
            commands = state.get("proposed_commands") or plan.get("commands") or []
            sandbox = detect_sandbox()
            payload = {
                "title": ("Apply repair patch" if state.get("patch_mode") == "repair" else "Apply proposed patch")
                         + f" (attempt {patch['attempt']})",
                "plan_summary": plan.get("summary", ""), "diff": patch["diff"], "files": patch["files"],
                "commands": commands, "warnings": patch["warnings"],
                "sandbox": sandbox.description if sandbox.available else f"Not sandboxed: {sandbox.description}",
                "attempt": patch["attempt"],
            }
            approval = self.db.add_approval(state["task_id"], kind="patch", patch_id=patch["id"], payload=payload)
            await self.phase(state, "approval", "Waiting for your approval")
            await self.emit(state, "approval_required", {"approval": approval})
        decision = interrupt({"approval_id": approval["id"], "patch_id": patch["id"]})
        kind = (decision or {}).get("decision")
        if kind == "approve":
            proposed = [c["argv"] for c in (approval["payload"].get("commands") or [])]
            requested = decision.get("commands")
            chosen = proposed if requested is None else [c for c in requested if c in proposed]
            self.db.update_patch(patch["id"], status="approved", decided_at=now_iso())
            await self.phase(state, "approval", "Waiting for your approval", "completed",
                             f"Approved; {len(chosen)} verification command(s) approved")
            return {"approval_decision": "approved", "approved_commands": chosen, "feedback": None}
        self.db.update_patch(patch["id"], status="rejected", decided_at=now_iso())
        note = ((decision or {}).get("note") or "").strip()
        if kind == "cancel":
            await self.phase(state, "approval", "Waiting for your approval", "skipped", "Task cancelled")
            return {"approval_decision": "cancelled", "final_status": "cancelled"}
        await self.phase(state, "approval", "Waiting for your approval", "completed",
                         "Rejected" + (f" with feedback: {note[:200]}" if note else ""))
        return {"approval_decision": "rejected", "feedback": note or None}

    def _approvals_for_patch(self, task_id: str, patch_id: str) -> list[dict]:
        rows = self.db._all("SELECT id FROM approvals WHERE task_id=? AND patch_id=? ORDER BY created_at",
                            (task_id, patch_id))
        return [a for a in (self.db.get_approval(r["id"]) for r in rows) if a is not None]

    def route_after_approval(self, state: AgentState) -> str:
        decision = state.get("approval_decision")
        if decision == "approved":
            return "apply"
        if decision == "rejected" and state.get("feedback") and \
                state.get("patch_attempts", 0) < self.settings.max_patch_attempts + 1:
            return "generate_patch"
        return "finalize"

    async def apply(self, state: AgentState) -> dict:
        patch = self.db.get_patch(state["patch_id"])
        assert patch is not None
        workspace = Path(state["worktree_path"])
        changes = [FileChange.from_dict(c) for c in patch["changes"]]
        tests = [c for c in changes if c.kind == "test"]
        prior_fixes = [FileChange.from_dict(c) for p in self._applied_patches(state) if p["id"] != patch["id"]
                       for c in p["changes"] if c["kind"] != "test"]
        has_fix = any(c.kind != "test" for c in changes) or bool(prior_fixes)
        want_before = bool(tests and has_fix and state.get("approved_commands") and state.get("intent") != "add_tests"
                           and state.get("before_checked_for") != patch["id"])
        try:
            if want_before:
                label = "Applying the regression test to the unfixed code"
                await self.phase(state, "apply", label)

                def stage_tests() -> list[str]:
                    reverted, skipped = revert_changes(workspace, prior_fixes) if prior_fixes else ([], [])
                    if skipped:
                        raise UnexpectedChangeError(skipped[0]["path"])
                    apply_changes(workspace, tests)
                    return reverted

                reverted = await asyncio.to_thread(stage_tests)
                get_index(workspace).invalidate()
                detail = "Wrote " + ", ".join(c.path for c in tests)
                if reverted:
                    detail += "; temporarily reverted the fix in " + ", ".join(reverted)
                await self.phase(state, "apply", label, "completed",
                                 detail + " — running the tests to confirm the new test reproduces the bug")
                return {"apply_stage": "tests", "verify_stage": "before_fix", "before_checked_for": patch["id"]}
            await self.phase(state, "apply", "Applying the patch to the isolated worktree")

            def apply_all() -> list[str]:
                apply_changes(workspace, prior_fixes)  # restores a temporarily reverted fix; no-op otherwise
                return apply_changes(workspace, changes)

            await asyncio.to_thread(apply_all)
        except UnexpectedChangeError as exc:
            self.db.update_patch(patch["id"], status="failed")
            await self.phase(state, "apply", "Applying the patch", "failed", str(exc))
            return {"unresolved": (state.get("unresolved") or []) + [str(exc)], "apply_stage": "failed"}
        get_index(workspace).invalidate()
        self.db.update_patch(patch["id"], status="applied", applied_at=now_iso())
        applied = list(state.get("applied_patch_ids") or [])
        if patch["id"] not in applied:
            applied.append(patch["id"])
        await self.phase(state, "apply", "Applying the patch to the isolated worktree", "completed",
                         f"Modified {len(changes)} file(s): " + ", ".join(c.path for c in changes))
        return {"apply_stage": "all", "verify_stage": "after_fix", "applied_patch_ids": applied}

    def route_after_apply(self, state: AgentState) -> str:
        if state.get("apply_stage") == "failed":
            return "finalize"
        if not state.get("approved_commands"):
            return "evaluate"
        return "verify"

    async def verify(self, state: AgentState) -> dict:
        stage = state.get("verify_stage") or "after_fix"
        if state.get("repair_attempts", 0) and stage == "after_fix":
            stage = f"retry_{state['repair_attempts']}"
        label = {"before_fix": "Running tests before the fix (expecting the new test to fail)"}.get(
            stage, "Running verification commands")
        await self.phase(state, "verify", label)
        runs = []
        for argv in state.get("approved_commands") or []:
            try:
                row = await run_verification_command(
                    settings=self.settings, db=self.db, emit=lambda t, d, persist=True: self.emit(state, t, d, persist),
                    task_id=state["task_id"], turn=state["turn"], stage=stage, argv=argv,
                    workspace=Path(state["worktree_path"]), origin_repo=Path(state["repo_path"]))
            except CommandPolicyError as exc:
                await self.log(state, f"Command blocked by policy: {exc}", "error")
                continue
            runs.append(run_entry(row))
        outcomes = ", ".join(f"{(r['summary'] or {}).get('outcome', 'unknown')} (exit {r['exit_code']})" for r in runs)
        await self.phase(state, "verify", label, "completed", outcomes or "no commands ran")
        if stage == "before_fix":
            return {"before_runs": runs}
        # A fresh verification of the current code supersedes what the user observed before it.
        return {"runs": (state.get("runs") or []) + runs, "last_runs": runs, "user_runs": []}

    def route_after_verify(self, state: AgentState) -> str:
        return "apply" if state.get("verify_stage") == "before_fix" and state.get("apply_stage") == "tests" else "evaluate"

    async def evaluate(self, state: AgentState) -> dict:
        turn_patches = [p for p in self.db.list_patches(state["task_id"])
                        if p["turn"] == state["turn"] and p["status"] == "applied"]
        new_tests = list(dict.fromkeys(c["path"] for p in turn_patches for c in p["changes"] if c["kind"] == "test"))
        test_names = declared_test_names([
            "\n".join(line[1:] for line in c["diff"].splitlines() if line.startswith("+") and not line.startswith("+++"))
            for p in turn_patches for c in p["changes"] if c["kind"] == "test"])
        runs = state.get("runs") or []
        last = [r for r in runs if r["stage"] == runs[-1]["stage"]] if runs else []
        verification = compute_verification(state.get("before_runs") or [], last, new_tests,
                                            commands_approved=bool(state.get("approved_commands")),
                                            new_test_names=test_names)
        signature = failure_signature(last)
        stalled = verification["verdict"] in ("failed", "partial") and \
            signature in (state.get("failure_signatures") or [])
        await self.emit(state, "log", {"level": "info" if verification["verdict"] == "passed" else "warning",
                                       "text": f"Verification verdict: {verification['verdict']} — "
                                               f"{verification['regression_test']['detail']}"})
        # A new test that passes on the unfixed code does not reproduce the bug (whatever happened after the fix).
        test_repair = (state.get("intent") != "add_tests"
                       and verification["regression_test"]["status"] == "did_not_fail_before_fix")
        return {"verification": verification, "stalled": stalled, "test_repair_needed": test_repair,
                "failure_signatures": (state.get("failure_signatures") or []) + ([signature] if signature else [])}

    def route_after_evaluate(self, state: AgentState) -> str:
        v = state.get("verification") or {}
        # Tests that were failing before and still fail may be the very thing the user asked to fix ("the suite is
        # red"): worth one look at whether they are, instead of filing them under "already broken".
        red_suite = (v.get("verdict") == "partial" and v.get("still_failing") and state.get("intent") != "add_tests"
                     and (state.get("diagnosis") or {}).get("cause") != "preexisting_failure")
        if v.get("verdict") != "failed" and not state.get("test_repair_needed") and not red_suite:
            return "finalize"
        if state.get("stalled"):
            return "finalize"
        if state.get("repair_attempts", 0) >= self.settings.max_retries:
            return "finalize"
        if state.get("llm_calls", 0) + 3 > self.settings.max_llm_calls:
            return "finalize"
        return "diagnose"

    def _runs_text(self, runs: list[dict]) -> str:
        if not runs:
            return "(no command output available)"
        parts = []
        for r in runs:
            s = r.get("summary") or {}
            by_user = r.get("stage") == USER_STAGE
            header = f"$ {' '.join(r['argv'])}" + ("   (run by the user)" if by_user else "")
            if by_user and s.get("framework") in (None, "unknown"):  # not a test run: the exit code says it all
                stats = f"exit code: {r['exit_code']} | status: {r['status']}"
            else:
                stats = (f"exit code: {r['exit_code']} | outcome: {s.get('outcome')} | "
                         f"passed={s.get('passed')} failed={s.get('failed')} errors={s.get('errors')}\n"
                         f"failing tests: {', '.join(s.get('failing_tests') or []) or 'n/a'}")
            parts.append(f"{header}\n{stats}\noutput:\n{r.get('output') or ''}")
        return "\n\n".join(parts)[:7000]

    @staticmethod
    def _observed_runs(state: AgentState) -> list[dict]:
        """What is known about the current code from execution: the user's own runs first, then LocPilot's."""
        return (state.get("user_runs") or []) + (state.get("last_runs") or [])

    def _current_diff(self, state: AgentState) -> str:
        return "".join(p["diff"] for p in self._applied_patches(state))[:7000] or "(no changes applied)"

    async def diagnose(self, state: AgentState) -> dict:
        if state.get("test_repair_needed"):
            label = "Checking the regression test"
            await self.phase(state, "diagnose", label)
            diag = {"diagnosis": "The new regression test passed on the code before the fix, so it does not reproduce "
                                 "the reported bug (established by execution).",
                    "cause": "test_incorrect", "should_retry": True,
                    "next_step": "Rewrite the regression test so it fails without the fix and passes with it."}
            await self.phase(state, "diagnose", label, "completed", diag["diagnosis"] + " Rewriting the test.")
            return {"diagnosis": diag, "retry_planned": True, "repair_target": "test", "patch_mode": "repair",
                    "repair_attempts": state.get("repair_attempts", 0) + 1, "test_repair_needed": False,
                    "feedback": None}
        await self.phase(state, "diagnose", "Inspecting the failure")
        runs = self._observed_runs(state)
        # When the failures can be attributed — LocPilot's own new test, or existing tests that were passing — the
        # question is which side is wrong, and the user's request decides. (Asked openly, a small model tends to
        # "restore" a correct fix, or to rewrite working code, to satisfy its own wrong test.)
        v = state.get("verification") or {}
        new_failing, existing_failing = v.get("new_test_failures") or [], v.get("existing_failures") or []
        still_failing = v.get("still_failing") or []
        attributed = bool(new_failing or existing_failing or still_failing)
        runs_text = self._runs_text(runs) + (f"\n\nUser message: {state['request']}"
                                             if state.get("intent") == "retry_fix" else "")
        if attributed:
            job = prompts.DIAGNOSE_TESTS.format(
                runs=runs_text, diff=self._current_diff(state), request=state["request"],
                new_failing="; ".join(new_failing[:6]) or "passes",
                existing_failing="; ".join(existing_failing[:8]) or "all pass",
                still_failing="; ".join(still_failing[:8]) or "none")
        else:
            job = prompts.DIAGNOSE.format(runs=runs_text, diff=self._current_diff(state))
        job += prompts.pitfalls([*self._touched_files(state), *(i["path"] for i in state.get("evidence") or [])])
        # With the failing tests named, the model traces the failing assertion with real values before it judges:
        # asked for a verdict straight away it defends the theory its change was built on.
        answer, budget = (LLMTracedFailure, 1200) if attributed else (LLMFailureAnalysis, 600)
        # Where the model wrote the test (or the fix) it can see its own work and the code: diagnose there.
        results: list = []
        fa = None
        messages = self._follow_up(state.get("work_messages") or [], state.get("work_tokens", 0), job, reserve=600) \
            if attributed and await self._tools_ok() else None
        if messages is not None:
            try:
                fa, usage = await self.llm_json(state, "diagnose", "", answer, max_tokens=budget,
                                                messages=messages, keep=results, tools=EDIT_TOOLS)
            except LLMResponseError:
                results = []  # ask again the plain way
        if fa is None:
            user = self._context(state, evidence_lines=30, with_user_runs=False) + "\n\n" + job
            fa, usage = await self.llm_json(state, "diagnose", user, answer, max_tokens=budget)
        env_cause = environment_problem(runs)
        diag = fa.model_dump()
        if env_cause:
            diag.update(cause="environment", should_retry=False,
                        diagnosis=f"{env_cause} (detected in the command output). {fa.diagnosis}")
        # The model classifies the failure; the retry decision itself is deterministic and bounded.
        retry = diag["cause"] in ("fix_incorrect", "test_incorrect", "unknown") and \
            state.get("repair_attempts", 0) < self.settings.max_retries
        detail = f"{diag['cause'].replace('_', ' ')}: {diag['diagnosis'][:300]}"
        await self.phase(state, "diagnose", "Inspecting the failure", "completed",
                         detail + ("" if retry else " — not retrying"))
        target, outdated = "code", []
        if attributed and diag["cause"] == "test_incorrect":  # the tests change, the code stays
            target = "test_fix"
            index = await asyncio.to_thread(get_index, Path(state["worktree_path"] or state["workspace_path"]))
            outdated = test_files_in(existing_failing, index.files)
        updates = {"diagnosis": diag, "retry_planned": retry, "repair_target": target, "repair_tests": outdated,
                   "diagnose_messages": [], "diagnose_tokens": 0, **usage}
        if len(results) == 1 and messages is not None:  # the repair continues from here
            updates.update(diagnose_messages=[*messages, {"role": "assistant", "content": results[0].content}],
                           diagnose_tokens=results[0].prompt_tokens + results[0].completion_tokens)
        if retry:
            updates.update(repair_attempts=state.get("repair_attempts", 0) + 1, patch_mode="repair", feedback=None)
        else:
            updates["unresolved"] = (state.get("unresolved") or []) + [f"Verification failure: {diag['diagnosis'][:500]}"]
        return updates

    def route_after_diagnose(self, state: AgentState) -> str:
        return "generate_patch" if state.get("retry_planned") else "finalize"

    async def review(self, state: AgentState) -> dict:
        await self.phase(state, "review", "Reviewing LocPilot's changes")
        task = ("Explain the changes LocPilot made, why they fix the problem, and how they were verified."
                if state["intent"] == "explain_changes" else
                "Review the changes for correctness, possible side effects, edge cases and missing tests.")
        observed = self._observed_runs(state)
        verification = self._runs_text(observed) if observed else \
            "No verification commands have been run for these changes."
        plan = state.get("plan") or {}
        user = self._context(state, evidence_lines=25, with_user_runs=False) + "\n\n" + prompts.REVIEW.format(
            request=state["request"], plan=f"{plan.get('summary', '')}\n{plan.get('approach', '')}",
            diff=self._current_diff(state), verification=verification[:3500], task=task)
        review, usage = await self.llm_json(state, "review", user, LLMChangeReview, max_tokens=1400)
        index = await asyncio.to_thread(self.index, state)
        resolver = CitationResolver(index, state.get("evidence") or [])
        out = review.model_dump()
        out["answer_markdown"] = resolver.linkify(out.pop("answer"))
        touched = set(self._touched_files(state))
        out["file_notes"] = [n for n in out["file_notes"] if n["path"] in touched]
        await self.phase(state, "review", "Reviewing LocPilot's changes", "completed")
        return {"review": out, **usage}

    async def finalize(self, state: AgentState) -> dict:
        await self.phase(state, "report", "Writing the final report")
        report = build_final_report(state, self.db)
        self.db.save_report(state["task_id"], state["turn"], report)
        message = final_message(report)
        msg = self.db.add_message(state["task_id"], turn=state["turn"], role="assistant", content=message,
                                  mode=state["mode"])
        await self.emit(state, "message", msg)
        await self.emit(state, "final", {"report": report})
        await self.phase(state, "report", "Writing the final report", "completed", report["headline"])
        history = list(state.get("history") or []) + [
            {"turn": state["turn"], "request": state["request"][:300], "headline": report["headline"][:300]}]
        task_status = {"needs_input": "needs_input", "cancelled": "cancelled", "failed": "failed"}.get(
            report["status"], "completed")
        stats = self.db.get_task(state["task_id"])["stats"] or {}
        for key in ("llm_calls", "prompt_tokens", "completion_tokens", "tool_calls"):
            stats[key] = stats.get(key, 0) + report["stats"].get(key, 0)
        stats["retries"] = stats.get("retries", 0) + report["retries"]
        self.db.update_task(state["task_id"], stats=stats)
        return {"history": history[-12:], "task_status": task_status, "final_status": report["status"]}


# --- Verification analysis ----------------------------------------------------------------

_TEST_NAME_RE = re.compile(r"""(?:\b(?:test|it)\(\s*['"`](.+?)['"`]|\bdef (test_\w+)|\bfunc (Test\w+)|\bfn (\w+)\s*\()""")


def declared_test_names(sources: list[str]) -> list[str]:
    """Test names declared in new/changed test files (to match runner output that has no file paths)."""
    names: list[str] = []
    for src in sources:
        for m in _TEST_NAME_RE.finditer(src or ""):
            name = next((g for g in m.groups() if g), None)
            if name and name not in names:
                names.append(name)
    return names


def _test_matches(test_id: str, paths: list[str], names: list[str] | None = None) -> bool:
    for name in names or []:
        if test_id == name or test_id.endswith("." + name) or test_id.endswith("::" + name) or \
                test_id.startswith(name + " ") or test_id == name.strip():
            return True
    for p in paths:
        stem = p.rsplit(".", 1)[0]
        dotted = stem.replace("/", ".")
        name = stem.rsplit("/", 1)[-1]
        if p in test_id or dotted in test_id or f"{name}." in test_id or f"{name}::" in test_id:
            return True
    return False


def compute_verification(before: list[dict], after: list[dict], new_test_paths: list[str], *,
                         commands_approved: bool, new_test_names: list[str] | None = None) -> dict:
    runs = [{"command_id": r["command_id"], "stage": r["stage"], "argv": r["argv"], "exit_code": r["exit_code"],
             "summary": r["summary"]} for r in [*before, *after]]
    result: dict = {"tests_run": bool(after), "verdict": "not_run", "runs": runs,
                    "regression_test": {"status": "not_created" if not new_test_paths else "unknown",
                                        "detail": "No regression test was added." if not new_test_paths else ""},
                    "preexisting_failures": [], "new_failures": []}
    if not after:
        result["regression_test"]["detail"] = (result["regression_test"]["detail"] or "") + (
            " Tests were not run because no verification command was approved." if not commands_approved
            else " Tests were not run.")
        result["regression_test"]["detail"] = result["regression_test"]["detail"].strip()
        return result
    outcomes = [(r["summary"] or {}).get("outcome", "unknown") for r in after]
    failing_after = sorted({t for r in after for t in ((r["summary"] or {}).get("failing_tests") or [])})
    failing_before = sorted({t for r in before for t in ((r["summary"] or {}).get("failing_tests") or [])})
    before_outcomes = [(r["summary"] or {}).get("outcome", "unknown") for r in before]

    if all(o == "passed" for o in outcomes):
        verdict = "passed"
    elif any(o in ("failed", "error") for o in outcomes):
        verdict = "failed"
    else:
        verdict = "partial"
    new_in_before = [t for t in failing_before if _test_matches(t, new_test_paths, new_test_names)]
    preexisting = [t for t in failing_before if t not in new_in_before]
    new_failures = [t for t in failing_after if t not in failing_before]
    result["preexisting_failures"] = preexisting
    result["new_failures"] = new_failures
    # Existing tests that were failing (the suite was already red) and pass with the change.
    fixed_existing = [t for t in preexisting if t not in failing_after]
    failed_before = sum(((r["summary"] or {}).get("failed") or 0) for r in before) - (1 if new_in_before else 0)
    result["fixed_tests"] = fixed_existing
    result["still_failing"] = [t for t in preexisting if t in failing_after]

    reg = result["regression_test"]
    if new_test_paths:
        new_failing_after = [t for t in failing_after if _test_matches(t, new_test_paths, new_test_names)]
        if not before:
            reg.update(status="unknown",
                       detail="The regression test and fix were applied together, so it is unknown whether the test "
                              "fails without the fix.")
        elif new_in_before and not new_failing_after:
            reg.update(status="detected_bug", detail=f"The new test failed before the fix ({', '.join(new_in_before[:3])}) "
                                                     "and passes after it — confirmed by execution.")
        elif before_outcomes and all(o == "passed" for o in before_outcomes):
            reg.update(status="did_not_fail_before_fix",
                       detail="The new test already passed on the unfixed code, so it does not reproduce the bug.")
        elif fixed_existing and verdict == "passed":
            # The project's own tests prove the change, whatever LocPilot's new test does.
            reg.update(status="fixed_failing_tests",
                       detail=f"{failed_before or len(fixed_existing)} existing test(s) that failed before the change "
                              f"pass after it ({', '.join(fixed_existing[:3])}) — confirmed by execution."
                              + ("" if new_in_before else " LocPilot's own new test did not fail before the change, "
                                                          "so it adds no proof."))
        elif any(o in ("failed", "error") for o in before_outcomes) and not failing_before and verdict == "passed":
            reg.update(status="detected_bug",
                       detail="Tests failed before the fix and pass after it (per-test names were not available; "
                              "inferred from the runner summary).")
        elif new_failing_after:
            reg.update(status="unknown", detail="The new test still fails after the fix.")
        else:
            reg.update(status="unknown", detail="Could not attribute failures to the new test from the output.")
    if verdict == "failed" and failing_after and all(t in preexisting for t in failing_after):
        verdict = "partial"
    result["verdict"] = verdict
    # Whose failures these are (runners name a test with or without its file): the test LocPilot just wrote, or
    # tests of the project that were passing — failures that existed before the change are neither.
    mine = [t for t in failing_after if new_test_paths and (
        _test_matches(t, new_test_paths, new_test_names)
        or any(len(n) >= 8 and n in t for n in new_test_names or []))]
    result["new_test_failures"] = mine
    result["existing_failures"] = [t for t in failing_after if t not in mine and t not in preexisting]
    result["only_new_test_fails"] = bool(mine) and len(mine) == len(failing_after)
    return result


def failure_signature(runs: list[dict]) -> str:
    if not runs:
        return ""
    parts = []
    for r in runs:
        s = r.get("summary") or {}
        if s.get("outcome") == "passed":
            continue
        out = re.sub(r"\d+(\.\d+)?s\b|0x[0-9a-f]+|line \d+", "", r.get("output") or "")
        errors = re.findall(r"(?m)^(?:E\s+)?(\w*(?:Error|Exception)\b.*)$", out)[:3]
        parts.append(json.dumps([sorted(s.get("failing_tests") or []), errors, r.get("exit_code")]))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16] if parts else ""


_ENV_PATTERNS = [
    (r"No module named (pytest|unittest)", "The test runner is not installed for this interpreter"),
    (r"ModuleNotFoundError: No module named '([\w.]+)'", "A Python dependency is missing"),
    (r"command not found|No such file or directory: '(npm|node|go|cargo)'", "A required tool is not installed"),
    (r"Cannot find module '([^']+)'", "A Node.js dependency is missing (node_modules not installed?)"),
    (r"nodename nor servname|Temporary failure in name resolution|getaddrinfo ENOTFOUND", "Network access is blocked"),
    (r"Operation not permitted", "The sandbox blocked an operation"),
]


def environment_problem(runs: list[dict]) -> str | None:
    text = "\n".join(r.get("output") or "" for r in runs)
    for pattern, label in _ENV_PATTERNS:
        m = re.search(pattern, text)
        if m:
            if "ModuleNotFoundError" in pattern:
                # Missing *project* modules can be a real bug; third-party modules are environment issues.
                module = m.group(1).split(".")[0]
                if module in {"tests", "test", "src", "app"}:
                    continue
            return label
    return None


# --- Final report ---------------------------------------------------------------------------

def build_final_report(state: AgentState, db: Database) -> dict:
    mode, intent = state["mode"], state.get("intent") or "question"
    report = state.get("report") if state.get("report_turn") == state["turn"] else None
    plan = state.get("plan") if state.get("patch_id") or state.get("patch_error") else None
    patches = [p for p in db.list_patches(state["task_id"]) if p["turn"] == state["turn"]]
    verification = state.get("verification")
    unresolved = list(state.get("unresolved") or [])
    next_steps: list[str] = []
    review = state.get("review")
    decision = state.get("approval_decision")

    if state.get("final_status") == "cancelled":
        status, headline = "cancelled", "Cancelled before the change was applied."
    elif state.get("final_status") == "failed":
        status, headline = "failed", unresolved[0] if unresolved else "The task could not be completed."
    elif state.get("clarifying_question"):
        status, headline = "needs_input", state["clarifying_question"]
    elif review is not None:
        status = "answered"
        headline = "Reviewed LocPilot's changes." if intent == "review_changes" else "Explained LocPilot's changes."
    elif state.get("patch_error") and verification is None:
        status, headline = "no_changes", state["patch_error"]
        unresolved.append(state["patch_error"])
        next_steps.append("Rephrase the request with the exact file or function to change, or make the edit manually "
                          "using the root-cause analysis above.")
    elif decision == "rejected" and verification is None:
        status, headline = "changes_rejected", "You rejected the proposed patch; no files were changed."
        next_steps.append("Reply with what to change about the approach and LocPilot will propose a new patch.")
    elif verification is not None:
        if state.get("patch_error"):
            unresolved.append("A later repair attempt failed: " + state["patch_error"])
        verdict = verification["verdict"]
        n_pass = sum(((r.get("summary") or {}).get("passed") or 0) for r in verification["runs"]
                     if r["stage"] != "before_fix")
        reg_status = verification["regression_test"]["status"]
        passed_note = f" ({n_pass} tests passed)" if n_pass else ""
        if verdict == "passed" and reg_status == "fixed_failing_tests" and intent != "add_tests":
            status = "changes_verified"
            headline = ("Verified by execution: tests of this project that failed before the change pass after "
                        f"it{passed_note}.")
            next_steps.append("Review the diff in the Changes panel, then download the .patch and apply it to your "
                              "branch (git apply) — LocPilot never commits or pushes.")
        elif verdict == "passed" and (reg_status == "detected_bug" or intent == "add_tests"):
            status = "changes_verified"
            headline = ("Verified by execution: LocPilot's regression test failed before the change and passes after "
                        f"it{passed_note}. Review the test — it encodes LocPilot's reading of the expected behavior."
                        if intent != "add_tests" else f"New tests added and passing{passed_note}.")
            next_steps.append("Review the diff in the Changes panel, then download the .patch and apply it to your "
                              "branch (git apply) — LocPilot never commits or pushes.")
        elif verdict == "passed":
            status = "changes_unverified"
            reason = {"not_created": "no regression test reproduces the bug",
                      "did_not_fail_before_fix": "the new test already passed before the fix, so it does not "
                                                 "reproduce the bug"}.get(reg_status, "the regression check was "
                                                                                       "inconclusive")
            headline = f"Patch applied and existing tests pass{passed_note}, but the fix is NOT proven: {reason}."
            unresolved.append(f"The fix is unproven because {reason}.")
            next_steps.append("Ask LocPilot to add a regression test for this bug, or verify the behavior manually.")
        elif verdict == "not_run":
            status = "changes_unverified"
            headline = "Patch applied in the isolated worktree; tests were NOT run, so the fix is unverified."
            next_steps.append("Run the project's tests yourself or approve a verification command next time.")
        elif verdict == "partial":
            status = "changes_unverified"
            headline = "Patch applied; only failures that already existed before the change remain."
            unresolved.append("Pre-existing failing tests: " + ", ".join(verification["preexisting_failures"][:8]))
        else:
            status = "changes_failed"
            headline = "Patch applied in the isolated worktree, but verification failed."
            if state.get("stalled"):
                unresolved.append("Stopped retrying: the same failure repeated after a repair attempt.")
            elif not unresolved:
                unresolved.append(f"Verification still fails after {state.get('repair_attempts', 0)} repair "
                                  "attempt(s).")
            next_steps.append("Ask LocPilot to investigate the failure and try again, or roll back the changes.")
        if verification["regression_test"]["status"] == "did_not_fail_before_fix":
            unresolved.append("The generated regression test passed on the unfixed code, so it does not prove the fix.")
    elif report is not None:
        status = "answered"
        if report.get("kind") == "rca":
            if report.get("most_likely_root_cause") and report.get("evidence_sufficient"):
                headline = "Most likely root cause: " + report["most_likely_root_cause"]
            else:
                headline = "Evidence is insufficient to confirm a root cause."
            if mode == "ask":
                next_steps.append("Switch to Code mode and ask LocPilot to fix it with the smallest possible change.")
            elif intent == "root_cause":
                next_steps.append("Ask LocPilot to fix the bug; it will propose a patch for your approval.")
        else:
            headline = report.get("problem_summary") or "Answered from the repository's code."
        if mode == "ask" and intent in ("fix", "add_tests"):
            next_steps.insert(0, "Ask mode is read-only. Switch to Code mode to have LocPilot change files.")
    else:
        status, headline = "failed", "No answer could be produced."
    duration = int((time.time() - (state.get("turn_started") or time.time())) * 1000)
    plan_out = None
    if plan:
        plan_out = {k: plan.get(k) for k in ("summary", "approach", "files_to_modify", "test_file", "test_description",
                                             "risks", "commands")}
    return {
        "task_id": state["task_id"], "turn": state["turn"], "mode": mode, "intent": intent, "status": status,
        "headline": headline[:400], "investigation": report, "plan": plan_out,
        "patches": [{"id": p["id"], "attempt": p["attempt"], "status": p["status"], "files": p["files"]}
                    for p in patches],
        "verification": verification, "change_review": review,
        "clarifying_question": state.get("clarifying_question"), "retries": state.get("repair_attempts", 0),
        "unresolved_issues": list(dict.fromkeys(unresolved + ([] if report is None else
                                                               (report.get("uncertainties") or [])[:3]))),
        "next_steps": next_steps, "warnings": state.get("warnings") or [],
        "stats": {"llm_calls": state.get("llm_calls", 0), "prompt_tokens": state.get("prompt_tokens", 0),
                  "completion_tokens": state.get("completion_tokens", 0), "tool_calls": state.get("tool_calls", 0),
                  "duration_ms": duration},
    }


def final_message(report: dict) -> str:
    parts = [f"**{report['headline']}**"]
    inv = report.get("investigation")
    if report.get("change_review"):
        parts.append(report["change_review"].get("answer_markdown", ""))
    elif inv and report["status"] == "answered":
        parts.append(inv.get("answer_markdown", ""))
    v = report.get("verification")
    if v:
        parts.append(f"Verification: **{v['verdict']}** — {v['regression_test']['detail']}")
    if report.get("unresolved_issues") and report["status"] != "answered":
        parts.append("Unresolved: " + "; ".join(report["unresolved_issues"][:3]))
    return "\n\n".join(p for p in parts if p)


def build_graph(deps: AgentDeps) -> StateGraph:
    g = LocPilotGraph(deps)
    graph = StateGraph(AgentState)
    for name in ("start_turn", "understand", "inspect", "seed_evidence", "plan_evidence", "analyze", "validate",
                 "clarify", "plan", "generate_patch", "approval", "apply", "verify", "evaluate", "diagnose", "review",
                 "finalize"):
        graph.add_node(name, getattr(g, name))
    graph.add_edge(START, "start_turn")
    graph.add_conditional_edges("start_turn", g.route_after_start, ["understand", "finalize"])
    graph.add_conditional_edges("understand", g.route_after_understand,
                                ["review", "diagnose", "plan", "clarify", "inspect"])
    graph.add_edge("inspect", "seed_evidence")
    graph.add_conditional_edges("seed_evidence", g.route_after_seed, ["plan_evidence", "analyze"])
    graph.add_conditional_edges("plan_evidence", g.route_after_plan_evidence, ["plan_evidence", "analyze"])
    graph.add_edge("analyze", "validate")
    graph.add_conditional_edges("validate", g.route_after_validate, ["plan", "finalize"])
    graph.add_edge("clarify", "finalize")
    graph.add_conditional_edges("plan", g.route_after_plan, ["generate_patch", "finalize"])
    graph.add_conditional_edges("generate_patch", g.route_after_patch, ["approval", "finalize"])
    graph.add_conditional_edges("approval", g.route_after_approval, ["apply", "generate_patch", "finalize"])
    graph.add_conditional_edges("apply", g.route_after_apply, ["verify", "evaluate", "finalize"])
    graph.add_conditional_edges("verify", g.route_after_verify, ["apply", "evaluate"])
    graph.add_conditional_edges("evaluate", g.route_after_evaluate, ["diagnose", "finalize"])
    graph.add_conditional_edges("diagnose", g.route_after_diagnose, ["generate_patch", "finalize"])
    graph.add_edge("review", "finalize")
    graph.add_edge("finalize", END)
    return graph
