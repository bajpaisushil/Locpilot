"""Tasks: creation, follow-ups, SSE event stream, approvals, cancellation, diffs, rollback and the user's own
commands."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from locpilot.agent.runner import TaskConflictError
from locpilot.agent.workspace import WorkspaceError
from locpilot.api.models import (
    ApiError,
    ApprovalDecision,
    CommitCreate,
    FileSelection,
    MessageCreate,
    TaskCreate,
    UserCommandCreate,
    db_of,
    manager_of,
    settings_of,
)
from locpilot.llm.ollama import LLMUnavailableError
from locpilot.security.paths import PathSecurityError, normalize_relpath
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
    await manager.stop_user_command(task_id)
    await request.app.state.terminals.close_where(task_id=task_id)
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


@router.post("/{task_id}/commands", status_code=202)
async def run_command(task_id: str, body: UserCommandCreate, request: Request) -> dict:
    """Run a command the user typed in the task worktree (never reachable by the agent)."""
    _task_or_404(request, task_id)
    try:
        return await manager_of(request).run_user_command(task_id, body.command, body.allow_network)
    except TaskConflictError as exc:
        raise ApiError(409, "task_busy", str(exc)) from None
    except WorkspaceError as exc:
        raise ApiError(400, "no_worktree", str(exc)) from None


@router.post("/{task_id}/commands/{command_id}/stop")
async def stop_command(task_id: str, command_id: str, request: Request) -> dict:
    _task_or_404(request, task_id)
    if not await manager_of(request).stop_user_command(task_id, command_id):
        raise ApiError(409, "command_not_running", "This command is not running.")
    return db_of(request).get_command(command_id, include_output=False)


@router.post("/{task_id}/rollback")
async def rollback(task_id: str, request: Request) -> dict:
    db, manager = db_of(request), manager_of(request)
    task = _task_or_404(request, task_id)
    if manager.is_running(task_id):
        raise ApiError(409, "task_busy", "Cancel the running turn before rolling back.")
    if manager.user_command_running(task_id):
        raise ApiError(409, "task_busy", "Stop the command you are running before rolling back.")
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


# --- bringing a task's changes out: apply to the user's checkout, commit to a branch, push --------------

def _worktree_or_400(task: dict) -> Path:
    worktree = task.get("worktree_path")
    if not worktree or not Path(worktree).is_dir():
        raise ApiError(400, "no_worktree", "This task has no LocPilot worktree (changes are made in Code mode).")
    return Path(worktree)


def _ensure_idle(request: Request, task_id: str) -> None:
    if manager_of(request).is_running(task_id):
        raise ApiError(409, "task_busy", "LocPilot is working on this task. Wait for the turn to finish first.")


def _branch_slug(title: str, limit: int = 40) -> str:
    """The first words of the title, lowercase and hyphenated, never cut mid-word."""
    words = [w for w in re.split(r"[^a-z0-9]+", title.lower()) if w]
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > limit:
            break
        slug = candidate
    return slug or (words[0][:limit] if words else "changes")


def _publish_view(request: Request, task: dict, worktree: Path) -> dict | None:
    publish = db_of(request).get_publish(task["id"])
    if publish is None:
        return None
    remote = git.remote_url(worktree)
    return {**publish, "remote_url": remote,
            "pull_request_url": git.pull_request_url(remote, publish["branch"], settings_of(request).allowed_git_hosts)}


def _selected(worktree: Path, files: list[str]) -> list[str]:
    """The requested paths, each of which must be an uncommitted change in the worktree."""
    changed = {f.path for f in git.worktree_changes(worktree)}
    selected = []
    for raw in files:
        try:
            path = normalize_relpath(raw)
        except PathSecurityError:
            raise ApiError(400, "invalid_path", f"Invalid path: {raw!r}") from None
        if path not in changed:
            raise ApiError(400, "not_changed", f"{path} has no uncommitted changes in this task's worktree.")
        selected.append(path)
    return list(dict.fromkeys(selected))


def _git_hint(message: str) -> str | None:
    lowered = message.lower()
    if "tell me who you are" in lowered or "author identity unknown" in lowered:
        return 'Set your Git identity once: git config --global user.name "Your Name" && ' \
               "git config --global user.email you@example.com"
    if any(s in lowered for s in ("could not resolve host", "unable to access", "network is unreachable",
                                  "connection timed out", "could not read from remote")):
        return "Check your internet connection, then push again — the commit is saved locally."
    if any(s in lowered for s in ("authentication failed", "permission denied", "403", "denied to")):
        return "LocPilot pushes with your own Git credentials (credential helper or SSH key). Make sure " \
               "`git push` works for this repository in a terminal."
    return None


@router.get("/{task_id}/worktree-changes")
async def worktree_changes(task_id: str, request: Request) -> dict:
    """Uncommitted changes in the task worktree (LocPilot's and any others), plus commit/push state."""
    task = _task_or_404(request, task_id)
    worktree = _worktree_or_400(task)
    files = await asyncio.to_thread(git.worktree_changes, worktree)
    ours = {c.path for c in _applied_changes(request, task_id)}
    publish = await asyncio.to_thread(_publish_view, request, task, worktree)
    reports = db_of(request).list_reports(task_id)
    plan_summary = ((reports[-1].get("plan") or {}).get("summary") if reports else None) or ""
    return {
        "base_commit": task.get("base_commit"), "worktree_path": str(worktree),
        "files": [{"path": f.path, "status": f.status, "locpilot": f.path in ours} for f in files],
        "suggested_branch": publish["branch"] if publish else f"locpilot/{_branch_slug(task['title'])}",
        "suggested_message": (plan_summary or task["title"]).strip()[:200],
        "publish": publish,
    }


@router.post("/{task_id}/apply-to-repo")
async def apply_to_repo(task_id: str, body: FileSelection, request: Request) -> dict:
    """Write the selected changes into the user's own working tree (plain file edits, explicit user action)."""
    db, settings = db_of(request), settings_of(request)
    task = _task_or_404(request, task_id)
    worktree = _worktree_or_400(task)
    _ensure_idle(request, task_id)
    repo = db.get_repo(task["repo_id"])
    if repo is None or not task.get("base_commit"):
        raise ApiError(400, "no_repository", "The task's repository is no longer available.")
    files = await asyncio.to_thread(_selected, worktree, body.files)
    try:
        await asyncio.to_thread(git.apply_to_checkout, worktree, Path(repo["path"]), base=task["base_commit"],
                                files=files, patch_file=settings.scratch_dir / task_id / "apply-to-repo.patch")
    except git.GitError as exc:
        raise ApiError(409, "apply_failed", str(exc),
                       "Commit the changes to a branch instead, or download the .patch.") from None
    await manager_of(request).emit(task_id, task["turn"], "log", {
        "level": "info", "text": f"Applied {len(files)} file(s) to your repository at {repo['path']}: " + ", ".join(files)})
    return {"applied": files, "repo_path": repo["path"]}


@router.post("/{task_id}/commit")
async def commit_changes(task_id: str, body: CommitCreate, request: Request) -> dict:
    """Commit the selected worktree changes to a branch (created or advanced; the worktree stays detached)."""
    db, settings = db_of(request), settings_of(request)
    task = _task_or_404(request, task_id)
    worktree = _worktree_or_400(task)
    _ensure_idle(request, task_id)
    if not await asyncio.to_thread(git.valid_branch_name, body.branch):
        raise ApiError(400, "invalid_branch", f"{body.branch!r} is not a valid branch name.")
    files = await asyncio.to_thread(_selected, worktree, body.files)
    publish = db.get_publish(task_id)
    expected = publish["head"] if publish and publish["branch"] == body.branch else None
    try:
        commit = await asyncio.to_thread(git.commit_to_branch, worktree, branch=body.branch, message=body.message,
                                         files=files, expected_tip=expected, hooks_dir=settings.empty_hooks_dir)
    except git.GitError as exc:
        raise ApiError(409, "commit_failed", str(exc), _git_hint(str(exc))) from None
    db.record_commit(task_id, branch=body.branch, head=commit)
    await manager_of(request).emit(task_id, task["turn"], "log", {
        "level": "info", "text": f"Committed {len(files)} file(s) to branch {body.branch} ({commit[:10]})"})
    return {"commit": commit, "branch": body.branch, "files": files,
            "publish": await asyncio.to_thread(_publish_view, request, task, worktree)}


@router.post("/{task_id}/push")
async def push_changes(task_id: str, request: Request) -> dict:
    """Push the task's branch to origin with the user's own Git credentials."""
    db, settings = db_of(request), settings_of(request)
    task = _task_or_404(request, task_id)
    worktree = _worktree_or_400(task)
    publish = db.get_publish(task_id)
    if publish is None:
        raise ApiError(400, "nothing_committed", "Commit the changes to a branch before pushing.")
    try:
        await asyncio.to_thread(git.push_branch, worktree, publish["branch"], timeout=settings.clone_timeout_s,
                                hooks_dir=settings.empty_hooks_dir)
    except git.GitError as exc:
        raise ApiError(409, "push_failed", str(exc), _git_hint(str(exc))) from None
    db.record_push(task_id, head=publish["head"])
    view = await asyncio.to_thread(_publish_view, request, task, worktree)
    await manager_of(request).emit(task_id, task["turn"], "log", {
        "level": "info", "text": f"Pushed {publish['branch']} to origin"})
    return {"publish": view}

