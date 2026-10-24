"""Interactive terminals: the user's own shell on a pseudo-terminal, streamed to the UI.

A terminal is not a sandboxed command. It is the user's login shell with their usual
environment (minus LocPilot's own token), so `npm install`, dev servers and `git push` work
as in any terminal app. Only the user can open one (the agent has no tool for it), and every
call goes through the same token-authenticated local API. A terminal for a Code-mode task
starts in that task's worktree, i.e. on LocPilot's version of the code.

POSIX only (macOS and Linux); Windows would need ConPTY.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import shlex
import signal
import struct
import subprocess
import sys
import uuid
from pathlib import Path

from locpilot.storage.db import now_iso

SUPPORTED = not sys.platform.startswith("win")
if SUPPORTED:
    import fcntl
    import termios


class TerminalError(RuntimeError):
    pass


def _make_controlling_tty() -> None:
    """Runs in the child between fork and exec: a new session whose controlling terminal is the PTY,
    so Ctrl-C, job control and window-size changes behave as in any terminal."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def shell_argv(configured: str | None) -> list[str]:
    """The configured shell command, or the user's login shell."""
    if configured:
        return shlex.split(configured)
    shell = os.environ.get("SHELL", "")
    if not (shell and os.path.isfile(shell) and os.access(shell, os.X_OK)):
        shell = next((s for s in ("/bin/zsh", "/bin/bash", "/bin/sh") if os.path.isfile(s)), "")
    if not shell:
        raise TerminalError("No shell was found on this system.")
    return [shell, "-l"] if Path(shell).name in ("zsh", "bash", "fish") else [shell]


