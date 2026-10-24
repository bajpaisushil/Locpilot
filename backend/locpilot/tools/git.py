"""Git CLI wrapper.

Read operations run with ``GIT_OPTIONAL_LOCKS=0`` so they never take the index
lock or rewrite the user's index. LocPilot's own write operations are
``clone`` (into its own workspace, on explicit user request) and
``worktree add/remove`` for its own isolated task worktrees. Hooks are disabled
for all of them. LocPilot never commits, pushes, stashes, resets or checks out in the
user's working tree.

Three more writes happen only when the user clicks for them: committing a task
worktree's changes to a new branch (the worktree stays detached, so the branch
remains free to check out), pushing that branch with the user's own Git
credentials, and applying the changes to the user's working tree as plain file
edits (never their index, HEAD or branches).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

from locpilot.tools.process import ProcessResult, base_env, run_sync


class GitError(RuntimeError):
    pass


def git_executable() -> str | None:
    return shutil.which("git")


def git_version() -> str | None:
    exe = git_executable()
    if not exe:
        return None
    result = run_sync([exe, "--version"], Path.cwd(), timeout=10)
    return result.stdout.strip() if result.ok else None


def _git(args: list[str], cwd: Path, *, timeout: float = 60, check: bool = True,
         extra_env: dict[str, str] | None = None) -> ProcessResult:
    exe = git_executable()
    if not exe:
        raise GitError("Git is not installed or not on PATH")
    env = base_env({"GIT_LFS_SKIP_SMUDGE": "1", "LC_ALL": "C", **(extra_env or {})})
    result = run_sync([exe, "-c", "core.quotepath=off", *args], cwd, timeout=timeout, env=env)
    if check and not result.ok:
        message = (result.stderr or result.stdout or result.error or "git failed").strip()
        if result.timed_out:
            message = f"git {args[0]} timed out after {timeout:.0f}s"
        raise GitError(sanitize_git_message(message))
    return result


def sanitize_git_message(message: str) -> str:
    """Strip credentials from URLs that git may echo back."""
    return re.sub(r"(://)[^/\s@]+@", r"\1***@", message)[:2000]


def sanitize_remote_url(url: str | None) -> str | None:
    if not url:
        return None
    return re.sub(r"(://)[^/\s@]+@", r"\1", url.strip())


def toplevel(path: Path) -> Path:
    result = _git(["rev-parse", "--show-toplevel"], path, timeout=15, check=False)
    if not result.ok:
        raise GitError("not a Git repository (or Git is unavailable)")
    return Path(result.stdout.strip()).resolve()


def is_bare(path: Path) -> bool:
    result = _git(["rev-parse", "--is-bare-repository"], path, timeout=15, check=False)
    return result.ok and result.stdout.strip() == "true"


def head_commit(path: Path) -> str | None:
    result = _git(["rev-parse", "--verify", "-q", "HEAD"], path, timeout=15, check=False)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def remote_url(path: Path) -> str | None:
    result = _git(["config", "--get", "remote.origin.url"], path, timeout=15, check=False)
    return sanitize_remote_url(result.stdout.strip()) if result.ok else None


@dataclass
class StatusFile:
    path: str
    status: str


@dataclass
class RepoStatusInfo:
    branch: str | None = None
    head: str | None = None
    head_summary: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: int = 0
    modified: int = 0
    untracked: int = 0
    conflicted: int = 0
    files: list[StatusFile] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.modified or self.untracked or self.conflicted)


def status(path: Path, limit: int = 200) -> RepoStatusInfo:
    result = _git(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal"], path, timeout=60)
    info = RepoStatusInfo()
    entries = result.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        if entry.startswith("# branch.oid "):
            oid = entry.split(" ", 2)[2]
            info.head = None if oid == "(initial)" else oid
        elif entry.startswith("# branch.head "):
            head = entry.split(" ", 2)[2]
            info.detached = head == "(detached)"
            info.branch = None if info.detached else head
        elif entry.startswith("# branch.upstream "):
            info.upstream = entry.split(" ", 2)[2]
        elif entry.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", entry)
            if m:
                info.ahead, info.behind = int(m.group(1)), int(m.group(2))
        elif entry.startswith("1 ") or entry.startswith("2 "):
            parts = entry.split(" ", 8 if entry.startswith("1 ") else 9)
            xy = parts[1]
            file_path = parts[-1]
            if entry.startswith("2 "):
                i += 1  # rename entries carry the original path as a separate NUL field
            if xy[0] != ".":
                info.staged += 1
            if xy[1] != ".":
                info.modified += 1
            code = xy.replace(".", "")[:1] or "M"
            if len(info.files) < limit:
                info.files.append(StatusFile(file_path, code))
        elif entry.startswith("u "):
            info.conflicted += 1
            if len(info.files) < limit:
                info.files.append(StatusFile(entry.split(" ", 10)[-1], "U"))
        elif entry.startswith("? "):
            info.untracked += 1
            if len(info.files) < limit:
                info.files.append(StatusFile(entry[2:], "??"))
    if info.head:
        summary = _git(["log", "-1", "--format=%s", info.head], path, timeout=15, check=False)
        info.head_summary = summary.stdout.strip()[:200] if summary.ok else None
    return info


def list_files(path: Path) -> list[str]:
    """Tracked + untracked-but-not-ignored files (respects .gitignore)."""
    result = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], path, timeout=120)
    return sorted({p for p in result.stdout.split("\0") if p})


def log(path: Path, *, file: str | None = None, limit: int = 10) -> list[dict]:
    args = ["log", f"-{max(1, min(limit, 50))}", "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=short"]
    if file:
        args += ["--", file]
    result = _git(args, path, timeout=30, check=False)
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"commit": parts[0][:12], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return commits


def blame(path: Path, file: str, start: int, end: int) -> list[dict]:
    result = _git(["blame", "--porcelain", "-L", f"{start},{end}", "--", file], path, timeout=30, check=False)
    if not result.ok:
        return []
    lines: list[dict] = []
    meta: dict[str, dict] = {}
    current: dict | None = None
    for raw in result.stdout.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", raw)
        if header:
            sha = header.group(1)
            current = {"commit": sha[:12], "line": int(header.group(2))}
            meta.setdefault(sha[:12], {})
            continue
        if current is None:
            continue
        if raw.startswith("author "):
            meta[current["commit"]]["author"] = raw[7:]
        elif raw.startswith("summary "):
            meta[current["commit"]]["summary"] = raw[8:]
        elif raw.startswith("\t"):
            current.update(meta.get(current["commit"], {}))
            current["text"] = raw[1:]
            lines.append(current)
            current = None
    return lines


def diff_head(path: Path, files: list[str] | None = None, max_bytes: int = 400_000) -> str:
    args = ["diff", "--no-color", "--no-ext-diff", "HEAD"]
    if files:
        args += ["--", *files]
    result = _git(args, path, timeout=60, check=False)
    return result.stdout[:max_bytes]


def show_file_at(path: Path, commit: str, file: str) -> str | None:
    result = _git(["show", f"{commit}:{file}"], path, timeout=30, check=False)
    return result.stdout if result.ok else None


# --- Write operations limited to LocPilot-managed locations ------------------------

def clone(url: str, dest: Path, *, timeout: float, hooks_dir: Path) -> None:
    """Clone a validated remote URL into LocPilot's workspace (explicit user action)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Credentials come only from the user's own git credential helper / SSH agent.
    env = base_env({"GIT_LFS_SKIP_SMUDGE": "1"})
    for key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH_COMMAND"):
        if key in os.environ:
            env[key] = os.environ[key]
    args = [
        git_executable() or "git",
        "-c", f"core.hooksPath={hooks_dir}",
        "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
        "clone", "--no-recurse-submodules", "--single-branch", "--", url, str(dest),
    ]
    result = run_sync(args, dest.parent, timeout=timeout, env=env)
    if not result.ok:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        message = (result.stderr or result.error or "clone failed").strip()
        if result.timed_out:
            message = f"git clone timed out after {timeout:.0f}s"
        raise GitError(sanitize_git_message(message))


