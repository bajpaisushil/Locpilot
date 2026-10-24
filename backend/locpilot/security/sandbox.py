"""Best-effort OS sandbox for approved verification commands.

* macOS: ``sandbox-exec`` with a generated Seatbelt profile — no network egress
  (IP or DNS), writes confined to the task worktree + scratch/temp dirs, and no
  reads of credential stores or LocPilot's own data directory.
* Linux: ``bwrap`` (bubblewrap) when installed and user namespaces work — read-only
  root, writable worktree/scratch, no network namespace access.
* Windows / unavailable: commands run unsandboxed (after explicit approval) and
  the UI says so.

This is defence in depth, not a security guarantee. A Git worktree is *not* a
sandbox: it only isolates LocPilot's edits from the user's working tree.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SECRET_HOME_DIRS = [".ssh", ".aws", ".gnupg", ".config/gh", ".docker", ".kube", ".azure", ".config/gcloud",
                     "Library/Keychains", ".password-store"]
_SECRET_HOME_FILES = [".netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass"]


@dataclass(frozen=True)
class SandboxInfo:
    kind: str  # "seatbelt" | "bwrap" | "none"
    available: bool
    description: str


def _sb_quote(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


@lru_cache
def detect_sandbox() -> SandboxInfo:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        try:
            probe = subprocess.run(
                ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"],
                capture_output=True, timeout=10,
            )
            if probe.returncode == 0:
                return SandboxInfo("seatbelt", True,
                                   "macOS Seatbelt: no network, writes limited to the task worktree, credential stores hidden")
        except (OSError, subprocess.SubprocessError):
            pass
    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap:
            try:
                probe = subprocess.run([bwrap, "--ro-bind", "/", "/", "--unshare-net", "true"],
                                       capture_output=True, timeout=10)
                if probe.returncode == 0:
                    return SandboxInfo("bwrap", True,
                                       "bubblewrap: no network, read-only system, writable task worktree only")
            except (OSError, subprocess.SubprocessError):
                pass
        return SandboxInfo("none", False, "Install bubblewrap (bwrap) to sandbox test commands on Linux")
    if sys.platform.startswith("win"):
        return SandboxInfo("none", False, "No command sandbox on Windows; approved commands run with your user rights")
    return SandboxInfo("none", False, "No supported sandbox found; approved commands run with your user rights")


def seatbelt_profile(workspace: Path, scratch: Path, data_dir: Path, allow_localhost: bool) -> str:
    home = Path.home().resolve()
    ws = workspace.resolve()
    sc = scratch.resolve()
    dd = data_dir.resolve()
    deny_reads = [f"(subpath {_sb_quote(home / d)})" for d in _SECRET_HOME_DIRS]
    deny_reads += [f"(literal {_sb_quote(home / f)})" for f in _SECRET_HOME_FILES]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network-outbound (remote ip))",
    ]
    if allow_localhost:
        lines.append('(allow network-outbound (remote ip "localhost:*"))')
    lines += [
        '(deny network-outbound (remote unix-socket (subpath "/private/var/run")))',
        '(deny network-outbound (remote unix-socket (subpath "/var/run")))',
        f"(deny file-read* {' '.join(deny_reads)})",
        # LocPilot's data dir (other tasks, DB, clones): no file contents or listings, but path metadata stays
        # readable so runtimes can lstat()/realpath() the worktree's parent directories (Node does this).
        f"(deny file-read-data (subpath {_sb_quote(dd)}))",
        # Operation-specific rules outrank file-read*, so re-allow file-read-data explicitly for the task dirs.
        f"(allow file-read-data (subpath {_sb_quote(ws)}) (subpath {_sb_quote(sc)}))",
        f"(allow file-read* (subpath {_sb_quote(ws)}) (subpath {_sb_quote(sc)}))",
        "(deny file-write*)",
        "(allow file-write*"
        f" (subpath {_sb_quote(ws)}) (subpath {_sb_quote(sc)})"
        ' (subpath "/private/tmp") (subpath "/private/var/folders")'
        ' (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/dtracehelper")'
        ' (regex #"^/dev/fd/") (regex #"^/dev/ttys[0-9]+$"))',
    ]
    return "\n".join(lines) + "\n"


def wrap_argv(
    argv: list[str],
    *,
    workspace: Path,
    scratch: Path,
    data_dir: Path,
    allow_localhost: bool = False,
    enabled: bool = True,
) -> tuple[list[str], str]:
    """Return (argv to execute, sandbox kind used)."""
    info = detect_sandbox()
    if not enabled or not info.available:
        return argv, "none"
    scratch.mkdir(parents=True, exist_ok=True)
    if info.kind == "seatbelt":
        profile = scratch / "sandbox.sb"
        profile.write_text(seatbelt_profile(workspace, scratch, data_dir, allow_localhost))
        return ["/usr/bin/sandbox-exec", "-f", str(profile), *argv], "seatbelt"
    if info.kind == "bwrap":
        bwrap = shutil.which("bwrap") or "bwrap"
        home = Path.home()
        wrapped = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                   "--bind", str(workspace), str(workspace), "--bind", str(scratch), str(scratch),
                   "--tmpfs", str(data_dir)]
        # Re-expose the worktree/scratch after hiding the data dir.
        wrapped += ["--bind", str(workspace), str(workspace), "--bind", str(scratch), str(scratch)]
        for d in _SECRET_HOME_DIRS:
            if (home / d).exists():
                wrapped += ["--tmpfs", str(home / d)]
        if not allow_localhost:
            wrapped.append("--unshare-net")
        wrapped += ["--unshare-pid", "--die-with-parent", "--new-session", "--chdir", str(workspace), "--", *argv]
        return wrapped, "bwrap"
    return argv, "none"
