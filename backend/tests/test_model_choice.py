"""Which model LocPilot uses, and whether a thinking model reasons first.

- If the configured model isn't installed but other chat models are (say you ran `ollama rm` on the default
  and `ollama pull` on another), LocPilot switches to an installed one instead of blocking on setup.
- Thinking models (MiMo, Qwen3, …) think by default. LocPilot tells them explicitly whether to, per step,
  gives the reasoning its own token budget, and answers without thinking if the reasoning never ends.
"""

from __future__ import annotations

import json

import pytest

from locpilot.app import create_app
from locpilot.config import Settings
from locpilot.llm.ollama import OllamaClient, pick_installed_model, reasoning_in_circles
from tests.conftest import FakeLLM, make_repo
from tests.test_agent import Harness, rca_only_script
from tests.test_model_memory import RunningOllama
from tests.test_ollama_launcher import checks_of, pytestmark, started_app, stop_app  # noqa: F401

H = {"X-LocPilot-Client": "1"}
GB = 1_000_000_000
MIMO = {"name": "mimo-v2.6:9b-q4_K_M", "size": 6_550_811_033, "capabilities": ["completion", "vision", "tools",
                                                                                "thinking"]}
EMBED = {"name": "nomic-embed-text:latest", "size": 274_302_450, "capabilities": ["embedding"]}
CODER = {"name": "qwen2.5-coder:7b", "size": 4_683_087_332, "capabilities": ["completion", "tools", "insert"]}


@pytest.fixture
def coder_only(tmp_path):
    server = RunningOllama(tmp_path, models=[EMBED, CODER])  # the user removed the default model (or never had it)
    yield server
    server.stop()


# Picking a model -------------------------------------------------------------------------------------------


def m(name, size_gb, caps=("completion",), modified="2026-11-01T00:00:00Z"):
    return {"name": name, "size_bytes": int(size_gb * GB), "capabilities": list(caps) if caps is not None else None,
            "modified_at": modified}


def test_pick_prefers_the_largest_model_that_fits_comfortably():
    models = [m("small:3b", 2.0), m("mid:9b", 6.6), m("big:32b", 20.0), m("embed", 0.3, caps=["embedding"])]
    assert pick_installed_model(models, ram_bytes=16 * GB) == "mid:9b"  # 20 GB > half of 16 GB
    assert pick_installed_model(models, ram_bytes=64 * GB) == "big:32b"
    assert pick_installed_model(models, ram_bytes=8 * GB) == "small:3b"


def test_pick_falls_back_sensibly():
    too_big = [m("huge:70b", 40.0), m("big:32b", 20.0)]
    assert pick_installed_model(too_big, ram_bytes=16 * GB) == "big:32b"  # none fits: the smallest
    recent = [m("old:7b", 4.7, modified="2026-10-01T00:00:00Z"), m("new:3b", 2.0, modified="2026-11-12T00:00:00Z")]
    assert pick_installed_model(recent, ram_bytes=None) == "new:3b"  # RAM unknown: the latest download
    older_ollama = [m("nomic-embed-text:latest", 0.3, caps=None), m("llama3.2:3b", 2.0, caps=None)]
    assert pick_installed_model(older_ollama, ram_bytes=16 * GB) == "llama3.2:3b"
    assert pick_installed_model([m("embed", 0.3, caps=["embedding"])], ram_bytes=16 * GB) is None
    assert pick_installed_model([], ram_bytes=16 * GB) is None


# Using an installed model ------------------------------------------------------------------------------------


def test_the_default_model_and_thinking_mode():
    defaults = Settings.model_fields
    assert defaults["model"].default == MIMO["name"]  # the explicit tag: plain `9b` is an 8.7 GB MLX build on Macs
    assert defaults["thinking"].default == "off"  # thinking made LocPilot slower and no more accurate


async def test_an_installed_model_replaces_the_missing_configured_one(settings, coder_only):
    assert settings.model == MIMO["name"]
    app, client, cm = await started_app(settings, ollama_url=coder_only.url)
    try:
        setup = (await client.get("/api/setup/status")).json()
        assert setup["ready"] is True and setup["model"] == CODER["name"]
        detail = checks_of(setup)["model"]["detail"]
        assert f"{MIMO['name']} is not installed, so LocPilot uses this model" in detail
        assert app.state.db.get_setting("model") == CODER["name"]  # remembered, like a choice in Settings
        status = (await client.get("/api/model/status")).json()
        assert status["configured_model"] == CODER["name"] and status["switched_from"] == MIMO["name"]
        # Choosing it in Settings makes it the user's choice: no more "switched from" note.
        r = await client.put("/api/model/config", json={"model": CODER["name"]}, headers=H)
        assert r.status_code == 200 and r.json()["switched_from"] is None
    finally:
        await stop_app(client, cm)


