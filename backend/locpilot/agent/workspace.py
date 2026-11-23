"""Isolated task worktrees, approved verification commands and the user's own commands."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from locpilot.config import Settings
from locpilot.security.commands import validate_command
from locpilot.security.sandbox import wrap_argv
from locpilot.storage.db import Database
from locpilot.tools import git
from locpilot.tools.files import drop_index
from locpilot.tools.process import base_env, run_async
from locpilot.tools.symbols import forget_symbols
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
    linked = _link_node_modules(repo_path, dest)
    if linked is True:
        notices.append("Linked node_modules from your checkout so JavaScript tests can run without installing "
                       "dependencies.")
    elif linked is False:
        notices.append("Could not link node_modules into the worktree; JavaScript tests may fail to run.")
    return {"worktree_path": str(dest), "base_commit": base, "notices": notices}


def _link_node_modules(repo_path: Path, dest: Path) -> bool | None:
    """Link the user's node_modules into a worktree: True if linked, False if that failed, None if not needed."""
    node_modules = repo_path / "node_modules"
    if not node_modules.is_dir() or (dest / "node_modules").exists():
        return None
    try:
        os.symlink(node_modules, dest / "node_modules", target_is_directory=True)
        return True
    except OSError:
        return False


# --- Freeing disk space: archive a finished task's worktree, restore it on next use ----------------------
#
# A worktree is a full checkout, so finished tasks can add up to gigabytes. Archiving keeps exactly what matters —
# the worktree's HEAD commit plus a patch of every change in it (edits, deletions, new files) — in a small file,
# then removes the folder. Anything that needs the worktree again (a follow-up message, the file viewer, a
# command, a terminal, commit/apply) restores it first, so nothing is lost. Ignored files (build output, caches)
# are not kept: they can be regenerated.

ARCHIVE_VERSION = 1
_restore_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


def archive_path(settings: Settings, task_id: str) -> Path:
    return settings.data_dir / "archived-worktrees" / f"{task_id}.json"


def worktree_is_archived(settings: Settings, task: dict) -> bool:
    wt = task.get("worktree_path")
    return bool(wt) and not (Path(wt) / ".git").exists() and archive_path(settings, task["id"]).is_file()


def directory_size(path: Path) -> int:
    """Bytes used by the files under ``path``; links (such as the node_modules link) are not followed."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def archive_worktree(settings: Settings, repo_path: Path, task: dict) -> int:
    """Save a task worktree's state to a small file and remove the folder. Returns the bytes freed."""
    wt = Path(task["worktree_path"])
    if not (wt / ".git").exists():
        raise WorkspaceError("This task's worktree does not exist.")
    if not wt.resolve().is_relative_to(settings.worktrees_dir.resolve()):
        raise WorkspaceError("Refusing to archive a folder outside LocPilot's worktrees.")
    head = git.head_commit(wt)
    if not head:
        raise WorkspaceError("Could not read the worktree's commit.")
    if not git.commit_is_kept(repo_path, head):
        raise WorkspaceError("The worktree has commits that are on no branch; commit them to a branch first.")
    freed = directory_size(wt)
    diff = git.worktree_snapshot_diff(wt)
    target = archive_path(settings, task["id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": ARCHIVE_VERSION, "task_id": task["id"], "head": head, "diff": diff,
                               "archived_at": time.time()}), encoding="utf-8")
    tmp.replace(target)  # the archive is complete before anything is removed
    git.worktree_remove(repo_path, wt, managed_root=settings.worktrees_dir)
    drop_index(wt)
    forget_symbols(wt)
    scratch = settings.scratch_dir / task["id"]  # caches of the task's commands (npm, go build, tmp)
    if scratch.is_dir() and scratch.resolve().is_relative_to(settings.scratch_dir.resolve()):
        freed += directory_size(scratch)
        shutil.rmtree(scratch, ignore_errors=True)
    return freed


def restore_worktree(settings: Settings, repo_path: Path, task: dict) -> bool:
    """Recreate an archived worktree exactly as it was. Returns True if it was restored."""
    with _restore_locks[task["id"]]:
        if not worktree_is_archived(settings, task):
            return False
        source = archive_path(settings, task["id"])
        data = json.loads(source.read_text(encoding="utf-8"))
        wt = Path(task["worktree_path"])
        head = data["head"]
        if not git.has_commit(repo_path, head):
            raise WorkspaceError(f"The commit this task's worktree was based on ({head[:10]}) is no longer in the "
                                 f"repository. Its saved changes are in {source}.")
        if wt.exists():  # an empty or broken leftover folder
            shutil.rmtree(wt, ignore_errors=True)
        git.worktree_add(repo_path, wt, head, hooks_dir=settings.empty_hooks_dir)
        if data.get("diff", "").strip():
            patch = settings.scratch_dir / task["id"] / "restore.patch"
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text(data["diff"], encoding="utf-8")
            try:
                git.apply_snapshot_diff(wt, patch)
            except git.GitError as exc:
                git.worktree_remove(repo_path, wt, managed_root=settings.worktrees_dir)
                raise WorkspaceError(f"Could not restore this task's worktree: {exc}. Its saved changes are in "
                                     f"{source}.") from exc
            finally:
                patch.unlink(missing_ok=True)
        _link_node_modules(repo_path, wt)
        source.unlink()
        drop_index(wt)
        return True