def worktree_add(repo: Path, dest: Path, commit: str, *, hooks_dir: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["-c", f"core.hooksPath={hooks_dir}", "worktree", "add", "--detach", str(dest), commit], repo, timeout=300)


def worktree_remove(repo: Path, dest: Path, *, managed_root: Path) -> None:
    resolved = dest.resolve()
    if not resolved.is_relative_to(managed_root.resolve()):
        raise GitError("refusing to remove a worktree outside LocPilot's workspace")
    result = _git(["worktree", "remove", "--force", str(resolved)], repo, timeout=120, check=False)
    if not result.ok and resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)
    _git(["worktree", "prune"], repo, timeout=60, check=False)


# --- Explicit user actions: bring a worktree's changes out ----------------------------

# LocPilot links the user's node_modules into worktrees; the link is never part of a change.
WORKTREE_ARTIFACTS = frozenset({"node_modules"})
MAX_CHANGED_FILES = 500
_ZERO_OID = ""  # `git update-ref <ref> <new> ""` succeeds only if <ref> does not exist yet


def _user_git_env() -> dict[str, str]:
    """The user's own environment (credential helpers, SSH agent, commit signing) minus LocPilot's secrets."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("LOCPILOT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "LC_ALL": "C"})
    return env


def _git_as_user(args: list[str], cwd: Path, *, timeout: float) -> ProcessResult:
    exe = git_executable()
    if not exe:
        raise GitError("Git is not installed or not on PATH")
    result = run_sync([exe, "-c", "core.quotepath=off", *args], cwd, timeout=timeout, env=_user_git_env())
    if not result.ok:
        message = (result.stderr or result.stdout or result.error or "git failed").strip()
        if result.timed_out:
            message = f"git {args[-1] if args else ''} timed out after {timeout:.0f}s"
        raise GitError(sanitize_git_message(message))
    return result


def worktree_changes(worktree: Path) -> list[StatusFile]:
    """Uncommitted changes in a task worktree (untracked files listed one by one), LocPilot's link excluded."""
    result = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"], worktree, timeout=60)
    files: list[StatusFile] = []
    for entry in result.stdout.split("\0"):
        if len(entry) < 4 or entry[3:] in WORKTREE_ARTIFACTS:
            continue
        xy, path = entry[:2], entry[3:]
        code = "A" if xy == "??" or "A" in xy else "D" if "D" in xy else "M"
        files.append(StatusFile(path, code))
        if len(files) >= MAX_CHANGED_FILES:
            break
    return files


