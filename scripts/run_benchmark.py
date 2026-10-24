#!/usr/bin/env python3
"""LocPilot agent benchmark.

For each fixture in ``fixtures/*/benchmark.json`` this script:

1. Validates the fixture itself: the hidden acceptance tests FAIL on the original
   code and PASS with the reference fix; existing tests pass on the original code.
2. Runs the real agent (local Ollama model) in Code mode on a fresh Git copy.
   BENCHMARK HARNESS ONLY: the harness auto-approves the agent's proposed patches
   and test commands. The app itself always requires a human approval.
3. Measures, independently of the agent's own claims:
   - relevant_files: expected files appear in the agent's examined evidence
   - root_cause_location: a root-cause citation overlaps the known bug lines
   - root_cause_keywords: the root-cause text mentions the expected concept
   - patch_accepted: hidden acceptance tests pass on the agent's worktree
   - regression_test_detects_bug: the agent's new tests fail on a pristine copy of
     the original code and pass on the patched worktree (re-run by this harness)
   - existing_tests_pass: the fixture's original tests pass on the patched worktree
   - within_limits: the task terminated with LLM calls/retries within budget

Usage: uv run --project backend python scripts/run_benchmark.py [--fixtures a,b] [--model NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from locpilot.app import create_app  # noqa: E402
from locpilot.config import Settings  # noqa: E402
from locpilot.tools.patch import apply_changes, build_patch, parse_edit_blocks  # noqa: E402

FIXTURES = ROOT / "fixtures"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@example.invalid",
           "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@example.invalid"}


def sh(argv: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=GIT_ENV)


def resolve_cmd(argv: list[str]) -> list[str]:
    if argv and argv[0] in ("python3", "python"):
        return [sys.executable, *argv[1:]]
    return argv


def fresh_repo(fixture: str, dest: Path) -> Path:
    shutil.copytree(FIXTURES / fixture / "repo", dest)
    sh(["git", "init", "-q", "-b", "main"], dest)
    sh(["git", "add", "-A"], dest)
    sh(["git", "commit", "-q", "-m", "fixture"], dest)
    return dest


def run_acceptance(spec: dict, fixture: str, workdir: Path) -> tuple[bool, str]:
    acc = workdir / "acceptance"
    if acc.exists():
        shutil.rmtree(acc)
    shutil.copytree(FIXTURES / fixture / "acceptance", acc)
    if spec["language"] == "python":
        (acc / "__init__.py").write_text("")
    res = sh(resolve_cmd(spec["acceptance_command"]), workdir)
    shutil.rmtree(acc)
    return res.returncode == 0, (res.stdout + res.stderr)[-1500:]


def reference_bug_lines(fixture: str) -> dict[str, set[int]]:
    """Lines of the ORIGINAL files that the reference fix changes (the known bug location)."""
    import difflib

    blocks, _ = parse_edit_blocks((FIXTURES / fixture / "reference_fix.txt").read_text())
    root = FIXTURES / fixture / "repo"
    build = build_patch(root, blocks)
    out: dict[str, set[int]] = {}
    for ch in build.changes:
        before = (ch.before or "").splitlines()
        after = ch.after.splitlines()
        lines: set[int] = set()
        for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, before, after).get_opcodes():
            if tag != "equal":
                lines.update(range(i1 + 1, max(i2, i1 + 1) + 1))
        out[ch.path] = lines
    return out


def validate_fixture(fixture: str, spec: dict, tmp: Path) -> dict:
    original = fresh_repo(fixture, tmp / f"{fixture}-orig")
    acc_orig, _ = run_acceptance(spec, fixture, original)
    existing = sh(resolve_cmd(spec["existing_test_command"]), original).returncode == 0
    fixed = fresh_repo(fixture, tmp / f"{fixture}-ref")
    blocks, problems = parse_edit_blocks((FIXTURES / fixture / "reference_fix.txt").read_text())
    build = build_patch(fixed, blocks)
    assert build.ok and not problems, (build.errors, problems)
    apply_changes(fixed, build.changes)
    acc_fixed, out = run_acceptance(spec, fixture, fixed)
    return {"acceptance_fails_on_original": not acc_orig, "acceptance_passes_with_reference_fix": acc_fixed,
            "existing_tests_pass_on_original": existing, "valid": (not acc_orig) and acc_fixed and existing}


async def run_agent(fixture: str, spec: dict, tmp: Path, model: str | None, max_minutes: float) -> dict:
    repo = fresh_repo(fixture, tmp / fixture)
    overrides = {"data_dir": tmp / f"data-{fixture}", "allowed_repo_roots": [tmp]}
    if model:
        overrides["model"] = model
    settings = Settings(**overrides)
    app = create_app(settings)
    db, manager = app.state.db, app.state.manager
    start = time.monotonic()
    approvals = 0
    async with app.router.lifespan_context(app):
        await app.state.llm_factory().ensure_ready()
        r = db.add_repo(name=fixture, source_type="local", source_url=None, path=str(repo), managed=False)
        task = db.create_task(repo_id=r["id"], title=spec["prompt"][:60], mode="code", model=settings.model)
        await manager.start_turn(task["id"], spec["prompt"], "code")
        timed_out = False
        while True:
            while manager.is_running(task["id"]):
                if time.monotonic() - start > max_minutes * 60:
                    await manager.cancel(task["id"])
                    timed_out = True
                    break
                await asyncio.sleep(0.5)
            row = db.get_task(task["id"])
            if row["status"] == "awaiting_approval" and not timed_out:
                approval = db.pending_approval(task["id"])
                approvals += 1
                await manager.decide(task["id"], approval["id"], {
                    "decision": "approve", "commands": [c["argv"] for c in approval["payload"]["commands"]]})
                continue
            break
        row = db.get_task(task["id"])
        reports = db.list_reports(task["id"])
        patches = db.list_patches(task["id"])
    report = reports[-1] if reports else {}
    return {"repo": repo, "task": row, "report": report, "patches": patches, "approvals": approvals,
            "duration_s": round(time.monotonic() - start, 1), "timed_out": timed_out, "settings": settings}


def score(fixture: str, spec: dict, run: dict, tmp: Path) -> dict:
    report = run["report"] or {}
    inv = report.get("investigation") or {}
    worktree = Path(run["task"]["worktree_path"]) if run["task"].get("worktree_path") else None
    examined = {f["path"] for f in inv.get("files_examined") or []}
    expected = set(spec["expected_files"])
    rc = spec["root_cause"]
    cites = inv.get("root_cause_citations") or []
    bug_lines = reference_bug_lines(fixture)
    loc = any(set(range(c["start_line"], c["end_line"] + 1)) & bug_lines.get(c["path"], set()) for c in cites)
    text = (inv.get("most_likely_root_cause") or "").lower()
    kw = any(k.lower() in text for k in rc["keywords"])
    result = {
        "status": report.get("status"), "headline": report.get("headline"),
        "relevant_files": expected <= examined, "root_cause_location": loc, "root_cause_keywords": kw,
        "root_cause_text": inv.get("most_likely_root_cause"),
        "patch_accepted": False, "regression_test_detects_bug": False, "existing_tests_pass": False,
        "agent_claimed_regression": ((report.get("verification") or {}).get("regression_test") or {}).get("status"),
        "within_limits": False, "llm_calls": (report.get("stats") or {}).get("llm_calls"),
        "retries": report.get("retries"), "approvals": run["approvals"], "duration_s": run["duration_s"],
        "intent": report.get("intent"), "root_cause_citations": [f"{c['path']}:{c['start_line']}-{c['end_line']}"
                                                                 for c in cites],
        "known_bug_lines": {k: sorted(v) for k, v in bug_lines.items()},
        "diff": "".join(p["diff"] for p in run["patches"] if p["status"] == "applied")[:6000],
        "unresolved": report.get("unresolved_issues"),
    }
    settings = run["settings"]
    result["within_limits"] = bool(
        not run["timed_out"] and run["task"]["status"] in ("completed", "failed", "needs_input")
        and (result["llm_calls"] or 0) <= settings.max_llm_calls and (result["retries"] or 0) <= settings.max_retries)
    if worktree is None or not worktree.exists():
        return result
    result["patch_accepted"], _ = run_acceptance(spec, fixture, worktree)
    created_tests = sorted({c["path"] for p in run["patches"] if p["status"] == "applied" for c in p["changes"]
                            if c["kind"] == "test"})
    result["agent_tests"] = created_tests
    # Existing tests on the patched code (agent-created tests removed from a copy).
    patched_copy = tmp / f"{fixture}-patched-existing"
    shutil.copytree(worktree, patched_copy, ignore=shutil.ignore_patterns(".git"))
    for t in created_tests:
        (patched_copy / t).unlink(missing_ok=True)
    result["existing_tests_pass"] = sh(resolve_cmd(spec["existing_test_command"]), patched_copy).returncode == 0
    # Agent tests: must fail on a pristine original and pass on the patched worktree.
    if created_tests:
        pristine = fresh_repo(fixture, tmp / f"{fixture}-pristine")
        for t in created_tests:
            (pristine / t).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(worktree / t, pristine / t)
        fails_before = sh(resolve_cmd(spec["existing_test_command"]), pristine).returncode != 0
        passes_after = sh(resolve_cmd(spec["existing_test_command"]), worktree).returncode == 0
        result["regression_test_detects_bug"] = fails_before and passes_after
    return result


def markdown(results: list[dict], meta: dict) -> str:
    def mark(v):
        return "✅" if v is True else ("❌" if v is False else "—")

    lines = [
        "# LocPilot agent benchmark — measured results", "",
        f"- Date: {meta['date']}", f"- Model: `{meta['model']}` via local Ollama",
        f"- Machine: {meta['machine']}", f"- Fixtures: {len(results)} small, deterministic bug-fix repositories "
        "(`fixtures/`)", "- Harness auto-approved the agent's patches and test commands (the app itself always asks "
        "a human).", "",
        "| Fixture | Fixture valid | Relevant files | Root cause (location) | Root cause (concept) | Patch passes hidden "
        "tests | Regression test detects bug | Existing tests pass | Within limits | Agent status | LLM calls | "
        "Retries | Time |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        a = r.get("agent") or {}
        lines.append(f"| {r['fixture']} | {mark(r['fixture_check']['valid'])} | {mark(a.get('relevant_files'))} | "
                     f"{mark(a.get('root_cause_location'))} | {mark(a.get('root_cause_keywords'))} | "
                     f"{mark(a.get('patch_accepted'))} | {mark(a.get('regression_test_detects_bug'))} | "
                     f"{mark(a.get('existing_tests_pass'))} | {mark(a.get('within_limits'))} | "
                     f"{a.get('status', r.get('error', 'not run'))} | {a.get('llm_calls', '—')} | "
                     f"{a.get('retries', '—')} | {a.get('duration_s', '—')}s |")
    keys = ["relevant_files", "root_cause_location", "root_cause_keywords", "patch_accepted",
            "regression_test_detects_bug", "existing_tests_pass", "within_limits"]
    ran = [r["agent"] for r in results if r.get("agent")]
    if ran:
        lines += ["", "**Totals:** " + ", ".join(f"{k.replace('_', ' ')} {sum(1 for a in ran if a.get(k))}/{len(ran)}"
                                                  for k in keys)]
    lines += ["", "Caveats: these are tiny synthetic fixtures with known answers. Passing them does not demonstrate "
              "production-grade autonomous coding; results vary run to run with local model sampling, hardware and "
              "model choice.", "",
              "Disclosure: the root-cause prompt includes a generic checklist of common bug patterns, and these "
              "fixtures exercise common bug classes that the checklist also names (state reset in a loop, "
              "off-by-one, missing keys, arithmetic scale). The 'root cause (concept)' keyword lists were extended "
              "after the first run to include canonical phrasings such as 'off-by-one' and 'KeyError'. "
              "'Root cause (location)' is computed from the lines the reference fix changes. Hidden acceptance tests "
              "and the regression re-run are independent of the agent's own claims.", ""]
    for r in results:
        a = r.get("agent") or {}
        if a:
            lines.append(f"- **{r['fixture']}** — {a.get('headline')} Root cause stated: “{a.get('root_cause_text')}”. "
                         f"Agent's own regression claim: `{a.get('agent_claimed_regression')}`.")
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", default="")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-minutes", type=float, default=15)
    parser.add_argument("--output", default=str(ROOT / "docs" / "benchmark-results.md"))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    names = [n for n in args.fixtures.split(",") if n] or sorted(p.parent.name for p in FIXTURES.glob("*/benchmark.json"))
    tmp = Path(tempfile.mkdtemp(prefix="locpilot-bench-"))
    results = []
    model = args.model or Settings().model
    for name in names:
        spec = json.loads((FIXTURES / name / "benchmark.json").read_text())
        entry: dict = {"fixture": name, "fixture_check": validate_fixture(name, spec, tmp)}
        print(f"[{name}] fixture check: {entry['fixture_check']}", flush=True)
        if not args.validate_only and entry["fixture_check"]["valid"]:
            try:
                run = await run_agent(name, spec, tmp, args.model, args.max_minutes)
                entry["agent"] = score(name, spec, run, tmp)
            except Exception as exc:  # noqa: BLE001 - record and continue with the next fixture
                entry["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[{name}] {json.dumps(entry.get('agent') or entry.get('error'), default=str)}", flush=True)
        results.append(entry)
    meta = {"date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"), "model": model,
            "machine": f"{platform.machine()} {platform.system()} {platform.release()}, "
                       f"{os.cpu_count()} CPUs"}
    if sys.platform == "darwin":
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
        mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip() or 0)
        meta["machine"] = f"{chip}, {mem // 2**30} GB RAM, macOS {platform.mac_ver()[0]}"
    if not args.validate_only:
        Path(args.output).write_text(markdown(results, meta))
        Path(args.output).with_suffix(".json").write_text(json.dumps({"meta": meta, "results": results}, indent=2,
                                                                      default=str))
        print(f"wrote {args.output}")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
