"""Shared test fixtures.

``FakeLLM`` is a scripted stand-in for Ollama used ONLY by automated tests (it is
injected through ``create_app(llm_factory=...)`` and cannot be enabled at runtime).
Everything else — Git, worktrees, file edits, sandboxed test commands, SQLite and
LangGraph checkpoints — is real.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from locpilot.config import Settings
from locpilot.llm.ollama import LLMResult

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "LocPilot Tests", "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "LocPilot Tests", "GIT_COMMITTER_EMAIL": "tests@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV, "HOME": str(cwd)}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def make_repo(dest: Path, fixture: str | None = None, files: dict[str, str] | None = None) -> Path:
    """Create a committed Git repository from a fixture directory or a dict of files."""
    dest.mkdir(parents=True, exist_ok=True)
    if fixture:
        shutil.copytree(FIXTURES / fixture / "repo", dest, dirs_exist_ok=True)
    for rel, content in (files or {}).items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "initial")
    return dest.resolve()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", allowed_repo_roots=[tmp_path], command_timeout_s=60,
                 max_llm_calls=30)
    s.ensure_dirs()
    return s


Responder = str | dict | Callable[[list[dict]], str | dict]


class FakeLLM:
    """Deterministic scripted model keyed by the step's instruction text."""

    STEPS = {
        "understand": "Classify the request and extract search hints",
        "plan_evidence": "Decide whether the evidence above is enough",
        "analyze_rca": "Find the root cause using ONLY the evidence above",
        "analyze_explain": "Answer using ONLY the evidence above",
        "plan": "Plan the smallest safe change",
        "fix": "Implement the fix described in the plan",
        "test": "Write ONE focused regression test",
        "diagnose": "Diagnose why verification failed",
        "repair": "Fix the problem with SEARCH/REPLACE edits",
        "review": "only claim test results that appear above",
        "test_repair": "so it does NOT reproduce the bug",
        "test_spec": "Design ONE regression test before writing any code",
    }

    def __init__(self, script: dict[str, Responder | list[Responder]], model: str = "fake-model") -> None:
        self.script = script
        self.model = model
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.block_step: str | None = None

    def _step(self, messages: list[dict]) -> str:
        text = messages[1]["content"] if len(messages) > 1 else messages[-1]["content"]
        for step, marker in self.STEPS.items():
            if marker in text:
                return step
        raise AssertionError(f"FakeLLM: unknown prompt: {text[-300:]}")

    async def chat(self, messages, *, schema=None, max_tokens=1024, on_progress=None, temperature=None) -> LLMResult:
        step = self._step(messages)
        self.calls.append(step)
        if self.block_step == step and self.gate is not None:
            await self.gate.wait()
        if on_progress is not None:
            await on_progress(16)
        responder = self.script.get(step)
        if isinstance(responder, list):
            index = sum(1 for c in self.calls if c == step) - 1
            responder = responder[min(index, len(responder) - 1)]
        if responder is None:
            raise AssertionError(f"FakeLLM: no script for step {step}")
        value = responder(messages) if callable(responder) else responder
        content = value if isinstance(value, str) else json.dumps(value)
        return LLMResult(content=content, prompt_tokens=100, completion_tokens=50, duration_ms=5, model=self.model)

    async def status(self) -> dict:
        return {"ollama_reachable": True, "ollama_version": "test", "ollama_url": "http://127.0.0.1:11434",
                "configured_model": self.model, "model_available": True,
                "models": [{"name": self.model, "size_bytes": 1, "parameter_size": None, "quantization": None,
                            "family": None}], "num_ctx": 8192, "temperature": 0.1, "error": None, "hint": None}

    async def ensure_ready(self) -> None:
        return None

    @staticmethod
    def model_installed(model: str, names: list[str]) -> bool:
        return model in names


def evidence_id_for(messages: list[dict], path: str) -> str | None:
    """Find the evidence id the agent assigned to a file (the fake model cites real evidence)."""
    m = re.search(rf'<evidence id="(E\d+)" file="{re.escape(path)}" lines="(\d+)-(\d+)"', messages[1]["content"])
    return m.group(1) if m else None


def cite(messages: list[dict], path: str, start: int | None = None, end: int | None = None) -> str:
    eid = evidence_id_for(messages, path)
    if eid is None:
        return f"{path}:{start or 1}"
    return f"{eid}:{start}-{end}" if start else eid