async def test_a_task_uses_the_installed_model(settings, coder_only, tmp_path):
    app, client, cm = await started_app(settings, ollama_url=coder_only.url)
    try:
        repo = app.state.db.add_repo(name="r", source_type="local", source_url=None, managed=False,
                                     path=str(make_repo(tmp_path / "r", files={"a.py": "x = 1\n"})))
        # Straight to a task, without the setup screen ever asking.
        r = await client.post("/api/tasks", json={"repo_id": repo["id"], "message": "What is x?", "mode": "ask"},
                              headers=H)
        assert r.status_code == 201, r.text
        assert r.json()["model"] == CODER["name"]
        await app.state.manager.cancel(r.json()["id"])
    finally:
        await stop_app(client, cm)


async def test_no_switch_while_the_configured_model_downloads(settings, coder_only):
    app, client, cm = await started_app(settings, ollama_url=coder_only.url)
    try:
        app.state.pull.update(model=MIMO["name"], state="pulling")
        setup = (await client.get("/api/setup/status")).json()
        assert setup["ready"] is False and setup["model"] == MIMO["name"]
        app.state.pull.update(state="error")
        assert (await client.get("/api/setup/status")).json()["model"] == CODER["name"]
    finally:
        await stop_app(client, cm)


async def test_embedding_models_are_not_used_for_chat(settings, tmp_path):
    ollama = RunningOllama(tmp_path, models=[EMBED])
    try:
        app, client, cm = await started_app(settings, ollama_url=ollama.url)
        try:
            setup = (await client.get("/api/setup/status")).json()
            assert setup["ready"] is False and setup["model"] == MIMO["name"]
            assert checks_of(setup)["model"]["status"] == "missing"
        finally:
            await stop_app(client, cm)
    finally:
        ollama.stop()


async def test_an_installed_configured_model_is_left_alone(settings, tmp_path):
    ollama = RunningOllama(tmp_path, models=[MIMO, CODER])
    try:
        app, client, cm = await started_app(settings, ollama_url=ollama.url)
        try:
            setup = (await client.get("/api/setup/status")).json()
            assert setup["ready"] is True and setup["model"] == MIMO["name"]
            assert "switched" not in checks_of(setup)["model"]["detail"]
            assert app.state.db.get_setting("model") is None
        finally:
            await stop_app(client, cm)
    finally:
        ollama.stop()


# Thinking ---------------------------------------------------------------------------------------------------


def client_for(ollama: RunningOllama, model: str) -> OllamaClient:
    return OllamaClient(ollama.url, model, thinking_tokens=4096)


async def test_a_thinking_model_is_told_whether_to_think(tmp_path):
    ollama = RunningOllama(tmp_path, models=[MIMO])
    try:
        llm = client_for(ollama, MIMO["name"])
        quick = await llm.chat([{"role": "user", "content": "hi"}], max_tokens=500)
        assert quick.content == "ok" and quick.thinking == ""
        deep = await llm.chat([{"role": "user", "content": "hi"}], max_tokens=500, think=True)
        assert deep.content == "ok" and deep.thinking == "let me see"
        sent = [(r["think"], r["num_predict"]) for r in ollama.requests("/api/chat")]
        # Off explicitly (it would think by default); when on, the reasoning gets a budget of its own.
        assert sent == [(False, 500), (True, 500 + 4096)]
    finally:
        ollama.stop()


async def test_models_that_cannot_think_get_no_think_flag(tmp_path):
    older = {"name": "llama3.2:3b", "size": 2 * GB}  # an Ollama version that doesn't report capabilities
    ollama = RunningOllama(tmp_path, models=[CODER, older])
    try:
        for model in (CODER["name"], older["name"]):
            result = await client_for(ollama, model).chat([{"role": "user", "content": "hi"}], max_tokens=64,
                                                          think=True)
            assert result.content == "ok"
        assert [(r["think"], r["num_predict"]) for r in ollama.requests("/api/chat")] == [("unset", 64)] * 2
    finally:
        ollama.stop()


async def test_reasoning_that_never_ends_still_gets_an_answer(tmp_path):
    ollama = RunningOllama(tmp_path, models=[MIMO], FAKE_OLLAMA_THINK_FOREVER="1")
    try:
        result = await client_for(ollama, MIMO["name"]).chat([{"role": "user", "content": "hi"}], max_tokens=300,
                                                             think=True)
        assert result.content == "ok" and result.thinking == "hmm "
        assert result.completion_tokens == 9 + 1  # both calls count
        assert [(r["think"], r["num_predict"]) for r in ollama.requests("/api/chat")] == [(True, 4396), (False, 300)]
    finally:
        ollama.stop()


async def test_an_empty_answer_without_thinking_is_asked_again_with_thinking(tmp_path):
    # Seen with MiMo on edit prompts: 52 tokens generated, nothing returned, and the step failed with "no edits".
    ollama = RunningOllama(tmp_path, models=[MIMO], FAKE_OLLAMA_SILENT_WITHOUT_THINKING="1")
    try:
        result = await client_for(ollama, MIMO["name"]).chat([{"role": "user", "content": "hi"}], max_tokens=300)
        assert result.content == "ok" and result.thinking == "let me see"
        assert result.completion_tokens == 52 + 1  # both calls count
        assert [(r["think"], r["num_predict"]) for r in ollama.requests("/api/chat")] == [(False, 300), (True, 4396)]
    finally:
        ollama.stop()


