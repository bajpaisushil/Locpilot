"""End-to-end agent tests: real Git, worktrees, sandboxed test runs, SQLite and LangGraph checkpoints;
only the model is scripted (FakeLLM)."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from locpilot.agent.graph import compute_verification, failure_signature
from locpilot.app import create_app
from tests.conftest import FIXTURES, FakeLLM, git, hotel_script, make_repo


def snapshot(repo: Path) -> dict:
    files = {}
    for p in sorted(repo.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(repo).parts:
            files[p.relative_to(repo).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"files": files, "status": git(repo, "status", "--porcelain"), "head": git(repo, "rev-parse", "HEAD"),
            "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "stash": git(repo, "stash", "list"),
            "commits": git(repo, "rev-list", "--count", "--all")}


class Harness:
    def __init__(self, settings, fake: FakeLLM):
        self.settings = settings
        self.fake = fake
        self.app = create_app(settings, llm_factory=lambda: fake)
        self.db = self.app.state.db
        self.manager = self.app.state.manager

    async def __aenter__(self):
        self._cm = self.app.router.lifespan_context(self.app)
        await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._cm.__aexit__(None, None, None)

    def add_repo(self, path: Path) -> dict:
        return self.db.add_repo(name=path.name, source_type="local", source_url=None, path=str(path), managed=False)

    async def start(self, repo: dict, mode: str, message: str) -> str:
        task = self.db.create_task(repo_id=repo["id"], title=message[:60], mode=mode, model="fake")
        await self.manager.start_turn(task["id"], message, mode)
        return task["id"]

    async def wait(self, task_id: str, timeout: float = 120) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.manager.is_running(task_id):
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("task did not finish")
            await asyncio.sleep(0.05)
        return self.db.get_task(task_id)

    async def approve(self, task_id: str, commands=None, decision="approve", note=None) -> None:
        approval = self.db.pending_approval(task_id)
        assert approval is not None, "expected a pending approval"
        payload = {"decision": decision, "note": note,
                   "commands": commands if commands is not None else [c["argv"] for c in approval["payload"]["commands"]]}
        await self.manager.decide(task_id, approval["id"], payload)

    def report(self, task_id: str) -> dict:
        return self.db.list_reports(task_id)[-1]


def rca_only_script(**kw):
    script = hotel_script(**kw)
    script["understand"] = {**script["understand"], "intent": "root_cause"}
    return script


async def test_ask_mode_rca_is_read_only_and_citations_are_validated(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    (repo_path / "README.md").write_text("local uncommitted edit\n")
    before = snapshot(repo_path)
    fake = FakeLLM(rca_only_script())
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause of duplicate hotel offers")
        task = await h.wait(task_id)
        assert task["status"] == "completed", task["error"]
        report = h.report(task_id)
        inv = report["investigation"]
        assert report["status"] == "answered" and inv["kind"] == "rca"
        assert "re-created inside the per-supplier loop" in inv["most_likely_root_cause"]
        for cit in inv["root_cause_citations"]:
            lines = (repo_path / cit["path"]).read_text().splitlines()
            assert cit["path"] == "offers/aggregator.py" and 1 <= cit["start_line"] <= cit["end_line"] <= len(lines)
        statuses = {hyp["title"]: hyp["status"] for hyp in inv["hypotheses"]}
        assert statuses["Dedupe map is reset for every supplier"] == "supported"
        assert statuses["Invented hypothesis"] == "unsupported"
        removed = " ".join(inv["removed_citations"])
        assert "offers/ghost.py" in removed and "E99" in removed
        assert "locpilot://open?path=offers%2Faggregator.py" in inv["answer_markdown"]
        assert "unverified" in inv["answer_markdown"]
        assert {f["path"] for f in inv["files_examined"]} >= {"offers/aggregator.py"}
        assert "plan" not in fake.calls and "fix" not in fake.calls
        assert h.db.list_commands(task_id) == [] and h.db.list_patches(task_id) == []
        assert task["worktree_path"] is None
    assert snapshot(repo_path) == before


async def test_ask_mode_fix_request_does_not_modify_anything(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    before = snapshot(repo_path)
    fake = FakeLLM(hotel_script())
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Fix the duplicate offers bug")
        await h.wait(task_id)
        report = h.report(task_id)
        assert report["status"] == "answered"
        assert any("Code mode" in s for s in report["next_steps"])
        assert "fix" not in fake.calls and not h.db.list_patches(task_id)
    assert snapshot(repo_path) == before


async def test_code_mode_end_to_end_with_approval_regression_check_and_preserved_user_changes(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    (repo_path / "offers" / "search.py").write_text((repo_path / "offers/search.py").read_text() + "# wip\n")
    (repo_path / "scratch_notes.txt").write_text("user's untracked notes\n")
    user_before = snapshot(repo_path)
    fake = FakeLLM(hotel_script())
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code",
                                "Users see duplicate hotel offers. Fix it with the smallest change and add a test.")
        task = await h.wait(task_id)
        assert task["status"] == "awaiting_approval", task["error"]
        worktree = Path(task["worktree_path"])
        assert worktree.is_dir() and worktree.resolve().is_relative_to(settings.worktrees_dir.resolve())
        approval = h.db.pending_approval(task_id)
        payload = approval["payload"]
        assert "best: dict" in payload["diff"] and "tests/test_cross_supplier_duplicates.py" in payload["diff"]
        assert payload["commands"] and "unittest" in " ".join(payload["commands"][0]["argv"])
        # Nothing is written before approval.
        assert "best: dict[tuple[str, str, str], Offer] = {}\n    for supplier" not in (worktree / "offers/aggregator.py").read_text()
        assert not (worktree / "tests/test_cross_supplier_duplicates.py").exists()

        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "completed", task["error"]
        report = h.report(task_id)
        assert report["status"] == "changes_verified", report
        v = report["verification"]
        assert v["verdict"] == "passed" and v["tests_run"]
        assert v["regression_test"]["status"] == "detected_bug", v["regression_test"]
        stages = [r["stage"] for r in v["runs"]]
        assert stages == ["before_fix", "after_fix"]
        before_run = next(r for r in v["runs"] if r["stage"] == "before_fix")
        assert before_run["exit_code"] != 0 and before_run["summary"]["outcome"] == "failed"
        cmds = h.db.list_commands(task_id, include_output=True)
        assert all(c["status"] == "completed" and c["duration_ms"] is not None for c in cmds)
        if sys.platform == "darwin":
            assert all(c["sandbox"] == "seatbelt" for c in cmds)
        assert [p["status"] for p in h.db.list_patches(task_id)] == ["applied"]

        # The hidden acceptance test passes against LocPilot's worktree.
        acc = worktree / "acceptance_check"
        shutil.copytree(FIXTURES / "hotel-offers" / "acceptance", acc)
        (acc / "__init__.py").write_text("")
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "acceptance_check", "-t", "."],
                             cwd=worktree, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
        shutil.rmtree(acc)

        # Follow-up turn: explain the changes (uses persisted context, no re-investigation).
        fake.script["understand"] = {**fake.script["understand"], "intent": "explain_changes",
                                     "relates_to_previous": True}
        await h.manager.start_turn(task_id, "Explain the changes you made.", "code")
        await h.wait(task_id)
        review_report = h.report(task_id)
        assert review_report["turn"] == 2 and review_report["change_review"]["answer_markdown"]
        assert fake.calls.count("analyze_rca") == 1

    # The user's working tree, index, branch, stash and history are untouched.
    assert snapshot(repo_path) == user_before


async def test_rejecting_the_patch_changes_nothing(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    fake = FakeLLM(hotel_script())
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        task = await h.wait(task_id)
        worktree = Path(task["worktree_path"])
        original = (worktree / "offers/aggregator.py").read_text()
        await h.approve(task_id, decision="reject")
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_rejected"
        assert (worktree / "offers/aggregator.py").read_text() == original
        assert h.db.list_commands(task_id) == []
        assert h.db.list_patches(task_id)[0]["status"] == "rejected"


async def test_failed_verification_triggers_bounded_repair_and_stall_detection(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    wrong_fix = """offers/aggregator.py
<<<<<<< SEARCH
    return sort_offers(merged)