def valid_branch_name(name: str) -> bool:
    if not name or name.startswith("-") or len(name) > 200:
        return False
    return _git(["check-ref-format", f"refs/heads/{name}"], Path.cwd(), timeout=10, check=False).ok


def branch_tip(repo: Path, branch: str) -> str | None:
    result = _git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], repo, timeout=15, check=False)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def branch_checked_out_at(repo: Path, branch: str) -> str | None:
    """The working tree (main checkout or another worktree) that has ``branch`` checked out, if any."""
    result = _git(["worktree", "list", "--porcelain"], repo, timeout=30)
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return current
    return None


def _stage_only(worktree: Path, files: list[str]) -> None:
    """Make the worktree's index (LocPilot's own) equal HEAD plus exactly these files."""
    _git(["reset", "-q"], worktree, timeout=60)
    _git(["add", "-A", "--", *files], worktree, timeout=120)


def commit_to_branch(worktree: Path, *, branch: str, message: str, files: list[str], expected_tip: str | None,
                     hooks_dir: Path) -> str:
    """Commit ``files`` from a task worktree and point ``branch`` at the commit; returns the commit id.

    The worktree stays detached, so the branch is never checked out by LocPilot and the user can check it
    out anywhere. The branch is created only if it does not exist, or advanced only from ``expected_tip``
    (LocPilot's previous commit for this task): a branch moved by anyone else is never overwritten.
    """
    holder = branch_checked_out_at(worktree, branch)
    if holder is not None:
        raise GitError(f"Branch {branch} is checked out in {holder}. Choose another branch name, or switch that "
                       "checkout to a different branch first.")
    tip = branch_tip(worktree, branch)
    if tip is not None and tip != expected_tip:
        raise GitError(f"Branch {branch} already exists. Choose another branch name.")
    if expected_tip is not None and head_commit(worktree) != expected_tip:
        raise GitError("The worktree is no longer at LocPilot's last commit (was it changed from a terminal?). "
                       "Commit from the terminal instead.")
    _stage_only(worktree, files)
    _git_as_user(["-c", f"core.hooksPath={hooks_dir}", "commit", "-q", "-m", message], worktree, timeout=120)
    commit = head_commit(worktree)
    if not commit:
        raise GitError("The commit was not created.")
    _git(["update-ref", "-m", "LocPilot: commit task changes", f"refs/heads/{branch}", commit,
          expected_tip or _ZERO_OID], worktree, timeout=30)
    return commit


def push_branch(worktree: Path, branch: str, *, timeout: float, hooks_dir: Path) -> None:
    """Push ``branch`` to ``origin`` with the user's own Git credentials (explicit user action)."""
    if not _git(["remote", "get-url", "origin"], worktree, timeout=15, check=False).ok:
        raise GitError("This repository has no 'origin' remote to push to.")
    _git_as_user(["-c", f"core.hooksPath={hooks_dir}", "push", "--porcelain", "-u", "origin",
                  f"refs/heads/{branch}:refs/heads/{branch}"], worktree, timeout=timeout)


