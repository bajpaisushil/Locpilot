"""``locpilot-backend`` entry point (dev server and bundled desktop sidecar)."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

for _var in ("LANGCHAIN_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_ENDPOINT", "LANGSMITH_ENDPOINT"):
    os.environ.pop(_var, None)
# LangChain/LangGraph tracing to LangSmith must never be enabled: LocPilot is local-only.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

import uvicorn  # noqa: E402

from locpilot import __version__  # noqa: E402
from locpilot.app import create_app  # noqa: E402
from locpilot.config import Settings  # noqa: E402


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _login_shell_path() -> str | None:
    """GUI apps on macOS/Linux don't inherit the shell PATH; ask the user's login shell for it."""
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    try:
        out = subprocess.run([shell, "-ilc", "printf '__RP_PATH__%s__RP_END__' \"$PATH\""], capture_output=True,
                             text=True, timeout=8, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    start, end = out.stdout.find("__RP_PATH__"), out.stdout.find("__RP_END__")
    if start == -1 or end == -1:
        return None
    return out.stdout[start + len("__RP_PATH__"):end]


def fix_path() -> None:
    if sys.platform.startswith("win"):
        return
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    shell_path = _login_shell_path()
    if shell_path:
        parts += shell_path.split(os.pathsep)
    home = Path.home()
    parts += ["/opt/homebrew/bin", "/usr/local/bin", str(home / ".cargo/bin"), str(home / ".local/bin"),
              "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(parts))


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        # os.kill(pid, 0) would send CTRL_C_EVENT on Windows; query the process instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_parent(parent_pid: int, server: uvicorn.Server) -> None:
    while True:
        time.sleep(2)
        if not _pid_alive(parent_pid):
            logging.getLogger("locpilot").warning("parent process %s exited; shutting down", parent_pid)
            server.should_exit = True
            time.sleep(8)
            os._exit(0)


_LOCK_HANDLE = None


def acquire_data_dir_lock(data_dir: Path, wait_s: float = 0.0) -> bool:
    """Hold an exclusive OS lock on the data directory for the life of this process.

    Two backends sharing one data directory would both work on the same tasks
    (and each would mark the other's running tasks as interrupted). Waits up to
    ``wait_s`` for a previous backend that is still shutting down (an app restart).
    """
    global _LOCK_HANDLE
    handle = open(data_dir / "backend.lock", "a+")  # noqa: SIM115 - must stay open while the process runs
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return False
            time.sleep(0.2)
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _LOCK_HANDLE = handle
    return True


def configure_logging(settings: Settings, level: str) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    file_handler = logging.handlers.RotatingFileHandler(settings.logs_dir / "backend.log", maxBytes=2_000_000,
                                                        backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.handlers = [stderr, file_handler]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="locpilot-backend", description="LocPilot local agent backend")
    parser.add_argument("--host", default=None, help="bind address (loopback only)")
    parser.add_argument("--port", type=int, default=None, help="port (0 = pick a free port)")
    parser.add_argument("--data-dir", default=None, help="data directory (default ~/.locpilot)")
    parser.add_argument("--parent-pid", type=int, default=None, help="exit when this process exits")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--fix-path", action="store_true", help="import PATH from the login shell (desktop launch)")
    args = parser.parse_args(argv)

    overrides: dict = {}
    if args.host:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.data_dir:
        overrides["data_dir"] = Path(os.path.expanduser(args.data_dir))
    settings = Settings(**overrides)
    if not _is_loopback(settings.host):
        print("LocPilot only binds to loopback addresses (127.0.0.1 / localhost).", file=sys.stderr)
        sys.exit(2)
    if args.fix_path or getattr(sys, "frozen", False):
        fix_path()
    settings.ensure_dirs()
    if not acquire_data_dir_lock(settings.data_dir, wait_s=settings.lock_wait_s):
        print(f"Another LocPilot backend is already using {settings.data_dir}. Close the other LocPilot window "
              "(or stop the other backend) and try again.", file=sys.stderr)
        sys.exit(4)
    configure_logging(settings, args.log_level)

    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if not sys.platform.startswith("win"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((settings.host, settings.port))
    except OSError as exc:
        print(f"Cannot bind {settings.host}:{settings.port}: {exc.strerror}. Is another LocPilot running?",
              file=sys.stderr)
        sys.exit(3)
    sock.listen(128)
    port = sock.getsockname()[1]

    app = create_app(settings)
    config = uvicorn.Config(app, log_level=args.log_level, access_log=False, lifespan="on",
                            timeout_graceful_shutdown=5, log_config=None)
    server = uvicorn.Server(config)
    if args.parent_pid:
        threading.Thread(target=_watch_parent, args=(args.parent_pid, server), daemon=True).start()
    if getattr(sys, "frozen", False):
        # A onefile build runs as a launcher process plus this one. If the launcher is force-killed
        # (an app restart that timed out), exit too: an orphan would keep the data directory locked.
        threading.Thread(target=_watch_parent, args=(os.getppid(), server), daemon=True).start()

    async def run() -> None:
        serve = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not serve.done():
            await asyncio.sleep(0.05)
        if server.started:
            print("LOCPILOT_READY " + json.dumps({"port": port, "pid": os.getpid(), "version": __version__}),
                  flush=True)
        await serve

    asyncio.run(run())


if __name__ == "__main__":
    main()
