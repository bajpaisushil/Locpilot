"""Start the locally installed Ollama when it is not running.

LocPilot does not bundle or install Ollama. When Ollama is installed on this
computer and the configured Ollama URL is local, LocPilot starts it instead of
asking the user to:

* macOS with the Ollama app installed: open the app in the background. It keeps
  running after LocPilot quits, exactly as if the user had opened it.
* otherwise: run ``ollama serve`` as a background process that LocPilot owns and
  stops again when it quits (which also frees the model's memory).

A Linux install that runs Ollama as a systemd service is never started with
``ollama serve``: that service runs as the ``ollama`` user with its own model
folder, so a second server would report the models as missing. The setup screen
asks the user to start the service instead.

When Ollama is not installed, nothing happens here; the setup screen shows how
to install it. Nothing is ever downloaded or installed automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx

from locpilot.config import ollama_url_is_local

log = logging.getLogger("locpilot.ollama")

_IS_WINDOWS = sys.platform.startswith("win")
DEFAULT_ADDRESS = ("127.0.0.1", 11434)
RETRY_AFTER_S = 15.0  # after a failed start, wait this long before trying again
_LOG_MAX_BYTES = 5_000_000

InstallKind = Literal["app", "cli", "service"]
LaunchState = Literal["running", "starting", "failed", "not_installed", "service_stopped", "disabled", "remote"]


@dataclass(frozen=True)
class OllamaInstall:
    kind: InstallKind  # "app": macOS Ollama.app · "cli": the ollama binary · "service": Linux systemd unit
    path: Path

    def describe(self) -> str:
        if self.kind == "app":
            return f"the Ollama app ({self.path})"
        if self.kind == "service":
            return f"a system service ({self.path})"
        return str(self.path)


@dataclass(frozen=True)
class LaunchInfo:
    state: LaunchState
    install: OllamaInstall | None = None
    started_by_locpilot: bool = False
    error: str | None = None


# Where Ollama's installers put it. Module-level so tests can replace them. -----------------------------------
def app_candidates() -> list[Path]:
    if sys.platform != "darwin":
        return []
    return [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"]


def cli_candidates() -> list[Path]:
    if _IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        return [Path(local) / "Programs" / "Ollama" / "ollama.exe"] if local else []
    paths = [Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    paths += [app / "Contents" / "Resources" / "ollama" for app in app_candidates()]
    return paths


def service_units() -> list[Path]:
    if not sys.platform.startswith("linux"):
        return []
    return [Path("/etc/systemd/system/ollama.service"), Path("/usr/lib/systemd/system/ollama.service"),
            Path("/lib/systemd/system/ollama.service")]


def which_ollama() -> str | None:
    return shutil.which("ollama")


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_cli() -> Path | None:
    found = which_ollama()
    if found:
        return Path(found)
    return next((p for p in cli_candidates() if _is_executable(p)), None)


def locate_ollama() -> OllamaInstall | None:
    """The Ollama installation on this computer, or ``None`` when it is not installed."""
    unit = next((u for u in service_units() if u.is_file()), None)
    if unit is not None:
        return OllamaInstall("service", unit)
    app = next((a for a in app_candidates() if a.is_dir()), None)
    if app is not None:
        return OllamaInstall("app", app)
    cli = find_cli()
    return OllamaInstall("cli", cli) if cli else None


def listen_address(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname or "127.0.0.1", port


def ollama_host_value(base_url: str) -> str:
    """The ``OLLAMA_HOST`` value that makes ``ollama serve`` listen where LocPilot connects."""
    host, port = listen_address(base_url)
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def memory_saving_env(kv_cache_type: str) -> dict[str, str]:
    """Ollama settings that shrink the context's memory (the KV cache) for an ``ollama serve`` LocPilot starts.

    Flash attention is what lets Ollama store the cache in 8 bits (``q8_0``): about half the memory of the default
    16 bits, with no measurable loss in answer quality. Ollama ignores both for models that don't support them.
    An empty ``kv_cache_type`` leaves Ollama's defaults alone.
    """
    if not kv_cache_type:
        return {}
    return {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": kv_cache_type}


def serve_env(base_url: str, defaults: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for ``ollama serve``. ``defaults`` apply only where the user hasn't set the variable themselves."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("LOCPILOT_")}  # never pass our token on
    # A PyInstaller onefile build on Linux points LD_LIBRARY_PATH at its own bundle; give Ollama the user's.
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    for key, value in (defaults or {}).items():
        env.setdefault(key, value)
    env["OLLAMA_HOST"] = ollama_host_value(base_url)
    return env


def _stop_process(proc: subprocess.Popen) -> None:
    """Stop ``ollama serve`` and the model runners it started."""
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGTERM)  # its own session: the group is ollama + its runners
        proc.wait(timeout=8)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        if _IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


class OllamaLauncher:
    def __init__(self, base_url: str, logs_dir: Path, *, enabled: bool = True, ollama_path: Path | None = None,
                 start_timeout_s: float = 45.0, env_defaults: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.logs_dir = logs_dir
        self.enabled = enabled
        self.ollama_path = ollama_path
        self.start_timeout_s = start_timeout_s
        self.env_defaults = dict(env_defaults or {})  # e.g. memory_saving_env(): only for a server we start
        self._lock = asyncio.Lock()
        self._proc: subprocess.Popen | None = None  # the `ollama serve` LocPilot started, if any
        self._install: OllamaInstall | None = None  # what the last start attempt launched
        self._attempt_at: float | None = None  # monotonic: when we started waiting for Ollama to answer
        self._ready = False  # the process we started has answered at least once
        self._opened_app = False  # we opened the macOS Ollama app (it is not ours to stop)
        self._pending_app = False  # ...and it has not answered yet
        self._failure: LaunchInfo | None = None
        self._failed_at: float | None = None
        self._log_offset = 0  # where this attempt's output starts in ollama.log

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "ollama.log"

    @property
    def owns_process(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def probe(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
                resp = await client.get(f"{self.base_url}/api/version")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def _find_install(self) -> OllamaInstall | LaunchInfo:
        if self.ollama_path is not None:
            if _is_executable(self.ollama_path):
                return OllamaInstall("cli", self.ollama_path)
            return LaunchInfo("failed", error=f"LOCPILOT_OLLAMA_PATH ({self.ollama_path}) is not an executable file")
        install = locate_ollama()
        return install if install is not None else LaunchInfo("not_installed")

    def installation(self) -> OllamaInstall | None:
        found = self._find_install()
        return found if isinstance(found, OllamaInstall) else None

    def _running(self) -> LaunchInfo:
        ours = self.owns_process or self._opened_app
        return LaunchInfo("running", self._install if ours else None, started_by_locpilot=ours)

    async def ensure_running(self, wait_s: float = 20.0) -> LaunchInfo:
        """Start Ollama if it is installed and not running; wait up to ``wait_s`` for it to answer."""
        if not ollama_url_is_local(self.base_url):
            return LaunchInfo("remote")
        if not self.enabled:
            return LaunchInfo("disabled", self.installation())
        if await self.probe():
            return self._running()
        async with self._lock:
            if await self.probe():
                return self._running()
            if self.owns_process or self._pending_app:
                if self._ready:  # it answered before and stopped: give it the start timeout to recover
                    self._ready, self._attempt_at = False, time.monotonic()
                return await self._wait_ready(wait_s)
            found = self._find_install()
            if isinstance(found, LaunchInfo):
                return found
            if found.kind == "service":
                return LaunchInfo("service_stopped", found)
            if self._failure is not None and self._failed_at is not None \
                    and time.monotonic() - self._failed_at < RETRY_AFTER_S:
                return self._failure
            failure = self._launch(found)
            return failure if failure is not None else await self._wait_ready(wait_s)

    def _timed_out(self) -> bool:
        return self._attempt_at is not None and time.monotonic() - self._attempt_at > self.start_timeout_s

    def _fail(self, message: str) -> LaunchInfo:
        log.warning("could not start Ollama: %s", message)
        self._failure = LaunchInfo("failed", self._install, error=message)
        self._failed_at = time.monotonic()
        self._ready = self._opened_app = self._pending_app = False
        return self._failure

    def _launch(self, install: OllamaInstall) -> LaunchInfo | None:
        self._install, self._failure = install, None
        self._ready = self._opened_app = self._pending_app = False
        self._attempt_at = time.monotonic()
        if install.kind == "app" and listen_address(self.base_url) == DEFAULT_ADDRESS:
            log.info("starting the Ollama app (%s)", install.path)
            try:
                result = subprocess.run(["open", "-g", "-a", str(install.path)], capture_output=True, text=True,
                                        timeout=20, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as exc:
                return self._fail(f"could not open the Ollama app: {exc}")
            if result.returncode != 0:
                return self._fail(f"could not open the Ollama app: {(result.stderr or result.stdout).strip()}")
            self._opened_app = self._pending_app = True
            return None
        binary = install.path
        if install.kind == "app":  # non-default address: the app can't be told where to listen; use its CLI
            binary = find_cli() or install.path / "Contents" / "Resources" / "ollama"
        return self._spawn(binary)

    def _spawn(self, binary: Path) -> LaunchInfo | None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > _LOG_MAX_BYTES:
                self.log_path.unlink()
        except OSError:
            pass
        kwargs: dict = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True  # not hit by a Ctrl+C meant for LocPilot; stopped on shutdown
        log.info("starting `%s serve` on %s", binary, ollama_host_value(self.base_url))
        try:
            with open(self.log_path, "ab") as out:
                self._log_offset = out.tell()
                self._proc = subprocess.Popen([str(binary), "serve"], stdin=subprocess.DEVNULL, stdout=out,
                                              stderr=subprocess.STDOUT,
                                              env=serve_env(self.base_url, self.env_defaults),
                                              cwd=str(Path.home()), **kwargs)
        except OSError as exc:
            self._proc = None
            return self._fail(f"could not run `{binary} serve`: {exc.strerror or exc}")
        return None

    def _exit_reason(self, limit: int = 400) -> str:
        """Why ``ollama serve`` exited, from the output of this attempt only."""
        try:
            with open(self.log_path, "rb") as fh:
                end = fh.seek(0, os.SEEK_END)
                fh.seek(max(self._log_offset, end - 8000))
                lines = [ln.strip() for ln in fh.read().decode("utf-8", errors="replace").splitlines() if ln.strip()]
        except OSError:
            return ""
        errors = [ln for ln in lines if ln.lower().startswith("error")]
        reason = errors[-1] if errors else " ".join(lines[-2:])
        if "address already in use" in reason.lower():
            return (f"another program is already using {ollama_host_value(self.base_url)} and it is not answering "
                    "like Ollama. Quit that program, or run Ollama on another port and set LOCPILOT_OLLAMA_URL "
                    f"to match ({reason[:200]})")
        return reason[-limit:]

    async def _wait_ready(self, wait_s: float) -> LaunchInfo:
        deadline = time.monotonic() + wait_s
        while True:
            if await self.probe():
                self._failure, self._ready, self._pending_app = None, True, False
                log.info("Ollama is running at %s", self.base_url)
                return self._running()
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._proc = None
                reason = self._exit_reason()
                return self._fail(f"`ollama serve` exited with code {proc.returncode}" + (f": {reason}" if reason else ""))
            if self._timed_out():
                if proc is not None:
                    await asyncio.to_thread(_stop_process, proc)
                    self._proc = None
                return self._fail(f"Ollama did not start answering at {self.base_url} within "
                                  f"{self.start_timeout_s:.0f} seconds")
            if time.monotonic() >= deadline:
                return LaunchInfo("starting", self._install, started_by_locpilot=True)
            await asyncio.sleep(0.25)

    async def shutdown(self) -> None:
        """Stop the ``ollama serve`` LocPilot started. An Ollama the user (or the Ollama app) runs is left alone."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            log.info("stopping the Ollama server LocPilot started (pid %s)", proc.pid)
            await asyncio.to_thread(_stop_process, proc)
