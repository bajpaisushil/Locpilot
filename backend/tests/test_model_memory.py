"""The model's RAM is LocPilot's only while LocPilot needs it.

Ollama loads a model on the first request and keeps it for ``ollama_keep_alive`` after the last one.
LocPilot additionally unloads the models *it* loaded when it quits, or when the user switches models,
and never touches models another program loaded.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from locpilot.config import Settings
from tests.test_ollama_launcher import (
    free_port,
    make_fake_ollama,
    ollama_host_value,
    pytestmark,  # noqa: F401 - same platform guard
    started_app,
    stop_app,
)

H = {"X-LocPilot-Client": "1"}


class RunningOllama:
    """An Ollama the user runs themselves (like `brew services start ollama`): not LocPilot's to stop."""

    def __init__(self, tmp_path: Path, preloaded: str = "", models: list[dict] | None = None,
                 **extra_env: str) -> None:
        self.url = f"http://127.0.0.1:{free_port()}"
        self.record = tmp_path / "requests.jsonl"
        self.binary = make_fake_ollama(tmp_path)
        env = {**os.environ, "OLLAMA_HOST": ollama_host_value(self.url), "FAKE_OLLAMA_RECORD": str(self.record),
               "FAKE_OLLAMA_LOADED": preloaded, "FAKE_OLLAMA_MODELS": json.dumps(models) if models else "",
               **extra_env}
        self.proc = subprocess.Popen([str(self.binary), "serve"], env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while True:
            try:
                httpx.get(f"{self.url}/api/version", timeout=1)
                return
            except httpx.HTTPError:
                assert time.monotonic() < deadline, "fake Ollama did not start"
                time.sleep(0.1)

    def loaded(self) -> list[str]:
        return [m["name"] for m in httpx.get(f"{self.url}/api/ps").json()["models"]]

    def requests(self, path: str) -> list[dict]:
        if not self.record.exists():
            return []
        rows = [json.loads(line) for line in self.record.read_text().splitlines()]
        return [r for r in rows if r["path"] == path]

    def stop(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture
def ollama(tmp_path):
    server = RunningOllama(tmp_path, preloaded="llama3.2:3b")  # another program already loaded a model
    yield server
    server.stop()


async def test_quitting_locpilot_frees_the_model_it_loaded(settings, ollama):
    app, client, cm = await started_app(settings, ollama_url=ollama.url, autostart_ollama=True,
                                        ollama_path=ollama.binary, ollama_keep_alive="10m")
    try:
        result = await app.state.llm_factory().chat([{"role": "user", "content": "hi"}], max_tokens=8)
        assert result.content == "ok"
        assert ollama.requests("/api/chat")[-1]["keep_alive"] == "10m"  # the configured idle timeout
        assert set(ollama.loaded()) == {"llama3.2:3b", "mimo-v2.6:9b-q4_K_M"}
        assert not app.state.ollama_launcher.owns_process
    finally:
        await stop_app(client, cm)
    assert [(r["model"], r["keep_alive"]) for r in ollama.requests("/api/generate")] == [("mimo-v2.6:9b-q4_K_M", 0)]
    # Only LocPilot's model is freed; the other program's model and the user's Ollama keep running.
    assert ollama.loaded() == ["llama3.2:3b"]
    assert ollama.proc.poll() is None


async def test_quitting_without_using_the_model_unloads_nothing(settings, ollama):
    app, client, cm = await started_app(settings, ollama_url=ollama.url, autostart_ollama=True,
                                        ollama_path=ollama.binary)
    try:
        assert (await client.get("/api/setup/status")).json()["ready"] is True
    finally:
        await stop_app(client, cm)
    assert ollama.requests("/api/generate") == []  # never loads a model just to unload it
    assert ollama.loaded() == ["llama3.2:3b"]


async def test_switching_models_frees_the_previous_one(settings, ollama):
    app, client, cm = await started_app(settings, ollama_url=ollama.url, autostart_ollama=True,
                                        ollama_path=ollama.binary)
    try:
        await app.state.llm_factory().chat([{"role": "user", "content": "hi"}], max_tokens=8)
        assert "mimo-v2.6:9b-q4_K_M" in ollama.loaded()
        r = await client.put("/api/model/config", json={"model": "llama3.2:3b"}, headers=H)
        assert r.status_code == 200 and r.json()["configured_model"] == "llama3.2:3b"
        assert "mimo-v2.6:9b-q4_K_M" not in ollama.loaded()
        await app.state.llm_factory().chat([{"role": "user", "content": "hi"}], max_tokens=8)
    finally:
        await stop_app(client, cm)
    # Another program had loaded llama3.2:3b before LocPilot used it: it stays, LocPilot's own model is gone.
    assert ollama.loaded() == ["llama3.2:3b"]


async def test_a_model_locpilot_switched_to_and_loaded_is_freed_on_quit(settings, tmp_path):
    ollama = RunningOllama(tmp_path)  # nothing loaded by anyone else
    try:
        app, client, cm = await started_app(settings, ollama_url=ollama.url, autostart_ollama=True,
                                            ollama_path=ollama.binary)
        try:
            await app.state.llm_factory().chat([{"role": "user", "content": "hi"}], max_tokens=8)
            await client.put("/api/model/config", json={"model": "llama3.2:3b"}, headers=H)
            await app.state.llm_factory().chat([{"role": "user", "content": "hi"}], max_tokens=8)
            assert ollama.loaded() == ["llama3.2:3b"]
        finally:
            await stop_app(client, cm)
        assert ollama.loaded() == []
    finally:
        ollama.stop()


def test_keep_alive_must_be_a_duration():
    assert Settings(ollama_keep_alive="10m").ollama_keep_alive == "10m"
    assert Settings(ollama_keep_alive="0").ollama_keep_alive == "0"
    with pytest.raises(ValidationError):
        Settings(ollama_keep_alive="until I quit")


# --- Does the model fit? ------------------------------------------------------------------------------------

LLAMA_8B_INFO = {"general.architecture": "llama", "llama.block_count": 32, "llama.attention.head_count": 32,
                 "llama.attention.head_count_kv": 8, "llama.embedding_length": 4096}


def test_context_memory_is_estimated_from_the_model_architecture():
    from locpilot.llm.ollama import kv_cache_bytes

    f16 = kv_cache_bytes(LLAMA_8B_INFO, 16384, "f16")
    assert f16 == 32 * 8 * (128 + 128) * 16384 * 2  # 2 GiB: the well-known figure for 8B Llama at 16k
    q8 = kv_cache_bytes(LLAMA_8B_INFO, 16384, "q8_0")
    assert 0.5 < q8 / f16 < 0.55  # 8-bit storage: about half
    assert kv_cache_bytes({"general.architecture": "x"}, 16384) is None  # unknown architecture: no guess


def test_available_memory_is_read_on_this_machine():
    from locpilot.llm.ollama import available_memory, physical_memory

    avail, total = available_memory(), physical_memory()
    assert total and avail and 0 < avail <= total


async def test_low_memory_is_reported_before_the_model_is_loaded(monkeypatch, tmp_path):
    from locpilot.agent.graph import memory_warning
    from locpilot.llm import ollama as ollama_mod
    from locpilot.llm.ollama import ModelUsage, OllamaClient

    sizes = tmp_path / "model-sizes.json"
    client = OllamaClient("http://127.0.0.1:11434", "big:8b", num_ctx=16384, kv_cache_type="q8_0",
                          usage=ModelUsage(sizes_path=sizes))

    async def loaded_details():
        return []

    async def list_models():
        return [{"name": "big:8b", "size_bytes": 5 * 2**30}]

    async def show():
        return {"model_info": LLAMA_8B_INFO, "capabilities": ["completion"]}

    monkeypatch.setattr(client, "loaded_details", loaded_details)
    monkeypatch.setattr(client, "list_models", list_models)
    monkeypatch.setattr(client, "_show", show)
    monkeypatch.setattr(ollama_mod, "available_memory", lambda: 4 * 2**30)
    mem = await client.memory_check()
    assert mem["short"] and mem["needed_bytes"] > 6 * 2**30 and mem["kv_cache_type"] == "q8_0"
    text = memory_warning(mem)
    assert "Only 4.0 GB of memory is free" in text and "for the context" in text

    monkeypatch.setattr(ollama_mod, "available_memory", lambda: 12 * 2**30)
    assert memory_warning(await client.memory_check()) is None  # it fits: say nothing

    async def loaded_now():
        return [{"name": "big:8b", "size_bytes": 6 * 2**30, "vram_bytes": 0, "context_length": 16384}]

    monkeypatch.setattr(client, "loaded_details", loaded_now)
    monkeypatch.setattr(ollama_mod, "available_memory", lambda: 1 * 2**30)
    mem = await client.memory_check()
    assert mem["needed_bytes"] == 0 and mem["loaded_bytes"] == 6 * 2**30 and not mem["short"]  # already loaded

    # Unloaded again: the next estimate is what it really took, not the file size + context guess, and that is
    # remembered across restarts.
    monkeypatch.setattr(client, "loaded_details", loaded_details)
    monkeypatch.setattr(ollama_mod, "available_memory", lambda: 12 * 2**30)
    assert (await client.memory_check())["needed_bytes"] == 6 * 2**30
    assert ModelUsage(sizes_path=sizes).sizes == {"big:8b|16384|q8_0": 6 * 2**30}


# MiMo 9B / Qwen3.5 as Ollama reports it: 32 layers, of which only every fourth keeps a growing cache.
MIMO_INFO = {"general.architecture": "qwen35", "qwen35.block_count": 32, "qwen35.attention.head_count": 16,
             "qwen35.attention.head_count_kv": 4, "qwen35.attention.key_length": 256,
             "qwen35.attention.value_length": 256, "qwen35.embedding_length": 4096,
             "qwen35.full_attention_interval": 4, "qwen35.attention.recurrent_layers": [True, True, True, False] * 8}


def test_models_with_cheap_context_get_a_larger_window_by_default(settings, monkeypatch):
    from locpilot.app import default_llm_factory
    from locpilot.llm import ollama as ollama_mod
    from locpilot.llm.ollama import context_for
    from locpilot.storage.db import Database

    url = "http://127.0.0.1:11434"
    gib = 2**30
    monkeypatch.setattr(ollama_mod, "_show_cache", {
        (url, "mimo"): {"model_info": {**MIMO_INFO, "qwen35.context_length": 262144}},
        (url, "llama"): {"model_info": {**LLAMA_8B_INFO, "llama.context_length": 131072}},
        (url, "short"): {"model_info": {**MIMO_INFO, "qwen35.context_length": 8192}},
    })
    assert context_for(url, "mimo", 16384, 16 * gib) == 32768  # 32k costs it about 1 GB
    assert context_for(url, "llama", 16384, 16 * gib) == 16384  # 32k would cost an 8B Llama over 4 GB
    assert context_for(url, "mimo", 16384, 8 * gib) == 16384  # not on a small machine
    assert context_for(url, "short", 16384, 16 * gib) == 16384  # the model was not trained for 32k
    assert context_for(url, "unknown", 16384, 16 * gib) == 16384  # Ollama has not described it (yet)

    monkeypatch.setattr("locpilot.app.physical_memory", lambda: 16 * gib)
    db = Database(settings.db_path)
    try:
        auto = default_llm_factory(settings.model_copy(update={"model": "mimo"}), db)()
        assert (auto.num_ctx, auto.num_ctx_auto) == (32768, True)
        from locpilot.config import Settings

        env = Settings(data_dir=settings.data_dir, model="mimo", num_ctx=16384)  # LOCPILOT_NUM_CTX is a choice
        assert default_llm_factory(env, db)().num_ctx == 16384
        db.set_setting("num_ctx", 8192)  # and so is the value saved in Settings
        chosen = default_llm_factory(settings.model_copy(update={"model": "mimo"}), db)()
        assert (chosen.num_ctx, chosen.num_ctx_auto) == (8192, False)
    finally:
        db.close()


def test_hybrid_models_only_cache_their_attention_layers():
    from locpilot.llm.ollama import kv_cache_bytes

    assert kv_cache_bytes(MIMO_INFO, 16384, "f16") == 8 * 4 * (256 + 256) * 16384 * 2  # 0.5 GiB, not 2 GiB
    by_interval = {k: v for k, v in MIMO_INFO.items() if not k.endswith("recurrent_layers")}
    assert kv_cache_bytes(by_interval, 16384, "f16") == 8 * 4 * (256 + 256) * 16384 * 2
    only_recurrent = {**MIMO_INFO, "qwen35.attention.recurrent_layers": [True] * 32}
    assert kv_cache_bytes(only_recurrent, 16384, "f16") == 0


async def test_free_ram_now_unloads_the_model_but_not_other_programs_models(settings, ollama):
    app, client, cm = await started_app(settings, ollama_url=ollama.url, autostart_ollama=True,
                                        ollama_path=ollama.binary)
    try:
        await app.state.llm_factory().chat([{"role": "user", "content": "hi"}], max_tokens=8)
        assert "mimo-v2.6:9b-q4_K_M" in ollama.loaded()
        r = await client.post("/api/model/unload", headers=H)
        assert r.status_code == 200 and r.json()["unloaded"] == ["mimo-v2.6:9b-q4_K_M"]
        assert ollama.loaded() == ["llama3.2:3b"]  # another program's model stays
    finally:
        await stop_app(client, cm)
