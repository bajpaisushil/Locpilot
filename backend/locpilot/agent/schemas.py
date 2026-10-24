"""Structured outputs.

``LLM*`` models are the (deliberately small) JSON schemas the local model must
fill via Ollama's schema-constrained decoding. Everything the UI renders is
built by LocPilot from those outputs *after* validation against the real
repository (see :mod:`locpilot.agent.citations`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