def terminal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("LOCPILOT_")}
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "LocPilot"})
    env.setdefault("LANG", "en_US.UTF-8")
    env.update(extra or {})
    return env


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class TerminalSession:
    def __init__(self, *, argv: list[str], cwd: Path, env: dict[str, str], title: str, location: str,
                 repo_id: str, task_id: str | None, cols: int, rows: int, scrollback_chars: int) -> None:
        self.id = f"term_{uuid.uuid4().hex[:12]}"
        self.argv, self.cwd, self.env = argv, cwd, env
        self.title, self.location = title, location
        self.repo_id, self.task_id = repo_id, task_id
        self.cols, self.rows = cols, rows
        self.created_at = now_iso()
        self.exit_code: int | None = None
        self.exited = False
        self.proc: subprocess.Popen | None = None
        self._master: int | None = None
        self._tty: str | None = None
        self._scrollback = ""
        self._scrollback_chars = scrollback_chars
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._subscribers: set[asyncio.Queue] = set()
        self._write_lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    # lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        master, slave = os.openpty()
        try:
            _set_winsize(master, self.rows, self.cols)
            self._tty = os.ttyname(slave).removeprefix("/dev/")
            self.proc = subprocess.Popen(self.argv, stdin=slave, stdout=slave, stderr=slave, cwd=str(self.cwd),
                                         env=self.env, preexec_fn=_make_controlling_tty, close_fds=True)
        except OSError as exc:
            os.close(master)
            raise TerminalError(f"Could not start {self.argv[0]}: {exc.strerror or exc}") from None
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self._master = master
        asyncio.get_running_loop().add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        assert self._master is not None
        try:
            data = os.read(self._master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: nothing holds the terminal open any more
            data = b""
        if data:
            text = self._decoder.decode(data)
            if text:
                self._record(text)
                self._publish({"type": "output", "data": text})
            return
        self._stop_reading()
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        assert self.proc is not None
        code = await asyncio.to_thread(self.proc.wait)
        self._mark_exited(code)

    def _mark_exited(self, code: int | None) -> None:
        if self.exited:
            return
        self.exited, self.exit_code = True, code
        self._publish({"type": "exit", "code": code})
        self._close_master()
        for queue in list(self._subscribers):
            self._end(queue, drop_pending=False)

    def _stop_reading(self) -> None:
        if self._master is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._master)

    def _close_master(self) -> None:
        self._stop_reading()
        if self._master is not None:
            with contextlib.suppress(OSError):
                os.close(self._master)
            self._master = None

    async def close(self) -> None:
        """Hang up the terminal: SIGHUP everything attached to it, then SIGKILL what is left."""
        if self.proc is None or self.exited:
            self._close_master()
            return
        pids = self._attached_pids() if self._tty else []  # while we still hold the tty: its name can't be reused
        for pid in {self.proc.pid, *pids}:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGHUP)
        try:
            code = await asyncio.wait_for(asyncio.to_thread(self.proc.wait), timeout=2)
        except TimeoutError:
            code = None
        for pid in {self.proc.pid, *pids}:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if code is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                code = await asyncio.to_thread(self.proc.wait, 2)
        self._mark_exited(code)

    def _attached_pids(self) -> list[int]:
        try:
            out = subprocess.run(["pgrep", "-t", self._tty or "", "."], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        return [int(p) for p in out.stdout.split() if p.isdigit() and int(p) != os.getpid()]

    # I/O --------------------------------------------------------------------------------
    async def write(self, data: str) -> None:
        if self.exited or self._master is None:
            raise TerminalError("This terminal's shell has exited.")
        payload = data.encode()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        async with self._write_lock:
            while payload:
                try:
                    payload = payload[os.write(self._master, payload):]
                except BlockingIOError:
                    if loop.time() > deadline:
                        raise TerminalError("The terminal is not accepting input.") from None
                    await asyncio.sleep(0.01)
                except OSError as exc:
                    raise TerminalError(f"Could not write to the terminal: {exc.strerror or exc}") from None

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if self._master is not None:
            with contextlib.suppress(OSError):
                _set_winsize(self._master, rows, cols)

    def _record(self, text: str) -> None:
        self._scrollback += text
        if len(self._scrollback) > self._scrollback_chars:
            self._scrollback = self._scrollback[-self._scrollback_chars:]

    @property
    def scrollback(self) -> str:
        return self._scrollback

    # subscribers ------------------------------------------------------------------------
    def subscribe(self) -> tuple[str, asyncio.Queue]:
        """Scrollback so far plus a queue of later events (``None`` ends the stream: reconnect to resync)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        if self.exited:
            queue.put_nowait({"type": "exit", "code": self.exit_code})
            queue.put_nowait(None)
        else:
            self._subscribers.add(queue)
        return self._scrollback, queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._end(queue, drop_pending=True)  # a slow reader resyncs from the scrollback on reconnect

    def _end(self, queue: asyncio.Queue, *, drop_pending: bool) -> None:
        """End a subscriber's stream after what it has queued (or right away, when it fell behind)."""
        self._subscribers.discard(queue)
        if drop_pending or queue.full():
            while not queue.empty():
                queue.get_nowait()
        queue.put_nowait(None)

    def view(self) -> dict:
        return {"id": self.id, "title": self.title, "location": self.location, "cwd": str(self.cwd),
                "shell": self.argv[0], "repo_id": self.repo_id, "task_id": self.task_id, "running": not self.exited,
                "exit_code": self.exit_code, "created_at": self.created_at}


class TerminalManager:
    def __init__(self, *, shell: str | None, max_terminals: int, scrollback_chars: int) -> None:
        self.shell = shell
        self.max_terminals = max_terminals
        self.scrollback_chars = scrollback_chars
        self._sessions: dict[str, TerminalSession] = {}

    def get(self, terminal_id: str) -> TerminalSession | None:
        return self._sessions.get(terminal_id)

    def list(self, *, repo_id: str | None = None, task_id: str | None = None) -> list[TerminalSession]:
        return [s for s in self._sessions.values()
                if (repo_id is None or s.repo_id == repo_id) and (task_id is None or s.task_id == task_id)]

    def open(self, *, cwd: Path, title: str, location: str, repo_id: str, task_id: str | None,
             env_extra: dict[str, str], cols: int, rows: int, startup_input: str = "") -> TerminalSession:
        if not SUPPORTED:
            raise TerminalError("Terminals are available on macOS and Linux.")
        if sum(1 for s in self._sessions.values() if not s.exited) >= self.max_terminals:
            raise TerminalError(f"At most {self.max_terminals} terminals can run at once. Close one first.")
        session = TerminalSession(argv=shell_argv(self.shell), cwd=cwd, env=terminal_env(env_extra), title=title,
                                  location=location, repo_id=repo_id, task_id=task_id, cols=cols, rows=rows,
                                  scrollback_chars=self.scrollback_chars)
        session.start()
        self._sessions[session.id] = session
        if startup_input:
            asyncio.get_running_loop().create_task(session.write(startup_input))
        return session

    async def close(self, terminal_id: str) -> bool:
        session = self._sessions.pop(terminal_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close_where(self, *, repo_id: str | None = None, task_id: str | None = None) -> None:
        for session in self.list(repo_id=repo_id, task_id=task_id):
            await self.close(session.id)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close(tid) for tid in list(self._sessions)), return_exceptions=True)
