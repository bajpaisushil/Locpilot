"""Allowlist and argument validation for verification commands.

The agent may only *propose* commands; they run only after the user approves
them, and only when they match one of the test-runner shapes below. Commands
are executed as argv lists (never through a shell). Anything else — package
installs, network tools, interpreters with ``-c``/``-e`` — is rejected outright
and cannot be approved.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

_SHELL_META = re.compile(r"[;&|`$<>\n\r\\]|\$\(")
_MAX_ARGS = 40
_MAX_ARG_LEN = 400

_PY_MODULES = {"pytest", "unittest"}
_NODE_SCRIPT_RUNNERS = {"npm", "pnpm", "yarn", "bun"}
_JS_TEST_BINS = {"jest", "vitest", "mocha", "ava", "tap"}


class CommandPolicyError(ValueError):
    """Raised when a command is not an allowed verification command."""


@dataclass
class ValidatedCommand:
    argv: list[str]
    display: str
    trusted: bool
    notes: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def display_argv(argv: list[str]) -> str:
    def quote(arg: str) -> str:
        return arg if re.fullmatch(r"[A-Za-z0-9_@%+=:,./\-]+", arg) else "'" + arg.replace("'", "'\\''") + "'"

    return " ".join(quote(a) for a in argv)


def _program_name(argv0: str) -> str:
    name = Path(argv0.replace("\\", "/")).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _is_python(name: str) -> bool:
    return re.fullmatch(r"python(3(\.\d+)?)?", name) is not None


def _check_args(args: list[str], workspace: Path) -> None:
    if len(args) > _MAX_ARGS:
        raise CommandPolicyError("too many arguments")
    for arg in args:
        if not isinstance(arg, str) or not arg:
            raise CommandPolicyError("empty or non-string argument")
        if len(arg) > _MAX_ARG_LEN:
            raise CommandPolicyError("argument too long")
        if _SHELL_META.search(arg):
            raise CommandPolicyError(f"shell metacharacters are not allowed: {arg!r}")
        if "\x00" in arg:
            raise CommandPolicyError("NUL byte in argument")
        candidate = arg.split("=", 1)[-1] if arg.startswith("-") else arg
        if candidate.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", candidate):
            resolved = Path(os.path.expanduser(candidate)).resolve()
            if not resolved.is_relative_to(workspace.resolve()):
                raise CommandPolicyError(f"path argument outside the workspace: {arg!r}")
        if ".." in candidate.replace("\\", "/").split("/"):
            resolved = (workspace / candidate).resolve()
            if not resolved.is_relative_to(workspace.resolve()):
                raise CommandPolicyError(f"path argument escapes the workspace: {arg!r}")


def _resolve_program(argv0: str, workspace: Path, origin_repo: Path | None) -> str:
    """Resolve the executable: a repo-relative tool (``.venv/bin/python``) or a PATH lookup.

    Worktrees do not contain ignored directories such as ``.venv`` or
    ``node_modules``, so repo-relative tools are also looked up in the original
    repository.
    """
    rel = argv0.replace("\\", "/")
    if "/" in rel:
        if rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
            raise CommandPolicyError("absolute program paths are not allowed; use a repository-relative path")
        if ".." in rel.split("/"):
            raise CommandPolicyError("program path escapes the workspace")
        rel = rel[2:] if rel.startswith("./") else rel
        for base in (workspace, origin_repo):
            if base is not None:
                candidate = base / rel
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
        raise CommandPolicyError(f"program not found in the repository: {argv0}")
    found = shutil.which(argv0)
    if not found:
        raise CommandPolicyError(f"program not found on PATH: {argv0}")
    return found


def validate_command(argv: list[str], workspace: Path, origin_repo: Path | None = None) -> ValidatedCommand:
    """Validate a proposed verification command. Raises :class:`CommandPolicyError`."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise CommandPolicyError("command must be a non-empty list of strings")
    argv = [a.strip() for a in argv]
    _check_args(argv[1:], workspace)
    name = _program_name(argv[0])
    args = argv[1:]
    notes: list[str] = []
    env: dict[str, str] = {}

    if _is_python(name):
        if len(args) < 2 or args[0] != "-m" or args[1] not in _PY_MODULES:
            raise CommandPolicyError("python may only run `-m pytest` or `-m unittest`")
        notes.append("Runs project test code (can execute arbitrary Python from the repository).")
    elif name in ("pytest", "py.test"):
        notes.append("Runs project test code (can execute arbitrary Python from the repository).")
    elif name == "node":
        if not args or args[0] != "--test":
            raise CommandPolicyError("node may only be used as `node --test [files]`")
        if any(a in ("-e", "--eval", "-p", "--print", "-r", "--require", "--import", "--loader") for a in args):
            raise CommandPolicyError("node eval/require flags are not allowed")
        notes.append("Runs project test code with the Node.js test runner.")
    elif name in _NODE_SCRIPT_RUNNERS:
        if not args:
            raise CommandPolicyError(f"{name} requires a test script")
        script_args = args[1:] if args[0] == "run" else args
        if not script_args or not (script_args[0] == "test" or script_args[0].startswith("test:")):
            raise CommandPolicyError(f"{name} may only run the `test` script (or `test:*` scripts)")
        notes.append("Runs a package.json script, which can execute arbitrary shell commands.")
        env.update({"npm_config_update_notifier": "false", "npm_config_fund": "false", "npm_config_audit": "false"})
    elif name == "npx":
        if not args or args[0] not in ("--no-install", "--no"):
            raise CommandPolicyError("npx must be used with --no-install (no package downloads)")
        if len(args) < 2 or _program_name(args[1]) not in _JS_TEST_BINS:
            raise CommandPolicyError("npx may only run a locally installed test runner (jest, vitest, mocha)")
        notes.append("Runs a locally installed JS test runner.")
    elif name in _JS_TEST_BINS:
        if name == "vitest" and (not args or args[0] not in ("run", "--run")):
            raise CommandPolicyError("vitest must be run in non-watch mode: `vitest run`")
        notes.append("Runs a locally installed JS test runner.")
    elif name == "go":
        if not args or args[0] != "test":
            raise CommandPolicyError("go may only run `go test`")
        if any(a.startswith("-exec") or a.startswith("-toolexec") for a in args):
            raise CommandPolicyError("go -exec/-toolexec flags are not allowed")
        env.update({"GOPROXY": "off", "GOFLAGS": "-mod=mod"})
    elif name == "cargo":
        if not args or args[0] != "test":
            raise CommandPolicyError("cargo may only run `cargo test`")
        if "--offline" not in args:
            argv = argv + ["--offline"]
            notes.append("Added --offline (no network).")
    elif name in ("mvn", "mvnw"):
        if "test" not in args:
            raise CommandPolicyError("maven may only run the test phase")
        if "-o" not in args and "--offline" not in args:
            argv = argv[:1] + ["-o"] + argv[1:]
            notes.append("Added -o (offline).")
    elif name in ("gradle", "gradlew"):
        if not args or not all(a in ("test", "check", "--offline", "-q", "--quiet") or a.startswith("--tests") for a in args):
            raise CommandPolicyError("gradle may only run `test`/`check`")
        if "--offline" not in args:
            argv = argv + ["--offline"]
    elif name == "make":
        if args not in (["test"], ["check"], ["-s", "test"], ["-s", "check"]):
            raise CommandPolicyError("make may only run `make test` or `make check`")
        notes.append("Runs a Makefile target, which can execute arbitrary shell commands.")
    elif name == "dotnet":
        if not args or args[0] != "test":
            raise CommandPolicyError("dotnet may only run `dotnet test`")
    elif name == "rspec":
        notes.append("Runs project test code (Ruby).")
    else:
        raise CommandPolicyError(f"`{argv[0]}` is not an allowed verification command")

    display = display_argv(argv)
    resolved = _resolve_program(argv[0], workspace, origin_repo)
    if resolved != argv[0]:
        notes.append(f"Executable: {resolved}")
    argv = [resolved, *argv[1:]]
    return ValidatedCommand(argv=argv, display=display, trusted=False, notes=notes, env=env)
