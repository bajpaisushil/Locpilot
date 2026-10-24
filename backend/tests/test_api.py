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


def test_env_lists_keep_urls_intact(monkeypatch, tmp_path):
    from locpilot.config import Settings

    monkeypatch.setenv("LOCPILOT_ALLOWED_ORIGINS", "http://127.0.0.1:5430, http://localhost:5430")
    monkeypatch.setenv("LOCPILOT_ALLOWED_REPO_ROOTS", f"{tmp_path / 'a'}:{tmp_path / 'b'},{tmp_path / 'c'}")
    s = Settings()
    assert s.allowed_origins == ["http://127.0.0.1:5430", "http://localhost:5430"]
    assert s.allowed_repo_roots == [tmp_path / "a", tmp_path / "b", tmp_path / "c"]


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


async def _wait_until_false(predicate, task_id: str) -> None:
    for _ in range(600):
        if not predicate(task_id):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"{predicate.__name__} stayed true")


async def test_user_commands_run_in_the_worktree_and_reach_the_next_turn(api, tmp_path):
    script = hotel_script()
    seen: dict[str, list[str]] = {"understand": [], "review": []}

    def capture(step: str, value: dict):
        def responder(messages):
            seen[step].append(messages[1]["content"])
            return value
        return responder

    fake = FakeLLM(script)
    app, client = await api(fake)
    manager = app.state.manager
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    (repo / ".venv" / "bin").mkdir(parents=True)  # ignored by Git: only exists in the user's checkout
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    task_id = (await client.post("/api/tasks", json={"repo_id": rid, "mode": "code", "message": "Fix duplicate offers"},
                                 headers=H)).json()["id"]
    await _wait_until_false(manager.is_running, task_id)
    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    worktree = Path(detail["worktree_path"])
    approval = detail["pending_approval"]
    decision = {"decision": "approve", "commands": [c["argv"] for c in approval["payload"]["commands"]]}

    # While the user's command runs, approving (which edits the worktree) and a second command are refused.
    r = await client.post(f"/api/tasks/{task_id}/commands",
                          json={"command": "echo waiting; touch started.flag; sleep 30"}, headers=H)
    assert r.status_code == 202 and r.json()["stage"] == "user" and r.json()["status"] == "running"
    long_id = r.json()["id"]
    for _ in range(200):
        if (worktree / "started.flag").exists():
            break
        await asyncio.sleep(0.05)
    r = await client.post(f"/api/tasks/{task_id}/approvals/{approval['id']}", json=decision, headers=H)
    assert r.status_code == 409 and r.json()["error"] == "approval_conflict"
    assert (await client.post(f"/api/tasks/{task_id}/commands", json={"command": "true"}, headers=H)).status_code == 409
    r = await client.post(f"/api/tasks/{task_id}/commands/{long_id}/stop", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["exit_code"] is None
    assert "waiting" in (await client.get(f"/api/tasks/{task_id}/commands/{long_id}")).json()["stdout"]
    assert (await client.post(f"/api/tasks/{task_id}/commands/{long_id}/stop", headers=H)).status_code == 409

    assert (await client.post(f"/api/tasks/{task_id}/approvals/{approval['id']}", json=decision,
                              headers=H)).status_code == 200
    await _wait_until_false(manager.is_running, task_id)
    assert (await client.get(f"/api/tasks/{task_id}")).json()["status"] == "completed"

    command = ('echo hello-from-run; pwd; touch created-by-run.txt; echo "CI=${CI-unset} VENV=${VIRTUAL_ENV-none}"; '
               'echo "File \\"$PWD/offers/aggregator.py\\", line 14"; exit 3')
    r = await client.post(f"/api/tasks/{task_id}/commands", json={"command": command}, headers=H)
    assert r.status_code == 202
    await _wait_until_false(manager.user_command_running, task_id)
    run = (await client.get(f"/api/tasks/{task_id}/commands/{r.json()['id']}")).json()
    assert run["status"] == "completed" and run["exit_code"] == 3 and run["argv"] == [command]
    assert "hello-from-run" in run["stdout"] and str(worktree.resolve()) in run["stdout"]
    assert "CI=unset" in run["stdout"] and f"VENV={repo / '.venv'}" in run["stdout"]
    assert (worktree / "created-by-run.txt").exists() and not (repo / "created-by-run.txt").exists()

    # The next turn sees what the user ran: in the context, and first in the verification output.
    fake.script["understand"] = capture("understand", {**script["understand"], "intent": "review_changes",
                                                       "relates_to_previous": True})
    fake.script["review"] = capture("review", script["review"])
    r = await client.post(f"/api/tasks/{task_id}/messages", json={"message": "It exits with 3. Review your changes."},
                          headers=H)
    assert r.status_code == 200
    await _wait_until_false(manager.is_running, task_id)
    assert "Commands the user just ran" in seen["understand"][-1] and "hello-from-run" in seen["understand"][-1]
    review_prompt = seen["review"][-1]
    assert "(run by the user)" in review_prompt and "exit code: 3 | status: completed" in review_prompt
    assert 'File "offers/aggregator.py", line 14' in review_prompt  # worktree paths made repository-relative
    assert "Commands the user just ran" not in review_prompt  # not repeated in the general context


async def test_user_commands_need_a_worktree_and_a_command(api, tmp_path):
    app, client = await api(FakeLLM(hotel_script()))
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    task = app.state.db.create_task(repo_id=rid, title="Explain offers", mode="ask", model="fake")
    r = await client.post(f"/api/tasks/{task['id']}/commands", json={"command": "ls"}, headers=H)
    assert r.status_code == 400 and r.json()["error"] == "no_worktree"
    r = await client.post(f"/api/tasks/{task['id']}/commands", json={"command": "   "}, headers=H)
    assert r.status_code == 422
    r = await client.post(f"/api/tasks/{task['id']}/commands", json={"command": "ls"})  # no client header
    assert r.status_code == 403


@pytest.fixture
def git_identity(tmp_path, monkeypatch):
    """Commits made for the user use their Git config; give the tests a hermetic one."""
    config = tmp_path / "gitconfig"
    config.write_text("[user]\n\tname = LocPilot Tests\n\temail = tests@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


async def _completed_code_task(client, manager, repo: Path) -> dict:
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    task_id = (await client.post("/api/tasks", json={"repo_id": rid, "mode": "code", "message": "Fix duplicate offers"},
                                 headers=H)).json()["id"]
    await _wait_until_false(manager.is_running, task_id)
    approval = (await client.get(f"/api/tasks/{task_id}")).json()["pending_approval"]
    await client.post(f"/api/tasks/{task_id}/approvals/{approval['id']}", headers=H,
                      json={"decision": "approve", "commands": [c["argv"] for c in approval["payload"]["commands"]]})
    await _wait_until_false(manager.is_running, task_id)
    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert detail["status"] == "completed"
    return detail


async def test_apply_commit_and_push_the_task_changes(api, tmp_path, git_identity):
    import subprocess

    def g(cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    app, client = await api(FakeLLM(hotel_script()))
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    origin = tmp_path / "origin.git"
    g(tmp_path, "init", "-q", "--bare", str(origin))
    g(repo, "remote", "add", "origin", str(origin))
    detail = await _completed_code_task(client, app.state.manager, repo)
    task_id, worktree = detail["id"], Path(detail["worktree_path"])
    (worktree / "notes.txt").write_text("scratch made from a terminal\n")
    head_before = g(repo, "rev-parse", "HEAD")

    changes = (await client.get(f"/api/tasks/{task_id}/worktree-changes")).json()
    files = {f["path"]: f for f in changes["files"]}
    assert set(files) == {"offers/aggregator.py", "tests/test_cross_supplier_duplicates.py", "notes.txt"}
    assert files["offers/aggregator.py"] == {"path": "offers/aggregator.py", "status": "M", "locpilot": True}
    assert files["notes.txt"]["status"] == "A" and not files["notes.txt"]["locpilot"]
    assert changes["suggested_branch"] == "locpilot/fix-duplicate-offers"
    assert changes["suggested_message"] == "Deduplicate across suppliers" and changes["publish"] is None

    # Apply to the user's checkout: plain file edits only (index, HEAD and branch untouched).
    r = await client.post(f"/api/tasks/{task_id}/apply-to-repo", json={"files": ["offers/aggregator.py"]}, headers=H)
    assert r.status_code == 200, r.text
    assert (repo / "offers/aggregator.py").read_text() == (worktree / "offers/aggregator.py").read_text()
    assert g(repo, "status", "--porcelain") == "M offers/aggregator.py"
    assert g(repo, "rev-parse", "HEAD") == head_before and g(repo, "branch", "--show-current") == "main"
    r = await client.post(f"/api/tasks/{task_id}/apply-to-repo", json={"files": ["offers/aggregator.py"]}, headers=H)
    assert r.status_code == 409 and r.json()["error"] == "apply_failed"  # already there: refuses, writes nothing
    r = await client.post(f"/api/tasks/{task_id}/apply-to-repo", json={"files": ["README.md"]}, headers=H)
    assert r.status_code == 400 and r.json()["error"] == "not_changed"

    # Commit only the selected files to a new branch; the worktree stays detached, the checkout untouched.
    branch = changes["suggested_branch"]
    selected = ["offers/aggregator.py", "tests/test_cross_supplier_duplicates.py"]
    r = await client.post(f"/api/tasks/{task_id}/commit", headers=H,
                          json={"branch": branch, "message": "Deduplicate offers across suppliers", "files": selected})
    assert r.status_code == 200, r.text
    first = r.json()["commit"]
    assert g(repo, "rev-parse", branch) == first and g(repo, "log", "-1", "--format=%s", branch) == \
        "Deduplicate offers across suppliers"
    assert set(g(repo, "show", "--name-only", "--format=", branch).splitlines()) == set(selected)
    assert g(repo, "rev-parse", "HEAD") == head_before and g(repo, "branch", "--show-current") == "main"
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=worktree).returncode != 0  # still detached
    remaining = (await client.get(f"/api/tasks/{task_id}/worktree-changes")).json()
    assert [f["path"] for f in remaining["files"]] == ["notes.txt"] and remaining["suggested_branch"] == branch

    # A second commit advances the same branch.
    r = await client.post(f"/api/tasks/{task_id}/commit", headers=H,
                          json={"branch": branch, "message": "Add notes", "files": ["notes.txt"]})
    assert r.status_code == 200, r.text
    assert g(repo, "rev-parse", f"{branch}~1") == first

    # Push with the user's Git setup (a local bare repo stands in for GitHub here).
    r = await client.post(f"/api/tasks/{task_id}/push", headers=H)
    assert r.status_code == 200, r.text
    publish = r.json()["publish"]
    assert g(origin, "rev-parse", f"refs/heads/{branch}") == publish["head"] == publish["pushed_head"]
    assert publish["pull_request_url"] is None  # not a GitHub/GitLab/Bitbucket remote

    # Never moves a branch someone else advanced, nor one checked out in a working tree, nor a bad name.
    g(repo, "branch", "-f", branch, "main")
    (worktree / "more.txt").write_text("x\n")
    r = await client.post(f"/api/tasks/{task_id}/commit", headers=H,
                          json={"branch": branch, "message": "More", "files": ["more.txt"]})
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    r = await client.post(f"/api/tasks/{task_id}/commit", headers=H,
                          json={"branch": "main", "message": "More", "files": ["more.txt"]})
    assert r.status_code == 409 and "checked out" in r.json()["detail"]
    r = await client.post(f"/api/tasks/{task_id}/commit", headers=H,
                          json={"branch": "bad..name", "message": "More", "files": ["more.txt"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_branch"


async def test_publishing_needs_a_worktree_and_a_commit(api, tmp_path):
    app, client = await api(FakeLLM(hotel_script()))
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    task = app.state.db.create_task(repo_id=rid, title="Explain offers", mode="ask", model="fake")
    assert (await client.get(f"/api/tasks/{task['id']}/worktree-changes")).status_code == 400
    r = await client.post(f"/api/tasks/{task['id']}/push", headers=H)
    assert r.status_code == 400


async def test_saving_edits_from_the_editor(api, tmp_path):
    fake = FakeLLM(hotel_script())
    app, client = await api(fake)
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    (repo / "blob.bin").write_bytes(b"\x00\x01binary")
    rid = (await client.post("/api/repos", json={"source": "local", "path": str(repo)}, headers=H)).json()["id"]
    url = f"/api/repos/{rid}/file"

    # Your working tree: saved in place; a stale save is refused and writes nothing.
    loaded = (await client.get(url, params={"path": "README.md"})).json()
    edited = loaded["content"] + "\nEdited in LocPilot.\n"
    r = await client.put(url, json={"path": "README.md", "content": edited, "expected_sha": loaded["sha"]}, headers=H)
    assert r.status_code == 200, r.text
    assert (repo / "README.md").read_text() == edited
    assert (await client.get(url, params={"path": "README.md"})).json()["sha"] == r.json()["sha"]
    r = await client.put(url, json={"path": "README.md", "content": "clobber", "expected_sha": loaded["sha"]}, headers=H)
    assert r.status_code == 409 and r.json()["error"] == "file_changed" and r.json()["hint"]
    assert (repo / "README.md").read_text() == edited

    any_sha = "0" * 64
    for path, status in [("../outside.txt", 403), (".git/config", 403), (".env", 403), ("nope.py", 404),
                         ("blob.bin", 400)]:
        r = await client.put(url, json={"path": path, "content": "x", "expected_sha": any_sha}, headers=H)
        assert r.status_code == status, (path, r.text)
    assert (await client.put(url, json={"path": "README.md", "content": "x", "expected_sha": "abc"},
                             headers=H)).status_code == 422
    assert (await client.put(url, json={"path": "README.md", "content": "x", "expected_sha": any_sha})).status_code == 403

    # A task worktree: refused while LocPilot works in it, saved there (not in your checkout) afterwards.
    fake.gate, fake.block_step = asyncio.Event(), "understand"
    task_id = (await client.post("/api/tasks", json={"repo_id": rid, "mode": "code", "message": "Fix duplicates"},
                                 headers=H)).json()["id"]
    for _ in range(200):
        worktree = app.state.db.get_task(task_id)["worktree_path"]
        if worktree:
            break
        await asyncio.sleep(0.05)
    params = {"path": "offers/models.py", "task_id": task_id}
    loaded = (await client.get(url, params=params)).json()
    body = {"path": "offers/models.py", "content": loaded["content"] + "# tweak\n", "expected_sha": loaded["sha"],
            "task_id": task_id}
    r = await client.put(url, json=body, headers=H)
    assert r.status_code == 409 and r.json()["error"] == "task_busy"
    fake.gate.set()
    await _wait_until_false(app.state.manager.is_running, task_id)
    r = await client.put(url, json=body, headers=H)
    assert r.status_code == 200, r.text
    assert (Path(worktree) / "offers/models.py").read_text().endswith("# tweak\n")
    assert not (repo / "offers/models.py").read_text().endswith("# tweak\n")


def test_branch_names_keep_whole_words():
    from locpilot.api.tasks import _branch_slug

    assert _branch_slug("Users see duplicate hotel offers in search results when the same room") == \
        "users-see-duplicate-hotel-offers-in"
    assert _branch_slug("Fix duplicate offers") == "fix-duplicate-offers"
    assert _branch_slug("Ünïcode ✨ only!!") == "n-code-only"
    assert _branch_slug("!!!") == "changes"
    assert _branch_slug("x" * 60) == "x" * 40


def test_pull_request_urls():
    from locpilot.tools.git import pull_request_url

    hosts = ["github.com", "gitlab.com", "bitbucket.org"]
    assert pull_request_url("https://github.com/acme/shop.git", "locpilot/fix-a", hosts) == \
        "https://github.com/acme/shop/compare/locpilot/fix-a?expand=1"
    assert pull_request_url("git@github.com:acme/shop.git", "b", hosts) == "https://github.com/acme/shop/compare/b?expand=1"
    assert pull_request_url("https://gitlab.com/acme/shop", "x/y", hosts).endswith("source_branch%5D=x%2Fy")
    assert pull_request_url("https://bitbucket.org/acme/shop", "b", hosts) == \
        "https://bitbucket.org/acme/shop/pull-requests/new?source=b"
    assert pull_request_url("/srv/git/shop.git", "b", hosts) is None
    assert pull_request_url(None, "b", hosts) is None


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
