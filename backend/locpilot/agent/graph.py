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
    LLMExplanation,
    LLMFailureAnalysis,
    LLMPlan,
    LLMTestSpec,
    TaskUnderstanding,
)
from locpilot.agent.workspace import WorkspaceError, prepare_worktree, run_verification_command
from locpilot.config import Settings
from locpilot.llm.ollama import LLMClient, LLMResponseError, chat_json
from locpilot.security.commands import CommandPolicyError, validate_command
from locpilot.security.paths import PathSecurityError, normalize_relpath
from locpilot.security.sandbox import detect_sandbox
from locpilot.security.secrets import redact
from locpilot.storage.db import Database, now_iso
from locpilot.tools import git
from locpilot.tools.files import RepoIndex, get_index, is_test_path, language_for
from locpilot.tools.manifests import inspect_project
from locpilot.tools.patch import (
    FileChange,
    UnexpectedChangeError,
    apply_changes,
    build_patch,
    parse_edit_blocks,
    parse_fenced_new_files,
    revert_changes,
)
from locpilot.tools.symbols import get_symbol_index

# Keys that survive from one turn to the next.
PERSISTENT_KEYS = ("task_id", "repo_id", "repo_path", "worktree_path", "base_commit", "history", "report",
                   "plan", "evidence", "notes", "overview", "approved_commands", "last_runs", "applied_patch_ids",
                   "proposed_commands")


class AgentState(TypedDict, total=False):
    task_id: str
    repo_id: str
    repo_path: str
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
    applied_patch_ids: list[str]
    verification: dict | None
    failure_signatures: list[str]
    stalled: bool
    repair_attempts: int
    diagnosis: dict | None
    retry_planned: bool
    repair_target: str
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


