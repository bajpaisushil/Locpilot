"""Structured outputs.

``LLM*`` models are the (deliberately small) JSON schemas the local model must
fill via Ollama's schema-constrained decoding. Everything the UI renders is
built by LocPilot from those outputs *after* validation against the real
repository (see :mod:`locpilot.agent.citations`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["explain", "root_cause", "fix", "add_tests", "review_changes", "explain_changes", "retry_fix",
                 "question"]


# --- Model-facing schemas ------------------------------------------------------------

class TaskUnderstanding(BaseModel):
    intent: Intent
    summary: str = Field(description="One sentence restating what the user wants")
    search_terms: list[str] = Field(max_length=10, description="Words or identifiers likely to appear in the relevant code")
    symbols: list[str] = Field(max_length=8, description="Function, class or method names mentioned or strongly implied")
    file_hints: list[str] = Field(max_length=6, description="File paths mentioned")
    error_messages: list[str] = Field(max_length=4, description="Exact error strings mentioned")
    relates_to_previous: bool
    needs_clarification: bool
    clarifying_question: str


class ToolRequest(BaseModel):
    tool: Literal["search_text", "search_regex", "find_definition", "find_callers", "read_file", "list_directory",
                  "find_tests", "git_log"]
    query: str = Field("", description="Search text, regex, or symbol name")
    path: str = Field("", description="Repository-relative file or directory path")
    start_line: int = 0
    end_line: int = 0
    reason: str = ""


class EvidencePlan(BaseModel):
    sufficient: bool
    missing: str = Field("", description="What information is still missing, if any")
    requests: list[ToolRequest] = Field(default_factory=list, max_length=4)


class LLMFlowStep(BaseModel):
    step: str
    evidence: list[str] = Field(max_length=4, description='Citations like "E2:14-18"')


class LLMSuspiciousLine(BaseModel):
    evidence: str = Field(description='Evidence id and line, e.g. "E2:14"')
    code: str = Field(description="The exact code on that line")
    concern: str = Field(description="How this line could cause the reported behavior")


class LLMHypothesis(BaseModel):
    title: str
    explanation: str
    confidence: Literal["high", "medium", "low"]
    supporting_evidence: list[str] = Field(max_length=5)
    contradicting_evidence: list[str] = Field(max_length=3)


class LLMAnalysis(BaseModel):
    problem_summary: str
    observed_symptoms: list[str] = Field(max_length=5)
    execution_flow: list[LLMFlowStep] = Field(min_length=1, max_length=8)
    suspicious_lines: list[LLMSuspiciousLine] = Field(min_length=1, max_length=5)
    hypotheses: list[LLMHypothesis] = Field(min_length=1, max_length=3)
    most_likely_root_cause: str
    root_cause_evidence: list[str] = Field(max_length=4)
    evidence_sufficient: bool
    minimal_fix: str
    side_effects: list[str] = Field(max_length=4)
    verification_steps: list[str] = Field(max_length=4)
    uncertainties: list[str] = Field(max_length=4)
    answer: str = Field(description="Concise markdown answer for the user, citing evidence like [E2:14-18]")


class LLMExplanation(BaseModel):
    summary: str
    execution_flow: list[LLMFlowStep] = Field(max_length=10)
    key_facts: list[str] = Field(max_length=8, description="Facts directly visible in the evidence, each with a citation")
    uncertainties: list[str] = Field(max_length=4)
    answer: str = Field(description="Markdown explanation for the user, citing evidence like [E2:14-18]")


class LLMPlannedFile(BaseModel):
    path: str
    change: str


class LLMPlan(BaseModel):
    summary: str
    approach: str
    files_to_modify: list[LLMPlannedFile] = Field(default_factory=list, max_length=5)
    test_file: str = Field("", description="Path of the test file to create or extend")
    test_description: str = ""
    risks: list[str] = Field(default_factory=list, max_length=4)


class LLMEdit(BaseModel):
    path: str = Field(description="the file to edit, exactly as shown")
    search: str = Field(default="", description="exact existing lines to replace, copied from the file with their "
                                                 "indentation; empty to create the file or append to it")
    replace: str = Field(description="the lines that replace them (for a new file: its whole content)")


class LLMEditList(BaseModel):
    """The same edits as SEARCH/REPLACE blocks, as JSON: asked for when a reply contained no blocks at all."""

    edits: list[LLMEdit] = Field(min_length=1, max_length=12)


class LLMRequirementCheck(BaseModel):
    requirement: str
    why: str = Field(description="the concrete input tried, what the changed code returns for it, what the request "
                                 "demands")
    holds: bool


class LLMPatchCheck(BaseModel):
    """A second look at a proposed change: does it do what the request asks?"""

    # At least one, each worked through before the verdict: an empty list is how a model skips the check.
    requirements: list[LLMRequirementCheck] = Field(min_length=1, max_length=8)
    verdict: Literal["ok", "code_wrong", "test_wrong", "both_wrong"]
    correction: str = ""


class LLMTestSpec(BaseModel):
    scenario: str = Field(description="The situation from the bug report the test recreates")
    inputs: str = Field(description="Concrete input values the test will use")
    expected_correct_behavior: str = Field(description="What the CORRECT code must return/do for those inputs")
    buggy_behavior: str = Field(description="What the current buggy code returns/does for those inputs")
    assertion: str = Field(description="The assertion that checks the correct behavior")


class LLMFailureAnalysis(BaseModel):
    diagnosis: str
    cause: Literal["fix_incorrect", "test_incorrect", "environment", "preexisting_failure", "unknown"]
    should_retry: bool
    next_step: str = ""


def _trace_first(schema: dict) -> None:
    """The model must write the trace (and before the verdict); scripted answers without one still validate."""
    schema["required"] = ["trace", *[name for name in schema.get("required", []) if name != "trace"]]


class LLMTracedFailure(BaseModel):
    """A failure diagnosis that works through the failing assertion with real values before it judges."""

    model_config = ConfigDict(json_schema_extra=_trace_first)

    trace: str = Field(default="", description="Step by step, with the actual values: how the code as it is now "
                                               "turns the failing test's input into the value it received")
    diagnosis: str
    cause: Literal["fix_incorrect", "test_incorrect", "environment", "preexisting_failure", "unknown"]
    should_retry: bool
    next_step: str = ""


class LLMWalkThrough(BaseModel):
    """The model's own check of a finished change: the request's example computed through the changed code."""

    example: str = Field(description="The input taken from the request (or the simplest one that shows the problem)")
    trace: str = Field(description="The changed code followed line by line for that input, with the actual values")
    result: str = Field(description="What the changed code returns for the example")
    expected: str = Field(description="What the request asks for")
    correct: bool = Field(description="true only if result and expected agree")
    problem: str = Field(default="", description="If not correct: the line where the values go wrong, and why")


class LLMFileNote(BaseModel):
    path: str
    note: str


class LLMChangeReview(BaseModel):
    answer: str = Field(description="Markdown explanation/review of the changes for the user")
    file_notes: list[LLMFileNote] = Field(default_factory=list, max_length=8)
    side_effects: list[str] = Field(default_factory=list, max_length=6)
    risks: list[str] = Field(default_factory=list, max_length=6)


# --- UI-facing report models (see docs/api.md) ----------------------------------------

class Citation(BaseModel):
    path: str
    start_line: int
    end_line: int
    evidence_id: str | None = None


class FlowStep(BaseModel):
    description: str
    citations: list[Citation] = Field(default_factory=list)


class Hypothesis(BaseModel):
    title: str
    explanation: str
    confidence: Literal["high", "medium", "low"]
    supporting: list[Citation] = Field(default_factory=list)
    contradicting: list[Citation] = Field(default_factory=list)
    status: Literal["supported", "weak", "unsupported", "contradicted"]


class ExaminedFile(BaseModel):
    path: str
    ranges: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    reason: str = ""


class EvidenceSummary(BaseModel):
    id: str
    path: str
    start_line: int
    end_line: int
    kind: str
    title: str


class InvestigationReport(BaseModel):
    kind: Literal["explanation", "rca"]
    intent: str
    answer_markdown: str
    problem_summary: str = ""
    observed_symptoms: list[str] = Field(default_factory=list)
    execution_flow: list[FlowStep] = Field(default_factory=list)
    files_examined: list[ExaminedFile] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    most_likely_root_cause: str | None = None
    root_cause_citations: list[Citation] = Field(default_factory=list)
    evidence_sufficient: bool = True
    minimal_fix: str = ""
    side_effects: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    analysis_basis: Literal["static"] = "static"
    removed_citations: list[str] = Field(default_factory=list)
    evidence: list[EvidenceSummary] = Field(default_factory=list)
