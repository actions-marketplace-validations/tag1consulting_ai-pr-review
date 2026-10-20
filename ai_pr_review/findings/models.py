"""Pydantic Finding model — typed representation of a single review finding.

Closes #190 (schema validation) and #126 (to_finding() framework).
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

Severity = Literal["Critical", "High", "Medium", "Low"]
JudgeVerdict = Literal["keep", "downrank"]

# Shared taxonomy ported from claude-comprehensive-review#76. Finding.category
# is typed `Category` below, but what actually prevents an unrecognized value
# from being rejected is the `mode="before"` validator (_normalise_category):
# it runs before pydantic's type check and coerces any unknown/missing input
# to "other" ahead of time, so the Literal type never sees an invalid value to
# reject. CATEGORIES is derived from Category via get_args() so there is
# exactly one place that lists the values; the tuple form is what
# _normalise_category checks membership against.
Category = Literal[
    "authz",
    "injection",
    "dependency-cve",
    "secret",
    "crypto",
    "architecture-coupling",
    "test-gap",
    "edge-case",
    "observability",
    "docs",
    "lint",
    "other",
]
CATEGORIES: tuple[str, ...] = get_args(Category)

# Longest `impact` or `trigger` text that a Finding keeps (#1078).
MAX_DETAIL_CHARS = 300


MAX_ALSO_REPORTED = 5
MAX_ALSO_REPORTED_STORED = 100
_ALSO_REPORTED_OVERFLOW_PREFIX = "(+"
ALSO_REPORTED_TRUNCATED_NOTE = "(+more notes past the storage bound)"


def split_also_reported(notes: list[str]) -> tuple[list[str], bool]:
    """Split a stored `also_reported` list into real notes and a truncation flag.

    Marker entries start with `(+`. The flag is true when the list carries the
    storage-bound marker, so a count taken from the real notes is a floor.
    """
    real = [n for n in notes if not n.startswith(_ALSO_REPORTED_OVERFLOW_PREFIX)]
    return real, ALSO_REPORTED_TRUNCATED_NOTE in notes


def combine_also_reported(groups: list[list[str]]) -> list[str]:
    """Union several `Finding.also_reported` lists, keep order, drop repeats.

    Used by `merge._collapse_cluster` and by `scope.rollup_repeated_findings`,
    so a collapsed group keeps the notes of every member (#1044). The display
    cap (`MAX_ALSO_REPORTED`) is applied at render time. This function only
    applies the storage bound `MAX_ALSO_REPORTED_STORED`. It keeps the first
    notes and appends `ALSO_REPORTED_TRUNCATED_NOTE`, so the count is a floor.
    Other marker entries of an input list are dropped.
    """
    notes: list[str] = []
    seen: set[str] = set()
    truncated = False
    for group in groups:
        real, group_truncated = split_also_reported(group)
        truncated = truncated or group_truncated
        for note in real:
            if note in seen:
                continue
            seen.add(note)
            notes.append(note)
    if len(notes) > MAX_ALSO_REPORTED_STORED:
        notes = notes[:MAX_ALSO_REPORTED_STORED]
        truncated = True
    if truncated:
        notes.append(ALSO_REPORTED_TRUNCATED_NOTE)
    return notes


# The pipeline sets these fields. Agent JSON must never set them: each one
# changes how a finding is scored, grouped, suppressed, or shown. The extract
# step drops every name in this set from agent output. A test requires each
# `Finding` field to be in exactly one of the two sets, so a new field forces a
# decision here and an agent cannot set it by default (#1085).
INTERNAL_ONLY_FIELDS: frozenset[str] = frozenset({
    "out_of_diff",
    "demoted_to_body",
    "corroborated",
    "rolled_up_by_rule",
    "show_badge",
    "judge_verdict",
    "also_reported",
    "style_only",
})

# The fields that agent JSON may carry. `source` and `sources` are stamped by
# the extract step after the agent output is read.
AGENT_INPUT_FIELDS: frozenset[str] = frozenset({
    "severity",
    "confidence",
    "finding",
    "source",
    "sources",
    "category",
    "file",
    "line",
    "start_line",
    "remediation",
    "impact",
    "trigger",
    "suggested_code",
    "agent",
})

# A finding from one of these sources never joins a cluster, and a thread that
# carries one never matches a finding of another kind. The suppression-check
# finding names a comment that turns off a security rule on a line. If it
# clustered with the agent finding for that line, the agent finding (higher
# severity) would win the headline and the suppression would vanish from the
# review (#1077). The merge and the thread matching both use this one set.
STANDALONE_SOURCES: frozenset[str] = frozenset({"suppression-check"})


class Finding(BaseModel):
    """A single finding from an agent or static analyzer."""

    severity: Severity
    confidence: int = Field(ge=0, le=100)
    finding: str = Field(min_length=1)
    source: str = ""
    category: Category = "other"
    file: str = ""
    line: int | None = Field(default=None, ge=1)
    start_line: int | None = Field(default=None, ge=1)
    remediation: str = ""
    # Optional text from the agent (#1078). `impact` says who is harmed and
    # how, in one sentence. `trigger` gives a concrete input or call that
    # reaches the defect. They are display text only: neither one enters the
    # fingerprint, the clustering, the judge payload or any approval decision.
    impact: str = ""
    trigger: str = ""
    suggested_code: str = ""
    sources: list[str] = Field(default_factory=list)
    # cve-check only: "dependency-check" tag required in the output.
    agent: str = ""
    # Set by apply_diff_scope when a native-analyzer finding falls outside the
    # changed-line set.  Findings with out_of_diff=True are capped to Low
    # severity (by apply_diff_scope, in the same model_copy) and rendered in a
    # collapsed body section rather than the main findings list. This is a
    # true invariant enforced at the single call site that sets the flag to
    # True — do not set out_of_diff=True without also capping severity to
    # Low, or renderers that filter on out_of_diff (e.g. vcs/_body.py's
    # compute_headline) will silently misrepresent the review's real risk.
    # (extract.py also writes to this field, but only to reset an
    # agent-injected out_of_diff=True back to False as a prompt-injection
    # defense — that is not a second site that sets the invariant-bearing
    # True value, so the "single call site" claim above still holds.)
    out_of_diff: bool = False
    # Set by judge._apply_verdicts on a "downrank" verdict. Distinct from
    # out_of_diff: severity is intentionally left unchanged (a downranked
    # High is still a High) — only its *placement* changes, from an inline PR
    # comment to the review body's findings list. See findings/judge.py and
    # prompts/finding-judge.md for the judge's contract ("no information is
    # lost; the only question is inline vs. summary").
    demoted_to_body: bool = False
    # Set by merge._collapse_cluster when the proximity cluster contains BOTH
    # at least one native static-analyzer source AND at least one LLM-agent
    # source — independent corroboration of the same file+line region.
    # Internal-only: not serialised by to_dict().
    corroborated: bool = False
    # Set by scope.rollup_repeated_findings on the representative of a group
    # that it rolled up by rule id (semgrep). Rule-id groups join hits with
    # different messages, so the representative stands for lines far from its
    # own, and a fuzzy dismissal match on its line must not read as a match for
    # the whole group (#1073). A group of the same message is not marked: a
    # dismissal of one hit covers the repeats of it.
    # Internal-only: not serialised by to_dict().
    rolled_up_by_rule: bool = False
    # Display preference only: when True, the renderers show the evidence label and
    # score beside the finding. The label is computed from this finding's own fields
    # when it is rendered (findings/badge.py), so it cannot go stale. The orchestrator
    # sets this when AI_FINDING_BADGES is on. It never enters an approval decision.
    show_badge: bool = False
    # Set by judge._apply_verdicts to the judge's raw per-finding verdict
    # ("keep" or "downrank"), or left at the default `None` when this finding
    # never went through the judge pass at all (AI_JUDGE_PASS=false, no
    # judge_model/judge_prompt_path configured, an empty candidate list, or a
    # fail-soft judge error/parse failure — see judge_findings' docstring).
    # Deliberately records the judge's raw verdict even for a `corroborated`
    # finding, whose `demoted_to_body`/`confidence` are never actually
    # touched by a "downrank" verdict (corroboration always overrides the
    # placement decision) — collapsing the two into one "effective" value
    # would destroy the ability to later ask "how often does corroboration
    # save a finding the judge wanted to downrank?" against exactly the
    # `corroborated` field this Finding already carries. Internal-only: not
    # serialised by to_dict().
    judge_verdict: JudgeVerdict | None = None
    # Set by merge._collapse_cluster when two different analyzer tools report
    # nearby lines (#1044). Each entry is one lost message, already composed as
    # "<tool> (line N): <message>". The surviving finding keeps its own
    # `finding` text unchanged because vcs/_finding_ids.fingerprint() hashes
    # it. Kept out of `remediation` so suppression rules (which match on
    # finding + remediation) cannot hide the surviving finding through the
    # other tool's text. Internal-only: not serialised by to_dict().
    also_reported: list[str] = Field(default_factory=list)
    # Set by an analyzer when the rule it reports is about style only (a phpcs
    # style sniff, a ruff formatting or naming code, an eslint formatting rule).
    # ruff, eslint and phpcs use the category "lint" for style and for real
    # defects, so the category cannot tell them apart. The merge keeps a
    # style-only finding out of a cluster with a finding that is not style-only
    # (#1074). Internal-only: not serialised by to_dict(), and reset on agent
    # output by extract.py.
    style_only: bool = False

    @field_validator("severity", mode="before")
    @classmethod
    def _normalise_severity(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.capitalize()
            if v not in ("Critical", "High", "Medium", "Low"):
                raise ValueError(f"Invalid severity {v!r}")
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _normalise_category(cls, v: object) -> object:
        # Permissive by design, unlike _normalise_severity: an LLM emitting an
        # unrecognized or missing category must never cause the finding to be
        # dropped. Falls back to "other" instead of raising.
        if isinstance(v, str):
            v = v.strip().lower()
            if v not in CATEGORIES:
                return "other"
            return v
        return "other"

    @field_validator("impact", "trigger", mode="before")
    @classmethod
    def _normalise_detail(cls, v: object) -> object:
        # Permissive like _normalise_category: a missing or odd value must never
        # drop the finding. A non-string becomes empty, and a long string is cut
        # so one verbose agent cannot fill a comment.
        if not isinstance(v, str):
            return ""
        text = " ".join(v.split())
        if len(text) > MAX_DETAIL_CHARS:
            text = text[: MAX_DETAIL_CHARS - 1] + "…"
        return text

    @model_validator(mode="after")
    def _populate_sources(self) -> Finding:
        if self.source and not self.sources:
            self.sources = [self.source]
        return self

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict of the finding fields."""
        d: dict[str, object] = {
            "severity": self.severity,
            "confidence": self.confidence,
            "finding": self.finding,
            "source": self.source,
            "category": self.category,
        }
        if self.file:
            d["file"] = self.file
        if self.line is not None:
            d["line"] = self.line
        if self.start_line is not None:
            d["start_line"] = self.start_line
        if self.impact:
            d["impact"] = self.impact
        if self.trigger:
            d["trigger"] = self.trigger
        if self.remediation:
            d["remediation"] = self.remediation
        if self.suggested_code:
            d["suggested_code"] = self.suggested_code
        if self.sources:
            d["sources"] = self.sources
        return d


def is_standalone(f: Finding) -> bool:
    """Does `f` come from a source that never joins a cluster?"""
    return any(s in STANDALONE_SOURCES for s in (f.sources or ([f.source] if f.source else [])))