def turn_input(carry: dict, *, task_id: str, repo: dict, turn: int, mode: str, request: str) -> dict:
    """Initial state for a new turn: persistent context + fresh per-turn fields."""
    state = {k: carry[k] for k in PERSISTENT_KEYS if k in carry and carry[k] is not None}
    state.update({
        "task_id": task_id, "repo_id": repo["id"], "repo_path": repo["path"], "turn": turn, "mode": mode,
        "request": request, "understanding": None, "intent": "", "tool_log": [], "evidence_rounds": 0,
        "evidence_done": False, "raw_analysis": None, "patch_id": None, "patch_mode": "initial",
        "patch_attempts": 0, "patch_error": None, "approval_decision": None, "feedback": None, "apply_stage": "",
        "verify_stage": "", "before_runs": [], "runs": [], "verification": None, "failure_signatures": [],
        "stalled": False, "repair_attempts": 0, "diagnosis": None, "retry_planned": False,
        "repair_target": "code", "test_repair_needed": False, "before_checked_for": None, "review": None, "clarifying_question": None,
        "unresolved": [], "warnings": [], "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "tool_calls": 0, "turn_started": time.time(), "final_status": "", "task_status": "running",
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

    def _debug(self, state: AgentState, step: str, messages: list[dict], outputs: list[str]) -> None:
        if not self.settings.debug_llm:
            return
        record = {"ts": now_iso(), "task": state["task_id"], "turn": state["turn"], "step": step,
                  "prompt": messages[-1]["content"], "outputs": outputs}
        with open(self.settings.logs_dir / "llm-debug.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def _budget(self, state: AgentState, needed: int = 1) -> None:
        if state.get("llm_calls", 0) + needed > self.settings.max_llm_calls:
            raise BudgetExceeded(f"LLM call budget of {self.settings.max_llm_calls} reached for this turn")

    def _usage(self, state: AgentState, results: list) -> dict:
        return {"llm_calls": state.get("llm_calls", 0) + len(results),
                "prompt_tokens": state.get("prompt_tokens", 0) + sum(r.prompt_tokens for r in results),
                "completion_tokens": state.get("completion_tokens", 0) + sum(r.completion_tokens for r in results)}

    async def llm_json(self, state: AgentState, step: str, user: str, model_cls, max_tokens: int):
        self._budget(state)
        client: LLMClient = self.deps.llm()
        await self.emit(state, "llm", {"step": step, "state": "started", "model": client.model})

        async def progress(tokens: int) -> None:
            await self.emit(state, "llm_progress", {"step": step, "tokens": tokens}, persist=False)

        messages = [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": user}]
        try:
            obj, results = await chat_json(client, messages, model_cls, max_tokens=max_tokens, on_progress=progress)
        except LLMResponseError as exc:
            self._debug(state, step, messages, [f"ERROR: {exc}"])
            await self.emit(state, "llm", {"step": step, "state": "failed", "model": client.model})
            raise
        self._debug(state, step, messages, [r.content for r in results])
        await self.emit(state, "llm", {
            "step": step, "state": "completed", "model": client.model,
            "prompt_tokens": sum(r.prompt_tokens for r in results),
            "completion_tokens": sum(r.completion_tokens for r in results),
            "duration_ms": sum(r.duration_ms for r in results)})
        return obj, self._usage(state, results)

    async def llm_text(self, state: AgentState, step: str, messages: list[dict], max_tokens: int):
        self._budget(state)
        client: LLMClient = self.deps.llm()
        await self.emit(state, "llm", {"step": step, "state": "started", "model": client.model})

        async def progress(tokens: int) -> None:
            await self.emit(state, "llm_progress", {"step": step, "tokens": tokens}, persist=False)

        result = await client.chat(messages, max_tokens=max_tokens, on_progress=progress)
        self._debug(state, step, messages, [result.content])
        await self.emit(state, "llm", {"step": step, "state": "completed", "model": client.model,
                                       "prompt_tokens": result.prompt_tokens,
                                       "completion_tokens": result.completion_tokens,
                                       "duration_ms": result.duration_ms})
        return result

    def _context(self, state: AgentState, *, evidence_lines: int | None = None, with_evidence: bool = True) -> str:
        overview = state.get("overview") or {}
        summary = overview.get("prompt_summary") or ""
        history = "\n".join(f"- Turn {h['turn']}: user asked \"{h['request'][:200]}\" → {h['headline'][:200]}"
                            for h in (state.get("history") or [])[-4:])
        evidence = render_evidence(state.get("evidence") or [], max_lines_per_item=evidence_lines) if with_evidence else ""
        return prompts.context_block(summary, evidence, state.get("notes") or [], history)

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
        return updates

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

        def work() -> dict:
            index = self.index(state)
            index.refresh(force=True)
            symbols = get_symbol_index(index)
            return {"files": len(index.files), "symbols": symbols.symbol_count, "truncated": index.truncated}

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

        def work():
            index = self.index(state)
            collector = EvidenceCollector(index, get_symbol_index(index), state.get("evidence") or [],
                                          max_chars=self.settings.max_evidence_chars)
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

    async def plan_evidence(self, state: AgentState) -> dict:
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
            return collector, outcomes

        collector, outcomes = await asyncio.to_thread(work)
        tool_log = list(state.get("tool_log") or [])
        for req, summary, ids in outcomes:
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
        user = self._context(state) + "\n\n" + template.format(request=state["request"])
        model_cls = LLMAnalysis if rca else LLMExplanation
        raw, usage = await self.llm_json(state, "analyze", user, model_cls, max_tokens=2000)
        await self.phase(state, "analyze", label, "completed")
        return {"raw_analysis": {"kind": "rca" if rca else "explanation", "data": raw.model_dump()}, **usage}

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
        tests = [near] if near else [f for f in index.files if is_test_path(f) and f.endswith("." + ext)]
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
        user = self._context(state) + "\n\n" + prompts.PLAN.format(
            problem=report.get("problem_summary", ""), root_cause=report.get("most_likely_root_cause") or "unknown",
            minimal_fix=report.get("minimal_fix", ""), request=state["request"],
            test_files=", ".join((overview.get("test_files") or [])[:12]) or "none",
            frameworks=", ".join(overview.get("test_frameworks") or []) or "unknown")
        raw, usage = await self.llm_json(state, "plan", user, LLMPlan, max_tokens=800)
        index = await asyncio.to_thread(self.index, state)
        warnings: list[str] = []
        files = []
        for f in raw.files_to_modify:
            path = self._existing_path(index, f.path)
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
        return {"plan": plan_out, "proposed_commands": commands, **usage,
                "warnings": (state.get("warnings") or []) + warnings}

    def route_after_plan(self, state: AgentState) -> str:
        return "finalize" if state.get("patch_error") else "generate_patch"

    def _file_block(self, index: RepoIndex, path: str, max_lines: int = 500) -> str:
        lines = index.read_lines(path)
        if lines is None:
            return f"### {path}\n(new file — does not exist yet)"
        body = "\n".join(lines[:max_lines])
        more = f"\n… ({len(lines) - max_lines} more lines not shown)" if len(lines) > max_lines else ""
        return f"### {path}\n```{_lang_fence(path)}\n{redact(body)}\n```{more}"

    async def _edit_loop(self, state: AgentState, step: str, prompt: str, *, workspace: Path, allowed: set[str],
                         allowed_new: set[str], only_tests: bool = False, attempts: int = 2,
                         protected: set[str] | None = None):
        """Ask for SEARCH/REPLACE edits, validate them, and feed errors back (bounded)."""
        messages = [{"role": "system", "content": prompts.SYSTEM}, {"role": "user", "content": prompt}]
        usage = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        build = None
        errors: list[str] = []
        local_state = dict(state)
        for attempt in range(attempts):
            result = await self.llm_text(local_state, step, messages, max_tokens=1800)
            for k, v in (("llm_calls", 1), ("prompt_tokens", result.prompt_tokens),
                         ("completion_tokens", result.completion_tokens)):
                usage[k] += v
            local_state["llm_calls"] = state.get("llm_calls", 0) + usage["llm_calls"]
            blocks, problems = parse_edit_blocks(result.content)
            if not blocks:
                creatable = {a for a in allowed_new if not (workspace / a).exists()}
                blocks = parse_fenced_new_files(result.content, creatable)
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
                messages = messages + [
                    {"role": "assistant", "content": result.content[:6000]},
                    {"role": "user", "content": "Those edits could not be applied:\n- " + "\n- ".join(errors[:8]) +
                                                "\nReply again with corrected SEARCH/REPLACE blocks only. Copy SEARCH "
                                                "lines exactly from the file contents shown."},
                ]
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
        if repair and state.get("repair_target") == "test" and test_path:
            report = state.get("report") or {}
            prompt = prompts.TEST_REPAIR.format(
                request=state["request"], root_cause=report.get("most_likely_root_cause") or "",
                fix_diff=self._current_diff(state), original_code=self._original_code(state, index),
                test_file=self._file_block(index, test_path), test_path=test_path,
                runs=self._runs_text((state.get("before_runs") or []) + (state.get("last_runs") or []))[:4000],
                framework=self._framework_label(state, test_path),
                rules=prompts.EDIT_RULES.format(example_path=test_path))
            build, usage, errors = await self._edit_loop({**state, **self._merge_usage(state, usage_total)},
                                                         "test_repair_patch", prompt, workspace=workspace,
                                                         allowed={test_path}, allowed_new={test_path},
                                                         only_tests=True, protected=protected)
            add_usage(usage)
            if build is None or not build.changes:
                return await self._patch_failed(state, usage_total, errors)
            changes = build.changes
            warnings += build.warnings + errors
        elif repair:
            touched = self._touched_files(state)
            allowed = set(touched) | set(fix_files) | ({test_path} if test_path else set())
            files_text = "\n\n".join(self._file_block(index, p) for p in sorted(allowed))
            diag = state.get("diagnosis") or {}
            prompt = prompts.REPAIR.format(runs=self._runs_text(state.get("last_runs") or state.get("runs") or []),
                                           diagnosis=f"{diag.get('diagnosis', '')}\nNext step: {diag.get('next_step', '')}",
                                           files=files_text, allowed=", ".join(sorted(allowed)),
                                           rules=prompts.EDIT_RULES.format(example_path=sorted(allowed)[0]))
            build, usage, errors = await self._edit_loop({**state, **self._merge_usage(state, usage_total)},
                                                         "repair_patch", prompt, workspace=workspace, allowed=allowed,
                                                         allowed_new=set(), protected=protected)
            add_usage(usage)
            if build is None or not build.changes:
                return await self._patch_failed(state, usage_total, errors)
            changes = build.changes
            warnings += build.warnings + errors
        else:
            tests_only = state.get("intent") == "add_tests"
            fix_diff = "(no code change in this step — the code under test is shown below)"
            if not tests_only:
                files_text = "\n\n".join(self._file_block(index, p) for p in fix_files)
                report = state.get("report") or {}
                prompt = prompts.EDIT_FIX.format(request=state["request"],
                                                 root_cause=report.get("most_likely_root_cause") or "(see plan)",
                                                 minimal_fix=report.get("minimal_fix") or plan.get("approach", ""),
                                                 plan=plan_text, files=files_text, allowed=", ".join(fix_files),
                                                 rules=prompts.EDIT_RULES.format(example_path=fix_files[0]))
                build, usage, errors = await self._edit_loop({**state, **self._merge_usage(state, usage_total)},
                                                             "fix_patch", prompt, workspace=workspace,
                                                             allowed=set(fix_files), allowed_new=set())
                add_usage(usage)
                if build is None or not build.changes:
                    return await self._patch_failed(state, usage_total, errors)
                changes = list(build.changes)
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
                test_prompt = prompts.EDIT_TEST.format(
                    request=state["request"], problem=report.get("problem_summary", ""),
                    root_cause=report.get("most_likely_root_cause") or "",
                    fix_diff=fix_diff, test_file_state="(existing — append a new test)" if exists else
                    "(does not exist yet — create it with an empty SEARCH section)",
                    test_file=self._file_block(index, test_path) if exists else f"### {test_path}\n(new file)",
                    example_test=example or "(no existing tests found)", test_path=test_path,
                    code_context=code_context, spec="{spec}",
                    framework=framework, rules=prompts.EDIT_RULES.format(example_path=test_path))
                spec_text = "(no specification)"
                if state.get("llm_calls", 0) + usage_total["llm_calls"] + 2 < self.settings.max_llm_calls:
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
                if state.get("llm_calls", 0) + usage_total["llm_calls"] < self.settings.max_llm_calls:
                    tbuild, tusage, terrors = await self._edit_loop(
                        {**state, **self._merge_usage(state, usage_total)}, "test_patch", test_prompt,
                        workspace=workspace, allowed={test_path}, allowed_new={test_path}, only_tests=True,
                        protected=protected)
                    add_usage(tusage)
                    if tbuild is not None and tbuild.changes:
                        changes += tbuild.changes
                        warnings += tbuild.warnings + terrors
                    elif tests_only:
                        return await self._patch_failed(state, usage_total, terrors)
                    else:
                        warnings.append("Could not generate a valid regression test: " + "; ".join(terrors)[:400])
        diff = "".join(c.diff for c in changes)
        summary = plan.get("summary", "") if not repair else (state.get("diagnosis") or {}).get("next_step", "Repair")
        if repair and state.get("repair_target") == "test":
            summary = "Rewrite the regression test so it reproduces the bug"
        patch = self.db.add_patch(state["task_id"], turn=state["turn"], attempt=attempt_no, summary=summary[:500],
                                  diff=diff, changes=[c.to_dict() for c in changes], warnings=warnings)
        public = {k: v for k, v in patch.items() if k != "changes"}
        await self.emit(state, "patch", {"patch": public})
        await self.phase(state, "patch", label, "completed",
                         f"{len(changes)} file(s): " + ", ".join(f"{c.path} (+{c.additions}/-{c.deletions})"
                                                                for c in changes))
        merged = self._merge_usage(state, usage_total)
        return {"patch_id": patch["id"], "patch_attempts": attempt_no, "patch_error": None, **merged,
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
            runs.append({"stage": stage, "command_id": row["id"], "argv": argv, "exit_code": row["exit_code"],
                         "status": row["status"], "summary": row["summary"],
                         "output": _focus_output(row["stdout"] or "", row["stderr"] or "")})
        outcomes = ", ".join(f"{(r['summary'] or {}).get('outcome', 'unknown')} (exit {r['exit_code']})" for r in runs)
        await self.phase(state, "verify", label, "completed", outcomes or "no commands ran")
        if stage == "before_fix":
            return {"before_runs": runs}
        return {"runs": (state.get("runs") or []) + runs, "last_runs": runs}

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
        stalled = verification["verdict"] == "failed" and signature in (state.get("failure_signatures") or [])
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
        if v.get("verdict") != "failed" and not state.get("test_repair_needed"):
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
            parts.append(f"$ {' '.join(r['argv'])}\nexit code: {r['exit_code']} | outcome: {s.get('outcome')} | "
                         f"passed={s.get('passed')} failed={s.get('failed')} errors={s.get('errors')}\n"
                         f"failing tests: {', '.join(s.get('failing_tests') or []) or 'n/a'}\n"
                         f"output:\n{r.get('output') or ''}")
        return "\n\n".join(parts)[:7000]

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
        runs = state.get("last_runs") or []
        user = self._context(state, evidence_lines=30) + "\n\n" + prompts.DIAGNOSE.format(
            runs=self._runs_text(runs) + (f"\n\nUser message: {state['request']}" if state.get("intent") == "retry_fix" else ""),
            diff=self._current_diff(state))
        fa, usage = await self.llm_json(state, "diagnose", user, LLMFailureAnalysis, max_tokens=600)
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
        updates = {"diagnosis": diag, "retry_planned": retry, "repair_target": "code", **usage}
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
        verification = self._runs_text(state.get("last_runs") or []) if state.get("last_runs") else \
            "No verification commands have been run for these changes."
        plan = state.get("plan") or {}
        user = self._context(state, evidence_lines=25) + "\n\n" + prompts.REVIEW.format(
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
        if verdict == "passed" and (reg_status == "detected_bug" or intent == "add_tests"):
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
