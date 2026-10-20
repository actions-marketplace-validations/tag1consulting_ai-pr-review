"""The out-of-diff group header on a GitHub review uses the Low severity icon
from the shared table, so it follows any change to `_SEVERITY_ICONS` (#1037)."""

from __future__ import annotations

from ai_pr_review.vcs._body import severity_icon
from ai_pr_review.vcs.github import _render_review_body


def test_out_of_diff_header_uses_the_low_severity_icon() -> None:
    body = _render_review_body(
        event="COMMENT",
        findings=[],
        inline_count=0,
        body_findings_text="",
        out_of_diff_findings_text="- a finding",
        failed_agents=[],
        usage_block="",
        usage_warning="",
        agent_prompt="",
    )
    assert f"<summary>{severity_icon('low')} Out-of-diff analyzer findings" in body
    assert "<summary>🔵" not in body
