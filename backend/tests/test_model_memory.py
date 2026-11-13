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
