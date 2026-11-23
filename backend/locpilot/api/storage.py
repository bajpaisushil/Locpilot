"""Disk space LocPilot uses, and freeing it: archive the worktrees of finished tasks (restored on next use)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from locpilot.agent.workspace import (
    WorkspaceError,
    archive_path,
    archive_worktree,
    directory_size,
    worktree_is_archived,
)
from locpilot.api.models import db_of, manager_of, settings_of
from locpilot.llm.ollama import LLMUnavailableError
from locpilot.tools import git
from locpilot.tools.symbols import symbol_store

router = APIRouter(prefix="/api/storage")

BUSY_STATUSES = {"queued", "running", "awaiting_approval"}


class ArchiveRequest(BaseModel):
    task_ids: list[str] | None = Field(None, max_length=1000)  # None: every finished task's worktree


def _blocker(request: Request, task: dict) -> str | None:
    """Why a task's worktree must stay as it is right now (None: it can be archived)."""
    manager = manager_of(request)
    if manager.is_running(task["id"]) or task["status"] in BUSY_STATUSES:
        return "waiting for your approval" if task["status"] == "awaiting_approval" else "LocPilot is working on it"
    if manager.user_command_running(task["id"]):
        return "a command is running in it"
    if request.app.state.terminals.list(task_id=task["id"]):
        return "a terminal is open in it"
    return None


def _worktree_rows(request: Request) -> list[dict]:
    db, settings = db_of(request), settings_of(request)
    repos = {r["id"]: r for r in db.list_repos()}
    rows = []
    for task in db.list_tasks(limit=10_000):
        wt = task.get("worktree_path")
        if not wt:
            continue
        archived = worktree_is_archived(settings, task)
        if not archived and not (Path(wt) / ".git").exists():
            continue
        size = archive_path(settings, task["id"]).stat().st_size if archived else directory_size(Path(wt))
        blocker = None if archived else _blocker(request, task)
        rows.append({"task_id": task["id"], "repo_id": task["repo_id"],
                     "repo_name": (repos.get(task["repo_id"]) or {}).get("name"), "title": task["title"],
                     "status": task["status"], "updated_at": task.get("updated_at"),
                     "state": "archived" if archived else "active", "bytes": size,
                     "can_archive": not archived and blocker is None, "reason": blocker})
    return rows


def _db_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists())


def _other_sizes(request: Request) -> dict:
    """Space used by files that grow with use (besides worktrees and models)."""
    settings = settings_of(request)
    return {"history_bytes": _db_bytes(settings.db_path), "checkpoints_bytes": _db_bytes(settings.checkpoint_path),
            "logs_bytes": directory_size(settings.logs_dir), "caches_bytes": directory_size(settings.scratch_dir),
            "index_bytes": symbol_store().size_bytes()}


GROWING_LOGS = ("ollama.log", "llm-debug.jsonl")  # appended to without limit (backend.log rotates by itself)


def _clean_files(request: Request, idle_ids: set[str], known_ids: set[str]) -> None:
    settings = settings_of(request)
    # Caches of commands (npm, go build, tmp) of tasks that are idle or gone: recreated when needed.
    if settings.scratch_dir.is_dir():
        for entry in settings.scratch_dir.iterdir():
            if entry.is_dir() and not entry.is_symlink() and (entry.name in idle_ids or entry.name not in known_ids):
                shutil.rmtree(entry, ignore_errors=True)
    if settings.logs_dir.is_dir():
        for entry in settings.logs_dir.iterdir():
            try:
                if entry.name.startswith("backend.log.") and entry.is_file():
                    entry.unlink()  # rotated old backend logs
                elif entry.name in GROWING_LOGS and entry.is_file():
                    with open(entry, "w", encoding="utf-8"):
                        pass  # emptied, not removed: a running Ollama keeps appending to it
            except OSError:
                continue  # in use (Windows): leave it
    symbol_store().compact()
    db_of(request).compact()


async def _models_bytes(request: Request) -> int | None:
    try:
        models = await request.app.state.llm_factory().list_models()
    except (httpx.HTTPError, LLMUnavailableError, ValueError, AttributeError):
        return None
    return sum(int(m.get("size_bytes") or 0) for m in models)


@router.get("")
async def storage(request: Request) -> dict:
    settings = settings_of(request)
    rows = await asyncio.to_thread(_worktree_rows, request)
    active = [r for r in rows if r["state"] == "active"]
    return {
        "data_dir": str(settings.data_dir),
        "worktrees": rows,
        "worktrees_bytes": sum(r["bytes"] for r in active),
        "archivable_bytes": sum(r["bytes"] for r in active if r["can_archive"]),
        "archived_count": len(rows) - len(active),
        "archived_bytes": sum(r["bytes"] for r in rows if r["state"] == "archived"),
        **await asyncio.to_thread(_other_sizes, request),
        "models_bytes": await _models_bytes(request),
    }


@router.post("/clean-up")
async def clean_up(request: Request) -> dict:
    """Free disk space without losing anything you use: old saved agent states, command caches, logs, stale index
    entries; then compact the databases so the space returns to the disk."""
    db = db_of(request)
    tasks = db.list_tasks(limit=10_000)
    idle = [t for t in tasks if _blocker(request, t) is None]
    before = await asyncio.to_thread(_other_sizes, request)
    await manager_of(request).compact_checkpoints(idle)
    await asyncio.to_thread(_clean_files, request, {t["id"] for t in idle}, {t["id"] for t in tasks})
    after = await asyncio.to_thread(_other_sizes, request)
    details = {key.removesuffix("_bytes"): max(0, before[key] - after[key]) for key in before}
    return {"bytes_freed": sum(details.values()), "details": details}


@router.post("/archive-worktrees")
async def archive_worktrees(body: ArchiveRequest, request: Request) -> dict:
    """Free disk space: save each finished task's worktree to a small file and remove the folder."""
    db, settings = db_of(request), settings_of(request)
    wanted = set(body.task_ids) if body.task_ids is not None else None
    archived, skipped, freed = [], [], 0
    for task in db.list_tasks(limit=10_000):
        if wanted is not None and task["id"] not in wanted:
            continue
        wt = task.get("worktree_path")
        if not wt or not (Path(wt) / ".git").exists():
            continue
        blocker = _blocker(request, task)
        repo = db.get_repo(task["repo_id"])
        if blocker is None and repo is None:
            blocker = "its repository is no longer connected"
        if blocker is not None:
            skipped.append({"task_id": task["id"], "reason": blocker})
            continue
        try:
            freed += await asyncio.to_thread(archive_worktree, settings, Path(repo["path"]), task)
            archived.append(task["id"])
        except (WorkspaceError, git.GitError) as exc:
            skipped.append({"task_id": task["id"], "reason": str(exc)})
    return {"archived": archived, "skipped": skipped, "bytes_freed": freed}
