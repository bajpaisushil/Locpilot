"""Parse test-runner output into a structured summary.

Only facts present in the actual output are reported. When a runner's format is
unknown the outcome falls back to the exit code and is marked ``unknown``
framework; "no tests collected" is never treated as a pass.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class TestSummary:
    framework: str = "unknown"
    outcome: str = "unknown"  # passed | failed | error | no_tests | unknown
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    total: int | None = None
    failing_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _num(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def _finish(summary: TestSummary, exit_code: int | None) -> TestSummary:
    counts = [c for c in (summary.passed, summary.failed, summary.errors) if c is not None]
    if summary.total is None and counts:
        summary.total = sum(counts) + (summary.skipped or 0)
    if summary.outcome == "unknown":
        if exit_code is None:
            summary.outcome = "error"
        elif (summary.failed or 0) > 0 or (summary.errors or 0) > 0:
            summary.outcome = "failed"
        elif exit_code == 0:
            summary.outcome = "no_tests" if summary.total == 0 else "passed"
        else:
            summary.outcome = "failed"
    summary.failing_tests = summary.failing_tests[:50]
    return summary


def parse_test_output(stdout: str, stderr: str, exit_code: int | None) -> TestSummary:
    text = f"{stdout}\n{stderr}"

    # pytest -------------------------------------------------------------------------
    pytest_line = None
    for line in reversed(text.splitlines()):
        stripped = line.strip("= ").strip()
        if re.search(r"\b(passed|failed|error|errors|skipped|no tests ran|deselected|xfailed)\b", stripped) and \
                re.search(r"\bin [\d.]+s\b", stripped):
            pytest_line = stripped
            break
    if pytest_line is not None:
        s = TestSummary(framework="pytest")
        s.passed = _num(r"(\d+) passed", pytest_line) or 0
        s.failed = _num(r"(\d+) failed", pytest_line) or 0
        s.errors = _num(r"(\d+) errors?", pytest_line) or 0
        s.skipped = _num(r"(\d+) skipped", pytest_line) or 0
        s.failing_tests = re.findall(r"^(?:FAILED|ERROR) (\S+)", text, re.MULTILINE)
        if "no tests ran" in pytest_line or exit_code == 5:
            s.outcome = "no_tests"
            s.total = 0
        elif exit_code not in (0, 1) and not s.failed and not s.passed:
            s.outcome = "error"
        return _finish(s, exit_code)

    # unittest -------------------------------------------------------------------------
    ran = re.search(r"^Ran (\d+) tests? in [\d.]+s", text, re.MULTILINE)
    if ran:
        s = TestSummary(framework="unittest", total=int(ran.group(1)))
        status = re.search(r"^(OK|FAILED)(?: \((.*)\))?\s*$", text, re.MULTILINE)
        details = (status.group(2) or "") if status else ""
        s.failed = _num(r"failures=(\d+)", details) or 0
        s.errors = _num(r"errors=(\d+)", details) or 0
        s.skipped = _num(r"skipped=(\d+)", details) or 0
        s.passed = max(s.total - s.failed - s.errors - s.skipped, 0)
        # Python >= 3.11 prints the full id in parentheses ("module.Class.method"); older versions omit the method.
        s.failing_tests = [m.group(2) if m.group(2).endswith("." + m.group(1)) else f"{m.group(2)}.{m.group(1)}"
                           for m in re.finditer(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", text, re.MULTILINE)]
        if s.total == 0:
            s.outcome = "no_tests"
        return _finish(s, exit_code)

    # node:test (TAP-ish summary) --------------------------------------------------------
    if re.search(r"^# (?:pass|tests) \d+", text, re.MULTILINE) or re.search(r"^ℹ (?:pass|tests) \d+", text, re.MULTILINE):
        s = TestSummary(framework="node:test")
        s.total = _num(r"(?m)^[#ℹ] tests (\d+)", text)
        s.passed = _num(r"(?m)^[#ℹ] pass (\d+)", text)
        s.failed = _num(r"(?m)^[#ℹ] fail (\d+)", text)
        s.skipped = _num(r"(?m)^[#ℹ] skipped (\d+)", text)
        s.failing_tests = [m.strip() for m in re.findall(r"(?m)^\s*not ok \d+ - (.+)$", text)]
        if not s.failing_tests:
            # spec reporter: "✖ name (1.2ms)" — skip the "✖ failing tests:" header
            s.failing_tests = list(dict.fromkeys(
                re.sub(r"\s*\([\d.]+m?s\)\s*$", "", m).strip()
                for m in re.findall(r"(?m)^\s*✖ (.+)$", text) if not m.startswith("failing tests")))
        if s.total == 0:
            s.outcome = "no_tests"
        return _finish(s, exit_code)

    # jest -------------------------------------------------------------------------------
    jest = re.search(r"^Tests:\s+(.*)$", text, re.MULTILINE)
    if jest:
        line = jest.group(1)
        s = TestSummary(framework="jest", passed=_num(r"(\d+) passed", line) or 0,
                        failed=_num(r"(\d+) failed", line) or 0, skipped=_num(r"(\d+) skipped", line) or 0,
                        total=_num(r"(\d+) total", line))
        s.failing_tests = [m.strip() for m in re.findall(r"(?m)^\s*● (.+)$", text)][:50]
        return _finish(s, exit_code)

    # vitest -----------------------------------------------------------------------------
    vitest = re.search(r"^\s*Tests\s+(.*\(\d+\))\s*$", text, re.MULTILINE)
    if vitest:
        line = vitest.group(1)
        s = TestSummary(framework="vitest", passed=_num(r"(\d+) passed", line) or 0,
                        failed=_num(r"(\d+) failed", line) or 0, skipped=_num(r"(\d+) skipped", line) or 0,
                        total=_num(r"\((\d+)\)", line))
        s.failing_tests = [m.strip() for m in re.findall(r"(?m)^\s*(?:FAIL|×)\s+(.+)$", text)][:50]
        return _finish(s, exit_code)

    # go test ----------------------------------------------------------------------------
    if re.search(r"^(ok|FAIL|---)\s", text, re.MULTILINE) and re.search(r"(?m)^(ok|FAIL)\s+\S+", text):
        s = TestSummary(framework="go test")
        s.failed = len(re.findall(r"(?m)^--- FAIL: ", text))
        s.passed = len(re.findall(r"(?m)^--- PASS: ", text)) or None
        s.failing_tests = re.findall(r"(?m)^--- FAIL: (\S+)", text)
        return _finish(s, exit_code)

    # cargo test -------------------------------------------------------------------------
    cargo = list(re.finditer(r"test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored", text))
    if cargo:
        s = TestSummary(framework="cargo test", passed=sum(int(m.group(2)) for m in cargo),
                        failed=sum(int(m.group(3)) for m in cargo), skipped=sum(int(m.group(4)) for m in cargo))
        s.failing_tests = re.findall(r"(?m)^test (\S+) \.\.\. FAILED", text)
        return _finish(s, exit_code)

    return _finish(TestSummary(), exit_code)