async def test_structured_output_cut_off_is_retried_with_a_larger_budget():
    from pydantic import BaseModel

    from locpilot.llm.ollama import LLMResult, chat_json

    class Answer(BaseModel):
        text: str

    class Verbose:
        model = "verbose"

        def __init__(self) -> None:
            self.budgets: list[int] = []

        async def chat(self, messages, *, schema=None, max_tokens=1024, on_progress=None, temperature=None,
                       think=False):
            self.budgets.append(max_tokens)
            if max_tokens < 4000:  # the answer needs more than the first budget
                return LLMResult(content='{"text": "a long analys', prompt_tokens=1, completion_tokens=max_tokens,
                                 duration_ms=1, model=self.model, truncated=True)
            return LLMResult(content='{"text": "done"}', prompt_tokens=1, completion_tokens=5, duration_ms=1,
                             model=self.model)

    client = Verbose()
    answer, results = await chat_json(client, [{"role": "user", "content": "analyze"}], Answer, max_tokens=3000)
    assert answer.text == "done" and client.budgets == [3000, 4096] and len(results) == 2


def steps(n: int) -> list[str]:
    return [f"step {i}, " for i in range(n)]  # reasoning that keeps moving on


async def test_reasoning_within_its_budget_is_kept(tmp_path):
    ollama = RunningOllama(tmp_path, models=[MIMO], FAKE_OLLAMA_REASONING=json.dumps(steps(400)))
    try:
        llm = OllamaClient(ollama.url, MIMO["name"], thinking_tokens=1024)
        result = await llm.chat([{"role": "user", "content": "hi"}], max_tokens=300, think=True)
        assert result.content == "ok" and result.thinking == "".join(steps(400))
        assert [(r["think"], r["num_predict"]) for r in ollama.requests("/api/chat")] == [(True, 1324)]
    finally:
        ollama.stop()


async def test_reasoning_is_cut_off_at_its_budget(tmp_path):
    # Ollama would let it reason into the answer's share of num_predict; LocPilot stops it at the budget instead.
    ollama = RunningOllama(tmp_path, models=[MIMO], FAKE_OLLAMA_REASONING=json.dumps(steps(500)))
    try:
        llm = OllamaClient(ollama.url, MIMO["name"], thinking_tokens=256)
        result = await llm.chat([{"role": "user", "content": "hi"}], max_tokens=300, think=True)
        assert result.content == "ok" and result.thinking == "".join(steps(256))
        assert result.truncated is False
        assert [(r["think"], r["num_predict"]) for r in ollama.requests("/api/chat")] == [(True, 556), (False, 300)]
    finally:
        ollama.stop()


async def test_reasoning_in_circles_is_stopped_early(tmp_path):
    # Seen with MiMo: the same few sentences over and over until the budget ran out, 14 minutes for one step.
    circle = [f"Maybe the bug is in part {i}? No, that part looks fine. " for i in range(8)]
    ollama = RunningOllama(tmp_path, models=[MIMO], FAKE_OLLAMA_REASONING=json.dumps(steps(40) + circle * 100))
    try:
        result = await client_for(ollama, MIMO["name"]).chat([{"role": "user", "content": "hi"}], max_tokens=300,
                                                             think=True)
        assert result.content == "ok"
        assert len(result.thinking) < len("".join(steps(40) + circle * 5))  # a few rounds, not 4096 tokens
        assert [(r["think"], r["num_predict"]) for r in ollama.requests("/api/chat")] == [(True, 4396), (False, 300)]
    finally:
        ollama.stop()


def test_what_counts_as_reasoning_in_circles():
    assert not reasoning_in_circles("".join(steps(2000)))
    code = "".join(f"    total += item.price * {i}\n" for i in range(12))  # ~400 characters of code
    quoted_twice = "Reading it: " + code + "".join(steps(30)) + "Again: " + code
    assert not reasoning_in_circles(quoted_twice)  # re-reading code is not a loop
    assert reasoning_in_circles("Hmm. " + "Maybe it is the sort? No, the sort is fine. Maybe the merge? " * 20)


@pytest.mark.parametrize(("mode", "thinking_steps"), [
    ("auto", {"analyze_rca"}),
    ("off", set()),
    ("on", {"understand", "plan_evidence", "analyze_rca"}),
])
async def test_which_agent_steps_think(settings, tmp_path, mode, thinking_steps):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    fake = FakeLLM(rca_only_script())
    async with Harness(settings.model_copy(update={"thinking": mode}), fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause of duplicate hotel offers")
        assert (await h.wait(task_id))["status"] == "completed"
    assert set(fake.thinks) >= {"understand", "analyze_rca"}
    assert {step for step, flags in fake.thinks.items() if any(flags)} == thinking_steps


def test_create_app_passes_the_thinking_budget(settings):
    app = create_app(settings.model_copy(update={"thinking_tokens": 2048}))
    assert app.state.llm_factory().thinking_tokens == 2048
