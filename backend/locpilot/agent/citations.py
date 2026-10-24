"""Citation validation and report assembly.

The model cites evidence ids (``E3`` / ``E3:14-18``) or ``path:line``
references. Every citation is resolved against evidence that was actually
shown to the model and against the repository on disk. Anything that cannot be
verified is dropped from structured fields, marked in prose, and listed in
``removed_citations`` so the UI can show it.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from locpilot.agent.schemas import (
    Citation,
    EvidenceSummary,
    ExaminedFile,
    FlowStep,
    Hypothesis,
    InvestigationReport,
    LLMAnalysis,
    LLMExplanation,
)
from locpilot.tools.files import RepoIndex

_EV_RE = re.compile(r"^\[?(E\d+)(?:\s*[:@]\s*(?:L|lines?\s*)?(\d+)(?:\s*[-–]\s*(\d+))?)?\]?$", re.IGNORECASE)
_EV_ONE = r"E\d+(?:\s*[:@]\s*(?:L|lines?\s*)?\d+(?:\s*[-–]\s*\d+)?)?"
_EV_INLINE = re.compile(rf"[\[(]\s*({_EV_ONE}(?:\s*[,;]\s*{_EV_ONE})*)\s*[\])]", re.IGNORECASE)
_PATH_REF = re.compile(r"^`?((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8})`?(?:\s*[:#]\s*L?(\d+)(?:\s*[-–]\s*L?(\d+))?)?$")
_INLINE_PATH_LINE = re.compile(r"(?<![\w/.:-])`?((?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7}):(\d+)(?:[-–](\d+))?`?")
_BACKTICK_PATH = re.compile(r"`((?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,7})`")


def open_link(path: str, start: int, end: int) -> str:
    return f"locpilot://open?path={quote(path, safe='')}&start={start}&end={end}"


class CitationResolver:
    def __init__(self, index: RepoIndex, evidence: list[dict]) -> None:
        self.index = index
        self.by_id = {e["id"].upper(): e for e in evidence}
        self.removed: list[str] = []

    def _file_len(self, path: str) -> int:
        lines = self.index.read_lines(path)
        return len(lines) if lines else 0

    def resolve(self, ref: str) -> Citation | None:
        raw = (ref or "").strip()
        if not raw:
            return None
        m = _EV_RE.match(raw)
        if m:
            item = self.by_id.get(m.group(1).upper())
            if item is None:
                self.removed.append(f"{raw} (no such evidence item)")
                return None
            start = int(m.group(2)) if m.group(2) else item["start_line"]
            end = int(m.group(3)) if m.group(3) else (start if m.group(2) else item["end_line"])
            if end < start:
                start, end = end, start
            if start < item["start_line"] or end > item["end_line"]:
                self.removed.append(f"{raw} (lines outside the evidence shown; cited the whole item instead)")
                start, end = item["start_line"], item["end_line"]
            return Citation(path=item["path"], start_line=start, end_line=end, evidence_id=item["id"])
        m = _PATH_REF.match(raw)
        if m:
            path = m.group(1).lstrip("./")
            if not self.index.has_file(path):
                self.removed.append(f"{raw} (file does not exist in the repository)")
                return None
            total = self._file_len(path)
            if m.group(2):
                start = int(m.group(2))
                end = int(m.group(3)) if m.group(3) else start
                if start < 1 or end > total or end < start:
                    self.removed.append(f"{raw} (line numbers do not exist in {path})")
                    return None
            else:
                start, end = 1, min(total, 1)
            return Citation(path=path, start_line=start, end_line=end, evidence_id=None)
        self.removed.append(f"{raw} (unrecognized citation)")
        return None

    def resolve_many(self, refs: list[str]) -> list[Citation]:
        out: list[Citation] = []
        for ref in refs:
            for part in re.split(r"[,;]\s*", ref or ""):
                cit = self.resolve(part)
                if cit and cit not in out:
                    out.append(cit)
        return out

    def humanize(self, text: str) -> str:
        """Replace evidence ids in plain-text fields with readable file:line references."""
        if not text:
            return text

        def sub(m: re.Match) -> str:
            parts = []
            for part in re.split(r"\s*[,;]\s*", m.group(1)):
                cit = self.resolve(part)
                if cit is not None:
                    parts.append(f"{cit.path}:{cit.start_line}" +
                                 (f"-{cit.end_line}" if cit.end_line != cit.start_line else ""))
            return f"({', '.join(parts)})" if parts else ""

        out = _EV_INLINE.sub(sub, text)
        bare = re.compile(rf"(?<![\w/]){_EV_ONE}(?![\w])", re.IGNORECASE)
        out = bare.sub(lambda m: sub(re.match(r"(.*)", m.group(0))), out)
        return re.sub(r"\s{2,}", " ", out).strip()

    def linkify(self, markdown: str) -> str:
        """Turn valid citations in prose into viewer links; mark invalid ones."""

        def ev_sub(m: re.Match) -> str:
            links = []
            for part in re.split(r"\s*[,;]\s*", m.group(1)):
                cit = self.resolve(part)
                if cit is None:
                    links.append(f"~~{part}~~ *(unverified citation removed)*")
                    continue
                label = f"{cit.path}:{cit.start_line}" + (f"-{cit.end_line}" if cit.end_line != cit.start_line else "")
                links.append(f"[{label}]({open_link(cit.path, cit.start_line, cit.end_line)})")
            return "(" + ", ".join(links) + ")"

        out = _EV_INLINE.sub(ev_sub, markdown)

        def path_line_sub(m: re.Match) -> str:
            path = m.group(1).lstrip("./")
            start = int(m.group(2))
            end = int(m.group(3)) if m.group(3) else start
            if self.index.has_file(path) and 1 <= start <= end <= self._file_len(path):
                return f"[{path}:{start}{'-' + str(end) if end != start else ''}]({open_link(path, start, end)})"
            self.removed.append(f"{m.group(0)} (not found in the repository)")
            return f"`{m.group(1)}:{m.group(2)}` *(unverified reference)*"

        # Avoid rewriting inside links we just created.
        segments = re.split(r"(\[[^\]]*\]\(locpilot://[^)]*\))", out)
        segments = [s if s.startswith("[") and "locpilot://" in s else _INLINE_PATH_LINE.sub(path_line_sub, s)
                    for s in segments]
        out = "".join(segments)

        def bare_path_sub(m: re.Match) -> str:
            path = m.group(1).lstrip("./")
            if self.index.has_file(path):
                return f"[`{path}`]({open_link(path, 1, 1)})"
            self.removed.append(f"{path} (file does not exist in the repository)")
            return f"`{m.group(1)}` *(unverified path)*"

        segments = re.split(r"(\[[^\]]*\]\(locpilot://[^)]*\))", out)
        return "".join(s if s.startswith("[") and "locpilot://" in s else _BACKTICK_PATH.sub(bare_path_sub, s)
                       for s in segments)


_CODE_TOKEN = re.compile(r"`([^`]{1,80})`|\b([A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+|[a-z][A-Z][A-Za-z0-9]*))\b")


def _identifiers(text: str) -> list[str]:
    out = []
    for m in _CODE_TOKEN.finditer(text or ""):
        token = (m.group(1) or m.group(2) or "").strip()
        token = re.split(r"[\s(=:\[]", token)[0].strip(".")
        if len(token) >= 3 and token not in out:
            out.append(token)
    return out[:8]


def refine_citations(text: str, citations: list[Citation], index: RepoIndex, evidence: list[dict],
                     removed: list[str]) -> list[Citation]:
    """Make sure claim citations point at lines that mention the identifiers the claim talks about.

    If none of the cited lines contain any identifier named in ``text`` (e.g. `best`), look for the identifier in
    the evidence and cite that line instead. Deterministic; never invents locations.
    """
    idents = _identifiers(text)
    if not idents:
        return citations

    def mentions(c: Citation) -> bool:
        lines = index.read_lines(c.path) or []
        chunk = "\n".join(lines[c.start_line - 1:c.end_line])
        return any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(i)}(?![A-Za-z0-9_])", chunk) for i in idents)

    if any(mentions(c) for c in citations):
        return [c for c in citations if mentions(c)] or citations
    for ident in idents:
        pattern = re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(ident)}\s*(?::[^=]*)?=(?!=)")
        for item in evidence:
            lines = index.read_lines(item["path"]) or []
            for no in range(item["start_line"], min(item["end_line"], len(lines)) + 1):
                if pattern.search(lines[no - 1]):
                    for c in citations:
                        removed.append(f"{c.path}:{c.start_line}-{c.end_line} (does not mention `{ident}`; "
                                       f"re-cited {item['path']}:{no})")
                    return [Citation(path=item["path"], start_line=no, end_line=no, evidence_id=item["id"])]
    return citations


def _hypothesis_status(confidence: str, supporting: list[Citation], contradicting: list[Citation]) -> str:
    if not supporting:
        return "unsupported"
    if contradicting and len(contradicting) >= len(supporting):
        return "contradicted"
    if confidence == "low":
        return "weak"
    return "supported"


def files_examined(evidence: list[dict], tool_log: list[dict]) -> list[ExaminedFile]:
    by_path: dict[str, ExaminedFile] = {}
    for item in evidence:
        entry = by_path.setdefault(item["path"], ExaminedFile(path=item["path"], reason=item.get("reason", "")))
        entry.ranges.append(f"{item['start_line']}-{item['end_line']}")
        if item.get("symbol") and item["symbol"] not in entry.symbols:
            entry.symbols.append(item["symbol"])
    return list(by_path.values())


def evidence_summaries(evidence: list[dict]) -> list[EvidenceSummary]:
    return [EvidenceSummary(id=e["id"], path=e["path"], start_line=e["start_line"], end_line=e["end_line"],
                            kind=e["kind"], title=e["title"]) for e in evidence]


def build_rca_report(raw: LLMAnalysis, *, intent: str, index: RepoIndex, evidence: list[dict],
                     tool_log: list[dict]) -> InvestigationReport:
    resolver = CitationResolver(index, evidence)
    flow = [FlowStep(description=s.step, citations=resolver.resolve_many(s.evidence)) for s in raw.execution_flow]
    hypotheses = []
    for h in raw.hypotheses:
        sup = resolver.resolve_many(h.supporting_evidence)
        con = resolver.resolve_many(h.contradicting_evidence)
        status = _hypothesis_status(h.confidence, sup, con)
        confidence = "low" if status == "unsupported" else h.confidence
        hypotheses.append(Hypothesis(title=h.title, explanation=h.explanation, confidence=confidence,
                                     supporting=sup, contradicting=con, status=status))
    root_text = raw.most_likely_root_cause.strip()
    if re.fullmatch(r"[\[(]?\s*E\d+(?:\s*:\s*\d+(?:\s*-\s*\d+)?)?\s*[\])]?", root_text or "-"):
        # The model put a citation where a description belongs: keep it as evidence, describe the top hypothesis.
        raw.root_cause_evidence.append(root_text)
        ranked = sorted(raw.hypotheses, key=lambda h: {"high": 0, "medium": 1, "low": 2}[h.confidence])
        raw.most_likely_root_cause = f"{ranked[0].title}: {ranked[0].explanation}" if ranked else ""
    root_citations = refine_citations(raw.most_likely_root_cause + " " + raw.answer,
                                      resolver.resolve_many(raw.root_cause_evidence), index, evidence,
                                      resolver.removed)
    for sus in raw.suspicious_lines:
        cits = resolver.resolve_many([sus.evidence])
        if cits:
            flow.append(FlowStep(description=f"Suspicious line `{sus.code.strip()[:160]}` — {sus.concern}",
                                 citations=cits))
    uncertainties = list(raw.uncertainties)
    sufficient = raw.evidence_sufficient
    root = raw.most_likely_root_cause.strip() or None
    if root and not root_citations:
        # Fall back to the best supported hypothesis' citations, else flag it.
        best = next((h for h in hypotheses if h.status == "supported"), None)
        if best is not None:
            root_citations = best.supporting
        else:
            sufficient = False
            uncertainties.insert(0, "The proposed root cause could not be tied to verified evidence.")
    if root is None:
        sufficient = False
    answer = resolver.linkify(raw.answer.strip() or raw.problem_summary)
    h = resolver.humanize
    for step in flow:
        step.description = h(step.description)
    for hyp in hypotheses:
        hyp.title, hyp.explanation = h(hyp.title), h(hyp.explanation)
    root = h(root) if root else root
    return InvestigationReport(
        kind="rca", intent=intent, answer_markdown=answer, problem_summary=h(raw.problem_summary),
        observed_symptoms=[h(x) for x in raw.observed_symptoms], execution_flow=flow,
        files_examined=files_examined(evidence, tool_log), hypotheses=hypotheses,
        most_likely_root_cause=root, root_cause_citations=root_citations, evidence_sufficient=sufficient,
        minimal_fix=h(raw.minimal_fix), side_effects=[h(x) for x in raw.side_effects],
        verification_steps=[h(x) for x in raw.verification_steps], uncertainties=[h(x) for x in uncertainties], removed_citations=list(dict.fromkeys(resolver.removed)),
        evidence=evidence_summaries(evidence),
    )


def build_explanation_report(raw: LLMExplanation, *, intent: str, index: RepoIndex, evidence: list[dict],
                             tool_log: list[dict]) -> InvestigationReport:
    resolver = CitationResolver(index, evidence)
    flow = [FlowStep(description=resolver.humanize(s.step), citations=resolver.resolve_many(s.evidence))
            for s in raw.execution_flow]
    facts = [resolver.linkify(f) for f in raw.key_facts]
    answer = resolver.linkify(raw.answer.strip() or raw.summary)
    if facts:
        answer += "\n\n**Key facts (from the code)**\n" + "\n".join(f"- {f}" for f in facts)
    return InvestigationReport(
        kind="explanation", intent=intent, answer_markdown=answer, problem_summary=resolver.humanize(raw.summary),
        execution_flow=flow, files_examined=files_examined(evidence, tool_log),
        uncertainties=[resolver.humanize(u) for u in raw.uncertainties],
        evidence_sufficient=bool(evidence), removed_citations=list(dict.fromkeys(resolver.removed)),
        evidence=evidence_summaries(evidence),
    )
