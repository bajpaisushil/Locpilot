"""HTTP API tests: local-only security, validation, repository/file endpoints, SSE, diffs and rollback."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from locpilot.app import create_app
from locpilot.llm.ollama import OllamaClient
from tests.conftest import FakeLLM, hotel_script, make_repo

H = {"X-LocPilot-Client": "1"}


@pytest.fixture
async def api(settings):
    holders = []

    async def make(fake=None, **overrides):
        s = settings.model_copy(update=overrides)
        app = create_app(s, llm_factory=(lambda: fake) if fake else None)
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8420")
        holders.append((client, cm))
        return app, client

    yield make
    for client, cm in holders:
        await client.aclose()
        await cm.__aexit__(None, None, None)


async def test_health_and_local_only_protections(api):
    app, client = await api()
    assert (await client.get("/api/health")).json()["status"] == "ok"
    # DNS rebinding: foreign Host header
    r = await client.get("/api/repos", headers={"Host": "evil.example.com"})
    assert r.status_code == 403 and r.json()["error"] == "host_not_allowed"
    # Cross-origin request from a website
    r = await client.get("/api/repos", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403 and r.json()["error"] == "origin_not_allowed"
    # Form-style CSRF without the custom header
    r = await client.post("/api/repos", json={"source": "local", "path": "/tmp"})
    assert r.status_code == 403 and r.json()["error"] == "missing_client_header"
    # Allowed origins pass (Tauri + dev server)
    for origin in ("tauri://localhost", "http://tauri.localhost", "http://127.0.0.1:5420"):
        assert (await client.get("/api/repos", headers={"Origin": origin})).status_code == 200


async def test_bearer_token_required_when_configured(api):
    _, client = await api(auth_token="s3cret-token")
    assert (await client.get("/api/health")).status_code == 200  # readiness probe stays public
    assert (await client.get("/api/repos")).status_code == 401
    assert (await client.get("/api/repos", headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.get("/api/repos", headers={"Authorization": "Bearer s3cret-token"})).status_code == 200


async def test_cors_preflight(api):
    _, client = await api()
    r = await client.options("/api/repos", headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "POST",
                                                    "Access-Control-Request-Headers": "x-locpilot-client,content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "tauri://localhost"
    r = await client.options("/api/repos", headers={"Origin": "https://evil.example.com",
                                                    "Access-Control-Request-Method": "POST"})
    # Rejected by CORSMiddleware (400) before reaching handlers; no allow-origin header is granted.
    assert r.status_code in (400, 403) and "access-control-allow-origin" not in r.headers


async def test_repo_registration_validation(api, tmp_path, settings):
    _, client = await api(FakeLLM(hotel_script()))
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    cases = [
        {"source": "local", "path": "relative/path"},
        {"source": "local", "path": str(tmp_path / "missing")},
        {"source": "local", "path": str(plain)},
        {"source": "local", "path": "/etc"},
        {"source": "github", "url": "https://token@github.com/a/b"},
        {"source": "github", "url": "file:///etc/passwd"},
        {"source": "github", "url": "https://evil.com/a/b"},
    ]
    for body in cases:
        r = await client.post("/api/repos", json=body, headers=H)
        assert r.status_code == 400, (body, r.text)
        assert r.json()["error"] == "invalid_repository"
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    r = await client.post("/api/repos", json={"source": "local", "path": str(repo / "offers")}, headers=H)
    assert r.status_code == 201, r.text
    detail = r.json()
    assert detail["path"] == str(repo) and detail["status"]["branch"] == "main"
    assert detail["languages"][0]["name"] == "python" and detail["test_commands"]
    # Re-adding the same repo returns the same record.
    r2 = await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)
    assert r2.json()["id"] == detail["id"]
    assert len((await client.get("/api/repos")).json()) == 1


async def test_github_clone_from_local_remote_is_validated_not_executed(api, tmp_path):
    # Only https/ssh URLs on allowed hosts are accepted; a clone of an unreachable host fails cleanly.
    _, client = await api(FakeLLM(hotel_script()), allowed_git_hosts=["github.invalid"], clone_timeout_s=20)
    r = await client.post("/api/repos", json={"source": "github", "url": "https://github.invalid/a/b"}, headers=H)
    assert r.status_code == 400 and "Clone failed" in r.json()["detail"]


async def test_file_tree_search_and_traversal(api, tmp_path):
    _, client = await api(FakeLLM(hotel_script()))
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    (repo / ".env").write_text("TOKEN=abc\n")
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    tree = (await client.get(f"/api/repos/{rid}/tree")).json()
    names = {e["name"] for e in tree}
    assert {"offers", "tests", "README.md"} <= names and ".env" not in names
    f = (await client.get(f"/api/repos/{rid}/file", params={"path": "offers/aggregator.py"})).json()
    assert f["language"] == "python" and "def merge_offers" in f["content"] and f["total_lines"] > 10
    part = (await client.get(f"/api/repos/{rid}/file", params={"path": "offers/aggregator.py", "start": 6, "end": 7})).json()
    assert part["content"].startswith("def merge_offers")
    for bad in ("../../etc/passwd", "/etc/passwd", ".git/config", ".env", "offers/../../x"):
        r = await client.get(f"/api/repos/{rid}/file", params={"path": bad})
        assert r.status_code in (400, 403), (bad, r.status_code)
    hits = (await client.get(f"/api/repos/{rid}/search", params={"q": "dedupe_key"})).json()
    assert any(h["path"] == "offers/models.py" for h in hits)
    assert (await client.get(f"/api/repos/{rid}/search", params={"q": "(", "regex": "true"})).status_code == 400


async def test_task_creation_requires_a_model(api, tmp_path):
    # Real Ollama client pointed at a closed port: the API must refuse with a setup hint, never fake an answer.
    _, client = await api(ollama_url="http://127.0.0.1:9")
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    r = await client.post("/api/tasks", json={"repo_id": rid, "mode": "ask", "message": "Explain merge_offers"},
                          headers=H)
    assert r.status_code == 503 and r.json()["error"] == "model_unavailable"
    assert "ollama" in (r.json()["hint"] or "").lower()
    setup = (await client.get("/api/setup/status")).json()
    assert setup["ready"] is False and setup["cloud_fallback"] is False
    by_id = {c["id"]: c for c in setup["checks"]}
    assert by_id["ollama"]["status"] == "missing" and by_id["git"]["status"] == "ok"
    assert setup["ollama_install"] and setup["model_install"][0]["command"].startswith("ollama pull")


def test_remote_ollama_is_refused_by_default():
    client = OllamaClient("https://api.example.com", "m")
    with pytest.raises(Exception) as info:
        asyncio.run(client.ensure_ready())
    assert "non-local" in str(info.value)


async def test_full_task_flow_over_http_with_diff_patch_and_rollback(api, tmp_path):
    app, client = await api(FakeLLM(hotel_script()))
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    r = await client.post("/api/tasks", json={"repo_id": rid, "mode": "code", "message": "Fix duplicate offers"},
                          headers=H)
    assert r.status_code == 201
    task_id = r.json()["id"]
    manager = app.state.manager
    for _ in range(600):
        if not manager.is_running(task_id):
            break
        await asyncio.sleep(0.05)
    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert detail["status"] == "awaiting_approval" and detail["pending_approval"]
    # A second message while awaiting approval is rejected.
    r = await client.post(f"/api/tasks/{task_id}/messages", json={"message": "hi"}, headers=H)
    assert r.status_code == 409
    approval = detail["pending_approval"]
    r = await client.post(f"/api/tasks/{task_id}/approvals/{approval['id']}",
                          json={"decision": "approve", "commands": [c["argv"] for c in approval["payload"]["commands"]]},
                          headers=H)
    assert r.status_code == 200
    for _ in range(600):
        if not manager.is_running(task_id):
            break
        await asyncio.sleep(0.05)
    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert detail["status"] == "completed" and detail["reports"][-1]["status"] == "changes_verified"
    assert detail["commands"] and detail["commands"][0]["stdout"] is None  # summaries only
    full = (await client.get(f"/api/tasks/{task_id}/commands/{detail['commands'][-1]['id']}")).json()
    assert "OK" in (full["stderr"] + full["stdout"])
    diff = (await client.get(f"/api/tasks/{task_id}/diff")).json()
    paths = {f["path"]: f for f in diff["files"]}
    assert paths["offers/aggregator.py"]["change"] == "modify" and paths["tests/test_cross_supplier_duplicates.py"]["change"] == "create"
    patch = await client.get(f"/api/tasks/{task_id}/patch")
    assert patch.headers["content-type"].startswith("text/x-diff") and "+++ b/offers/aggregator.py" in patch.text
    # The downloaded patch applies cleanly to the user's checkout (they stay in control).
    import subprocess

    check = subprocess.run(["git", "apply", "--check", "-"], input=patch.text, text=True, cwd=repo, capture_output=True)
    assert check.returncode == 0, check.stderr
    worktree = Path(detail["worktree_path"])
    rb = (await client.post(f"/api/tasks/{task_id}/rollback", headers=H)).json()
    assert set(rb["restored"]) == {"offers/aggregator.py", "tests/test_cross_supplier_duplicates.py"}
    assert not (worktree / "tests/test_cross_supplier_duplicates.py").exists()
    assert (worktree / "offers/aggregator.py").read_text() == (repo / "offers/aggregator.py").read_text()
    # Delete removes the worktree but never touches the user's repository.
    assert (await client.delete(f"/api/tasks/{task_id}", headers=H)).json()["ok"]
    assert not worktree.exists() and (repo / "offers/aggregator.py").exists()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sse_stream_replays_and_resumes_after_reconnect(settings, tmp_path):
    fake = FakeLLM({**hotel_script(), "understand": {**hotel_script()["understand"], "intent": "root_cause"}})
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
        task_id = httpx.post(f"{base}/api/tasks", json={"repo_id": rid, "mode": "ask", "message": "Why duplicates?"},
                             headers=H).json()["id"]
        seen: list[dict] = []
        with httpx.stream("GET", f"{base}/api/tasks/{task_id}/events", timeout=30) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    seen.append(event)
                    if event["type"] == "final":
                        break
        ids = [e["id"] for e in seen if e["id"] is not None]
        assert ids == sorted(ids) and any(e["type"] == "report" for e in seen)
        # Reconnect with Last-Event-ID: only newer events are replayed.
        middle = ids[len(ids) // 2]
        replay: list[int] = []
        with httpx.stream("GET", f"{base}/api/tasks/{task_id}/events", headers={"Last-Event-ID": str(middle)},
                          timeout=5) as resp:
            try:
                for line in resp.iter_lines():
                    if line.startswith("id: "):
                        replay.append(int(line[4:]))
                        if replay[-1] >= ids[-1]:
                            break
            except httpx.ReadTimeout:
                pass
        assert replay and min(replay) > middle
    finally:
        server.should_exit = True
        thread.join(timeout=10)
