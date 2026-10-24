"""Task manager: runs graph turns as asyncio tasks, persists events, fans out SSE, handles approvals,
cancellation and recovery after restarts."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from locpilot.agent.graph import AgentDeps, BudgetExceeded, build_graph, turn_input
from locpilot.config import Settings
from locpilot.llm.ollama import LLMClient, LLMResponseError, LLMUnavailableError
from locpilot.security.secrets import redact
from locpilot.storage.db import Database

log = logging.getLogger("locpilot.runner")


class TaskConflictError(RuntimeError):
    pass


class TaskManager:
    def __init__(self, settings: Settings, db: Database, llm_factory: Callable[[], LLMClient]) -> None:
        self.settings = settings
        self.db = db
        self.llm_factory = llm_factory
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._running: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._saver_cm: Any = None
        self._shutting_down = False
        self.graph: Any = None

    # lifecycle -----------------------------------------------------------------------
    async def startup(self) -> None:
        self._saver_cm = AsyncSqliteSaver.from_conn_string(str(self.settings.checkpoint_path))
        saver = await self._saver_cm.__aenter__()
        deps = AgentDeps(settings=self.settings, db=self.db, llm=self.llm_factory, emitter=self._emit_from_graph)
        self.graph = build_graph(deps).compile(checkpointer=saver)
        for task_id in self.db.mark_interrupted_tasks():
            task = self.db.get_task(task_id)
            await self.emit(task_id, task["turn"] if task else 0, "task_status",
                            {"status": "interrupted", "phase": task.get("phase") if task else None})
            await self.emit(task_id, task["turn"] if task else 0, "log", {
                "level": "warning",
                "text": "LocPilot restarted while this task was running. Resume continues from the last saved "
                        "checkpoint; already-applied edits are not repeated."})

    async def shutdown(self) -> None:
        # Turns still running at shutdown become "interrupted" (resumable from their checkpoint).
        self._shutting_down = True
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        if self._saver_cm is not None:
            await self._saver_cm.__aexit__(None, None, None)

    # events --------------------------------------------------------------------------
    async def emit(self, task_id: str, turn: int, type_: str, data: dict, persist: bool = True) -> dict:
        if persist:
            event = self.db.add_event(task_id, turn=turn, type=type_, data=data)
        else:
            event = {"id": None, "task_id": task_id, "turn": turn, "type": type_, "data": data, "created_at": None}
        for queue in list(self._subscribers.get(task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    async def _emit_from_graph(self, task_id: str, turn: int, type_: str, data: dict, persist: bool = True) -> dict:
        return await self.emit(task_id, turn, type_, data, persist)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers[task_id].add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(task_id, set()).discard(queue)

    def is_running(self, task_id: str) -> bool:
        task = self._running.get(task_id)
        return task is not None and not task.done()

    # turns ---------------------------------------------------------------------------
    @staticmethod
    def thread_config(task_id: str, turn: int) -> dict:
        return {"configurable": {"thread_id": f"{task_id}#{turn}"}, "recursion_limit": 120}

    async def _carry(self, task_id: str, turn: int) -> dict:
        if turn <= 1:
            return {}
        snapshot = await self.graph.aget_state(self.thread_config(task_id, turn - 1))
        return dict(snapshot.values or {})

    async def start_turn(self, task_id: str, message: str, mode: str) -> None:
        async with self._locks[task_id]:
            task = self.db.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            if self.is_running(task_id):
                raise TaskConflictError("A turn is already running for this task. Wait for it or cancel it.")
            if task["status"] == "awaiting_approval":
                raise TaskConflictError("Approve or reject the pending change (or cancel the task) first.")
            repo = self.db.get_repo(task["repo_id"])
            if repo is None:
                raise KeyError(task["repo_id"])
            turn = task["turn"] + 1
            # The task is labelled "code" once it has (or is about to get) an isolated worktree.
            task_mode = "code" if mode == "code" or task["worktree_path"] else "ask"
            self.db.update_task(task_id, turn=turn, status="queued", mode=task_mode, error=None)
            msg = self.db.add_message(task_id, turn=turn, role="user", content=message, mode=mode)
            await self.emit(task_id, turn, "message", msg)
            carry = await self._carry(task_id, turn)
            state = turn_input(carry, task_id=task_id, repo=repo, turn=turn, mode=mode, request=message)
            self._launch(task_id, turn, state)

    def _launch(self, task_id: str, turn: int, graph_input: Any) -> None:
        self._running[task_id] = asyncio.create_task(self._run(task_id, turn, graph_input), name=f"task:{task_id}")

    async def _run(self, task_id: str, turn: int, graph_input: Any) -> None:
        config = self.thread_config(task_id, turn)
        self.db.update_task(task_id, status="running")
        await self.emit(task_id, turn, "task_status", {"status": "running", "phase": None})
        status, error = "completed", None
        try:
            await self.graph.ainvoke(graph_input, config=config, durability="sync")
            snapshot = await self.graph.aget_state(config)
            if snapshot.interrupts:
                status = "awaiting_approval"
            else:
                status = (snapshot.values or {}).get("task_status") or "completed"
                if status == "running":
                    status = "completed"
        except asyncio.CancelledError:
            if self._shutting_down:
                status = "interrupted"
                await self.emit(task_id, turn, "log", {"level": "warning",
                                                       "text": "LocPilot shut down during this turn; resume it later."})
            else:
                status = "cancelled"
                approval = self.db.pending_approval(task_id)
                if approval:
                    self.db.decide_approval(approval["id"], status="expired", decision={"decision": "cancel"})
                await self.emit(task_id, turn, "log", {"level": "warning", "text": "Task cancelled by user."})
        except LLMUnavailableError as exc:
            status, error = "failed", f"{exc}" + (f" — {exc.hint}" if exc.hint else "")
            await self.emit(task_id, turn, "error", {"message": error, "recoverable": True})
        except (LLMResponseError, BudgetExceeded) as exc:
            status, error = "failed", redact(str(exc))[:800]
            await self.emit(task_id, turn, "error", {"message": error, "recoverable": True})
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI without leaking secrets
            log.exception("task %s failed", task_id)
            status, error = "failed", redact(f"{type(exc).__name__}: {exc}")[:800]
            await self.emit(task_id, turn, "error", {"message": error, "recoverable": False})
        finally:
            self._running.pop(task_id, None)
        self.db.update_task(task_id, status=status, error=error)
        task = self.db.get_task(task_id)
        await self.emit(task_id, turn, "task_status", {"status": status, "phase": task.get("phase") if task else None})

    async def cancel(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        running = self._running.get(task_id)
        if running is not None and not running.done():
            running.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(running), timeout=15)
            except (asyncio.CancelledError, TimeoutError):
                pass
            return
        if task["status"] == "awaiting_approval":
            approval = self.db.pending_approval(task_id)
            if approval:
                await self.decide(task_id, approval["id"], {"decision": "cancel"})
                return
        if task["status"] in ("queued", "interrupted"):
            self.db.update_task(task_id, status="cancelled")
            await self.emit(task_id, task["turn"], "task_status", {"status": "cancelled", "phase": task.get("phase")})

    async def resume(self, task_id: str) -> None:
        async with self._locks[task_id]:
            task = self.db.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["status"] != "interrupted":
                raise TaskConflictError("Only interrupted tasks can be resumed.")
            snapshot = await self.graph.aget_state(self.thread_config(task_id, task["turn"]))
            if snapshot.interrupts:
                self.db.update_task(task_id, status="awaiting_approval")
                await self.emit(task_id, task["turn"], "task_status", {"status": "awaiting_approval",
                                                                       "phase": "approval"})
                return
            if not snapshot.next:
                self.db.update_task(task_id, status="completed")
                return
            await self.emit(task_id, task["turn"], "log", {"level": "info",
                                                           "text": f"Resuming from checkpoint at: {', '.join(snapshot.next)}"})
            self._launch(task_id, task["turn"], None)

    async def decide(self, task_id: str, approval_id: str, decision: dict) -> None:
        async with self._locks[task_id]:
            approval = self.db.get_approval(approval_id)
            if approval is None or approval["task_id"] != task_id:
                raise KeyError(approval_id)
            if approval["status"] != "pending":
                raise TaskConflictError(f"This approval is already {approval['status']}.")
            if self.is_running(task_id):
                raise TaskConflictError("The task is running; wait for it to request approval.")
            task = self.db.get_task(task_id)
            assert task is not None
            kind = decision.get("decision")
            status = {"approve": "approved", "reject": "rejected", "cancel": "expired"}[kind]
            self.db.decide_approval(approval_id, status=status, decision=decision)
            await self.emit(task_id, task["turn"], "approval_resolved", {
                "approval_id": approval_id, "decision": kind, "commands": decision.get("commands"),
                "note": decision.get("note")})
            snapshot = await self.graph.aget_state(self.thread_config(task_id, task["turn"]))
            if not snapshot.interrupts:
                self.db.update_task(task_id, status="completed" if kind != "cancel" else "cancelled")
                return
            self._launch(task_id, task["turn"], Command(resume=decision))
