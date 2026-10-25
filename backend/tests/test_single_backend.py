"""One backend per data directory, no double-running a task, and complete task deletion."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from locpilot.agent.runner import TaskConflictError
from tests.conftest import FakeLLM, hotel_script, make_repo
from tests.test_agent import Harness


def _start_backend(data_dir, capture_stderr: bool = False, lock_wait_s: float = 1.0) -> subprocess.Popen:
    # Undrained pipes can block a child on macOS (small pipe buffers), so stderr is discarded unless read.
    env = {**os.environ, "LOCPILOT_LOCK_WAIT_S": str(lock_wait_s)}
    return subprocess.Popen([sys.executable, "-m", "locpilot.main", "--port", "0", "--data-dir", str(data_dir)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
                            text=True, env=env)


def _wait_ready(proc: subprocess.Popen, timeout: float = 60) -> str:
    deadline = time.monotonic() + timeout
    line = ""
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.startswith("LOCPILOT_READY ") or not line:
            break
    return line


def test_second_backend_on_same_data_dir_refuses_to_start(tmp_path):
    first = _start_backend(tmp_path / "data")
    try:
        deadline = time.monotonic() + 60
        line = ""
        while time.monotonic() < deadline:
            line = first.stdout.readline()
            if line.startswith("LOCPILOT_READY "):
                break
        assert line.startswith("LOCPILOT_READY "), line
        assert json.loads(line.split(" ", 1)[1])["port"] > 0
        second = _start_backend(tmp_path / "data", capture_stderr=True)
        out, err = second.communicate(timeout=60)
        assert second.returncode == 4
        assert "Another LocPilot backend is already using" in err
        # A different data directory is fine.
        other = _start_backend(tmp_path / "other")
        assert other.stdout.readline().startswith("LOCPILOT_READY ")
        other.terminate()
        other.wait(timeout=30)
    finally:
        first.terminate()
        first.wait(timeout=30)


def test_restart_waits_for_the_previous_backend_to_exit(tmp_path):
    first = _start_backend(tmp_path / "data")
    try:
        assert _wait_ready(first).startswith("LOCPILOT_READY ")
        # The app restarts the backend: the new one starts while the old one is still shutting down.
        second = _start_backend(tmp_path / "data", lock_wait_s=30)
        time.sleep(1.5)
        assert second.poll() is None  # waiting for the lock, not failing
        first.terminate()
        first.wait(timeout=30)
        assert _wait_ready(second).startswith("LOCPILOT_READY ")
        second.terminate()
        second.wait(timeout=30)
    finally:
        for proc in (first, locals().get("second")):
            if proc is not None and proc.poll() is None:
                proc.kill()


async def test_resume_refuses_while_the_turn_is_still_running(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    script = hotel_script()
    script["understand"] = {**script["understand"], "intent": "root_cause"}
    fake = FakeLLM(script)
    fake.gate, fake.block_step = asyncio.Event(), "analyze_rca"
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause")
        for _ in range(400):
            if "analyze_rca" in fake.calls:
                break
            await asyncio.sleep(0.05)
        # Simulate another process marking it interrupted while this one is still running it.
        h.db.update_task(task_id, status="interrupted")
        with pytest.raises(TaskConflictError):
            await h.manager.resume(task_id)
        fake.gate.set()
        task = await h.wait(task_id)
        assert task["status"] == "completed"
        assert sum(1 for m in h.db.list_messages(task_id) if m["role"] == "assistant") == 1


async def test_deleting_a_task_removes_checkpoints_and_scratch(settings, tmp_path):
    import httpx

    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    async with Harness(settings, FakeLLM(hotel_script())) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        scratch = settings.scratch_dir / task_id
        assert scratch.is_dir()
        config = h.manager.thread_config(task_id, 1)
        assert (await h.manager.graph.aget_state(config)).values
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8420")
        r = await client.delete(f"/api/tasks/{task_id}", headers={"X-LocPilot-Client": "1"})
        await client.aclose()
        assert r.status_code == 200
        assert not (await h.manager.graph.aget_state(config)).values
        assert not scratch.exists()
        assert h.db.get_task(task_id) is None
