"""What the model gets to see: reads past known evidence, and calls followed into other files."""

from __future__ import annotations

from locpilot.agent.evidence import EvidenceCollector, extract_terms
from locpilot.agent.schemas import ToolRequest
from locpilot.tools.files import RepoIndex
from locpilot.tools.symbols import SymbolIndex
from tests.conftest import make_repo

HANDLER = "\n".join(
    ["from raft import candidate_is_up_to_date", "", "", "def handle_vote(state, request):",
     '    """Answer a vote request."""']
    + [f"    step_{i} = {i}  # bookkeeping" for i in range(150)]
    + ["    if not candidate_is_up_to_date(request.last_term, request.last_index, state.log):",
       "        return False", "    return True", ""])
RAFT = """def candidate_is_up_to_date(candidate_last_term, candidate_last_index, log):
    mine = log[-1].term if log else 0
    return candidate_last_term > mine or (candidate_last_term == mine and candidate_last_index > len(log))


def unrelated_helper():
    return 42
"""
REQUEST = ("A fresh cluster never elects a leader: followers refuse their vote to a candidate whose log is exactly "
           "as up to date as their own.")


def collector(tmp_path) -> EvidenceCollector:
    repo = make_repo(tmp_path / "raftlab", files={"consensus.py": HANDLER, "raft.py": RAFT})
    index = RepoIndex(repo)
    return EvidenceCollector(index, SymbolIndex(index).build(), [], max_chars=26_000)


def test_reading_past_what_is_known_adds_the_unseen_lines(tmp_path):
    c = collector(tmp_path)
    first = c.run_tool(ToolRequest(tool="read_file", path="consensus.py", start_line=1, end_line=200, query="", reason=""))
    assert first[1] and c.items[-1]["end_line"] < 156  # one item holds at most 110 lines: the call is not in it yet
    # Asking again for a range that is mostly known must still bring the rest (it used to return the old item).
    c.run_tool(ToolRequest(tool="read_file", path="consensus.py", start_line=60, end_line=200, query="", reason=""))
    assert any(i["start_line"] <= 156 <= i["end_line"] for i in c.items if i["path"] == "consensus.py")
    count = len(c.items)
    c.run_tool(ToolRequest(tool="read_file", path="consensus.py", start_line=60, end_line=200, query="", reason=""))
    assert len(c.items) == count  # nothing new left to show: no duplicate


def test_a_related_function_in_another_file_is_followed(tmp_path):
    c = collector(tmp_path)
    region = c.add("consensus.py", 150, 159, kind="file", title="consensus.py:150-159", reason="read")
    added = c.add_callees([region], extract_terms(REQUEST, None))
    assert [(i["path"], i["symbol"], i["kind"]) for i in added] == [("raft.py", "candidate_is_up_to_date", "callee")]


def test_followed_definitions_fit_even_when_the_budget_is_nearly_full(tmp_path):
    c = collector(tmp_path)
    c.add("consensus.py", 1, 110, kind="file", title="consensus.py:1-110", reason="read")
    region = c.add("consensus.py", 150, 159, kind="file", title="consensus.py:150-159", reason="read")
    c.max_chars = c.used_chars + 10  # nearly full: an ordinary item would not fit any more
    assert c.add("raft.py", 6, 7, kind="file", title="raft.py:6-7", reason="read") is None
    assert c.add_callees([region], extract_terms(REQUEST, None))


def test_unrelated_calls_are_not_followed(tmp_path):
    c = collector(tmp_path)
    region = c.add("consensus.py", 150, 159, kind="file", title="consensus.py:150-159", reason="read")
    assert c.add_callees([region], extract_terms("The dashboard colours are wrong in dark mode.", None)) == []


def test_matching_uses_the_whole_request_not_only_the_first_words():
    long_request = " ".join(f"filler{i}" for i in range(30)) + " candidate up to date"
    terms = extract_terms(long_request, None)
    assert "candidate" not in terms.words  # the ranked word list is capped
    assert {"candidate", "date"} <= terms.vocabulary