def apply_to_checkout(worktree: Path, repo: Path, *, base: str, files: list[str], patch_file: Path) -> None:
    """Write the worktree's changes to ``files`` (relative to ``base``) into the user's working tree.

    Plain file edits via ``git apply``: the user's index, HEAD and branches are untouched, and nothing is
    written unless the whole patch applies (a conflicting local edit makes it refuse).
    """
    _stage_only(worktree, files)
    try:
        diff = _git(["diff", "--cached", "--binary", "--no-color", "--no-ext-diff", base, "--", *files],
                    worktree, timeout=120)
    finally:
        _git(["reset", "-q"], worktree, timeout=60, check=False)
    if not diff.stdout.strip():
        raise GitError("These files have no changes to apply.")
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(diff.stdout)
    check = _git(["apply", "--check", str(patch_file)], repo, timeout=60, check=False)
    if not check.ok:
        raise GitError("The changes do not apply cleanly to your repository (it has different local edits or "
                       "commits in these files): " + sanitize_git_message((check.stderr or check.stdout).strip()))
    _git(["apply", str(patch_file)], repo, timeout=60)


def pull_request_url(remote: str | None, branch: str, hosts: list[str]) -> str | None:
    """A "create pull request" page for GitHub/GitLab/Bitbucket remotes, else None."""
    if not remote:
        return None
    try:
        parsed = parse_remote_url(remote, hosts)
    except ValueError:
        return None
    base = f"https://{parsed.host}/{parsed.owner}/{parsed.repo}"
    if parsed.host == "github.com":
        return f"{base}/compare/{quote(branch, safe='/')}?expand=1"
    if parsed.host == "gitlab.com":
        return f"{base}/-/merge_requests/new?merge_request%5Bsource_branch%5D={quote(branch, safe='')}"
    if parsed.host == "bitbucket.org":
        return f"{base}/pull-requests/new?source={quote(branch, safe='')}"
    return None


# --- URL validation ------------------------------------------------------------------

_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
_HTTPS_PATH_RE = re.compile(rf"^/(?P<owner>{_NAME})/(?P<repo>{_NAME}?)(?:\.git)?/?$")
_SCP_RE = re.compile(rf"^git@(?P<host>[a-z0-9.-]+):(?P<owner>{_NAME})/(?P<repo>{_NAME}?)(?:\.git)?$")


@dataclass
class ParsedRemote:
    url: str
    host: str
    owner: str
    repo: str


def parse_remote_url(raw: str, allowed_hosts: list[str]) -> ParsedRemote:
    """Validate a repository URL. Rejects credentials, odd transports and unknown hosts."""
    if not isinstance(raw, str):
        raise ValueError("URL must be a string")
    url = raw.strip()
    if not url or len(url) > 300:
        raise ValueError("URL is empty or too long")
    if any(ch.isspace() for ch in url) or any(ord(ch) < 32 for ch in url):
        raise ValueError("URL must not contain whitespace or control characters")
    if url.lower().startswith(("http://",)):
        raise ValueError("Only https:// (or git@host:owner/repo SSH) URLs are supported")
    if url.lower().startswith("https://"):
        parsed = urlparse(url)
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise ValueError("URLs with embedded credentials are not allowed. Configure a git credential helper or SSH key instead.")
        if parsed.port is not None:
            raise ValueError("Custom ports are not supported")
        if parsed.query or parsed.fragment or parsed.params:
            raise ValueError("URL must not contain a query string or fragment")
        match = _HTTPS_PATH_RE.match(parsed.path)
        host = (parsed.hostname or "").lower()
    elif url.startswith("git@"):
        match = _SCP_RE.match(url)
        host = match.group("host").lower() if match else ""
    else:
        raise ValueError("Unsupported URL. Use https://github.com/<owner>/<repo>")
    if not match:
        raise ValueError("Expected a repository URL like https://github.com/<owner>/<repo>")
    if host not in {h.lower() for h in allowed_hosts}:
        raise ValueError(f"Host {host!r} is not allowed (allowed: {', '.join(allowed_hosts)})")
    owner, repo = match.group("owner"), match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo or repo in (".", "..") or owner in (".", ".."):
        raise ValueError("Invalid owner or repository name")
    canonical = f"https://{host}/{owner}/{repo}.git" if url.startswith("https://") else f"git@{host}:{owner}/{repo}.git"
    return ParsedRemote(url=canonical, host=host, owner=owner, repo=repo)
