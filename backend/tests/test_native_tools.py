"""Agent-tuned models work by calling tools; LocPilot offers them and uses what comes back.

Seen with MiMo: asked for SEARCH/REPLACE text it answered "I'll make the edit" and then called an ``edit`` tool
with the correct change. LocPilot ignored tool calls, so the step failed with "no edits". Now tool-capable models
get read/grep/edit/write, their edits are staged and validated like text edits, and the investigation is a tool
loop too. Models without tools keep the text protocol.
"""

from __future__ import annotations

from pathlib import Path

from locpilot.agent.evidence import EvidenceCollector
from locpilot.agent.graph import compute_verification, new_source_path
from locpilot.agent.toolcalls import EditSession, ExploreSession
from locpilot.llm.ollama import LLMResult
from locpilot.tools.files import RepoIndex
from locpilot.tools.patch import parse_edit_blocks
from locpilot.tools.symbols import SymbolIndex
from tests.conftest import FIXTURES, FakeLLM, hotel_script, make_repo
from tests.test_agent import Harness

AGGREGATOR = "offers/aggregator.py"
RATES = "offers/rates.py"
REFERENCE = parse_edit_blocks((FIXTURES / "hotel-offers" / "reference_fix.txt").read_text())[0][0]
TEST_PATH = "tests/test_cross_supplier_duplicates.py"
TEST_FILE = '''import unittest

from offers.aggregator import merge_offers
from offers.models import Offer


class CrossSupplierDuplicatesTest(unittest.TestCase):
    def test_same_room_from_two_suppliers_is_returned_once(self):
        merged = merge_offers({
            "a": [Offer("H1", "DBL", "BB", "a", 120.0)],
            "b": [Offer("H1", "DBL", "BB", "b", 110.0)],
        })
        self.assertEqual([o.price for o in merged], [110.0])
'''


def calls(*tool_calls: tuple[str, dict], text: str = "") -> LLMResult:
    """A model reply made of tool calls."""
    return LLMResult(content=text, prompt_tokens=100, completion_tokens=20, duration_ms=5, model="tool-model",
                     tool_calls=[{"name": name, "arguments": args} for name, args in tool_calls])


def edit_call(path: str = AGGREGATOR, old: str = REFERENCE.search, new: str = REFERENCE.replace) -> tuple[str, dict]:
    return "edit", {"file_path": path, "old_string": old, "new_string": new}


class ToolLLM(FakeLLM):
    """A scripted model that can call tools."""

    async def supports_tools(self) -> bool:
        return True


def tool_script(**overrides) -> dict:
    script = hotel_script(
        explore=[calls(("read", {"file_path": AGGREGATOR})),
                 "merge_offers in offers/aggregator.py creates `best` inside the supplier loop."],
        fix=[calls(edit_call(), text="I'll make the edit."), "DONE"],
        test=[calls(("write", {"file_path": TEST_PATH, "content": TEST_FILE})), "DONE"],
    )
    script.update(overrides)
    return script


def tool_replies(fake: FakeLLM, step: str) -> list[str]:
    """What the model was told in reply to its tool calls in ``step``."""
    last = [messages for s, messages, _tools in fake.conversations if s == step][-1]
    return [m["content"] for m in last if m["role"] == "tool"]


# The agent ------------------------------------------------------------------------------------------------------


