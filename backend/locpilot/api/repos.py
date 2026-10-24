"""Repository registration, status, file tree, file content and search."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Query, Request

from locpilot.api.models import ApiError, RepoCreate, db_of, settings_of
from locpilot.repos import RepoValidationError, register_local, register_remote
from locpilot.security.paths import PathSecurityError, is_within, safe_join
from locpilot.tools import git
from locpilot.tools.files import MAX_VIEW_BYTES, drop_index, get_index, monaco_language, sha256_text
from locpilot.tools.manifests import inspect_project
from locpilot.tools.search import SearchError, search

router = APIRouter(prefix="/api/repos")


def _repo_or_404(request: Request, repo_id: str) -> dict:
    repo = db_of(request).get_repo(repo_id)
    if repo is None:
        raise ApiError(404, "repo_not_found", "Repository not found.")
    if not Path(repo["path"]).is_dir():
        raise ApiError(410, "repo_missing", f"The repository folder {repo['path']} no longer exists.",
                       "Remove it from LocPilot and add it again.")
    return repo


def _detail(repo: dict) -> dict:
    root = Path(repo["path"])
    status = git.status(root)
    index = get_index(root)
    overview = inspect_project(index)
    return {**repo, "status": {
        "branch": status.branch, "head": status.head, "head_summary": status.head_summary,
        "detached": status.detached, "upstream": status.upstream, "ahead": status.ahead, "behind": status.behind,
        "dirty": status.dirty, "staged": status.staged, "modified": status.modified, "untracked": status.untracked,
        "conflicted": status.conflicted, "files": [{"path": f.path, "status": f.status} for f in status.files],
    }, "languages": overview.languages, "manifests": overview.manifests, "test_commands": overview.test_commands,
        "file_count": overview.file_count}


@router.get("")
async def list_repos(request: Request) -> list[dict]:
    return db_of(request).list_repos()


@router.post("", status_code=201)
async def add_repo(body: RepoCreate, request: Request) -> dict:
    db, settings = db_of(request), settings_of(request)
    try:
        if body.source == "local":
            repo = await asyncio.to_thread(register_local, db, settings, body.path or "")
        else:
            repo = await asyncio.to_thread(register_remote, db, settings, body.url or "")
    except RepoValidationError as exc:
        raise ApiError(400, "invalid_repository", str(exc), exc.hint) from None
    return await asyncio.to_thread(_detail, repo)


@router.get("/{repo_id}")
async def get_repo(repo_id: str, request: Request) -> dict:
    repo = _repo_or_404(request, repo_id)
    try:
        return await asyncio.to_thread(_detail, repo)
    except git.GitError as exc:
        raise ApiError(500, "git_error", str(exc)) from None


@router.delete("/{repo_id}")
async def delete_repo(repo_id: str, request: Request, delete_clone: bool = False) -> dict:
    db, settings = db_of(request), settings_of(request)
    repo = db.get_repo(repo_id)
    if repo is None:
        raise ApiError(404, "repo_not_found", "Repository not found.")
    await request.app.state.terminals.close_where(repo_id=repo_id)
    for task in db.list_tasks(repo_id, limit=10_000):
        await request.app.state.manager.stop_user_command(task["id"])
        if task.get("worktree_path") and Path(repo["path"]).exists():
            try:
                await asyncio.to_thread(git.worktree_remove, Path(repo["path"]), Path(task["worktree_path"]),
                                        managed_root=settings.worktrees_dir)
            except git.GitError:
                pass
    db.delete_repo(repo_id)
    drop_index(Path(repo["path"]))
    if delete_clone and repo["managed"] and is_within(settings.repos_dir, Path(repo["path"])):
        await asyncio.to_thread(shutil.rmtree, repo["path"], True)
    return {"ok": True}


def _root_for(request: Request, repo: dict, task_id: str | None) -> Path:
    if task_id:
        task = db_of(request).get_task(task_id)
        if task is None or task["repo_id"] != repo["id"]:
            raise ApiError(404, "task_not_found", "Task not found for this repository.")
        if task.get("worktree_path") and Path(task["worktree_path"]).is_dir():
            return Path(task["worktree_path"])
    return Path(repo["path"])


@router.get("/{repo_id}/tree")
async def tree(repo_id: str, request: Request, path: str = "", task_id: str | None = None) -> list[dict]:
    repo = _repo_or_404(request, repo_id)
    root = _root_for(request, repo, task_id)
    try:
        return await asyncio.to_thread(lambda: get_index(root).list_dir(path))
    except PathSecurityError as exc:
        raise ApiError(400, "invalid_path", str(exc)) from None


@router.get("/{repo_id}/file")
async def read_file(repo_id: str, request: Request, path: str, start: int | None = Query(None, ge=1),
                    end: int | None = Query(None, ge=1), task_id: str | None = None) -> dict:
    repo = _repo_or_404(request, repo_id)
    root = _root_for(request, repo, task_id)
    try:
        full = safe_join(root, path)
    except PathSecurityError as exc:
        raise ApiError(403, "path_forbidden", str(exc)) from None
    if not full.is_file():
        raise ApiError(404, "file_not_found", f"{path} does not exist.")
    raw = await asyncio.to_thread(full.read_bytes)
    rel = full.relative_to(root.resolve()).as_posix()
    if b"\x00" in raw[:8192]:
        return {"path": rel, "content": "", "start_line": 0, "end_line": 0, "total_lines": 0,
                "language": "plaintext", "truncated": False, "binary": True, "sha": ""}
    truncated = len(raw) > MAX_VIEW_BYTES
    text = raw[:MAX_VIEW_BYTES].decode("utf-8", errors="replace")
    lines = text.splitlines()
    s = start or 1
    e = min(end or len(lines), len(lines))
    content = "\n".join(lines[s - 1:e]) if (start or end) else text
    return {"path": rel, "content": content, "start_line": s if lines else 0, "end_line": e, "total_lines": len(lines),
            "language": monaco_language(rel), "truncated": truncated, "binary": False, "sha": sha256_text(text)}


@router.get("/{repo_id}/search")
async def search_repo(repo_id: str, request: Request, q: str = Query(min_length=1, max_length=300),
                      regex: bool = False, task_id: str | None = None) -> list[dict]:
    repo = _repo_or_404(request, repo_id)
    root = _root_for(request, repo, task_id)
    try:
        result = await asyncio.to_thread(lambda: search(get_index(root), q, is_regex=regex, max_results=200))
    except SearchError as exc:
        raise ApiError(400, "invalid_query", str(exc)) from None
    return [{"path": h.path, "line": h.line, "text": h.text} for h in result.hits]