def hotel_script(**overrides) -> dict:
    """A correct end-to-end script for the hotel-offers fixture."""
    fix = (FIXTURES / "hotel-offers" / "reference_fix.txt").read_text()
    test_block = '''tests/test_cross_supplier_duplicates.py
<<<<<<< SEARCH
=======
import unittest

from offers.aggregator import merge_offers
from offers.models import Offer


class CrossSupplierDuplicatesTest(unittest.TestCase):
    def test_same_room_from_two_suppliers_is_returned_once(self):
        merged = merge_offers({
            "a": [Offer("H1", "DBL", "BB", "a", 120.0)],
            "b": [Offer("H1", "DBL", "BB", "b", 110.0)],
        })
        self.assertEqual([o.price for o in merged], [110.0])
>>>>>>> REPLACE
'''
    script = {
        "understand": {"intent": "fix", "summary": "Fix duplicate offers across suppliers",
                       "search_terms": ["merge_offers", "dedupe", "duplicate", "supplier", "offer"],
                       "symbols": ["merge_offers"], "file_hints": [], "error_messages": [],
                       "relates_to_previous": False, "needs_clarification": False, "clarifying_question": ""},
        "plan_evidence": {"sufficient": True, "missing": "", "requests": []},
        "analyze_rca": lambda m: {
            "problem_summary": "The same room from two suppliers appears twice.",
            "observed_symptoms": ["duplicate offers in search results"],
            "execution_flow": [{"step": "search_hotels merges supplier results", "evidence": [cite(m, "offers/search.py")]},
                               {"step": "merge_offers builds `best` per supplier", "evidence": [cite(m, "offers/aggregator.py", 14, 19)]}],
            "suspicious_lines": [{"evidence": cite(m, "offers/aggregator.py", 14, 14),
                                  "code": "best: dict[tuple[str, str, str], Offer] = {}",
                                  "concern": "created inside the supplier loop"}],
            "hypotheses": [{"title": "Dedupe map is reset for every supplier",
                            "explanation": "`best` is created inside the supplier loop.", "confidence": "high",
                            "supporting_evidence": [cite(m, "offers/aggregator.py", 14, 15)],
                            "contradicting_evidence": []},
                           {"title": "Invented hypothesis", "explanation": "Cites a file that does not exist",
                            "confidence": "medium", "supporting_evidence": ["offers/ghost.py:10"],
                            "contradicting_evidence": []}],
            "most_likely_root_cause": "The `best` dict is re-created inside the per-supplier loop, so offers are only "
                                      "de-duplicated within a supplier.",
            "root_cause_evidence": [cite(m, "offers/aggregator.py", 14, 15)],
            "evidence_sufficient": True,
            "minimal_fix": "Create `best` once before the loop and build the result after it.",
            "side_effects": ["Cheapest supplier wins for identical products"],
            "verification_steps": ["Merge two suppliers selling the same room and expect one offer"],
            "uncertainties": [],
            "answer": f"The dedupe map is reset per supplier [{cite(m, 'offers/aggregator.py', 14, 15)}]. "
                      "See also `offers/ghost.py:3` and [E99].",
        },
        "plan": {"summary": "Deduplicate across suppliers", "approach": "Hoist the dict out of the loop",
                 "files_to_modify": [{"path": "offers/aggregator.py", "change": "move best out of loop"}],
                 "test_file": "tests/test_cross_supplier_duplicates.py",
                 "test_description": "same room from two suppliers appears once", "risks": []},
        "fix": fix,
        "test": test_block,
        "test_spec": {"scenario": "two suppliers sell the same room", "inputs": "H1 DBL BB from a (120) and b (110)",
                      "expected_correct_behavior": "one offer at 110", "buggy_behavior": "two offers",
                      "assertion": "prices == [110.0]"},
        "review": {"answer": "Moved `best` out of the loop in offers/aggregator.py.",
                   "file_notes": [{"path": "offers/aggregator.py", "note": "dedupe across suppliers"}],
                   "side_effects": ["none known"], "risks": []},
        "diagnose": {"diagnosis": "The fix is incomplete.", "cause": "fix_incorrect", "should_retry": True,
                     "next_step": "Hoist the dict"},
        "repair": fix,
    }
    script.update(overrides)
    return script
