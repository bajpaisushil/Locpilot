"""How full the model's context window gets, and clearing a task's context so the next message starts fresh."""

from __future__ import annotations

import httpx

from tests.conftest import FakeLLM, make_repo
from tests.test_agent import Harness, rca_only_script

H = {"X-LocPilot-Client": "1"}


async def test_each_model_call_records_how_full_the_context_was(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    async with Harness(settings, FakeLLM(rca_only_script())) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause of duplicate hotel offers")
        assert (await h.wait(task_id))["status"] == "completed"
        stats = h.db.get_task(task_id)["stats"]
    assert stats["context_tokens"] == 100  # the fake model reports 100 prompt tokens per call
    assert stats["context_window"] == settings.num_ctx


async def test_clearing_the_context_starts_the_next_message_fresh(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    async with Harness(settings, FakeLLM(rca_only_script())) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause of duplicate hotel offers")
        await h.wait(task_id)
        carried = await h.manager._carry(task_id, 2)
        assert carried["history"] and carried["evidence"] and carried["report"]  # normally the next turn builds on these

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8420") as c:
            r = await c.post(f"/api/tasks/{task_id}/context/clear", headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["stats"]["context_cleared_turn"] == 1 and r.json()["stats"]["context_tokens"] == 0

        fresh = await h.manager._carry(task_id, 2)
        for key in ("history", "evidence", "report", "plan", "notes"):
            assert key not in fresh
        assert fresh["repo_id"] == carried["repo_id"]  # what the task works on stays
        # The marker only applies to the turn right after the clear.
        assert (await h.manager._carry(task_id, 3)) == dict(
            (await h.manager.graph.aget_state(h.manager.thread_config(task_id, 2))).values or {})


async def test_the_context_cannot_be_cleared_while_a_turn_waits_for_approval(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    async with Harness(settings, FakeLLM(rca_only_script())) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause of duplicate hotel offers")
        await h.wait(task_id)
        h.db.update_task(task_id, status="awaiting_approval")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://127.0.0.1:8420") as c:
            r = await c.post(f"/api/tasks/{task_id}/context/clear", headers=H)
        assert r.status_code == 409 and r.json()["error"] == "task_busy"
