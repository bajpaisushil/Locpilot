"""Tasks: creation, follow-ups, SSE event stream, approvals, cancellation, diffs and rollback."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from locpilot.agent.runner import TaskConflictError
from locpilot.api.models import ApiError, ApprovalDecision, MessageCreate, TaskCreate, db_of, manager_of, settings_of
from locpilot.llm.ollama import LLMUnavailableError
from locpilot.storage.db import now_iso
from locpilot.tools import git
from locpilot.tools.files import drop_index
from locpilot.tools.patch import FileChange, revert_changes, unified_diff

router = APIRouter(prefix="/api/tasks")


def _task_or_404(request: Request, task_id: str) -> dict:
    task = db_of(request).get_task(task_id)
    if task is None:
        raise ApiError(404, "task_not_found", "Task not found.")
    return task


def _public_patch(patch: dict) -> dict:
    return {k: v for k, v in patch.items() if k != "changes"}


def task_detail(request: Request, task_id: str) -> dict:
    db = db_of(request)
    task = _task_or_404(request, task_id)
    repo = db.get_repo(task["repo_id"])
    return {
        **{k: task[k] for k in ("id", "repo_id", "title", "mode", "status", "phase", "created_at", "updated_at",
                                "model", "workspace_path", "worktree_path", "base_commit", "error")},
        "turn": task["turn"], "repo": repo, "messages": db.list_messages(task_id), "reports": db.list_reports(task_id),
        "patches": [_public_patch(p) for p in db.list_patches(task_id)],
        "commands": db.list_commands(task_id, include_output=False),
        "pending_approval": db.pending_approval(task_id), "stats": task.get("stats") or {},
    }


async def _require_model(request: Request) -> str:
    llm = request.app.state.llm_factory()
    try:
        await llm.ensure_ready()
    except LLMUnavailableError as exc:
        raise ApiError(503, "model_unavailable", str(exc), exc.hint) from None
    return llm.model


@router.get("")
async def list_tasks(request: Request, repo_id: str | None = None) -> list[dict]:
    rows = db_of(request).list_tasks(repo_id)
    return [{k: r[k] for k in ("id", "repo_id", "title", "mode", "status", "phase", "created_at", "updated_at",
                               "model")} for r in rows]


@router.post("", status_code=201)
async def create_task(body: TaskCreate, request: Request) -> dict:
    db = db_of(request)
    if db.get_repo(body.repo_id) is None:
        raise ApiError(404, "repo_not_found", "Repository not found.")
    model = await _require_model(request)
    title = " ".join(body.message.split())[:90]
    task = db.create_task(repo_id=body.repo_id, title=title, mode=body.mode, model=model)
    db.touch_repo(body.repo_id)
    await manager_of(request).start_turn(task["id"], body.message, body.mode)
    return task_detail(request, task["id"])


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    return task_detail(request, task_id)


@router.delete("/{task_id}")
async def delete_task(task_id: str, request: Request) -> dict:
    db, settings, manager = db_of(request), settings_of(request), manager_of(request)
    task = _task_or_404(request, task_id)
    if manager.is_running(task_id):
        await manager.cancel(task_id)
    repo = db.get_repo(task["repo_id"])
    if task.get("worktree_path") and repo:
        try:
            await asyncio.to_thread(git.worktree_remove, Path(repo["path"]), Path(task["worktree_path"]),
                                    managed_root=settings.worktrees_dir)
        except git.GitError:
            pass
        drop_index(Path(task["worktree_path"]))
    db.delete_task(task_id)
    return {"ok": True}


@router.post("/{task_id}/messages")
async def post_message(task_id: str, body: MessageCreate, request: Request) -> dict:
    task = _task_or_404(request, task_id)
    await _require_model(request)
    mode = body.mode or task["mode"]
    try:
        await manager_of(request).start_turn(task_id, body.message.strip(), mode)
    except TaskConflictError as exc:
        raise ApiError(409, "task_busy", str(exc)) from None
    return task_detail(request, task_id)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> dict:
    _task_or_404(request, task_id)
    await manager_of(request).cancel(task_id)
    return task_detail(request, task_id)


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, request: Request) -> dict:
    _task_or_404(request, task_id)
    await _require_model(request)
    try:
        await manager_of(request).resume(task_id)
    except TaskConflictError as exc:
        raise ApiError(409, "not_resumable", str(exc)) from None
    return task_detail(request, task_id)


@router.post("/{task_id}/approvals/{approval_id}")
async def decide(task_id: str, approval_id: str, body: ApprovalDecision, request: Request) -> dict:
    _task_or_404(request, task_id)
    try:
        await manager_of(request).decide(task_id, approval_id, body.model_dump())
    except KeyError:
        raise ApiError(404, "approval_not_found", "Approval not found.") from None
    except TaskConflictError as exc:
        raise ApiError(409, "approval_conflict", str(exc)) from None
    return task_detail(request, task_id)


def _sse(event: dict) -> str:
    lines = []
    if event.get("id") is not None:
        lines.append(f"id: {event['id']}")
    lines.append(f"event: {event['type']}")
    lines.append("data: " + json.dumps(event, default=str))
    return "\n".join(lines) + "\n\n"


@router.get("/{task_id}/events")
async def events(task_id: str, request: Request, after: int = 0,
                 last_event_id: str | None = Header(None, alias="Last-Event-ID")) -> StreamingResponse:
    _task_or_404(request, task_id)
    manager, db = manager_of(request), db_of(request)
    start = after
    if last_event_id and last_event_id.isdigit():
        start = max(start, int(last_event_id))
    queue = manager.subscribe(task_id)

    async def stream():
        last = start
        try:
            for event in db.list_events(task_id, after=last):
                last = event["id"]
                yield _sse(event)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event.get("id") is not None:
                    if event["id"] <= last:
                        continue
                    last = event["id"]
                yield _sse(event)
        finally:
            manager.unsubscribe(task_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def _applied_changes(request: Request, task_id: str) -> list[FileChange]:
    changes: list[FileChange] = []
    for patch in db_of(request).list_patches(task_id):
        if patch["status"] == "applied":
            changes += [FileChange.from_dict(c) for c in patch["changes"]]
    return changes


@router.get("/{task_id}/diff")
async def task_diff(task_id: str, request: Request) -> dict:
    task = _task_or_404(request, task_id)
    worktree = task.get("worktree_path")
    files = []
    if worktree and Path(worktree).is_dir():
        seen: dict[str, FileChange] = {}
        for ch in _applied_changes(request, task_id):
            seen.setdefault(ch.path, ch)
        for path, first in seen.items():
            original = first.before
            target = Path(worktree) / path
            modified = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
            change = "create" if original is None else ("delete" if modified is None else "modify")
            unified = unified_diff(path, original, modified or "") if modified != original else ""
            files.append({"path": path, "change": change, "original": original, "modified": modified,
                          "unified": unified, "binary": False})
    return {"base_commit": task.get("base_commit"), "worktree_path": worktree, "files": files,
            "unified": "".join(f["unified"] for f in files)}


@router.get("/{task_id}/patch")
async def download_patch(task_id: str, request: Request) -> PlainTextResponse:
    diff = (await task_diff(task_id, request))["unified"]
    return PlainTextResponse(diff, media_type="text/x-diff",
                             headers={"Content-Disposition": f'attachment; filename="locpilot-{task_id}.patch"'})


@router.get("/{task_id}/commands/{command_id}")
async def get_command(task_id: str, command_id: str, request: Request) -> dict:
    _task_or_404(request, task_id)
    cmd = db_of(request).get_command(command_id)
    if cmd is None:
        raise ApiError(404, "command_not_found", "Command not found.")
    return cmd


@router.post("/{task_id}/rollback")
async def rollback(task_id: str, request: Request) -> dict:
    db, manager = db_of(request), manager_of(request)
    task = _task_or_404(request, task_id)
    if manager.is_running(task_id):
        raise ApiError(409, "task_busy", "Cancel the running turn before rolling back.")
    worktree = task.get("worktree_path")
    if not worktree or not Path(worktree).is_dir():
        raise ApiError(400, "no_worktree", "This task has no LocPilot worktree to roll back.")
    patches = [p for p in db.list_patches(task_id) if p["status"] == "applied"]
    changes = [FileChange.from_dict(c) for p in patches for c in p["changes"]]
    restored, skipped = await asyncio.to_thread(revert_changes, Path(worktree), changes)
    if not skipped:
        for p in patches:
            db.update_patch(p["id"], status="rolled_back")
    drop_index(Path(worktree))
    await manager.emit(task_id, task["turn"], "log", {
        "level": "info", "text": f"Rolled back {len(restored)} file(s) in the LocPilot worktree"
                                 + (f"; skipped {len(skipped)} file(s) changed by someone else" if skipped else "")
                                 + f" at {now_iso()}."})
    return {"restored": restored, "skipped": skipped}
