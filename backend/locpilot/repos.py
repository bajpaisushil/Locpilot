"""Repository registration: validation of local checkouts and cloning of remote URLs."""

from __future__ import annotations

import os
import re
from pathlib import Path

from locpilot.config import Settings
from locpilot.security.paths import is_protected_system_dir, is_within
from locpilot.storage.db import Database
from locpilot.tools import git


class RepoValidationError(ValueError):
    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def validate_local_path(raw: str, settings: Settings) -> Path:
    """Validate a user-supplied local repository path and return the Git top-level directory."""
    if not isinstance(raw, str) or not raw.strip():
        raise RepoValidationError("Enter the absolute path of a local Git repository.")
    if "\x00" in raw or len(raw) > 4096:
        raise RepoValidationError("Invalid path.")
    expanded = Path(os.path.expanduser(raw.strip()))
    if not expanded.is_absolute():
        raise RepoValidationError("The path must be absolute (for example /Users/you/code/project).")
    try:
        resolved = expanded.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise RepoValidationError(f"{expanded} does not exist.") from None
    if not resolved.is_dir():
        raise RepoValidationError(f"{resolved} is not a directory.")
    if not any(is_within(root, resolved) for root in settings.allowed_repo_roots):
        roots = ", ".join(str(r) for r in settings.allowed_repo_roots)
        raise RepoValidationError(f"{resolved} is outside the allowed locations ({roots}).",
                                  "Set LOCPILOT_ALLOWED_REPO_ROOTS to add more locations.")
    if is_within(settings.data_dir, resolved):
        raise RepoValidationError("LocPilot's own workspace cannot be registered as a repository.")
    if is_protected_system_dir(resolved):
        raise RepoValidationError(f"{resolved} is a protected location and cannot be used as a repository.")
    if git.git_executable() is None:
        raise RepoValidationError("Git is not installed.", "Install Git and try again.")
    try:
        top = git.toplevel(resolved)
    except git.GitError:
        raise RepoValidationError(f"{resolved} is not inside a Git repository.",
                                  "Run `git init` there first, or choose a different folder.") from None
    if git.is_bare(top):
        raise RepoValidationError("Bare repositories are not supported; choose a checkout with a working tree.")
    if is_protected_system_dir(top) or not any(is_within(root, top) for root in settings.allowed_repo_roots):
        raise RepoValidationError(f"The repository root {top} is not an allowed location.")
    return top


def register_local(db: Database, settings: Settings, raw_path: str) -> dict:
    top = validate_local_path(raw_path, settings)
    return db.add_repo(name=top.name, source_type="local", source_url=git.remote_url(top), path=str(top), managed=False)


def _clone_dir(settings: Settings, parsed: git.ParsedRemote) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{parsed.host}__{parsed.owner}__{parsed.repo}")
    return settings.repos_dir / safe


def register_remote(db: Database, settings: Settings, raw_url: str) -> dict:
    try:
        parsed = git.parse_remote_url(raw_url, settings.allowed_git_hosts)
    except ValueError as exc:
        raise RepoValidationError(str(exc)) from None
    existing = db.find_repo_by_url(parsed.url)
    if existing and Path(existing["path"]).exists():
        db.touch_repo(existing["id"])
        return db.get_repo(existing["id"])  # type: ignore[return-value]
    dest = _clone_dir(settings, parsed)
    if dest.exists():
        if (dest / ".git").exists():
            return db.add_repo(name=parsed.repo, source_type="github", source_url=parsed.url, path=str(dest.resolve()),
                               managed=True)
        raise RepoValidationError(f"{dest} already exists and is not a Git repository.")
    if git.git_executable() is None:
        raise RepoValidationError("Git is not installed.", "Install Git and try again.")
    try:
        git.clone(parsed.url, dest, timeout=settings.clone_timeout_s, hooks_dir=settings.empty_hooks_dir)
    except git.GitError as exc:
        message = str(exc)
        hint = None
        if "Authentication failed" in message or "could not read Username" in message or "Repository not found" in message:
            hint = ("Private repository? LocPilot never stores tokens. Authenticate with your own Git credential "
                    "helper (e.g. `gh auth setup-git` or the macOS keychain) or use an SSH URL with your SSH agent.")
        raise RepoValidationError(f"Clone failed: {message}", hint) from None
    return db.add_repo(name=parsed.repo, source_type="github", source_url=parsed.url, path=str(dest.resolve()),
                       managed=True)