async def test_a_fix_made_with_tool_calls_is_verified(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    fake = ToolLLM(tool_script())
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "completed", task["error"]
        report = h.report(task_id)
        patch = h.db.list_patches(task_id)[0]
    assert report["status"] == "changes_verified"
    assert sorted(c["path"] for c in patch["changes"]) == [AGGREGATOR, TEST_PATH]
    offered = {step: [t["function"]["name"] for t in tools] for step, _m, tools in fake.conversations if tools}
    assert offered["fix"] == ["read", "grep", "edit", "write"] and "edit" not in offered["explore"]
    assert tool_replies(fake, "fix") == [f"OK: {AGGREGATOR} updated in the pending patch (+2/-4). If the change "
                                         "needs more edits to be complete, make them; when it is complete, reply DONE."]
    # What the model read while exploring is evidence, and the analysis and the plan continue that conversation
    # (so the model server does not process the same context again for every step).
    assert f'file="{AGGREGATOR}"' in tool_replies(fake, "explore")[0]
    analysis = next(m for s, m, _t in fake.conversations if s == "analyze_rca")
    assert [m["role"] for m in analysis] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert "Investigate with the tools" in analysis[1]["content"]
    assert analysis[4]["content"].startswith("merge_offers in offers/aggregator.py creates `best`")
    assert "Find the root cause using ONLY the evidence above" in analysis[5]["content"]
    plan = next(m for s, m, _t in fake.conversations if s == "plan")
    assert plan[:6] == analysis and [m["role"] for m in plan[6:]] == ["assistant", "user"]
    assert "most_likely_root_cause" in plan[6]["content"] and "Plan the smallest safe change" in plan[7]["content"]
    # Every call of that conversation declares the same tools: only then is the beginning of the prompt identical.
    assert offered["analyze_rca"] == offered["plan"] == offered["explore"] == ["read", "grep", "find_definition",
                                                                              "find_callers", "list"]


async def test_a_step_that_no_longer_fits_the_conversation_starts_a_fresh_prompt(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    fake = ToolLLM(tool_script())
    fake.num_ctx = 4000  # a window too small for the analysis to continue the exploration
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    analysis = next(m for s, m, _t in fake.conversations if s == "analyze_rca")
    assert [m["role"] for m in analysis] == ["system", "user"]  # its own prompt, with the evidence
    assert "## Evidence" in analysis[1]["content"] and f'file="{AGGREGATOR}"' in analysis[1]["content"]
    plan = next(m for s, m, _t in fake.conversations if s == "plan")
    assert [m["role"] for m in plan] == ["system", "user", "assistant", "user"]  # continues that analysis


async def test_a_rejected_edit_is_explained_and_can_be_corrected(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    wrong = edit_call(old="    merged: list[Offer] = []\n    for supplier in supplier_results:\n")
    fake = ToolLLM(tool_script(fix=[calls(wrong), calls(edit_call()), "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    first, second = tool_replies(fake, "fix")
    assert first.startswith(f"ERROR: {AGGREGATOR}: old_string was not found in the file.")
    assert "The closest text in the file" in first and second.startswith("OK:")


async def test_the_fix_may_land_in_a_file_the_plan_did_not_name(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    plan = {**hotel_script()["plan"], "files_to_modify": [{"path": "offers/search.py", "change": "dedupe here"}]}
    fake = ToolLLM(tool_script(plan=plan, fix=[calls(("read", {"file_path": AGGREGATOR})), calls(edit_call()), "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        patch = h.db.list_patches(task_id)[0]
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    assert AGGREGATOR in [c["path"] for c in patch["changes"]]
    assert any("although the plan did not name it" in w for w in patch["warnings"])  # said in the approval dialog
    fix_prompt = next(m for s, m, _t in fake.conversations if s == "fix")[1]["content"]
    assert "If the code you read shows that the cause is in another source file" in fix_prompt


async def test_a_model_stuck_asking_for_a_shell_does_not_burn_the_turn_limit(settings, tmp_path):
    # Seen with MiMo: after writing the test it called `bash` to run it, six times in a row.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    shell = calls(("bash", {"command": "pwd; ls"}))
    fake = ToolLLM(tool_script(test=[calls(("write", {"file_path": TEST_PATH, "content": TEST_FILE})),
                                     shell, shell, shell, shell, shell, "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"  # the test it had written is kept
    assert fake.calls.count("test") == 3  # write, then two useless turns: stopped there
    assert "There is no shell here" in tool_replies(fake, "test")[-1]


async def test_a_model_that_keeps_checking_its_finished_work_is_stopped(settings, tmp_path):
    # Seen with MiMo in every run: the edit is made, and instead of DONE come a look at the result, a look at the
    # existing tests and then a test written in the wrong step — minutes of work that is thrown away.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    look = calls(("read", {"file_path": AGGREGATOR}))
    tests = calls(("read", {"file_path": "tests/test_aggregator.py"}))
    add_test = calls(("edit", {"file_path": "tests/test_aggregator.py", "old_string": "import unittest",
                               "new_string": "import unittest\nimport sys"}))
    fake = ToolLLM(tool_script(fix=[look, calls(edit_call()), look, tests, "DONE", add_test, add_test]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    # Read, edit, two turns of looking again — and instead of more turns, the one question that is worth asking.
    assert fake.calls.count("fix") == 4 and fake.calls.count("walk") == 1
    walk = next(m for s, m, _t in fake.conversations if s == "walk")
    assert walk[-2]["role"] == "tool" and "Tests are not written in this step" in walk[-2]["content"]


async def test_a_finished_fix_is_walked_through_once_and_can_still_be_corrected(settings, tmp_path):
    # Seen with MiMo on the hotel fixture, three runs in a row: a fix that moves a line and changes nothing,
    # declared done. Made to compute the request's example through its own code, it sees the result is unchanged.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    no_op = edit_call(old='    """Cheapest first, grouped by hotel."""', new='    """Cheapest first, per hotel."""')
    fake = ToolLLM(tool_script(
        fix=[calls(no_op), "DONE"], rework=[calls(edit_call()), "DONE"],
        walk={"example": "H1 from a (120) and b (110)", "trace": "after a: merged=[120]; after b: merged=[120, 110]",
              "result": "two offers", "expected": "one offer at 110", "correct": False,
              "problem": "merged is extended once per supplier, so both offers stay"}))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"  # without a repair round
    assert fake.calls.count("walk") == 1 and fake.calls.count("rework") == 2 and "repair" not in fake.calls
    walk = next((m, t) for s, m, t in fake.conversations if s == "walk")
    assert [t["function"]["name"] for t in walk[1]] == ["read", "grep", "edit", "write"]  # same conversation
    rework = next(m for s, m, _t in fake.conversations if s == "rework")
    # It is given the two results, not its own explanation of them (it would only repeat its first theory).
    assert "It returns: two offers\nThe request asks for: one offer at 110" in rework[-1]["content"]
    assert "merged is extended once per supplier" not in rework[-1]["content"]
    assert "replace the whole function in one edit" in rework[-1]["content"]
    assert '"correct": false' in rework[-2]["content"]  # its own trace stays in view while it corrects


async def test_reading_on_between_edits_is_not_mistaken_for_being_done(settings, tmp_path):
    # A change in two files: after the first edit the model reads the second file (new to it) and edits that too.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    second = ("edit", {"file_path": "offers/search.py", "old_string": "    return merge_offers(results)",
                       "new_string": "    return merge_offers(dict(results))"})
    grep_call = calls(("grep", {"pattern": "merge_offers"}))
    fake = ToolLLM(tool_script(fix=[calls(edit_call()), calls(("read", {"file_path": AGGREGATOR})), grep_call,
                                    calls(("read", {"file_path": "offers/search.py"})), calls(second), "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        patch = h.db.list_patches(task_id)[0]
    assert fake.calls.count("fix") == 6 and fake.calls.count("walk") == 1  # … and the walk-through at the end
    assert {c["path"] for c in patch["changes"]} >= {AGGREGATOR, "offers/search.py"}


async def test_a_tool_call_cut_off_at_the_length_limit_is_asked_for_again(settings, tmp_path):
    # Seen with MiMo: a long test file written in one `write` call ran into the reply limit. The model server drops
    # an incomplete call, so LocPilot saw a reply with no edits and eight minutes of writing were lost.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    cut = LLMResult(content="Now the regression test.", prompt_tokens=100, completion_tokens=3200, duration_ms=5,
                    model="tool-model", truncated=True)
    fake = ToolLLM(tool_script(test=[cut, calls(("write", {"file_path": TEST_PATH, "content": TEST_FILE})), "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    assert fake.calls.count("test") == 3
    retry = [m for s, m, _t in fake.conversations if s == "test"][1]
    assert retry[-1]["role"] == "user" and "cut off at the length limit" in retry[-1]["content"]


async def test_a_request_for_new_code_with_tests_is_not_treated_as_tests_only(settings, tmp_path):
    # Seen with MiMo: "Add a rate limiter … add tests" was classified as add_tests, the code was never written and
    # the test step searched for it in vain. A plan that creates a source file means the code is written first.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    script = tool_script(fix=[calls(("write", {"file_path": "offers/dedupe.py", "content": "def key(o):\n    return o\n"})),
                              "DONE"])
    script["understand"] = {**script["understand"], "intent": "add_tests"}
    script["plan"] = {**script["plan"], "files_to_modify": [{"path": "offers/dedupe.py", "change": "new helper"}]}
    fake = ToolLLM(script)
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Add a dedupe helper in offers/dedupe.py, with tests")
        await h.wait(task_id)
        patch = h.db.list_patches(task_id)[0]
    assert "fix" in fake.calls and fake.calls.index("fix") < fake.calls.index("test")
    assert "offers/dedupe.py" in [c["path"] for c in patch["changes"]]
    understand = next(m for s, m, _t in fake.conversations if s == "understand")[-1]["content"]
    assert "also when the request asks for tests along with" in understand


async def test_exploring_ends_when_the_model_starts_on_the_change(settings, tmp_path):
    # Seen with MiMo on a new-feature request: it wrote the new file while still exploring, was told "not yet",
    # and wrote it again — over a minute of output that cannot be used in that step.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    early = calls(("write", {"file_path": "offers/dedupe.py", "content": "def key(o):\n    return o\n"}))
    fake = ToolLLM(tool_script(explore=[calls(("read", {"file_path": AGGREGATOR})), early, early, "Found it."]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
    assert fake.calls.count("explore") == 2 and "fix" in fake.calls  # the read, the early write: then on it goes
    assert "this step only investigates" in tool_replies(fake, "analyze_rca")[-1]


async def test_a_repair_rewrites_the_function_instead_of_patching_lines_again(settings, tmp_path):
    # Seen with MiMo: a fix that moved one line, and two repairs that moved it back and forth. Asked for the whole
    # function it writes the right one, so that is what a repair of the code asks for.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    no_op = edit_call(old='    """Cheapest first, grouped by hotel."""', new='    """Cheapest first, per hotel."""')
    fake = ToolLLM(tool_script(fix=[calls(no_op), "DONE"], repair=[calls(edit_call()), "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "awaiting_approval", task["error"]
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    asked = next(m for s, m, _t in fake.conversations if s == "repair")[-1]["content"]
    assert "Do not patch it line by line again" in asked and "replace the complete function in ONE edit" in asked


async def test_a_reply_that_rambles_to_the_length_limit_after_the_edits_ends_the_step(settings, tmp_path):
    # Seen with MiMo: the test was written, then five minutes of working things out in prose, cut off at the limit.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    prose = LLMResult(content="Let me trace the buggy code once more. " * 60, prompt_tokens=100,
                      completion_tokens=3200, duration_ms=5, model="tool-model", truncated=True)
    fake = ToolLLM(tool_script(test=[calls(("write", {"file_path": TEST_PATH, "content": TEST_FILE})), prose,
                                     calls(("bash", {"command": "pytest"})), "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    assert fake.calls.count("test") == 2  # the write, the ramble: the test it wrote is kept and the step is over


async def test_models_without_tools_keep_the_text_protocol(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    for fake, overrides in ((FakeLLM(hotel_script()), {}), (ToolLLM(hotel_script()), {"native_tools": False})):
        async with Harness(settings.model_copy(update=overrides), fake) as h:
            task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
            await h.wait(task_id)
            await h.approve(task_id)
            await h.wait(task_id)
            assert h.report(task_id)["status"] == "changes_verified"
        assert all(tools is None for _s, _m, tools in fake.conversations)
        assert "explore" not in fake.calls and "plan_evidence" in fake.calls
        fix_prompt = next(m for s, m, _t in fake.conversations if s == "fix")[1]["content"]
        assert "<<<<<<< SEARCH" in fix_prompt and "Only edit these files: offers/aggregator.py" in fix_prompt


async def test_a_wrong_new_test_is_corrected_instead_of_the_fix(settings, tmp_path):
    # Seen with MiMo: a correct fix, a regression test with a wrong expected value, and a "repair" that tried to
    # undo the fix. When only LocPilot's new test fails, the diagnosis decides which is wrong, and the test is fixed.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    script = hotel_script()
    wrong_test = script["test"].replace("[110.0]", "[999.0]")
    corrected = f"""{TEST_PATH}
<<<<<<< SEARCH
        self.assertEqual([o.price for o in merged], [999.0])
=======
        self.assertEqual([o.price for o in merged], [110.0])
>>>>>>> REPLACE
"""
    fake = FakeLLM({**script, "test": wrong_test, "test_fix": corrected,
                    "diagnose": {"diagnosis": "The code returned [110.0]; the test expected [999.0]. The request asks "
                                              "for the cheapest offer once, so the test is wrong.",
                                 "cause": "test_incorrect", "should_retry": True, "next_step": "Expect [110.0]"}})
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "awaiting_approval", task["error"]  # the corrected test awaits approval
        assert "test_fix" in fake.calls and "repair" not in fake.calls
        diagnose_prompt = next(m for s, m, _t in fake.conversations if s == "diagnose")[1]["content"]
        assert "LocPilot's own new test (written in this task): " in diagnose_prompt
        assert "Existing tests of the project (they passed before the change): all pass" in diagnose_prompt
        # The verdict comes after a trace of the failing assertion with real values, with the language's classic
        # pitfalls in view: asked for a verdict straight away a small model defends the theory behind its change.
        assert "- trace: work it out before you judge." in diagnose_prompt
        assert "- Python: a mutable default argument" in diagnose_prompt
        await h.approve(task_id)
        await h.wait(task_id)
        report = h.report(task_id)
        patches = h.db.list_patches(task_id)
    assert report["status"] == "changes_verified"
    assert report["verification"]["regression_test"]["status"] == "detected_bug"  # still fails without the fix
    assert [c["path"] for c in patches[-1]["changes"]] == [TEST_PATH]  # the fix itself was left alone


async def test_a_change_that_does_not_do_what_was_asked_is_revised_before_approval(settings, tmp_path):
    # Seen with MiMo: a wrong fix and a test with wrong thresholds agreed with each other, so the run ended
    # "verified". Asked to check the change against the request, the same model found the problem in one call.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    wrong_fix = edit_call(old="    return sort_offers(merged)", new="    return sort_offers(list(merged))")
    on_top = edit_call(old=REFERENCE.search.replace("sort_offers(merged)", "sort_offers(list(merged))"))
    found = {"requirements": [{"requirement": "the same room from two suppliers appears once", "holds": False,
                               "why": "`best` is still created per supplier, so both offers survive"}],
             "verdict": "code_wrong", "correction": "deduplicate across suppliers"}
    fake = ToolLLM(tool_script(fix=[calls(wrong_fix), "DONE"], check=found, revise_fix=[calls(on_top), "DONE"],
                               revise_test="DONE"))  # the test was right: kept as it is
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        patch = h.db.list_patches(task_id)[0]
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
        assert len(h.db.list_patches(task_id)) == 1  # corrected before the first approval, not after a failed run
    fixed = next(c for c in patch["changes"] if c["path"] == AGGREGATOR)
    assert "return sort_offers(list(best.values()))" in fixed["after"]
    assert any(w.startswith("LocPilot checked this change against your request, found a problem and revised it")
               and "`best` is still created per supplier" in w for w in patch["warnings"])
    check_prompt = next(m for s, m, _t in fake.conversations if s == "check")[1]["content"]
    assert "sort_offers(list(merged))" in check_prompt and TEST_PATH in check_prompt  # the fix and the new test
    revision = next(m for s, m, _t in fake.conversations if s == "revise_fix")
    wrote_fix = [m for s, m, _t in fake.conversations if s == "fix"][-1]
    assert revision[:len(wrote_fix)] == wrote_fix  # continues the conversation that made the first edit
    assert "`best` is still created per supplier" in revision[-1]["content"]
    assert "deduplicate across suppliers" not in revision[-1]["content"]  # the finding, not the reviewer's remedy
    # The test was written for the first version of the fix, so it is looked at again with the corrected one.
    test_again = next(m for s, m, _t in fake.conversations if s == "revise_test")[-1]["content"]
    assert "sort_offers(list(best.values()))" in test_again and "reply DONE without editing" in test_again


async def test_without_tools_a_failed_check_is_shown_with_the_patch(settings, tmp_path):
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    doubt = {"requirements": [{"requirement": "cheapest offer wins", "holds": False, "why": "not obvious"}],
             "verdict": "code_wrong", "correction": ""}
    for overrides, expect_check in (({}, True), ({"check_changes": False}, False)):
        fake = FakeLLM(hotel_script(check=doubt))
        async with Harness(settings.model_copy(update=overrides), fake) as h:
            task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
            await h.wait(task_id)
            patch = h.db.list_patches(task_id)[0]
        noted = any("found a possible problem that was not resolved" in w and "cheapest offer wins" in w
                    for w in patch["warnings"])
        assert ("check" in fake.calls) is expect_check and noted is expect_check


async def test_the_diagnosis_and_the_repair_continue_the_conversation_that_wrote_the_test(settings, tmp_path):
    # The model sees its own test, the code and the real output in one place, and the model server does not have
    # to read the whole context again for every step of the repair.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    wrong, right = TEST_FILE.replace("[110.0]", "[999.0]"), TEST_FILE
    fake = ToolLLM(tool_script(
        test=[calls(("write", {"file_path": TEST_PATH, "content": wrong})), "DONE"],
        diagnose={"diagnosis": "Produced [110.0], expected [999.0]; the request wants the cheapest offer once.",
                  "cause": "test_incorrect", "should_retry": True, "next_step": "Expect [110.0]"},
        test_fix=[calls(("write", {"file_path": TEST_PATH, "content": right})), "DONE"]))
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Fix duplicate offers")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "awaiting_approval", task["error"]
        await h.approve(task_id)
        await h.wait(task_id)
        assert h.report(task_id)["status"] == "changes_verified"
    wrote = [m for s, m, _t in fake.conversations if s == "test"][-1]  # the conversation that wrote the test
    diagnosis = next(m for s, m, _t in fake.conversations if s == "diagnose")
    assert diagnosis[:len(wrote)] == wrote and [m["role"] for m in diagnosis[len(wrote):]] == ["assistant", "user"]
    assert diagnosis[-2]["content"] == "DONE" and "## Verification failed" in diagnosis[-1]["content"]
    repair = [m for s, m, _t in fake.conversations if s == "test_fix"][0]
    assert repair[:len(diagnosis)] == diagnosis and [m["role"] for m in repair[len(diagnosis):]] == ["assistant", "user"]
    assert '"cause": "test_incorrect"' in repair[-2]["content"]
    assert repair[-1]["content"].startswith("The diagnosis is that the code change is right")
    offered = {s: [t["function"]["name"] for t in tools] for s, _m, tools in fake.conversations if tools}
    assert offered["diagnose"] == offered["test_fix"] == offered["test"] == ["read", "grep", "edit", "write"]
    assert "repair" not in fake.calls


async def test_an_existing_test_the_request_makes_outdated_is_updated_with_a_warning(settings, tmp_path):
    # Seen with a feature request (a new field in a returned object): the code was right, but an existing test
    # compared the exact old value, and LocPilot never lets a model touch existing tests. Now, once such a test has
    # been SEEN failing and the diagnosis is that the request makes it outdated, the repair may update it — and the
    # approval says so.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    script = hotel_script()
    fix = """offers/aggregator.py
<<<<<<< SEARCH
    return sorted(offers, key=lambda o: (o.hotel_id, o.price))
=======
    return sorted(offers, key=lambda o: (o.hotel_id, -o.price))
>>>>>>> REPLACE
"""
    new_test = f"""{TEST_PATH}
<<<<<<< SEARCH
=======
import unittest

from offers.aggregator import sort_offers
from offers.models import Offer


class MostExpensiveFirstTest(unittest.TestCase):
    def test_most_expensive_first_within_a_hotel(self):
        offers = [Offer("H1", "SGL", "RO", "a", 70.0), Offer("H1", "DBL", "RO", "a", 90.0)]
        self.assertEqual([o.price for o in sort_offers(offers)], [90.0, 70.0])
>>>>>>> REPLACE
"""
    update_existing = """tests/test_aggregator.py
<<<<<<< SEARCH
                         [("H1", 70.0), ("H1", 90.0), ("H2", 50.0)])
=======
                         [("H1", 90.0), ("H1", 70.0), ("H2", 50.0)])
>>>>>>> REPLACE
"""
    fake = FakeLLM({**script, "fix": fix, "test": new_test, "test_fix": update_existing,
                    "diagnose": {"diagnosis": "test_sort_offers_cheapest_first_per_hotel expects cheapest first; the "
                                              "request asks for most expensive first, so that test is outdated.",
                                 "cause": "test_incorrect", "should_retry": True,
                                 "next_step": "Update the expected order in tests/test_aggregator.py"}})
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Sort offers most expensive first within each hotel")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "awaiting_approval", task["error"]
        diagnose_prompt = next(m for s, m, _t in fake.conversations if s == "diagnose")[1]["content"]
        assert "LocPilot's own new test (written in this task): passes" in diagnose_prompt
        assert "test_sort_offers_cheapest_first_per_hotel" in diagnose_prompt.split("## Which tests fail")[1]
        repair = h.db.list_patches(task_id)[-1]
        assert [c["path"] for c in repair["changes"]] == ["tests/test_aggregator.py"]
        assert any("Updates the existing test tests/test_aggregator.py" in w for w in repair["warnings"])
        fix_prompt = next(m for s, m, _t in fake.conversations if s == "test_fix")[1]["content"]
        assert "### tests/test_aggregator.py" in fix_prompt and "Correct the tests, not the code" in fix_prompt
        await h.approve(task_id)
        await h.wait(task_id)
        report = h.report(task_id)
    assert report["status"] == "changes_verified", report["headline"]
    assert "repair" not in fake.calls  # the code was never "repaired"


async def test_a_suite_that_stays_red_after_the_fix_is_not_filed_under_already_broken(settings, tmp_path):
    # "The tests fail, fix it": a first fix that leaves them failing is wrong, not "only failures that already
    # existed remain". Seen with MiMo: it changed two comparisons where one was the bug.
    red_test = TEST_FILE.replace("CrossSupplierDuplicatesTest", "AlreadyRedTest").replace(
        "test_same_room_from_two_suppliers_is_returned_once", "test_one_offer_per_room")
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers", files={"tests/test_already_red.py": red_test})
    script = hotel_script()
    wrong_fix = """offers/aggregator.py
<<<<<<< SEARCH
    return sort_offers(merged)
=======
    return sort_offers(list(merged))
>>>>>>> REPLACE
"""
    harmless_test = script["test"].replace("self.assertEqual([o.price for o in merged], [110.0])",
                                           "self.assertTrue(merged)")  # passes before and after: proves nothing
    right_fix = f"""offers/aggregator.py
<<<<<<< SEARCH
{REFERENCE.search.replace("return sort_offers(merged)", "return sort_offers(list(merged))")}
=======
{REFERENCE.replace}
>>>>>>> REPLACE
"""
    fake = FakeLLM({**script, "fix": wrong_fix, "test": harmless_test, "repair": right_fix,
                    "diagnose": {"diagnosis": "test_already_red still gets two offers: merge_offers is unchanged.",
                                 "cause": "fix_incorrect", "should_retry": True, "next_step": "Hoist the dict"}})
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "tests/test_already_red.py fails: fix the bug")
        await h.wait(task_id)
        await h.approve(task_id)
        task = await h.wait(task_id)
        assert task["status"] == "awaiting_approval", task["error"]  # a repair, not "already broken"
        diagnose_prompt = next(m for s, m, _t in fake.conversations if s == "diagnose")[1]["content"]
        still = diagnose_prompt.split("Tests that already failed before the change and still fail: ")[1]
        assert "test_already_red" in still.split("\n")[0]
        await h.approve(task_id)
        await h.wait(task_id)
        report = h.report(task_id)
    assert report["status"] == "changes_verified", report["headline"]
    assert report["headline"].startswith("Verified by execution: tests of this project that failed before the change")
    assert report["verification"]["regression_test"]["status"] == "fixed_failing_tests"


async def test_adding_tests_has_its_own_prompt_and_the_tests_must_pass(settings, tmp_path):
    # "Add tests for X" is not a bug fix: there is nothing to reproduce, and the tests must pass on the code as it is.
    repo_path = make_repo(tmp_path / "hotel", "hotel-offers")
    script = hotel_script()
    new_tests = """tests/test_sorting.py
<<<<<<< SEARCH
=======
import unittest

from offers.aggregator import sort_offers
from offers.models import Offer


class SortOffersTest(unittest.TestCase):
    def test_cheapest_first_within_a_hotel(self):
        offers = [Offer("H1", "DBL", "RO", "a", 90.0), Offer("H1", "SGL", "RO", "a", 70.0)]
        self.assertEqual([o.price for o in sort_offers(offers)], [70.0, 90.0])
>>>>>>> REPLACE
"""
    fake = FakeLLM({**script, "add_tests": new_tests,
                    "understand": {**script["understand"], "intent": "add_tests"},
                    "plan": {**script["plan"], "test_file": "tests/test_sorting.py"}})
    async with Harness(settings, fake) as h:
        task_id = await h.start(h.add_repo(repo_path), "code", "Add tests for sort_offers")
        await h.wait(task_id)
        patch = h.db.list_patches(task_id)[0]
        await h.approve(task_id)
        await h.wait(task_id)
        report = h.report(task_id)
    assert [c["path"] for c in patch["changes"]] == ["tests/test_sorting.py"]
    assert report["status"] == "changes_verified" and report["headline"].startswith("New tests added and passing")
    assert "add_tests" in fake.calls and "test" not in fake.calls and "test_spec" not in fake.calls
    prompt = next(m for s, m, _t in fake.conversations if s == "add_tests")[1]["content"]
    assert "every test must PASS on the code as it is" in prompt and "FAILS on the current" not in prompt


def test_failures_are_attributed_to_the_new_test_whatever_the_runner_calls_it():
    def run(failing: list[str], stage: str) -> dict:
        return {"command_id": "c", "stage": stage, "argv": ["npm", "test"], "exit_code": 1,
                "summary": {"outcome": "failed", "failing_tests": failing}}

    new_test = "packages/algorithms/test/circuit-breaker.test.ts"
    names = ["counts consecutive failures, not total failures"]
    vitest_ids = ["circuit breaker > counts consecutive failures, not total failures 10ms",
                  f"{new_test} > circuit breaker > counts consecutive failures, not total failures"]
    verdict = compute_verification([run(vitest_ids, "before_fix")], [run(vitest_ids, "after_fix")], [new_test],
                                   commands_approved=True, new_test_names=names)
    assert verdict["only_new_test_fails"] is True and verdict["new_test_failures"] == sorted(vitest_ids)
    assert verdict["existing_failures"] == []
    broke = "packages/algorithms/test/raft.test.ts > raft > elects a leader"
    old = "packages/algorithms/test/replication.test.ts > was already red"
    mixed = compute_verification([run([*vitest_ids, old], "before_fix")],
                                 [run([*vitest_ids, broke, old], "after_fix")], [new_test],
                                 commands_approved=True, new_test_names=names)
    assert mixed["only_new_test_fails"] is False
    assert mixed["existing_failures"] == [broke]  # newly failing; the one that was red before is neither
    from locpilot.agent.graph import test_files_in

    files = ["packages/algorithms/src/raft.ts", "packages/algorithms/test/raft.test.ts", "tests/test_aggregator.py"]
    assert test_files_in([broke], files) == ["packages/algorithms/test/raft.test.ts"]
    assert test_files_in(["test_sort (tests.test_aggregator.MergeOffersTest.test_sort)"], files) == \
        ["tests/test_aggregator.py"]


def test_a_red_suite_turning_green_proves_the_fix():
    # "Tests fail, fix it": the project's own failing tests are the proof, whatever LocPilot's new test does.
    def run(stage: str, failing: list[str], passed: int) -> dict:
        return {"command_id": "c", "stage": stage, "argv": ["npm", "test"], "exit_code": 1 if failing else 0,
                "summary": {"outcome": "failed" if failing else "passed", "failing_tests": failing,
                            "failed": len(failing), "passed": passed}}

    red = ["leader election > elects exactly one leader", "split brain > never commits"]
    result = compute_verification([run("before_fix", red, 436)], [run("after_fix", [], 439)],
                                  ["packages/algorithms/test/raft.regression.test.ts"], commands_approved=True,
                                  new_test_names=["grants a vote to an equally up to date candidate"])
    reg = result["regression_test"]
    assert result["verdict"] == "passed" and reg["status"] == "fixed_failing_tests"
    assert reg["detail"].startswith("2 existing test(s) that failed before the change pass after it")
    assert "LocPilot's own new test did not fail before the change" in reg["detail"]
    assert result["fixed_tests"] == sorted(red)
    still_red = compute_verification([run("before_fix", red, 436)], [run("after_fix", red[:1], 438)], [],
                                     commands_approved=True)
    assert still_red["regression_test"]["status"] == "not_created" and still_red["fixed_tests"] == [red[1]]


# Staged edits ---------------------------------------------------------------------------------------------------


def session(tmp_path: Path, **kw) -> EditSession:
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    defaults = {"planned": {AGGREGATOR}, "allowed_new": set(), "protected": {"tests/test_aggregator.py"}}
    return EditSession(repo, RepoIndex(repo), **{**defaults, **kw})


def do(s: EditSession, name: str, **arguments) -> str:
    return s.handle({"name": name, "arguments": arguments})


def test_edits_are_staged_on_top_of_each_other_and_nothing_is_written(tmp_path):
    s = session(tmp_path)
    before = (s.workspace / AGGREGATOR).read_text()
    assert do(s, *edit_call()[:1], **edit_call()[1]).startswith("OK:")
    second = do(s, "edit", file_path=AGGREGATOR, old_string="    return sort_offers(list(best.values()))",
                new_string="    return sort_offers([*best.values()])")
    assert second.startswith("OK:")  # matched against the file as the first edit left it
    assert "sort_offers([*best.values()])" in do(s, "read", file_path=AGGREGATOR)  # reads show the pending state
    assert (s.workspace / AGGREGATOR).read_text() == before
    build = s.build()
    assert [c.path for c in build.changes] == [AGGREGATOR] and "[*best.values()]" in build.changes[0].after


def test_an_edit_that_repeats_an_earlier_one_is_not_accepted_again(tmp_path):
    # Seen with MiMo: the same edit five times, each answered "OK", until the turn limit.
    s = session(tmp_path)
    assert do(s, *edit_call()[:1], **edit_call()[1]).startswith("OK:")
    again = do(s, "edit", file_path=AGGREGATOR, old_string="    return sort_offers(list(best.values()))",
               new_string="    return sort_offers(list(best.values()))")
    assert again.startswith("ERROR:") and "changes nothing" in again and "reply DONE" in again
    assert len(s.staged) == 1


def test_an_edit_that_cannot_apply_says_why(tmp_path):
    s = session(tmp_path)
    missing = do(s, "edit", file_path=AGGREGATOR, old_string="    merged = set()", new_string="    merged = []")
    assert missing.startswith("ERROR:") and "old_string was not found" in missing
    twice = do(s, "edit", file_path=AGGREGATOR, old_string="offers", new_string="items")
    assert "matches" in twice and "locations" in twice
    assert "changes nothing" in do(s, "edit", file_path=AGGREGATOR, old_string="    merged: list[Offer] = []",
                                   new_string="    merged: list[Offer] = []")
    assert "does not exist yet" in do(s, "edit", file_path="offers/dedupe.py", old_string="a", new_string="b") or \
        "does not exist" in s.problems[-1]
    assert s.staged == [] and len(s.problems) == 4


def test_text_copied_with_line_numbers_still_matches(tmp_path):
    s = session(tmp_path)
    listing = do(s, "read", file_path=AGGREGATOR, offset=12, limit=1)
    numbered_line = listing.splitlines()[1]
    assert numbered_line.startswith("12 | ") and "merged: list[Offer] = []" in numbered_line
    assert do(s, "edit", file_path=AGGREGATOR, old_string=numbered_line,
              new_string="12 |     merged: list[Offer] = list()").startswith("OK:")
    assert "    merged: list[Offer] = list()\n" in s.build().changes[0].after


def test_which_files_an_edit_may_touch(tmp_path):
    s = session(tmp_path)
    other = do(s, "edit", file_path="offers/search.py", old_string="    return merge_offers(results)",
               new_string="    return merge_offers(dict(results))")
    assert other.startswith("OK:") and s.unplanned == ["offers/search.py"]  # another source file: allowed, noted
    existing = {"file_path": "tests/test_aggregator.py", "old_string": "import unittest",
                "new_string": "import unittest  # x"}
    assert "is an existing test: do not modify it" in do(s, "edit", **existing)
    fixing = session(tmp_path / "fix", tests_later=True)  # the code change of a fix: its test is the next step
    assert "tests are not written or changed in this step" in do(fixing, "edit", **existing)
    assert "not a path inside the repository" in do(s, "write", file_path="../outside.py", content="x = 1\n")
    assert "creating it was not planned" in do(s, "write", file_path="offers/new_module.py", content="x = 1\n")
    absolute = str(s.workspace / AGGREGATOR)
    assert do(s, "edit", file_path=absolute, old_string="    merged: list[Offer] = []",
              new_string="    merged: list[Offer] = list()").startswith("OK:")  # absolute paths inside the repo


def test_a_read_counts_as_progress_only_when_it_shows_something_new(tmp_path):
    s = session(tmp_path, tests_later=True)
    do(s, "read", file_path=AGGREGATOR, limit=10)
    do(s, "read", file_path=AGGREGATOR, offset=5, limit=10)  # lines 11-14 are new
    assert s.new_reads == 2
    s.new_reads = 0
    do(s, "read", file_path=AGGREGATOR, offset=3, limit=8)
    assert s.new_reads == 0  # seen before
    plain = do(s, "read", file_path="tests/test_aggregator.py")
    assert s.new_reads == 0 and "Tests are not written" not in plain  # nothing edited yet: just looking
    assert do(s, *edit_call()[:1], **edit_call()[1]).endswith("when it is complete, reply DONE.")
    do(s, "read", file_path=AGGREGATOR, offset=15)
    assert s.new_reads == 0  # the file it has just edited: checking its own work
    reminded = do(s, "read", file_path="tests/test_aggregator.py")
    assert reminded.endswith("If the code change is complete, reply DONE.)") and s.new_reads == 0
    do(s, "read", file_path="offers/models.py")
    assert s.new_reads == 1  # another source file: the change may go on there
    repairing = session(tmp_path / "repair")  # not the fix step: a test may be read for what it expects
    do(repairing, *edit_call()[:1], **edit_call()[1])
    assert "Tests are not written" not in do(repairing, "read", file_path="tests/test_aggregator.py")
    assert repairing.new_reads == 1


def test_the_test_step_only_writes_the_regression_test(tmp_path):
    s = session(tmp_path, planned={TEST_PATH}, allowed_new={TEST_PATH}, only_tests=True)
    assert "Only the regression test may be edited" in do(s, *edit_call()[:1], **edit_call()[1])
    assert do(s, "write", file_path=TEST_PATH, content=TEST_FILE).startswith("OK:")
    again = do(s, "write", file_path=TEST_PATH, content=TEST_FILE.replace("110.0]", "110.0], merged"))
    assert again.startswith("OK:") and len(s.staged) == 1  # written again: the latest version counts
    change = s.build().changes[0]
    assert change.change == "create" and change.kind == "test" and "[110.0], merged" in change.after


def test_existing_tests_can_grow_but_not_shrink(tmp_path):
    s = session(tmp_path, planned={"tests/test_aggregator.py"}, only_tests=True)
    removed = do(s, "edit", file_path="tests/test_aggregator.py", old_string="import unittest\n", new_string="")
    assert "must not be deleted or rewritten" in removed
    assert "must not be rewritten" in do(s, "write", file_path="tests/test_aggregator.py", content="pass\n")


def test_other_tool_names_and_shapes_are_understood(tmp_path):
    s = session(tmp_path)
    assert do(s, "str_replace_editor", command="str_replace", path=AGGREGATOR, old_str="    merged: list[Offer] = []",
              new_str="    merged: list[Offer] = list()").startswith("OK:")
    assert "aggregator.py:" in do(s, "search_files", query="sort_offers")
    assert "There is no shell here" in do(s, "bash", command="pytest")
    assert "no tool named 'todo_write'" in do(s, "todo_write", todos=[])
    s.can_read = False
    assert "no more reading" in do(s, "read", file_path=AGGREGATOR)


# Exploring ------------------------------------------------------------------------------------------------------


def explorer(tmp_path: Path, max_chars: int = 26_000) -> ExploreSession:
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    index = RepoIndex(repo)
    return ExploreSession(EvidenceCollector(index, SymbolIndex(index).build(), [], max_chars=max_chars), repo)


def test_what_the_model_looks_at_becomes_citable_evidence(tmp_path):
    e = explorer(tmp_path)
    reply, asked, ids = e.handle({"name": "read", "arguments": {"file_path": AGGREGATOR, "offset": 6, "limit": 15}})
    # The file ends five lines after what was asked for: they come along instead of costing a second read.
    assert ids == ["E1"] and asked == {"tool": "read_file", "query": "", "path": AGGREGATOR, "start_line": 6,
                                       "end_line": 25}
    assert reply.startswith(f'<evidence id="E1" file="{AGGREGATOR}" lines="6-25"') and "best[key] = offer" in reply
    again, _asked, ids = e.handle({"name": "read", "arguments": {"file_path": AGGREGATOR, "offset": 6, "limit": 15}})
    assert ids == [] and again.startswith("You already did exactly this") and e.repeats == 1  # going in circles
    overlap, _asked, ids = e.handle({"name": "read", "arguments": {"file_path": AGGREGATOR, "offset": 8, "limit": 10}})
    assert ids == [] and overlap.startswith(f"You already have these lines of {AGGREGATOR} above (lines 6-25 are E1)")
    assert "<evidence" not in overlap and e.repeats == 2
    found, asked, ids = e.handle({"name": "grep", "arguments": {"pattern": "def dedupe_key"}})
    assert "offers/models.py:" in found and asked["tool"] == "search_text" and len(ids) == 1
    assert 'file="offers/models.py"' in found  # the code around the hit is shown, and is evidence
    definition, asked, ids = e.handle({"name": "find_definition", "arguments": {"name": "sort_offers"}})
    assert asked == {"tool": "find_definition", "query": "sort_offers", "path": ""} and "1 definitions" in definition
    listing, _asked, ids = e.handle({"name": "list", "arguments": {"path": "offers"}})
    assert "aggregator.py" in listing and ids == []


def long_file_explorer(tmp_path: Path, seeded: tuple[int, int], shown_lines: int | None = 45) -> ExploreSession:
    """An explorer over a repository with a 150-line file of which the lines ``seeded`` are evidence already."""
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    (repo / "offers" / "rates.py").write_text("".join(f"RATE_{n} = {n}\n" for n in range(1, 151)))
    index = RepoIndex(repo)
    collector = EvidenceCollector(index, SymbolIndex(index).build(), [])
    collector.add(RATES, *seeded, kind="search_hit", title="rates", reason="matches the request")
    return ExploreSession(collector, repo, shown_lines=shown_lines)


def read(e: ExploreSession, **args) -> tuple[str, list[str]]:
    reply, _asked, ids = e.handle({"name": "read", "arguments": {"file_path": RATES, **args}})
    return reply, ids


def test_code_is_shown_once_and_pointed_at_after_that(tmp_path):
    e = long_file_explorer(tmp_path, seeded=(60, 100))  # E1, 41 lines: the prompt shows it whole
    first, ids = read(e)
    assert ids == ["E2"] and first.startswith(f'<evidence id="E2" file="{RATES}" lines="1-110"')
    assert first.endswith(f"… 40 more lines in {RATES} (read again with offset=111)")
    rest, ids = read(e, offset=111)
    assert ids == ["E3"] and 'lines="111-150"' in rest and "RATE_150 = 150" in rest
    assert 'id="E2"' not in rest and "RATE_110 = 110" not in rest  # what the first read showed is not repeated
    assert rest.endswith(f"You now have the whole of {RATES} (150 lines).")
    # A short range it has: the lines, plainly, and no new evidence. A long one: only where they are.
    short, ids = read(e, offset=100, limit=20)
    assert ids == [] and short.startswith(f"You already have these lines of {RATES} above (lines 60-100 are E1, ")
    assert "RATE_119 = 119" in short and "<evidence" not in short and e.repeats == 1
    long, ids = read(e, offset=1, limit=100)
    assert ids == [] and "Nothing new here." in long and "RATE_50 = 50" not in long and e.repeats == 2


def test_a_read_that_overlaps_known_lines_adds_only_the_new_ones(tmp_path):
    e = long_file_explorer(tmp_path, seeded=(60, 100))
    reply, ids = read(e, offset=90, limit=30)  # 90-119, of which 90-100 are known
    assert ids == ["E2"] and 'lines="101-119"' in reply and "RATE_95 = 95" not in reply
    assert "The other lines you asked for are above (lines 60-100 are E1)." in reply
    assert reply.endswith(f"… 31 more lines in {RATES} (read again with offset=120)") and e.repeats == 0
    tail, ids = read(e, offset=120, limit=10)  # stops 21 lines before the end: the rest comes along
    assert ids == ["E3"] and 'lines="120-150"' in tail and not tail.endswith("(150 lines).")  # 1-59 still unread


def test_a_snippet_the_prompt_cut_short_is_shown_in_full_when_read(tmp_path):
    e = long_file_explorer(tmp_path, seeded=(20, 100))  # 81 lines, of which the prompt shows 45
    assert e.shown == set()
    reply, ids = read(e, offset=20, limit=81)
    assert ids == ["E2"] and 'lines="20-100"' in reply and "RATE_100 = 100" in reply and e.repeats == 0
    whole = long_file_explorer(tmp_path / "again", seeded=(20, 100), shown_lines=None)  # nothing was cut short
    assert whole.shown == {"E1"} and read(whole, offset=20, limit=81)[1] == []


def test_a_definition_the_model_already_has_is_pointed_at(tmp_path):
    e = explorer(tmp_path)
    first, _asked, ids = e.handle({"name": "find_definition", "arguments": {"name": "merge_offers"}})
    assert ids == ["E1"] and '<evidence id="E1"' in first
    callers, _asked, ids = e.handle({"name": "find_definition", "arguments": {"name": "merge_offers", "kind": "x"}})
    assert ids == [] and "already shown above: E1 (offers/aggregator.py:6-20)" in callers and "<evidence" not in callers


def test_exploring_never_changes_anything_and_respects_the_budget(tmp_path):
    e = explorer(tmp_path, max_chars=300)
    refused, _asked, _ids = e.handle({"name": "edit", "arguments": {"file_path": AGGREGATOR, "old_string": "a",
                                                                    "new_string": "b"}})
    assert "this step only investigates" in refused
    full, _asked, ids = e.handle({"name": "read", "arguments": {"file_path": AGGREGATOR}})
    assert "evidence budget is full" in full and ids == []
    missing, _asked, _ids = e.handle({"name": "read", "arguments": {"file_path": "offers/nope.py"}})
    assert "no such file" in missing


# Planning -------------------------------------------------------------------------------------------------------


def test_a_plan_may_create_a_source_file_next_to_related_ones(tmp_path):
    index = RepoIndex(make_repo(tmp_path / "hotel", "hotel-offers"))
    assert new_source_path(index, "offers/dedupe.py") == "offers/dedupe.py"
    assert new_source_path(index, "./offers/dedupe.py") == "offers/dedupe.py"
    assert new_source_path(index, AGGREGATOR) is None  # exists: not a new file
    assert new_source_path(index, "nowhere/dedupe.py") is None  # no folder with such files
    assert new_source_path(index, "offers/dedupe.rs") is None  # no Rust next to it
    assert new_source_path(index, "tests/test_new.py") is None  # tests are planned separately
    assert new_source_path(index, "../dedupe.py") is None and new_source_path(index, "dedupe.py") is None


def test_the_analysis_is_reminded_of_the_pitfalls_of_the_languages_in_the_evidence():
    from locpilot.agent import prompts

    js = prompts.pitfalls(["packages/a/src/metrics.ts", "apps/web/Clock.tsx", "README.md"])
    assert "JavaScript/TypeScript: array.sort() without a compare function" in js and "Python" not in js
    both = prompts.pitfalls(["offers/aggregator.py", "web/app.js"])
    assert "- JavaScript/TypeScript:" in both and "- Python: a mutable default argument" in both
    assert prompts.pitfalls(["Cargo.toml", "src/main.rs"]) == "" and prompts.pitfalls([]) == ""


def test_the_diagnosis_of_failing_tests_must_start_with_a_trace():
    from locpilot.agent.schemas import LLMTracedFailure
    from locpilot.llm.ollama import inline_refs

    schema = inline_refs(LLMTracedFailure.model_json_schema())
    assert list(schema["properties"])[0] == "trace" and schema["required"][0] == "trace"  # written first, always
    scripted = LLMTracedFailure.model_validate({"diagnosis": "d", "cause": "unknown", "should_retry": False})
    assert scripted.trace == ""  # an answer without one (older scripts, other models) is still understood
