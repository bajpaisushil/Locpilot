"""Subprocess execution without a shell, with timeouts, output caps and tree kills."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from locpilot.security.secrets import redact, scrub_env

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_IS_WINDOWS = sys.platform.startswith("win")


@dataclass
class ProcessResult:
    argv: list[str]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    error: str | None = None
    sandbox: str = "none"
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled and self.error is None


class _CappedBuffer:
    """Keeps the head and the tail of a stream (test summaries live at the end)."""

    def __init__(self, limit: int) -> None:
        self.head_limit = max(limit // 4, 1024)
        self.tail_limit = max(limit - self.head_limit, 1024)
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def write(self, chunk: bytes) -> None:
        if len(self.head) < self.head_limit:
            take = self.head_limit - len(self.head)
            self.head += chunk[:take]
            chunk = chunk[take:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.tail_limit:
                overflow = len(self.tail) - self.tail_limit
                self.dropped += overflow
                del self.tail[:overflow]

    @property
    def truncated(self) -> bool:
        return self.dropped > 0

    def text(self) -> str:
        head = self.head.decode("utf-8", errors="replace")
        tail = self.tail.decode("utf-8", errors="replace")
        if self.dropped:
            return f"{head}\n… [{self.dropped} bytes truncated by LocPilot] …\n{tail}"
        return head + tail


def clean_output(text: str) -> str:
    return redact(_ANSI.sub("", text).replace("\r\n", "\n"), process_output=True)


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = scrub_env()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PIP_NO_INPUT": "1",
        "npm_config_update_notifier": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "LANGSMITH_TRACING": "false",
    })
    if extra:
        env.update(extra)
    return env


def _kill_tree_sync(proc: subprocess.Popen) -> None:
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def run_sync(
    argv: list[str],
    cwd: Path | str,
    *,
    timeout: float = 60,
    env: dict[str, str] | None = None,
    max_output_bytes: int = 2_000_000,
    redact_output: bool = False,
) -> ProcessResult:
    """Run a short internal command (e.g. read-only git) synchronously."""
    start = time.monotonic()
    popen_kwargs: dict = {}
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env if env is not None else base_env(), **popen_kwargs,
        )
    except OSError as exc:
        return ProcessResult(argv, str(cwd), None, "", "", 0, error=f"failed to start: {exc.strerror or exc}")
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree_sync(proc)
        out, err = proc.communicate()
    duration = int((time.monotonic() - start) * 1000)
    truncated = len(out) > max_output_bytes or len(err) > max_output_bytes
    stdout = out[:max_output_bytes].decode("utf-8", errors="replace")
    stderr = err[:max_output_bytes].decode("utf-8", errors="replace")
    if redact_output:
        stdout, stderr = clean_output(stdout), clean_output(stderr)
    return ProcessResult(argv, str(cwd), None if timed_out else proc.returncode, stdout, stderr, duration,
                         timed_out=timed_out, truncated=truncated)


OutputCallback = Callable[[str, str], Awaitable[None]]


async def run_async(
    argv: list[str],
    cwd: Path | str,
    *,
    timeout: float,
    env: dict[str, str],
    max_output_bytes: int,
    on_output: OutputCallback | None = None,
    sandbox: str = "none",
) -> ProcessResult:
    """Run an approved command with streaming output, a hard timeout and process-tree kill.

    Cancellation of the awaiting task kills the whole process group.
    """
    start = time.monotonic()
    kwargs: dict = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, **kwargs,
        )
    except OSError as exc:
        return ProcessResult(argv, str(cwd), None, "", "", 0, error=f"failed to start: {exc.strerror or exc}",
                             sandbox=sandbox)

    out_buf, err_buf = _CappedBuffer(max_output_bytes), _CappedBuffer(max_output_bytes)

    async def pump(stream: asyncio.StreamReader | None, buf: _CappedBuffer, name: str) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            buf.write(chunk)
            if on_output is not None:
                try:
                    await on_output(name, clean_output(chunk.decode("utf-8", errors="replace")))
                except Exception:  # noqa: BLE001 - output streaming must never break execution
                    pass

    def kill_tree() -> None:
        try:
            if _IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    pumps = asyncio.gather(pump(proc.stdout, out_buf, "stdout"), pump(proc.stderr, err_buf, "stderr"))
    timed_out = cancelled = False
    try:
        await asyncio.wait_for(asyncio.shield(pumps), timeout=timeout)
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        timed_out = True
        kill_tree()
    except asyncio.CancelledError:
        cancelled = True
        kill_tree()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            pass
        raise
    finally:
        if timed_out:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                pass
            try:
                await asyncio.wait_for(pumps, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pumps.cancel()
    duration = int((time.monotonic() - start) * 1000)
    return ProcessResult(
        argv=argv, cwd=str(cwd), exit_code=None if (timed_out or cancelled) else proc.returncode,
        stdout=clean_output(out_buf.text()), stderr=clean_output(err_buf.text()), duration_ms=duration,
        timed_out=timed_out, cancelled=cancelled, truncated=out_buf.truncated or err_buf.truncated, sandbox=sandbox,
    )
