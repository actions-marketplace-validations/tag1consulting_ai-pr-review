"""Tests for the evidence label shown beside a finding (findings/badge.py)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import anyio

from ai_pr_review.findings.badge import finding_badge, finding_label
from ai_pr_review.findings.models import Finding
from ai_pr_review.orchestrate import OrchestrationConfig, run_review
from ai_pr_review.review.outcome import classify_review_outcome
from ai_pr_review.vcs._body import badge_suffix, format_body_finding
from ai_pr_review.vcs._code_insights import build_annotation_payload
from ai_pr_review.vcs.github import _build_inline_comment_body
from ai_pr_review.vcs.protocol import DiffContext
from tests.python.test_orchestrate import _FakeProvider, _llm_call_factory, _make_dispatch_context


def _f(**kw: Any) -> Finding:
    base: dict[str, Any] = {
        "severity": "High", "confidence": 80, "file": "db.py", "line": 42,
        "finding": "SQL injection via f-string", "source": "code-reviewer", "sources": ["code-reviewer"],
    }
    base.update(kw)
    return Finding(**base)


class TestLabel:
    def test_corroborated_wins(self) -> None:
        assert finding_label(_f(corroborated=True, judge_verdict="downrank")) == "corroborated by analyzer"
        assert finding_label(_f(sources=["semgrep", "code-reviewer"])) == "corroborated by analyzer"

    def test_analyzer_only(self) -> None:
        assert finding_label(_f(source="semgrep", sources=["semgrep"])) == "analyzer finding"

    def test_judge_kept(self) -> None:
        assert finding_label(_f(judge_verdict="keep")) == "judge kept"

    def test_single_agent_states(self) -> None:
        assert finding_label(_f()) == "single agent, not judged"
        assert finding_label(_f(judge_verdict="downrank")) == "single agent, unverified"

    def test_badge_adds_the_score(self) -> None:
        assert finding_badge(_f(confidence=77, judge_verdict="keep")) == "judge kept, confidence 77"


class TestRendering:
    def test_no_badge_means_no_change(self) -> None:
        assert badge_suffix(_f()) == ""
        assert "confidence" not in format_body_finding(_f(), finding_id=1)

    def test_body_bullet_puts_the_badge_after_the_text_and_keeps_the_header_tokens(self) -> None:
        text = format_body_finding(_f(show_badge=True, judge_verdict="keep"), finding_id=3, location_note="")
        assert text.startswith("- ")
        assert "**[High]**" in text and "**[F3]**" in text
        assert "SQL injection via f-string _(judge kept, confidence 80)_" in text
        assert re.search(r"\*\*\[(Critical|High|Medium|Low)\]\*\*", text)
        assert re.search(r"\*\*\[F(\d+)\]\*\*", text)

    def test_github_inline_comment_keeps_both_parser_tokens(self) -> None:
        body = _build_inline_comment_body(_f(show_badge=True, judge_verdict="keep"), finding_id=2)
        assert "_(judge kept, confidence 80)_" in body
        header = body.splitlines()[0]
        assert re.search(r"\*\*\[High\]\*\*", header) and re.search(r"\*\*\[F2\]\*\*", header)

    def test_the_label_is_computed_when_it_is_rendered_so_it_cannot_go_stale(self) -> None:
        """F7 (#1036): the model keeps a flag, not a pre-rendered string."""
        kept = _f(show_badge=True, judge_verdict="keep")
        assert "judge kept, confidence 80" in badge_suffix(kept)
        later = kept.model_copy(update={"judge_verdict": "downrank", "confidence": 65})
        assert "single agent, unverified, confidence 65" in badge_suffix(later)
        assert "judge kept" not in badge_suffix(later)

    def test_the_suffix_is_empty_unless_the_flag_is_set(self) -> None:
        assert badge_suffix(_f(judge_verdict="keep")) == ""

    def test_the_finding_model_has_no_pre_rendered_text_field(self) -> None:
        assert "badge" not in Finding.model_fields and "show_badge" in Finding.model_fields

    def test_bitbucket_annotation_summary_is_plain_text(self) -> None:
        payload = build_annotation_payload(_f(show_badge=True, judge_verdict="keep"), finding_id=1)
        assert payload is not None and payload["summary"].startswith("[F1] High: SQL injection via f-string (judge kept, confidence 80)")


class TestNoEffectOnTheDecision:
    def test_the_outcome_is_the_same_with_and_without_a_badge(self) -> None:
        class _L:
            def __init__(self, f: Finding) -> None:
                self.severity, self.confidence = f.severity, f.confidence
                self.finding = f.finding
                self.file, self.line = f.file, f.line

        plain = classify_review_outcome([_L(_f())], [], "full")
        badged = classify_review_outcome([_L(_f(show_badge=True))], [], "full")
        assert (plain.event, plain.may_approve) == (badged.event, badged.may_approve)


def _run_with(tmp_path: Path, enabled: bool) -> list[Finding]:
    provider = _FakeProvider()
    diff_text = (
        "diff --git a/db.py b/db.py\n--- a/db.py\n+++ b/db.py\n"
        "@@ -40,3 +40,4 @@\n x\n a\n+query = f'SELECT'\n b\n"
    )
    analyzer = _f(source="semgrep", sources=["semgrep"], confidence=90)
    posted: list[Finding] = []

    async def _go() -> None:
        await run_review(
            diff=DiffContext(diff_text=diff_text, head_sha="abc1234567"), summary_text="## S", agents=[],
            llm_call=_llm_call_factory({}), dispatch_context=_make_dispatch_context(tmp_path, diff_text),
            provider=provider, config=OrchestrationConfig(extra_findings=(analyzer,), finding_badges=enabled),
        )
        posted.extend(provider.findings_calls[0]["findings"])

    anyio.run(_go)
    return posted


class TestOrchestrator:
    def test_badges_are_set_when_enabled(self, tmp_path: Path) -> None:
        posted = _run_with(tmp_path, True)
        assert [f.show_badge for f in posted] == [True]
        assert [finding_badge(f) for f in posted] == ["analyzer finding, confidence 90"]

    def test_badges_are_not_shown_when_disabled(self, tmp_path: Path) -> None:
        assert [f.show_badge for f in _run_with(tmp_path, False)] == [False]


class TestSetting:
    def test_default_on_and_opt_out(self, monkeypatch: Any) -> None:
        from ai_pr_review.config import ReviewConfig

        monkeypatch.delenv("AI_FINDING_BADGES", raising=False)
        assert ReviewConfig.model_fields["finding_badges"].default is True
        monkeypatch.setenv("AI_FINDING_BADGES", "false")
        from ai_pr_review.config import _bool_env

        assert _bool_env("AI_FINDING_BADGES", True) is False
