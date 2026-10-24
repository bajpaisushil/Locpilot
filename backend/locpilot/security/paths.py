"""Repository-root confinement for every file operation.

All file access performed on behalf of the UI or the agent goes through
:func:`safe_join`, which rejects absolute paths, parent traversal, NUL bytes,
Windows drive/UNC/stream syntax, ``.git`` internals and symlinks that resolve
outside the repository root.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PurePosixPath

from locpilot.security.secrets import is_secret_path

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PathSecurityError(ValueError):
    """Raised when a path would escape its allowed root or touch protected files."""


def normalize_relpath(rel: str) -> str:
    """Validate a user/model supplied repository-relative path and return it in POSIX form."""
    if not isinstance(rel, str):
        raise PathSecurityError("path must be a string")
    rel = rel.strip()
    if not rel:
        raise PathSecurityError("path is empty")
    if "\x00" in rel or any(ord(ch) < 32 for ch in rel):
        raise PathSecurityError("path contains control characters")
    if len(rel) > 1024:
        raise PathSecurityError("path is too long")
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or rel.startswith("//") or _DRIVE_RE.match(rel):
        raise PathSecurityError("absolute paths are not allowed")
    if rel.startswith("~"):
        raise PathSecurityError("home-relative paths are not allowed")
    parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".")]
    if not parts:
        raise PathSecurityError("path is empty")
    for part in parts:
        if part == "..":
            raise PathSecurityError("parent directory traversal is not allowed")
        if sys.platform.startswith("win"):
            if ":" in part:
                raise PathSecurityError("alternate data streams are not allowed")
            if part.split(".")[0].lower() in _WINDOWS_RESERVED:
                raise PathSecurityError("reserved device names are not allowed")
    if parts[0].lower() == ".git":
        raise PathSecurityError(".git internals are not accessible")
    return "/".join(parts)


def safe_join(root: Path, rel: str, *, allow_secret: bool = False) -> Path:
    """Resolve ``rel`` inside ``root`` or raise :class:`PathSecurityError`.

    Symlinks are resolved, so a link inside the repository that points outside
    of it is rejected. The returned path may not exist yet (for new files).
    """
    clean = normalize_relpath(rel)
    root_resolved = Path(root).resolve(strict=True)
    candidate = (root_resolved / clean).resolve(strict=False)
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise PathSecurityError("path escapes the repository root")
    inner = candidate.relative_to(root_resolved).as_posix()
    if inner.split("/")[0].lower() == ".git":
        raise PathSecurityError(".git internals are not accessible")
    if not allow_secret and (is_secret_path(clean) or is_secret_path(inner)):
        raise PathSecurityError("access to secret/credential files is blocked")
    return candidate


def relative_to_root(root: Path, path: Path) -> str:
    return path.resolve().relative_to(Path(root).resolve()).as_posix()


def is_within(root: Path, path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def is_protected_system_dir(path: Path) -> bool:
    """Directories that must never be registered as a repository."""
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved == home or resolved == Path(resolved.anchor):
        return True
    sensitive = [
        home / ".ssh", home / ".aws", home / ".gnupg", home / ".config",
        home / ".kube", home / ".docker", home / "Library",
    ]
    for item in sensitive:
        if resolved == item or is_within(item, resolved):
            return True
    if os.name == "posix":
        for system in ("/etc", "/bin", "/sbin", "/usr", "/System", "/private/etc", "/var", "/dev", "/proc"):
            if str(resolved) == system or str(resolved).startswith(system + "/"):
                return True
    return False
