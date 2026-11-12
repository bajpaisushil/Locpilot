"""Starting an installed-but-stopped Ollama, and what the setup screen says when it can't.

A fake ``ollama`` executable stands in for the real one: ``ollama serve`` runs a tiny HTTP server that
answers ``/api/version`` and ``/api/tags`` on ``OLLAMA_HOST``, exactly where LocPilot connects.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from locpilot.app import create_app
from locpilot.llm import launcher as launcher_mod
from locpilot.llm.launcher import ollama_host_value

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="fake ollama is a POSIX script")

FAKE_SERVER = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.argv[1:] != ["serve"]:
    sys.exit(2)
if os.environ.get("FAKE_OLLAMA_MODE") == "fail":
    print("time=... level=INFO msg=\"starting\"", flush=True)
    print("Error: listen tcp: bind: address already in use", flush=True)
    sys.exit(1)
host, _, port = os.environ["OLLAMA_HOST"].rpartition(":")
leaked = sorted(k for k in os.environ if k.upper().startswith("LOCPILOT_"))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/version":
            body = {"version": "0.0.0-fake", "leaked_env": leaked}
        elif self.path == "/api/tags":
            body = {"models": [{"name": "qwen2.5-coder:7b", "size": 4_700_000_000, "details": {}}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

ThreadingHTTPServer((host.strip("[]"), int(port)), Handler).serve_forever()
'''


def make_fake_ollama(tmp_path: Path) -> Path:
    script = tmp_path / "fake_ollama_server.py"
    script.write_text(FAKE_SERVER)
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    binary.chmod(0o755)
    return binary


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def no_ollama_installed(monkeypatch) -> None:
    for name in ("service_units", "app_candidates", "cli_candidates"):
        monkeypatch.setattr(launcher_mod, name, lambda: [])
    monkeypatch.setattr(launcher_mod, "which_ollama", lambda: None)


async def started_app(settings, **overrides):
    app = create_app(settings.model_copy(update=overrides))
    cm = app.router.lifespan_context(app)
    await cm.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8420")
    return app, client, cm


async def stop_app(client, cm) -> None:
    await client.aclose()
    await cm.__aexit__(None, None, None)


def checks_of(setup: dict) -> dict:
    return {c["id"]: c for c in setup["checks"]}


async def test_installed_ollama_is_started_and_stopped_again_on_quit(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCPILOT_AUTH_TOKEN", "must-not-reach-ollama")
    url = f"http://127.0.0.1:{free_port()}"
    app, client, cm = await started_app(settings, ollama_url=url, autostart_ollama=True,
                                        ollama_path=make_fake_ollama(tmp_path))
    try:
        setup = (await client.get("/api/setup/status")).json()
        checks = checks_of(setup)
        assert checks["ollama"]["status"] == "ok", checks["ollama"]
        assert "started by LocPilot" in checks["ollama"]["detail"]
        assert checks["model"]["status"] == "ok" and setup["ready"] is True
        assert setup["ollama_installed"] is True
        launcher = app.state.ollama_launcher
        assert launcher.owns_process
        proc = launcher._proc
        # It listens where LocPilot connects, and never sees LocPilot's own token.
        version = httpx.get(f"{url}/api/version").json()
        assert version["version"] == "0.0.0-fake" and version["leaked_env"] == []
    finally:
        await stop_app(client, cm)
    assert proc.poll() is not None, "LocPilot must stop the ollama serve it started"
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{url}/api/version", timeout=1)


async def test_model_status_restarts_ollama_after_it_stops(settings, tmp_path):
    url = f"http://127.0.0.1:{free_port()}"
    app, client, cm = await started_app(settings, ollama_url=url, autostart_ollama=True,
                                        ollama_path=make_fake_ollama(tmp_path))
    try:
        assert (await client.get("/api/model/status")).json()["ollama_reachable"] is True
        launcher = app.state.ollama_launcher
        first = launcher._proc
        os.killpg(first.pid, signal.SIGKILL)  # Ollama crashes or is killed while LocPilot runs
        first.wait(timeout=10)
        status = (await client.get("/api/model/status")).json()
        assert status["ollama_reachable"] is True and status["model_available"] is True
        assert launcher.owns_process and launcher._proc.pid != first.pid
    finally:
        await stop_app(client, cm)


async def test_setup_explains_when_installed_ollama_cannot_start(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OLLAMA_MODE", "fail")
    port = free_port()
    (settings.logs_dir / "ollama.log").write_text("[GIN] 200 | GET /api/version  <- an earlier Ollama session\n")
    app, client, cm = await started_app(settings, ollama_url=f"http://127.0.0.1:{port}", autostart_ollama=True,
                                        ollama_path=make_fake_ollama(tmp_path))
    try:
        setup = (await client.get("/api/setup/status")).json()
        ollama = checks_of(setup)["ollama"]
        assert ollama["status"] == "error" and setup["ready"] is False
        assert "could not start it" in ollama["detail"] and "address already in use" in ollama["detail"]
        assert f"another program is already using 127.0.0.1:{port}" in ollama["detail"]
        assert "ollama.log" in ollama["hint"]
        assert "earlier Ollama session" not in ollama["detail"] and "level=INFO" not in ollama["detail"]
        assert setup["ollama_installed"] is True
        commands = [s["command"] for s in setup["ollama_install"] if s["command"]]
        # Installed: show how to start it (on the configured port), not how to install it.
        assert commands[0] == f"OLLAMA_HOST=127.0.0.1:{port} ollama serve"
        assert not any("install" in c for c in commands)
        # A failed start is not retried on every 3-second poll.
        again = checks_of((await client.get("/api/setup/status")).json())["ollama"]
        assert again["status"] == "error" and not app.state.ollama_launcher.owns_process
    finally:
        await stop_app(client, cm)


async def test_setup_shows_only_install_steps_when_ollama_is_not_installed(settings, monkeypatch):
    no_ollama_installed(monkeypatch)
    app, client, cm = await started_app(settings, ollama_url=f"http://127.0.0.1:{free_port()}",
                                        autostart_ollama=True)
    try:
        setup = (await client.get("/api/setup/status")).json()
        ollama = checks_of(setup)["ollama"]
        assert ollama["status"] == "missing" and ollama["detail"] == "Ollama is not installed on this computer"
        assert "starts it automatically" in ollama["hint"]
        assert setup["ollama_installed"] is False
        assert setup["ollama_install"][0]["url"].startswith("https://ollama.com/download")
        assert not any("serve" in (s["command"] or "") for s in setup["ollama_install"])
    finally:
        await stop_app(client, cm)


async def test_linux_system_service_is_not_started_as_the_user(settings, tmp_path, monkeypatch):
    unit = tmp_path / "ollama.service"
    unit.write_text("[Service]\nExecStart=/usr/local/bin/ollama serve\n")
    no_ollama_installed(monkeypatch)
    monkeypatch.setattr(launcher_mod, "service_units", lambda: [unit])
    app, client, cm = await started_app(settings, ollama_url=f"http://127.0.0.1:{free_port()}",
                                        autostart_ollama=True)
    try:
        setup = (await client.get("/api/setup/status")).json()
        ollama = checks_of(setup)["ollama"]
        assert ollama["status"] == "missing" and "system service" in ollama["detail"]
        assert setup["ollama_install"][0]["command"] == "sudo systemctl start ollama"
        assert not app.state.ollama_launcher.owns_process
    finally:
        await stop_app(client, cm)


async def test_an_ollama_already_running_is_used_and_left_running(settings, tmp_path):
    fake = make_fake_ollama(tmp_path)
    url = f"http://127.0.0.1:{free_port()}"
    theirs = subprocess.Popen([str(fake), "serve"], env={**os.environ, "OLLAMA_HOST": ollama_host_value(url)},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                httpx.get(f"{url}/api/version", timeout=1)
                break
            except httpx.HTTPError:
                assert time.monotonic() < deadline, "fake Ollama did not start"
                time.sleep(0.1)
        app, client, cm = await started_app(settings, ollama_url=url, autostart_ollama=True, ollama_path=fake)
        try:
            ollama = checks_of((await client.get("/api/setup/status")).json())["ollama"]
            assert ollama["status"] == "ok" and "started by LocPilot" not in ollama["detail"]
            assert not app.state.ollama_launcher.owns_process
        finally:
            await stop_app(client, cm)
        assert theirs.poll() is None, "an Ollama LocPilot did not start must keep running"
    finally:
        theirs.kill()
        theirs.wait(timeout=10)


async def test_autostart_off_reports_installed_but_not_running(settings, tmp_path):
    app, client, cm = await started_app(settings, ollama_url=f"http://127.0.0.1:{free_port()}",
                                        autostart_ollama=False, ollama_path=make_fake_ollama(tmp_path))
    try:
        ollama = checks_of((await client.get("/api/setup/status")).json())["ollama"]
        assert ollama["status"] == "missing" and "is installed" in ollama["detail"]
        assert "LOCPILOT_AUTOSTART_OLLAMA" in ollama["hint"]
        assert not app.state.ollama_launcher.owns_process
    finally:
        await stop_app(client, cm)


def test_ollama_host_matches_the_configured_url():
    assert ollama_host_value("http://127.0.0.1:11434") == "127.0.0.1:11434"
    assert ollama_host_value("http://localhost:11500/") == "localhost:11500"
    assert ollama_host_value("http://[::1]:11434") == "[::1]:11434"