async def ensure_task_worktree(settings: Settings, db: Database, task: dict | None) -> None:
    """Before anything uses a task's worktree: restore it if it was archived to free space."""
    if not task or not worktree_is_archived(settings, task):
        return
    repo = db.get_repo(task["repo_id"])
    if repo is None:
        raise WorkspaceError("The task's repository is no longer connected.")
    await asyncio.to_thread(restore_worktree, settings, Path(repo["path"]), task)


def command_env(settings: Settings, task_id: str, workspace: Path, env: dict[str, str]) -> dict[str, str]:
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
    extra.update(env)
    return base_env(extra)


SANDBOX_HINTS = ("Operation not permitted", "nodename nor servname", "Temporary failure in name resolution",
                 "Network is unreachable", "getaddrinfo")


@dataclass
class PreparedCommand:
    record: dict
    argv: list[str]
    env: dict[str, str]
    sandbox: str
    timeout: int


def _prepare(settings: Settings, db: Database, *, task_id: str, turn: int, stage: str, display_argv: list[str],
             argv: list[str], env: dict[str, str], workspace: Path, timeout: int,
             allow_network: bool = False) -> PreparedCommand:
    wrapped, sandbox_kind = wrap_argv(
        argv, workspace=workspace, scratch=settings.scratch_dir / task_id, data_dir=settings.data_dir,
        allow_localhost=settings.sandbox_allow_localhost, allow_network=allow_network,
        enabled=settings.sandbox != "off",
    )
    record = db.start_command(task_id, turn=turn, stage=stage, argv=display_argv, cwd=str(workspace),
                              sandbox=sandbox_kind)
    return PreparedCommand(record=record, argv=wrapped, env=env, sandbox=sandbox_kind, timeout=timeout)


async def execute_command(settings: Settings, db: Database, emit: Emit, prepared: PreparedCommand, *,
                          stop: asyncio.Event | None = None) -> dict:
    """Run a prepared command with streamed output and persist the finished record."""
    record = prepared.record
    await emit("command_started", {"command": record})

    async def on_output(stream: str, text: str) -> None:
        await emit("command_output", {"command_id": record["id"], "stream": stream, "text": text[-4000:]},
                   persist=False)

    result = await run_async(prepared.argv, record["cwd"], timeout=prepared.timeout, env=prepared.env,
                             max_output_bytes=settings.max_output_bytes, on_output=on_output,
                             sandbox=prepared.sandbox, stop=stop)
    summary = parse_test_output(result.stdout, result.stderr, result.exit_code).to_dict()
    if result.timed_out:
        status = "timed_out"
        summary["outcome"] = "error"
    elif result.cancelled:
        status = "cancelled"
        summary["outcome"] = "error"
    elif result.error:
        status = "error"
        summary["outcome"] = "error"
    else:
        status = "completed"
    hints = []
    if result.timed_out:
        hints.append(f"Command exceeded the {prepared.timeout}s timeout and was killed.")
    if prepared.sandbox != "none" and any(h in (result.stdout + result.stderr) for h in SANDBOX_HINTS):
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


async def run_verification_command(
    *, settings: Settings, db: Database, emit: Emit, task_id: str, turn: int, stage: str, argv: list[str],
    workspace: Path, origin_repo: Path,
) -> dict:
    """Run one approved command (re-validated at execution time) and persist the record."""
    validated = validate_command(list(argv), workspace, origin_repo)
    env = command_env(settings, task_id, workspace, validated.env)
    prepared = _prepare(settings, db, task_id=task_id, turn=turn, stage=stage, display_argv=list(argv),
                        argv=validated.argv, env=env, workspace=workspace, timeout=settings.command_timeout_s)
    return await execute_command(settings, db, emit, prepared)


USER_STAGE = "user"


def project_tool_env(workspace: Path, origin_repo: Path) -> dict[str, str]:
    """Put project-local tools first on PATH, like an editor terminal with the virtualenv activated.

    Ignored directories (``.venv``, ``node_modules``) only exist in the user's checkout, so they
    are looked up there as well as in the worktree.
    """
    bin_name = "Scripts" if sys.platform.startswith("win") else "bin"
    dirs: list[str] = []
    venv: Path | None = None
    for base in (workspace, origin_repo):
        for name in (".venv", "venv"):
            candidate = base / name
            if venv is None and (candidate / "pyvenv.cfg").is_file():
                venv = candidate
                dirs.append(str(candidate / bin_name))
        node_bin = base / "node_modules" / ".bin"
        if node_bin.is_dir():
            dirs.append(str(node_bin))
    env = {"PATH": os.pathsep.join([*dict.fromkeys(dirs), os.environ.get("PATH", "")])}
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
    return env


def user_shell_argv(command: str) -> list[str]:
    if sys.platform.startswith("win"):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return ["/bin/sh", "-c", command]


def prepare_user_command(settings: Settings, db: Database, *, task_id: str, turn: int, command: str,
                         workspace: Path, origin_repo: Path, allow_network: bool) -> PreparedCommand:
    """A command the user typed, run by the platform shell in the task worktree.

    Unlike verification commands it is not allow-listed: only the user can start one (never
    the agent). It still gets the sandbox (writes confined to the worktree, network off unless
    the user allows it), the scrubbed environment, a timeout and a Stop button.
    """
    env = command_env(settings, task_id, workspace, project_tool_env(workspace, origin_repo))
    env.pop("CI", None)  # behave like the user's terminal (CI=true changes how many tools build and run)
    return _prepare(settings, db, task_id=task_id, turn=turn, stage=USER_STAGE, display_argv=[command],
                    argv=user_shell_argv(command), env=env, workspace=workspace,
                    timeout=settings.user_command_timeout_s, allow_network=allow_network)