=======
    return sort_offers(list(merged))
>>>>>>> REPLACE
"""
    wrong_repair = """offers/aggregator.py
<<<<<<< SEARCH
def sort_offers(offers: list[Offer]) -> list[Offer]:
=======
def sort_offers(offers: list[Offer]) -> list[Offer]:  # repair attempt
>>>>>>> REPLACE
"""
    fake = FakeLLM(hotel_script(fix=wrong_fix, repair=wrong_repair))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        # verification failed → diagnose → repair patch awaits approval
        assert task["status"] == "awaiting_approval", task["error"]
        assert "diagnose" in fake.calls and "repair" in fake.calls
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "completed"
        report = h.report(task_id)
        assert report["status"] == "changes_failed"
        assert report["verification"]["verdict"] == "failed"
        assert report["retries"] == 1
        assert any("same failure repeated" in u for u in report["unresolved_issues"])
        assert report["stats"]["llm_calls"] <= settings.max_llm_calls
        assert fake.calls.count("repair") == 1  # stalled → no further attempts


def test_retry_budget_and_verification_logic():
    def run(stage, outcome, failing, code):
        return {"command_id": "c", "stage": stage, "argv": ["x"], "exit_code": code,
                "summary": {"outcome": outcome, "failing_tests": failing}, "output": "E   AssertionError: boom"}

    v = compute_verification([run("before_fix", "failed", ["tests.test_new.T.test_bug", "tests.test_old.T.test_x"], 1)],
                             [run("after_fix", "failed", ["tests.test_old.T.test_x"], 1)], ["tests/test_new.py"],
                             commands_approved=True)
    assert v["regression_test"]["status"] == "detected_bug"
    assert v["preexisting_failures"] == ["tests.test_old.T.test_x"] and v["verdict"] == "partial"
    v2 = compute_verification([run("before_fix", "passed", [], 0)], [run("after_fix", "passed", [], 0)],
                              ["tests/test_new.py"], commands_approved=True)
    assert v2["regression_test"]["status"] == "did_not_fail_before_fix"
    v3 = compute_verification([], [], ["tests/test_new.py"], commands_approved=False)
    assert v3["verdict"] == "not_run" and not v3["tests_run"]
    assert failure_signature([run("a", "failed", ["t1"], 1)]) == failure_signature([run("a", "failed", ["t1"], 1)])


async def test_restart_recovers_history_and_pending_approval(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    async with Harness(settings, FakeLLM(hotel_script())) as h:
        repo = h.add_repo(repo_path)
        task_id = await h.start(repo, "code", "Fix duplicate offers")
        task = await h.wait(task_id)
        assert task["status"] == "awaiting_approval"
        events_before = len(h.db.list_events(task_id))
    # "Restart": brand new app/manager/DB connections over the same data directory.
    fake2 = FakeLLM(hotel_script())
    async with Harness(settings, fake2) as h2:
        task = h2.db.get_task(task_id)
        assert task["status"] == "awaiting_approval"
        assert len(h2.db.list_messages(task_id)) == 1 and len(h2.db.list_events(task_id)) >= events_before
        await h2.approve(task_id)
        task = await h2.wait(task_id)
        assert task["status"] == "completed"
        assert h2.report(task_id)["status"] == "changes_verified"
        assert "understand" not in fake2.calls and "fix" not in fake2.calls  # resumed, not restarted


async def test_interrupted_task_resumes_from_checkpoint(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    fake = FakeLLM(rca_only_script())
    fake.gate, fake.block_step = asyncio.Event(), "analyze_rca"
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause of duplicate offers")
        for _ in range(400):
            if "analyze_rca" in fake.calls:
                break
            await asyncio.sleep(0.05)
        assert "analyze_rca" in fake.calls
    # Shutdown happened while the model call was blocked → interrupted, resumable.
    fake2 = FakeLLM(rca_only_script())
    async with Harness(settings, fake2) as h2:
        assert h2.db.get_task(task_id)["status"] == "interrupted"
        await h2.manager.resume(task_id)
        task = await h2.wait(task_id)
        assert task["status"] == "completed"
        assert h2.report(task_id)["investigation"]["most_likely_root_cause"]
        assert fake2.calls == ["analyze_rca"]  # completed steps were not repeated


async def test_cancel_running_task(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    fake = FakeLLM(rca_only_script())
    fake.gate, fake.block_step = asyncio.Event(), "analyze_rca"
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "Find the root cause")
        for _ in range(400):
            if "analyze_rca" in fake.calls:
                break
            await asyncio.sleep(0.05)
        await h.manager.cancel(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "cancelled"
        assert any(e["type"] == "log" and "cancelled" in e["data"]["text"] for e in h.db.list_events(task_id))


async def test_clarification_and_llm_failure_paths(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    vague = {"intent": "question", "summary": "unclear", "search_terms": [], "symbols": [], "file_hints": [],
             "error_messages": [], "relates_to_previous": False, "needs_clarification": True,
             "clarifying_question": "Which endpoint is failing?"}
    async with Harness(settings, FakeLLM(hotel_script(understand=vague))) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "help")
        task = await h.wait(task_id)
        assert task["status"] == "needs_input"
        assert h.report(task_id)["clarifying_question"] == "Which endpoint is failing?"
    async with Harness(settings, FakeLLM(hotel_script(understand="not json at all"))) as h:
        task_id = await h.start(h.add_repo(repo_path), "ask", "explain merge_offers")
        task = await h.wait(task_id)
        assert task["status"] == "failed" and "structured output" in task["error"]


WEAK_TEST = """tests/test_cross_supplier_duplicates.py
<<<<<<< SEARCH
=======
import unittest

