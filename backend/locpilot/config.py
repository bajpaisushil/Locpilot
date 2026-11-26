"""Runtime configuration.

Every setting can be overridden with an environment variable prefixed with
``LOCPILOT_`` (for example ``LOCPILOT_MODEL=qwen2.5-coder:14b``). Values that
users change from the UI (model, context size, temperature) are persisted in the
SQLite ``settings`` table and override these defaults at runtime.
"""

from __future__ import annotations

import ipaddress
import os
import re
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
    autostart_ollama: bool = True  # start an installed-but-stopped local Ollama (see llm/launcher.py)
    ollama_path: Path | None = None  # the ollama binary to start; default: found automatically
    ollama_start_timeout_s: float = 45.0
    # The explicit tag matters: on Apple Silicon, plain "mimo-v2.6:9b" is an 8.7 GB MLX build.
    model: str = "mimo-v2.6:9b-q4_K_M"
    # How long Ollama keeps the model in RAM after LocPilot's last request ("10m", "1h", "0" = unload at once).
    # LocPilot also unloads it when LocPilot quits or you switch models.
    ollama_keep_alive: str = "30m"
    num_ctx: int = 16384
    # How an Ollama server that LocPilot starts stores the context (KV cache): "q8_0" uses about half the memory of
    # Ollama's default with no measurable quality loss — what makes 16 GB machines comfortable. "" = Ollama's default.
    # An Ollama you run yourself (or the Ollama app) keeps its own settings; your OLLAMA_* variables always win.
    ollama_kv_cache_type: Literal["q8_0", "q4_0", "f16", ""] = "q8_0"
    # Thinking models (MiMo, Qwen3, …) can reason before they answer, which they do by default. "off" tells them not
    # to: in LocPilot's benchmark it was faster and no less accurate. "auto" thinks only in the root-cause analysis
    # and failure diagnosis; "on" in every step. Models that cannot think ignore this.
    thinking: Literal["auto", "on", "off"] = "off"
    thinking_tokens: int = Field(default=4096, ge=256, le=32768)  # reasoning budget per step, on top of the answer
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
    lock_wait_s: float = 15.0  # how long a starting backend waits for a previous one on the same data dir to exit
    terminal_scrollback_chars: int = 400_000
    max_output_bytes: int = 200_000
    sandbox: Literal["auto", "off"] = "auto"
    sandbox_allow_localhost: bool = False

    max_retries: int = 2
    # Model calls per turn. A step in which the model works with tools (reading, then editing) is several short
    # calls, so this is higher than the number of steps.
    max_llm_calls: int = 60
    # Offer tool-capable models real tools (read, grep, edit, write) instead of asking them to print edits as
    # text. Agent-tuned models answer with tool calls by training; turn off for a model whose tool calls misbehave.
    native_tools: bool = True
    # Before a change is proposed, the model checks it against the request (does every requirement really hold, with
    # concrete numbers?) and revises it if not: catches a wrong fix that its own wrong test would "verify".
    check_changes: bool = True
    max_evidence_rounds: int = 2
    max_patch_attempts: int = 2
    max_evidence_chars: int = 26_000
    # LOCPILOT.md in the repository root: the owner's notes (test commands, conventions, folders not to touch) given
    # to the model in every step. Capped so it never crowds out the code on small machines.
    project_notes_chars: int = Field(default=1500, ge=0, le=8000)

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

    @field_validator("data_dir", "ollama_path", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expanduser(value))
        return value

    @field_validator("ollama_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("ollama_keep_alive")
    @classmethod
    def _check_keep_alive(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"-?\d+(\.\d+)?(ms|s|m|h)?", value):
            raise ValueError('use a duration such as "10m", "1h" or "0"')
        return value

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
    def symbol_index_path(self) -> Path:
        """Parsed symbols of every indexed folder (a cache: safe to delete)."""
        return self.data_dir / "index" / "symbols.sqlite"

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
