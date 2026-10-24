from pathlib import Path

import pytest

from locpilot.tools import git
from locpilot.tools.files import RepoIndex
from locpilot.tools.manifests import inspect_project
from locpilot.tools.patch import (
    EditBlock,
    UnexpectedChangeError,
    apply_changes,
    build_patch,
    parse_edit_blocks,
    revert_changes,
)
from locpilot.tools.search import SearchError, search
from locpilot.tools.symbols import SymbolIndex
from locpilot.tools.testparse import parse_test_output
from tests.conftest import make_repo


@pytest.fixture
def hotel_repo(tmp_path) -> Path:
    repo = make_repo(tmp_path / "hotel", "hotel-offers")
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "node_modules" / "x").mkdir(parents=True)
    (repo / "node_modules" / "x" / "index.js").write_text("module.exports = 1\n")
    (repo / "ignored.log").write_text("noise\n")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "*.log\n")
    return repo


def test_inventory_respects_gitignore_exclusions_and_secrets(hotel_repo):
    index = RepoIndex(hotel_repo)
    index.refresh(force=True)
    assert "offers/aggregator.py" in index.files
    assert ".env" not in index.files
    assert not any(f.startswith("node_modules/") for f in index.files)
    assert "ignored.log" not in index.files
    assert index.read_text(".env") is None


def test_search_literal_regex_and_errors(hotel_repo):
    index = RepoIndex(hotel_repo)
    index.refresh(force=True)
    hits = search(index, "dedupe_key").hits
    assert {h.path for h in hits} >= {"offers/models.py", "offers/aggregator.py"}
    assert all(h.line > 0 for h in hits)
    regex_hits = search(index, r"def \w+_offers", is_regex=True).hits
    assert any(h.path == "offers/aggregator.py" for h in regex_hits)
    with pytest.raises(SearchError):
        search(index, "(unclosed", is_regex=True)


def test_symbols_definitions_callers_and_chain(hotel_repo):
    index = RepoIndex(hotel_repo)
    index.refresh(force=True)
    symbols = SymbolIndex(index).build()
    [merge] = symbols.definitions("merge_offers")
    assert merge.path == "offers/aggregator.py" and merge.start_line == 6
    callers = symbols.callers("merge_offers")
    assert any(c.path == "offers/search.py" and c.caller and c.caller.name == "search_hotels" for c in callers)
    chain = symbols.call_chain("merge_offers")
    assert chain and chain[0]["caller"] in ("search_hotels", "MergeOffersTest.test_keeps_cheapest_offer_within_one_supplier",
                                            "MergeOffersTest.test_different_products_are_kept")
    assert symbols.enclosing("offers/aggregator.py", 15).name == "merge_offers"
    assert any(path == "offers/search.py" for path, _, _ in symbols.importers_of("offers/aggregator.py"))


def test_manifest_detection_python_and_node(hotel_repo, tmp_path):
    ov = inspect_project(RepoIndex(hotel_repo))
    assert "unittest" in ov.test_frameworks
    assert ov.test_commands[0][-4:] == ["discover", "-s", "tests", "-t"] or "unittest" in ov.test_commands[0]
    js = make_repo(tmp_path / "cart", "cart-discount")
    ov_js = inspect_project(RepoIndex(js))
    assert ["npm", "test"] in ov_js.test_commands
    assert "node:test" in ov_js.test_frameworks


def test_git_status_and_url_validation(hotel_repo):
    (hotel_repo / "offers" / "models.py").write_text("# changed\n")
    (hotel_repo / "new_file.py").write_text("x = 1\n")
    st = git.status(hotel_repo)
    assert st.branch == "main" and st.dirty and st.modified == 2 and st.untracked >= 1  # models.py + .gitignore
    assert {"offers/models.py", "new_file.py"} <= {f.path for f in st.files}
    allowed = ["github.com", "gitlab.com"]
    assert git.parse_remote_url("https://github.com/pallets/flask", allowed).url == "https://github.com/pallets/flask.git"
    assert git.parse_remote_url("https://GitHub.com/a/b.git/", allowed).repo == "b"
    assert git.parse_remote_url("git@github.com:a/b.git", allowed).host == "github.com"
    for bad in ["http://github.com/a/b", "https://token@github.com/a/b", "https://user:pw@github.com/a/b",
                "https://evil.com/a/b", "file:///etc", "ext::sh -c touch% /tmp/pwned", "https://github.com/a",
                "https://github.com/a/b?x=1", "https://github.com/../b", "https://github.com:8443/a/b",
                "https://github.com/a/b/tree/main", "/Users/me/repo", "github.com/a/b"]:
        with pytest.raises(ValueError):
            git.parse_remote_url(bad, allowed)


def test_patch_build_apply_unexpected_change_and_rollback(hotel_repo):
    text = (Path(__file__).resolve().parents[2] / "fixtures/hotel-offers/reference_fix.txt").read_text()
    blocks, problems = parse_edit_blocks(text)
    assert not problems and blocks[0].path == "offers/aggregator.py"
    build = build_patch(hotel_repo, blocks)
    assert build.ok and "-        best: dict" in build.diff
    original = (hotel_repo / "offers/aggregator.py").read_text()
    apply_changes(hotel_repo, build.changes)
    assert (hotel_repo / "offers/aggregator.py").read_text() != original
    assert apply_changes(hotel_repo, build.changes) == []  # idempotent
    restored, skipped = revert_changes(hotel_repo, build.changes)
    assert restored == ["offers/aggregator.py"] and not skipped
    assert (hotel_repo / "offers/aggregator.py").read_text() == original
    (hotel_repo / "offers/aggregator.py").write_text(original + "# user edit\n")
    with pytest.raises(UnexpectedChangeError):
        apply_changes(hotel_repo, build.changes)


