"""Interactive terminals: a real shell on a real pseudo-terminal (POSIX only)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from locpilot.app import create_app
from locpilot.tools.terminal import TerminalManager
from tests.conftest import FakeLLM, hotel_script, make_repo

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="terminals are POSIX-only")

SHELL = "/bin/bash --noprofile --norc"
H = {"X-LocPilot-Client": "1"}


def _manager() -> TerminalManager:
    return TerminalManager(shell=SHELL, max_terminals=3, scrollback_chars=100_000)


async def _until(session, pattern: str, timeout: float = 10) -> re.Match:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        m = re.search(pattern, session.scrollback)
        if m:
            return m
        await asyncio.sleep(0.05)
    raise AssertionError(f"{pattern!r} not seen; terminal shows:\n{session.scrollback[-2000:]}")


def _open(manager: TerminalManager, cwd: Path, **kw):
    return manager.open(cwd=cwd, title="t", location="repository", repo_id="r_1", task_id=None,
                        env_extra=kw.pop("env_extra", {}), cols=80, rows=24, **kw)


@pytest.mark.asyncio
async def test_shell_runs_on_a_real_tty_without_locpilot_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCPILOT_AUTH_TOKEN", "super-secret-token")
    manager = _manager()
    session = _open(manager, tmp_path)
    try:
        await session.write('echo "sum=$((6*7)) token=${LOCPILOT_AUTH_TOKEN:-none} term=$TERM"; tty; pwd\n')
        await _until(session, r"sum=42 token=none term=xterm-256color")
        await _until(session, r"/dev/(ttys|pts/)\w+")
        await _until(session, re.escape(str(tmp_path.resolve())) + r"|" + re.escape(str(tmp_path)))
        assert "super-secret-token" not in session.scrollback
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_ctrl_c_interrupts_the_running_program_and_resize_reaches_it(tmp_path):
    manager = _manager()
    session = _open(manager, tmp_path)
    try:
        await session.write("sleep 30\n")
        await asyncio.sleep(0.5)
        start = time.monotonic()
        await session.write("\x03")
        await session.write('echo "status=$?"\n')
        await _until(session, r"status=130", timeout=5)  # 128 + SIGINT: the foreground job got Ctrl-C
        assert time.monotonic() - start < 5
        session.resize(132, 41)
        await session.write("stty size\n")
        await _until(session, r"41 132")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_exit_code_is_reported_and_late_subscribers_get_the_scrollback(tmp_path):
    manager = _manager()
    session = _open(manager, tmp_path)
    _, queue = session.subscribe()
    await session.write("echo goodbye-from-shell; exit 3\n")
    events = []
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=10)
        if event is None:
            break
        events.append(event)
    assert events[-1] == {"type": "exit", "code": 3} and not session.view()["running"]
    scrollback, late = session.subscribe()
    assert "goodbye-from-shell" in scrollback
    assert late.get_nowait() == {"type": "exit", "code": 3} and late.get_nowait() is None
    with pytest.raises(Exception, match="exited"):
        await session.write("echo nope\n")
    await manager.shutdown()


@pytest.mark.asyncio
async def test_closing_hangs_up_background_jobs_and_limits_apply(tmp_path):
    manager = _manager()
    session = _open(manager, tmp_path)
    await session.write("sleep 300 & echo bg-pid=$!\n")
    pid = int((await _until(session, r"bg-pid=(\d+)")).group(1))
    os.kill(pid, 0)  # running
    assert await manager.close(session.id)
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("background job survived closing the terminal")
    assert manager.get(session.id) is None and not await manager.close(session.id)
    sessions = [_open(manager, tmp_path) for _ in range(3)]
    with pytest.raises(Exception, match="At most 3 terminals"):
        _open(manager, tmp_path)
    await manager.shutdown()
    assert all(not s.view()["running"] for s in sessions)


@pytest.mark.asyncio
async def test_virtualenv_activation_is_typed_like_vs_code(tmp_path):
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text('export VIRTUAL_ENV_PROMPT="(demo)"\necho activated-demo-venv\n')
    manager = _manager()
    session = _open(manager, tmp_path, startup_input=f"source {venv / 'bin' / 'activate'}\n")
    try:
        await _until(session, r"activated-demo-venv")
    finally:
        await manager.shutdown()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_terminal_api_opens_in_the_task_worktree_and_streams(settings, tmp_path):
    settings = settings.model_copy(update={"terminal_shell": SHELL})
    fake = FakeLLM(hotel_script())
    app = create_app(settings, llm_factory=lambda: fake)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                httpx.get(f"{base}/api/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        repo = make_repo(tmp_path / "hotel", "hotel-offers")
        rid = httpx.post(f"{base}/api/repos", json={"source": "local", "path": str(repo)}, headers=H).json()["id"]
        task_id = httpx.post(f"{base}/api/tasks", json={"repo_id": rid, "mode": "code", "message": "Fix duplicates"},
                             headers=H).json()["id"]
        for _ in range(600):
            detail = httpx.get(f"{base}/api/tasks/{task_id}").json()
            if detail["status"] == "awaiting_approval":
                break
            time.sleep(0.05)
        worktree = detail["worktree_path"]

        # Repository terminal (no task) and worktree terminal (Code-mode task).
        r = httpx.post(f"{base}/api/terminals", json={"repo_id": rid}, headers=H)
        assert r.status_code == 201 and r.json()["location"] == "repository" and r.json()["cwd"] == str(repo)
        repo_term = r.json()["id"]
        r = httpx.post(f"{base}/api/terminals", json={"repo_id": rid, "task_id": task_id, "cols": 100, "rows": 30},
                       headers=H)
        assert r.status_code == 201, r.text
        term = r.json()
        assert term["location"] == "worktree" and term["cwd"] == worktree and term["running"]
        listed = httpx.get(f"{base}/api/terminals", params={"task_id": task_id}).json()
        assert [t["id"] for t in listed] == [term["id"]]

        # Type a command and read it back from the event stream.
        assert httpx.post(f"{base}/api/terminals/{term['id']}/input", headers=H,
                          json={"data": 'echo "where=$(pwd) py=$PYTHONPATH"\n'}).status_code == 204
        assert httpx.post(f"{base}/api/terminals/{term['id']}/resize", headers=H,
                          json={"cols": 120, "rows": 40}).status_code == 204
        text = ""
        with httpx.stream("GET", f"{base}/api/terminals/{term['id']}/stream", timeout=15) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    text += json.loads(line[6:]).get("data", "")
                    if f"where={worktree} py=" in text and worktree in text.split("py=", 1)[-1]:
                        break
        assert httpx.post(f"{base}/api/terminals/{term['id']}/input", json={"data": "x"}).status_code == 403
        assert httpx.delete(f"{base}/api/terminals/{term['id']}", headers=H).json()["ok"]
        assert httpx.post(f"{base}/api/terminals/{term['id']}/input", headers=H,
                          json={"data": "x"}).status_code == 404
        # Deleting the task closes its terminals; the repository terminal is unaffected until the repo goes.
        assert httpx.delete(f"{base}/api/tasks/{task_id}", headers=H).json()["ok"]
        assert [t["id"] for t in httpx.get(f"{base}/api/terminals").json()] == [repo_term]
        assert httpx.delete(f"{base}/api/repos/{rid}", headers=H).json()["ok"]
        assert httpx.get(f"{base}/api/terminals").json() == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