from offers.aggregator import merge_offers
from offers.models import Offer


class CrossSupplierDuplicatesTest(unittest.TestCase):
    def test_single_supplier(self):
        merged = merge_offers({"a": [Offer("H1", "DBL", "BB", "a", 120.0), Offer("H1", "DBL", "BB", "a", 100.0)]})
        self.assertEqual(len(merged), 1)
>>>>>>> REPLACE
"""

STRONG_TEST = """tests/test_cross_supplier_duplicates.py
<<<<<<< SEARCH
    def test_single_supplier(self):
        merged = merge_offers({"a": [Offer("H1", "DBL", "BB", "a", 120.0), Offer("H1", "DBL", "BB", "a", 100.0)]})
        self.assertEqual(len(merged), 1)
=======
    def test_two_suppliers_same_room(self):
        merged = merge_offers({"a": [Offer("H1", "DBL", "BB", "a", 120.0)], "b": [Offer("H1", "DBL", "BB", "b", 100.0)]})
        self.assertEqual([o.price for o in merged], [100.0])
>>>>>>> REPLACE
"""


async def test_non_reproducing_test_is_detected_and_repaired_with_fix_reverted(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    fake = FakeLLM(hotel_script(test=WEAK_TEST, test_repair=STRONG_TEST))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers across suppliers")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        # Weak test passed before the fix → LocPilot asks to apply a rewritten test.
        assert task["status"] == "awaiting_approval", task["error"]
        assert "test_repair" in fake.calls
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "completed", task["error"]
        report = h.report(task_id)
        assert report["status"] == "changes_verified", report["headline"]
        assert report["verification"]["regression_test"]["status"] == "detected_bug"
        stages = [c["stage"] for c in h.db.list_commands(task_id)]
        assert stages == ["before_fix", "after_fix", "before_fix", "retry_1"]
        worktree = Path(task["worktree_path"])
        # The fix was re-applied after the temporary revert.
        assert "best: dict[tuple[str, str, str], Offer] = {}\n    for supplier" in (worktree / "offers/aggregator.py").read_text()


async def test_existing_tests_cannot_be_rewritten(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    rewrite_existing = """tests/test_aggregator.py
<<<<<<< SEARCH
    def test_different_products_are_kept(self):
=======
    def test_renamed(self):
>>>>>>> REPLACE
"""
    script = hotel_script()
    script["plan"] = {**script["plan"], "test_file": "tests/test_aggregator.py"}
    script["test"] = [rewrite_existing, rewrite_existing]
    fake = FakeLLM(script)
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        approval = h.db.pending_approval(task_id)
        # Plan was redirected to a new regression file; the rewrite of the existing test was refused.
        plan = h.db.list_events(task_id)
        plan_event = next(e for e in plan if e["type"] == "plan")["data"]["plan"]
        assert plan_event["test_file"] == "tests/test_aggregator_regression.py"
        assert "tests/test_aggregator.py" not in approval["payload"]["diff"]
        assert any("regression test" in w.lower() for w in approval["payload"]["warnings"])
