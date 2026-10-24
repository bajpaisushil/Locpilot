"""Isolated task worktrees and approved verification command execution."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from locpilot.config import Settings
from locpilot.security.commands import ValidatedCommand, validate_command
from locpilot.security.sandbox import wrap_argv
from locpilot.storage.db import Database
from locpilot.tools import git
from locpilot.tools.process import base_env, run_async
from locpilot.tools.testparse import parse_test_output

Emit = Callable[..., Awaitable[None]]


class WorkspaceError(RuntimeError):
    pass


def prepare_worktree(settings: Settings, repo_path: Path, task_id: str) -> dict:
    """Create (or reuse) a detached Git worktree for a Code-mode task.

    The user's working tree, index, branch and stash are never touched. Their
    uncommitted changes are not copied into the worktree.
    """
    dest = settings.worktrees_dir / task_id
    if (dest / ".git").exists():
        base = git.head_commit(dest)
        return {"worktree_path": str(dest), "base_commit": base, "notices": []}
    base = git.head_commit(repo_path)
    if not base:
        raise WorkspaceError("The repository has no commits yet; Code mode needs a base commit for its worktree.")
    git.worktree_add(repo_path, dest, base, hooks_dir=settings.empty_hooks_dir)
    notices = [f"Created an isolated Git worktree at {dest} from HEAD {base[:10]}. "
               "LocPilot edits only this worktree; your working tree is not modified."]
    try:
        status = git.status(repo_path)
        if status.dirty:
            notices.append(
                f"Your working tree has uncommitted changes ({status.modified} modified, {status.untracked} untracked, "
                f"{status.staged} staged). They are not included in LocPilot's worktree and remain untouched."
            )
    except git.GitError:
        pass
    node_modules = repo_path / "node_modules"
    if node_modules.is_dir() and not (dest / "node_modules").exists():
        try:
            os.symlink(node_modules, dest / "node_modules", target_is_directory=True)
            notices.append("Linked node_modules from your checkout so JavaScript tests can run without installing "
                           "dependencies.")
        except OSError:
            notices.append("Could not link node_modules into the worktree; JavaScript tests may fail to run.")
    return {"worktree_path": str(dest), "base_commit": base, "notices": notices}


def command_env(settings: Settings, task_id: str, workspace: Path, validated: ValidatedCommand) -> dict[str, str]:
    scratch = settings.scratch_dir / task_id
    for sub in ("tmp", "cache", "npm", "go-build"):
        (scratch / sub).mkdir(parents=True, exist_ok=True)
    extra = {
        "TMPDIR": str(scratch / "tmp"), "TEMP": str(scratch / "tmp"), "TMP": str(scratch / "tmp"),
        "XDG_CACHE_HOME": str(scratch / "cache"), "npm_config_cache": str(scratch / "npm"),
        "GOCACHE": str(scratch / "go-build"), "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    py_paths = [str(workspace / "src")] if (workspace / "src").is_dir() else []
    py_paths.append(str(workspace))
    extra["PYTHONPATH"] = os.pathsep.join(py_paths)
    extra.update(validated.env)
    return base_env(extra)


SANDBOX_HINTS = ("Operation not permitted", "nodename nor servname", "Temporary failure in name resolution",
                 "Network is unreachable", "getaddrinfo")


async def run_verification_command(
    *, settings: Settings, db: Database, emit: Emit, task_id: str, turn: int, stage: str, argv: list[str],
    workspace: Path, origin_repo: Path,
) -> dict:
    """Run one approved command (re-validated at execution time) and persist the record."""
    validated = validate_command(list(argv), workspace, origin_repo)
    env = command_env(settings, task_id, workspace, validated)
    wrapped, sandbox_kind = wrap_argv(
        validated.argv, workspace=workspace, scratch=settings.scratch_dir / task_id, data_dir=settings.data_dir,
        allow_localhost=settings.sandbox_allow_localhost, enabled=settings.sandbox != "off",
    )
    record = db.start_command(task_id, turn=turn, stage=stage, argv=list(argv), cwd=str(workspace), sandbox=sandbox_kind)
    await emit("command_started", {"command": record})

    async def on_output(stream: str, text: str) -> None:
        await emit("command_output", {"command_id": record["id"], "stream": stream, "text": text[-4000:]},
                   persist=False)

    result = await run_async(wrapped, workspace, timeout=settings.command_timeout_s, env=env,
                             max_output_bytes=settings.max_output_bytes, on_output=on_output, sandbox=sandbox_kind)
    summary = parse_test_output(result.stdout, result.stderr, result.exit_code).to_dict()
    if result.timed_out:
        status = "timed_out"
        summary["outcome"] = "error"
    elif result.error:
        status = "error"
        summary["outcome"] = "error"
    else:
        status = "completed"
    hints = []
    if result.timed_out:
        hints.append(f"Command exceeded the {settings.command_timeout_s}s timeout and was killed.")
    if sandbox_kind != "none" and any(h in (result.stdout + result.stderr) for h in SANDBOX_HINTS):
        hints.append("The sandbox may have blocked network access or a write outside the worktree.")
    summary["hints"] = hints
    row = db.finish_command(record["id"], status=status, exit_code=result.exit_code,
                            stdout=result.stdout, stderr=result.stderr or (result.error or ""),
                            duration_ms=result.duration_ms, timed_out=result.timed_out, truncated=result.truncated,
                            summary=summary)
    event_row = dict(row)
    event_row["stdout"] = (row["stdout"] or "")[-4000:]
    event_row["stderr"] = (row["stderr"] or "")[-4000:]
    await emit("command_finished", {"command": event_row})
    return row
