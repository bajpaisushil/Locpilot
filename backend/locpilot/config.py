"""Runtime configuration.

Every setting can be overridden with an environment variable prefixed with
``LOCPILOT_`` (for example ``LOCPILOT_MODEL=qwen2.5-coder:14b``). Values that
users change from the UI (model, context size, temperature) are persisted in the
SQLite ``settings`` table and override these defaults at runtime.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_ORIGINS = [
    "http://127.0.0.1:5420",
    "http://localhost:5420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]


def _split_list(value: object, *, path_separator: bool = False) -> object:
    """Comma-separated env values; folder lists may also use the OS path separator (":" on POSIX).
    URLs contain ":", so only folder lists split on it."""
    if isinstance(value, str):
        if path_separator:
            value = value.replace(os.pathsep, ",")
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOCPILOT_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8420
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".locpilot")
    auth_token: str | None = None

    ollama_url: str = "http://127.0.0.1:11434"
    allow_remote_ollama: bool = False
    model: str = "qwen2.5-coder:7b"
    num_ctx: int = 16384
    temperature: float = 0.1
    llm_timeout_s: float = 900.0
    llm_seed: int = 7
    debug_llm: bool = False  # write prompts/responses to <data_dir>/logs/llm-debug.jsonl

    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(DEFAULT_ORIGINS))
    allowed_repo_roots: Annotated[list[Path], NoDecode] = Field(default_factory=lambda: [Path.home()])
    allowed_git_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["github.com", "gitlab.com", "bitbucket.org"]
    )

    clone_timeout_s: int = 600
    git_timeout_s: int = 60
    command_timeout_s: int = 300
    user_command_timeout_s: int = 900
    terminal_shell: str | None = None  # default: the user's login shell
    max_terminals: int = 8
    terminal_scrollback_chars: int = 400_000
    max_output_bytes: int = 200_000
    sandbox: Literal["auto", "off"] = "auto"
    sandbox_allow_localhost: bool = False

    max_retries: int = 2
    max_llm_calls: int = 24
    max_evidence_rounds: int = 2
    max_patch_attempts: int = 2
    max_evidence_chars: int = 26_000

    @field_validator("allowed_origins", "allowed_git_hosts", mode="before")
    @classmethod
    def _parse_str_list(cls, value: object) -> object:
        return _split_list(value)

    @field_validator("allowed_repo_roots", mode="before")
    @classmethod
    def _parse_roots(cls, value: object) -> object:
        value = _split_list(value, path_separator=True)
        if isinstance(value, list):
            return [Path(os.path.expanduser(str(v))) for v in value]
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expanduser(value))
        return value

    @field_validator("ollama_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    # Derived paths -----------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "locpilot.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def scratch_dir(self) -> Path:
        return self.data_dir / "scratch"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def empty_hooks_dir(self) -> Path:
        return self.data_dir / "empty-hooks"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.repos_dir,
            self.worktrees_dir,
            self.scratch_dir,
            self.logs_dir,
            self.empty_hooks_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass


def ollama_url_is_local(url: str) -> bool:
    """True when the Ollama URL points at this machine (loopback)."""
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def platform_name() -> Literal["macos", "windows", "linux", "other"]:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


@lru_cache
def get_settings() -> Settings:
    return Settings()