def test_patch_rejects_unsafe_and_invalid_edits(hotel_repo):
    assert build_patch(hotel_repo, [EditBlock("../x.py", "", "x")]).errors
    assert build_patch(hotel_repo, [EditBlock(".env", "", "A=2")]).errors
    assert build_patch(hotel_repo, [EditBlock("node_modules/x/index.js", "module", "x")]).errors
    assert build_patch(hotel_repo, [EditBlock("offers/aggregator.py", "    return sort_offers(merged)",
                                              "    return sort_offers(merged")]).errors  # syntax error
    assert build_patch(hotel_repo, [EditBlock("offers/new.py", "", "x = 1")], allowed_new_files=set()).errors
    assert "not found" in build_patch(hotel_repo, [EditBlock("offers/aggregator.py", "nonexistent()", "x")]).errors[0]


def test_parse_test_outputs():
    assert parse_test_output("1 failed, 2 passed in 0.1s", "", 1).outcome == "failed"
    assert parse_test_output("", "Ran 0 tests in 0.000s\n\nOK", 0).outcome == "no_tests"
    assert parse_test_output("no tests ran in 0.01s", "", 5).outcome == "no_tests"
    s = parse_test_output("ℹ tests 3\nℹ pass 3\nℹ fail 0\n", "", 0)
    assert (s.framework, s.outcome, s.passed) == ("node:test", "passed", 3)
    assert parse_test_output("Tests:       1 failed, 4 passed, 5 total", "", 1).failed == 1


def test_fenced_new_file_reply_is_accepted_only_for_allowed_new_files():
    from locpilot.tools.patch import parse_fenced_new_files

    reply = "tests/test_new.py\n```python\nimport unittest\n\nclass T(unittest.TestCase):\n    pass\n```\n"
    blocks = parse_fenced_new_files(reply, {"tests/test_new.py"})
    assert len(blocks) == 1 and blocks[0].search == "" and blocks[0].replace.startswith("import unittest")
    assert parse_fenced_new_files(reply, set()) == []
    assert parse_fenced_new_files("```python\nx = 1\n```", {"a.py", "b.py"}) == []


def test_import_resolution_for_code_context(hotel_repo, tmp_path):
    from locpilot.agent.graph import _resolve_import

    index = RepoIndex(hotel_repo)
    index.refresh(force=True)
    assert _resolve_import(index, "offers/aggregator.py", "offers.models") == "offers/models.py"
    assert _resolve_import(index, "offers/aggregator.py", "offers.models.Offer") == "offers/models.py"
    assert _resolve_import(index, "offers/aggregator.py", "os") is None
    js = make_repo(tmp_path / "cart", "cart-discount")
    jindex = RepoIndex(js)
    jindex.refresh(force=True)
    assert _resolve_import(jindex, "src/cart.js", "./discounts") == "src/discounts.js"
    assert _resolve_import(jindex, "test/cart.test.js", "../src/cart") == "src/cart.js"


def test_root_cause_citation_is_reanchored_to_the_identifier(hotel_repo):
    from locpilot.agent.citations import refine_citations
    from locpilot.agent.schemas import Citation

    index = RepoIndex(hotel_repo)
    index.refresh(force=True)
    evidence = [{"id": "E1", "path": "offers/aggregator.py", "start_line": 6, "end_line": 20},
                {"id": "E2", "path": "offers/search.py", "start_line": 8, "end_line": 13}]
    removed: list[str] = []
    wrong = [Citation(path="offers/search.py", start_line=11, end_line=12, evidence_id="E2")]
    fixed = refine_citations("The `best` dict is created inside the loop", wrong, index, evidence, removed)
    assert fixed == [Citation(path="offers/aggregator.py", start_line=14, end_line=14, evidence_id="E1")]
    assert removed and "does not mention `best`" in removed[0]
    right = [Citation(path="offers/aggregator.py", start_line=14, end_line=15, evidence_id="E1")]
    assert refine_citations("`best` is reset", right, index, evidence, []) == right


def test_evidence_ids_are_humanized_in_plain_text(hotel_repo):
    from locpilot.agent.citations import CitationResolver

    index = RepoIndex(hotel_repo)
    index.refresh(force=True)
    resolver = CitationResolver(index, [{"id": "E2", "path": "offers/aggregator.py", "start_line": 6, "end_line": 20}])
    assert resolver.humanize("Root cause E2:14-15") == "Root cause (offers/aggregator.py:14-15)"
    assert resolver.humanize("`best` reset [E2:14]") == "`best` reset (offers/aggregator.py:14)"
    assert resolver.humanize("no refs here, E2E tests ok") == "no refs here, E2E tests ok"
    assert resolver.humanize("bogus [E9]") == "bogus"
