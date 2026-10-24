"""Interactive terminals for the user (never the agent): open, stream output over SSE, type, resize, close."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from locpilot.agent.workspace import project_tool_env
from locpilot.api.models import ApiError, db_of
from locpilot.tools.terminal import TerminalError, TerminalManager, TerminalSession

router = APIRouter(prefix="/api/terminals")


class TerminalCreate(BaseModel):
    repo_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(None, max_length=64)
    cols: int = Field(80, ge=10, le=500)
    rows: int = Field(24, ge=4, le=300)


class TerminalInput(BaseModel):
    data: str = Field(min_length=1, max_length=65_536)


class TerminalResize(BaseModel):
    cols: int = Field(ge=10, le=500)
    rows: int = Field(ge=4, le=300)


def terminals_of(request: Request) -> TerminalManager:
    return request.app.state.terminals


def _session_or_404(request: Request, terminal_id: str) -> TerminalSession:
    session = terminals_of(request).get(terminal_id)
    if session is None:
        raise ApiError(404, "terminal_not_found", "This terminal is closed.")
    return session


def _activate_command(env: dict[str, str]) -> str:
    """Like VS Code: activate the project's virtualenv in a new bash/zsh terminal."""
    venv = env.get("VIRTUAL_ENV")
    if not venv or not os.path.isfile(os.path.join(venv, "bin", "activate")):
        return ""
    return f"source {shlex.quote(os.path.join(venv, 'bin', 'activate'))}\n"


@router.get("")
async def list_terminals(request: Request, repo_id: str | None = None, task_id: str | None = None) -> list[dict]:
    return [s.view() for s in terminals_of(request).list(repo_id=repo_id, task_id=task_id)]


@router.post("", status_code=201)
async def open_terminal(body: TerminalCreate, request: Request) -> dict:
    """A shell in the task's worktree (LocPilot's version of the code), or in the repository folder."""
    db = db_of(request)
    repo = db.get_repo(body.repo_id)
    if repo is None:
        raise ApiError(404, "repo_not_found", "Repository not found.")
    repo_path = Path(repo["path"])
    cwd, location, title, extra = repo_path, "repository", repo["name"], {}
    if body.task_id:
        task = db.get_task(body.task_id)
        if task is None or task["repo_id"] != repo["id"]:
            raise ApiError(404, "task_not_found", "Task not found for this repository.")
        worktree = task.get("worktree_path")
        if worktree and Path(worktree).is_dir():
            cwd, location, title = Path(worktree), "worktree", f"{repo['name']} · {task['title'][:40]}"
            src = [str(cwd / "src")] if (cwd / "src").is_dir() else []
            extra["PYTHONPATH"] = os.pathsep.join([*src, str(cwd)])
    tools = project_tool_env(cwd, repo_path)
    if "VIRTUAL_ENV" in tools:
        extra["VIRTUAL_ENV"] = tools["VIRTUAL_ENV"]
    try:
        session = terminals_of(request).open(cwd=cwd, title=title, location=location, repo_id=repo["id"],
                                             task_id=body.task_id if location == "worktree" else None,
                                             env_extra=extra, cols=body.cols, rows=body.rows,
                                             startup_input=_activate_command(extra))
    except TerminalError as exc:
        raise ApiError(409, "terminal_unavailable", str(exc)) from None
    return session.view()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/{terminal_id}/stream")
async def stream_terminal(terminal_id: str, request: Request) -> StreamingResponse:
    """Scrollback first (as one `reset` event), then live `output` and a final `exit` event."""
    session = _session_or_404(request, terminal_id)
    scrollback, queue = session.subscribe()

    async def stream():
        try:
            yield _sse("reset", {"data": scrollback})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                yield _sse(event["type"], event)
        finally:
            session.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@router.post("/{terminal_id}/input", status_code=204)
async def terminal_input(terminal_id: str, body: TerminalInput, request: Request) -> None:
    try:
        await _session_or_404(request, terminal_id).write(body.data)
    except TerminalError as exc:
        raise ApiError(409, "terminal_exited", str(exc)) from None


@router.post("/{terminal_id}/resize", status_code=204)
async def terminal_resize(terminal_id: str, body: TerminalResize, request: Request) -> None:
    _session_or_404(request, terminal_id).resize(body.cols, body.rows)


@router.delete("/{terminal_id}")
async def close_terminal(terminal_id: str, request: Request) -> dict:
    if not await terminals_of(request).close(terminal_id):
        raise ApiError(404, "terminal_not_found", "This terminal is closed.")
    return {"ok": True}
