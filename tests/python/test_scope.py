"""Tests for ai_pr_review.findings.scope."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.scope import (
    _ANALYZER_PREFIXES,
    ROLLUP_THRESHOLD,
    SEMGREP_ROLLUP_THRESHOLD,
    _is_analyzer,
    apply_diff_scope,
    rollup_repeated_findings,
)

# Minimal unified diff: one hunk touching lines 10-11 of web/quickbooks.inc
_DIFF = """\
diff --git a/web/quickbooks.inc b/web/quickbooks.inc
index abc..def 100644
--- a/web/quickbooks.inc
+++ b/web/quickbooks.inc
@@ -9,3 +9,4 @@
 context line
+added line one
+added line two
 context line
"""

# Lines 10 and 11 are in the diff; line 100 is not.


def _phpcs(file: str, line: int, severity: str = "High", text: str = "phpcs finding") -> Finding:
    return Finding(severity=severity, confidence=80, finding=text, source="phpcs", file=file, line=line)


def _phpstan(file: str, line: int, text: str = "phpstan finding") -> Finding:
    return Finding(severity="High", confidence=80, finding=text, source="phpstan", file=file, line=line)


def _agent(file: str, line: int, text: str = "agent finding") -> Finding:
    return Finding(severity="High", confidence=80, finding=text, source="code-reviewer", file=file, line=line)


# apply_diff_scope


def test_in_diff_analyzer_finding_unchanged() -> None:
    f = _phpcs("web/quickbooks.inc", 10)
    result = apply_diff_scope([f], _DIFF)
    assert len(result) == 1
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_out_of_diff_analyzer_finding_capped_to_low() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="cap")
    assert len(result) == 1
    assert result[0].severity == "Low"
    assert result[0].out_of_diff


def test_absolute_file_path_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """An absolute Finding.file can never match parse_added_lines()'s
    repo-relative keys, so it silently falls into the out-of-diff branch as
    if genuinely out of scope -- this is exactly how issue #713's
    path-normalization bug hid undetected. Must be logged, not silent."""
    f = _phpcs("/home/runner/work/owner/repo/web/quickbooks.inc", 10)
    with caplog.at_level("WARNING"):
        result = apply_diff_scope([f], _DIFF)
    assert len(result) == 1
    assert result[0].severity == "Low"
    assert result[0].out_of_diff
    assert any("absolute file path" in r.message for r in caplog.records)


def test_relative_file_path_does_not_log_warning(caplog: pytest.LogCaptureFixture) -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    with caplog.at_level("WARNING"):
        apply_diff_scope([f], _DIFF)
    assert not any("absolute file path" in r.message for r in caplog.records)


def test_out_of_diff_analyzer_finding_dropped() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="drop")
    assert result == []


def test_out_of_diff_analyzer_finding_passthrough_when_off() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="off")
    assert len(result) == 1
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_llm_agent_finding_never_capped() -> None:
    f = _agent("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], _DIFF, mode="cap")
    assert len(result) == 1
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_phpstan_out_of_diff_capped() -> None:
    f = _phpstan("web/quickbooks.inc", 500)
    result = apply_diff_scope([f], _DIFF)
    assert result[0].severity == "Low"
    assert result[0].out_of_diff


def test_finding_without_line_not_capped() -> None:
    f = Finding(severity="High", confidence=80, finding="body finding", source="phpcs", file="web/quickbooks.inc")
    result = apply_diff_scope([f], _DIFF)
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_finding_with_line_zero_not_capped() -> None:
    # line=0 is a file-level sentinel used by some analyzers; treat like no line.
    f = Finding(severity="High", confidence=80, finding="file-level finding", source="phpcs", file="web/quickbooks.inc", line=None)
    result = apply_diff_scope([f], _DIFF)
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


def test_finding_without_file_not_capped() -> None:
    f = Finding(severity="High", confidence=80, finding="global finding", source="phpcs")
    result = apply_diff_scope([f], _DIFF)
    assert result[0].severity == "High"


def test_mixed_findings_correctly_partitioned() -> None:
    findings = [
        _phpcs("web/quickbooks.inc", 10),   # in diff
        _phpcs("web/quickbooks.inc", 100),  # out of diff
        _agent("web/quickbooks.inc", 100),  # LLM, should not be capped
    ]
    result = apply_diff_scope(findings, _DIFF)

    phpcs_findings = [f for f in result if f.source == "phpcs"]
    agent_findings = [f for f in result if f.source == "code-reviewer"]

    # Two phpcs findings: one in-diff (High, out_of_diff=False), one out (Low, out_of_diff=True)
    assert len(phpcs_findings) == 2
    in_diff_phpcs = [f for f in phpcs_findings if not f.out_of_diff]
    ood_phpcs = [f for f in phpcs_findings if f.out_of_diff]
    assert len(in_diff_phpcs) == 1 and in_diff_phpcs[0].severity == "High"
    assert len(ood_phpcs) == 1 and ood_phpcs[0].severity == "Low"

    # LLM finding untouched regardless of line position
    assert len(agent_findings) == 1
    assert agent_findings[0].severity == "High"
    assert not agent_findings[0].out_of_diff


def test_multiple_phpcs_in_and_out() -> None:
    findings = [
        _phpcs("web/quickbooks.inc", 10),   # in diff
        _phpcs("web/quickbooks.inc", 11),   # in diff
        _phpcs("web/quickbooks.inc", 50),   # out of diff
        _phpcs("web/quickbooks.inc", 100),  # out of diff
    ]
    result = apply_diff_scope(findings, _DIFF)
    in_diff = [f for f in result if not f.out_of_diff]
    out_of_diff = [f for f in result if f.out_of_diff]
    assert len(in_diff) == 2
    assert len(out_of_diff) == 2
    assert all(f.severity == "High" for f in in_diff)
    assert all(f.severity == "Low" for f in out_of_diff)


def test_empty_diff_text_passthrough() -> None:
    f = _phpcs("web/quickbooks.inc", 100)
    result = apply_diff_scope([f], "", mode="cap")
    assert result[0].severity == "High"
    assert not result[0].out_of_diff


# rollup_repeated_findings


def _make_repeated(n: int, file: str = "web/file.php", source: str = "phpcs", text: str = "Use short array syntax") -> list[Finding]:
    return [
        Finding(severity="High", confidence=80, finding=text, source=source, file=file, line=i + 1)
        for i in range(n)
    ]


def test_below_threshold_not_rolled_up() -> None:
    findings = _make_repeated(ROLLUP_THRESHOLD)
    result = rollup_repeated_findings(findings, threshold=ROLLUP_THRESHOLD)
    assert len(result) == ROLLUP_THRESHOLD


def test_above_threshold_rolled_up() -> None:
    findings = _make_repeated(ROLLUP_THRESHOLD + 1)
    result = rollup_repeated_findings(findings, threshold=ROLLUP_THRESHOLD)
    assert len(result) == 1
    assert f"({ROLLUP_THRESHOLD + 1} occurrences" in result[0].finding


def test_rollup_preserves_line_list() -> None:
    findings = _make_repeated(10)
    result = rollup_repeated_findings(findings, threshold=5)
    assert len(result) == 1
    text = result[0].finding
    assert "lines:" in text
    assert "1" in text


def test_different_rules_not_merged() -> None:
    f1 = _make_repeated(10, text="Rule A")
    f2 = _make_repeated(10, text="Rule B")
    result = rollup_repeated_findings(f1 + f2, threshold=5)
    assert len(result) == 2


def test_different_files_not_merged() -> None:
    f1 = _make_repeated(10, file="a.php")
    f2 = _make_repeated(10, file="b.php")
    result = rollup_repeated_findings(f1 + f2, threshold=5)
    assert len(result) == 2


def test_different_sources_not_merged() -> None:
    f1 = _make_repeated(10, source="phpcs")
    f2 = _make_repeated(10, source="phpstan")
    result = rollup_repeated_findings(f1 + f2, threshold=5)
    assert len(result) == 2


def test_non_analyzer_finding_passthrough() -> None:
    agent_findings = [
        _agent("web/file.php", i + 1, text="security issue")
        for i in range(20)
    ]
    result = rollup_repeated_findings(agent_findings, threshold=5)
    assert len(result) == 20


def test_rollup_line_preview_truncated_for_large_sets() -> None:
    findings = _make_repeated(30)
    result = rollup_repeated_findings(findings, threshold=5)
    assert len(result) == 1
    text = result[0].finding
    assert "more" in text


def test_rollup_preserves_input_order() -> None:
    """Non-collapsed findings stay in place, whatever their source (#1000)."""
    agent_a = _agent("a.php", 1, text="agent a")
    analyzer = _phpcs("b.php", 2, text="analyzer b")
    agent_c = _agent("c.php", 3, text="agent c")
    result = rollup_repeated_findings([agent_a, analyzer, agent_c], threshold=5)
    assert [f.finding for f in result] == ["agent a", "analyzer b", "agent c"]


def test_rollup_collapsed_group_takes_first_member_slot() -> None:
    before = _agent("a.php", 1, text="before")
    repeated = _make_repeated(10, file="b.php")
    after = _agent("c.php", 3, text="after")
    result = rollup_repeated_findings([before, *repeated, after], threshold=5)
    assert [f.file for f in result] == ["a.php", "b.php", "c.php"]
    assert "occurrences" in result[1].finding


def test_rollup_non_contiguous_group_collapses_into_first_member_slot() -> None:
    reps = _make_repeated(10, file="b.php")
    agent_x = _agent("x.php", 1, text="agent x")
    agent_y = _agent("y.php", 2, text="agent y")
    # Group members are split by an agent finding and followed by another.
    result = rollup_repeated_findings(
        [reps[0], agent_x, *reps[1:], agent_y], threshold=5,
    )
    assert [f.file for f in result] == ["b.php", "x.php", "y.php"]
    assert "10 occurrences" in result[0].finding


def test_rollup_interleaved_collapsed_groups_ordered_by_first_member() -> None:
    a = _make_repeated(10, file="a.php", text="Rule A")
    b = _make_repeated(10, file="b.php", text="Rule B")
    agent = _agent("m.php", 1, text="agent between")
    interleaved = [a[0], b[0], agent, *[f for pair in zip(a[1:], b[1:], strict=True) for f in pair]]
    result = rollup_repeated_findings(interleaved, threshold=5)
    assert [f.file for f in result] == ["a.php", "b.php", "m.php"]


def test_rollup_uncollapsed_group_members_keep_their_own_slots() -> None:
    p1, p2, p3 = _make_repeated(3, file="b.php")
    agent_x = _agent("x.php", 1, text="agent x")
    agent_y = _agent("y.php", 2, text="agent y")
    result = rollup_repeated_findings([p1, agent_x, p2, agent_y, p3], threshold=5)
    assert [(f.file, f.line) for f in result] == [
        ("b.php", 1), ("x.php", 1), ("b.php", 2), ("y.php", 2), ("b.php", 3),
    ]


def test_corroborated_critical_keeps_inline_slot_after_rollup() -> None:
    """Issue #1000: an agent+analyzer Critical must not fall behind agent-only
    findings and past the inline cap."""
    from ai_pr_review.findings.merge import merge_findings
    from ai_pr_review.vcs._inline import partition_findings

    agent_only = [
        Finding(
            severity="Medium", confidence=90, finding=f"agent finding {i}",
            source="code-reviewer", sources=["code-reviewer"],
            file=f"src/file{i}.py", line=i + 1, category="bug",
        )
        for i in range(30)
    ]
    critical = Finding(
        severity="Critical", confidence=95, finding="SQL injection",
        source="semgrep", sources=["code-reviewer", "semgrep"],
        file="api/user.py", line=5, category="injection",
    )
    merged = merge_findings([*agent_only, critical])
    rolled = rollup_repeated_findings(merged)
    assert rolled[0].file == "api/user.py"

    eligible = {(f.file, f.line) for f in rolled}
    inline, _body = partition_findings(rolled, eligible_new=eligible, max_inline=25)
    assert any(f.file == "api/user.py" for f in inline)


# Config validation


def test_config_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.config import ReviewConfig
    for val in ("cap", "drop", "off"):
        monkeypatch.setenv("AI_ANALYZER_DIFF_SCOPE", val)
        cfg = ReviewConfig.from_env()
        assert cfg.analyzer_diff_scope == val


def test_config_invalid_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.config import ReviewConfig
    monkeypatch.setenv("AI_ANALYZER_DIFF_SCOPE", "invalid")
    with pytest.raises(ValidationError):
        ReviewConfig.from_env()


def test_config_default_is_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_pr_review.config import ReviewConfig
    monkeypatch.delenv("AI_ANALYZER_DIFF_SCOPE", raising=False)
    cfg = ReviewConfig.from_env()
    assert cfg.analyzer_diff_scope == "cap"


# Analyzer prefix coverage — every source emitted by analyzers/run-*.sh must
# be recognised.  If a new analyzer is added, update _ANALYZER_PREFIXES too.


@pytest.mark.parametrize("source", [
    "checkov",
    "eslint",
    "golangci-lint",
    "hadolint",
    "kube-linter",
    "osv",
    "phpcs",
    "phpstan",
    "ruff",
    "sarif:bandit",
    "semgrep",
    "shellcheck",
    "tflint",
    "trufflehog",
])
def test_is_analyzer_recognises_all_sources(source: str) -> None:
    f = Finding(severity="High", confidence=80, finding="x", source=source)
    assert _is_analyzer(f), f"_is_analyzer returned False for source={source!r}"


def test_is_analyzer_rejects_llm_agent_sources() -> None:
    for source in ("code-reviewer", "security-reviewer", "architecture-reviewer", "blind-hunter"):
        f = Finding(severity="High", confidence=80, finding="x", source=source)
        assert not _is_analyzer(f), f"_is_analyzer returned True for LLM agent source={source!r}"


def test_prefix_list_has_no_duplicates() -> None:
    assert len(_ANALYZER_PREFIXES) == len(set(_ANALYZER_PREFIXES)), (
        "Duplicate entries in _ANALYZER_PREFIXES"
    )


# Rollup: in-diff and out-of-diff occurrences of the same rule must not merge.


def test_rollup_does_not_merge_across_diff_boundary() -> None:
    """An in-diff High finding must not be folded into an out-of-diff Low group."""
    # 5 out-of-diff Low findings + 1 in-diff High = 6 total, above threshold.
    # The in-diff High must survive as a separate entry.
    ood = [
        Finding(
            severity="Low", confidence=80, finding="same rule",
            source="phpcs", file="a.php", line=i + 50,
            out_of_diff=True,
        )
        for i in range(5)
    ]
    in_diff = Finding(
        severity="High", confidence=90, finding="same rule",
        source="phpcs", file="a.php", line=5,
        out_of_diff=False,
    )
    result = rollup_repeated_findings(ood + [in_diff], threshold=5)
    # Out-of-diff group (5 entries, exactly at threshold) is preserved unchanged.
    # In-diff entry is kept separate.
    in_diff_results = [f for f in result if not f.out_of_diff]
    ood_results = [f for f in result if f.out_of_diff]
    assert len(in_diff_results) == 1, "in-diff High finding must not be collapsed"
    assert in_diff_results[0].severity == "High"
    assert len(ood_results) == 5


def test_rollup_unions_also_reported_notes_of_all_occurrences() -> None:
    """#1044: a rolled-up group keeps the notes of every member, not only the first."""
    from ai_pr_review.findings.scope import rollup_repeated_findings

    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding="Same sniff",
            source="phpcs",
            file="a.php",
            line=10 + i * 20,
            category="lint",
            also_reported=[f"phpstan (line {11 + i * 20}): msg{i}"],
        )
        for i in range(4)
    ]
    result = rollup_repeated_findings(findings, threshold=3)
    assert len(result) == 1
    assert result[0].also_reported == [f"phpstan (line {11 + i * 20}): msg{i}" for i in range(4)]


def test_rollup_with_eight_notes_renders_five_plus_overflow_line() -> None:
    """#1047: storage keeps all notes, the display cap applies at render time."""
    from ai_pr_review.findings.scope import rollup_repeated_findings
    from ai_pr_review.vcs._body import also_reported_notes

    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding="Same sniff",
            source="phpcs",
            file="a.php",
            line=10 + i * 20,
            category="lint",
            also_reported=[f"phpstan (line {11 + i * 20}): msg{i}", f"psalm (line {12 + i * 20}): p{i}"],
        )
        for i in range(4)
    ]
    result = rollup_repeated_findings(findings, threshold=3)
    assert len(result) == 1
    assert len(result[0].also_reported) == 8
    shown = also_reported_notes(result[0])
    assert len(shown) == 6
    assert shown[:5] == result[0].also_reported[:5]
    assert shown[5] == "(+3 more from other analyzers)"


def _finding_with_notes(n: int) -> Finding:
    return Finding(
        severity="High", confidence=90, finding="Same sniff", source="phpcs",
        file="a.php", line=10, category="lint",
        also_reported=[f"phpstan: m{i}" for i in range(n)],
    )


@pytest.mark.parametrize(
    ("count", "overflow"), [(1, None), (4, None), (5, None), (6, "(+1 more from other analyzers)")]
)
def test_also_reported_display_cap_boundary(count: int, overflow: str | None) -> None:
    """The display cap is 5 notes. The overflow line appears only past it."""
    from ai_pr_review.vcs._body import also_reported_notes

    shown = also_reported_notes(_finding_with_notes(count))
    assert shown[:min(count, 5)] == [f"phpstan: m{i}" for i in range(min(count, 5))]
    if overflow is None:
        assert len(shown) == count
    else:
        assert len(shown) == 6
        assert shown[-1] == overflow


def test_also_reported_storage_bound_boundary() -> None:
    """100 notes fit. The 101st is cut and the render says "or more"."""
    from ai_pr_review.findings.models import (
        ALSO_REPORTED_TRUNCATED_NOTE,
        MAX_ALSO_REPORTED_STORED,
        combine_also_reported,
    )
    from ai_pr_review.vcs._body import also_reported_notes

    exact = combine_also_reported([[f"n{i}" for i in range(MAX_ALSO_REPORTED_STORED)]])
    assert len(exact) == MAX_ALSO_REPORTED_STORED
    assert ALSO_REPORTED_TRUNCATED_NOTE not in exact
    shown_exact = also_reported_notes(_finding_with_notes(0).model_copy(update={"also_reported": exact}))
    assert shown_exact[-1] == f"(+{MAX_ALSO_REPORTED_STORED - 5} more from other analyzers)"

    over = combine_also_reported([[f"n{i}" for i in range(MAX_ALSO_REPORTED_STORED + 1)]])
    assert len(over) == MAX_ALSO_REPORTED_STORED + 1
    assert over[-1] == ALSO_REPORTED_TRUNCATED_NOTE
    shown_over = also_reported_notes(_finding_with_notes(0).model_copy(update={"also_reported": over}))
    assert shown_over[-1] == f"(+{MAX_ALSO_REPORTED_STORED - 5} or more from other analyzers)"


def test_combine_also_reported_drops_old_markers_and_keeps_order() -> None:
    from ai_pr_review.findings.models import combine_also_reported

    notes = combine_also_reported([[f"n{i}" for i in range(4)], ["n3", "(+9 more from other analyzers)", "n4", "n5", "n6"]])
    assert notes == ["n0", "n1", "n2", "n3", "n4", "n5", "n6"]


def test_combine_also_reported_storage_bound_keeps_first_and_marks_truncation() -> None:
    from ai_pr_review.findings.models import (
        ALSO_REPORTED_TRUNCATED_NOTE,
        MAX_ALSO_REPORTED_STORED,
        combine_also_reported,
    )

    notes = combine_also_reported([[f"n{i}" for i in range(MAX_ALSO_REPORTED_STORED + 20)]])
    assert len(notes) == MAX_ALSO_REPORTED_STORED + 1
    assert notes[0] == "n0"
    assert notes[MAX_ALSO_REPORTED_STORED - 1] == f"n{MAX_ALSO_REPORTED_STORED - 1}"
    assert notes[-1] == ALSO_REPORTED_TRUNCATED_NOTE
    # The flag survives a second combine and no second marker appears.
    again = combine_also_reported([notes, ["extra"]])
    assert again.count(ALSO_REPORTED_TRUNCATED_NOTE) == 1
    assert len(again) == MAX_ALSO_REPORTED_STORED + 1


def test_also_reported_display_says_or_more_past_storage_bound() -> None:
    from ai_pr_review.findings.models import MAX_ALSO_REPORTED_STORED, combine_also_reported
    from ai_pr_review.vcs._body import also_reported_notes

    f = Finding(
        severity="High", confidence=90, finding="x", source="phpcs", file="a.php", line=1, category="lint",
        also_reported=combine_also_reported([[f"n{i}" for i in range(MAX_ALSO_REPORTED_STORED + 5)]]),
    )
    shown = also_reported_notes(f)
    assert len(shown) == 6
    assert shown[-1] == f"(+{MAX_ALSO_REPORTED_STORED - 5} or more from other analyzers)"


# rollup_repeated_findings: semgrep rules (#1081)

_SQL_RULE = "python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query"


def _semgrep(
    line: int,
    rule: str = _SQL_RULE,
    message: str = "Avoiding SQL string concatenation",
    file: str = "api/user.py",
    **kw: object,
) -> Finding:
    fields: dict[str, object] = {
        "severity": "High",
        "confidence": 90,
        "finding": f"{rule}: {message}",
        "source": "semgrep",
        "file": file,
        "line": line,
    }
    return Finding.model_validate(fields | kw)


def test_semgrep_rule_rolls_up_at_two_occurrences() -> None:
    assert SEMGREP_ROLLUP_THRESHOLD == 1
    result = rollup_repeated_findings([_semgrep(31), _semgrep(65)])
    assert len(result) == 1
    assert "(2 occurrences in api/user.py, lines: 31, 65)" in result[0].finding


def test_semgrep_single_hit_is_not_rolled_up() -> None:
    findings = [_semgrep(31)]
    assert rollup_repeated_findings(findings) == findings


def test_semgrep_pr70_sql_hits_become_one_finding_at_first_line() -> None:
    """PR #70: the same rule fired at lines 31, 65, 93 and 101 of api/user.py,
    each with a message that names the site, so the text key never matched."""
    findings = [
        _semgrep(31, message="query built from search term"),
        _semgrep(65, message="query built from user id"),
        _semgrep(93, message="query built from new_email"),
        _semgrep(101, message="query built from user id"),
    ]
    result = rollup_repeated_findings(findings)
    assert len(result) == 1
    assert result[0].line == 31
    assert "(4 occurrences in api/user.py, lines: 31, 65, 93, 101)" in result[0].finding


def test_semgrep_different_rules_and_files_stay_apart() -> None:
    other_rule = "python.lang.security.audit.subprocess-shell-true.subprocess-shell-true"
    result = rollup_repeated_findings(
        [
            _semgrep(31),
            _semgrep(65),
            _semgrep(37, rule=other_rule),
            _semgrep(40, rule=other_rule),
            _semgrep(5, file="api/other.py"),
            _semgrep(9, file="api/other.py"),
        ]
    )
    assert len(result) == 3
    assert sorted(f.file for f in result) == ["api/other.py", "api/user.py", "api/user.py"]


def test_semgrep_rule_id_is_case_insensitive_and_severity_still_splits() -> None:
    result = rollup_repeated_findings(
        [_semgrep(1), _semgrep(2, rule=_SQL_RULE.upper()), _semgrep(3, severity="Medium")]
    )
    assert len(result) == 2


def test_semgrep_corroborated_finding_is_left_out_of_the_rollup() -> None:
    corroborated = _semgrep(
        31, corroborated=True, sources=["code-reviewer", "semgrep"], confidence=100
    )
    result = rollup_repeated_findings([corroborated, _semgrep(65), _semgrep(93)])
    assert corroborated in result
    assert len(result) == 2
    rolled = next(f for f in result if f is not corroborated)
    assert "(2 occurrences in api/user.py, lines: 65, 93)" in rolled.finding


def test_semgrep_finding_with_only_the_corroborated_flag_is_left_out_of_the_rollup() -> None:
    """The flag guards on its own, even when the sources list shows a single tool."""
    flagged = _semgrep(31, corroborated=True)
    result = rollup_repeated_findings([flagged, _semgrep(65), _semgrep(93)])
    assert flagged in result and len(result) == 2


def test_semgrep_multi_source_finding_is_left_out_of_the_rollup() -> None:
    merged = _semgrep(31, sources=["ruff", "semgrep"])
    result = rollup_repeated_findings([merged, _semgrep(65)])
    assert len(result) == 2


def test_semgrep_text_without_rule_prefix_uses_the_text_key() -> None:
    free = [
        Finding(
            severity="High", confidence=90, finding="no rule prefix here", source="semgrep",
            file="api/user.py", line=i,
        )
        for i in (1, 2)
    ]
    assert len(rollup_repeated_findings(free)) == 2


def test_semgrep_rollup_keeps_input_order_and_first_slot() -> None:
    first = _agent("api/user.py", 1, text="agent first")
    last = _agent("api/user.py", 200, text="agent last")
    result = rollup_repeated_findings([first, _semgrep(31), _phpcs("x.php", 3), _semgrep(65), last])
    assert [f.source for f in result] == ["code-reviewer", "semgrep", "phpcs", "code-reviewer"]


def test_semgrep_rollup_keeps_impact_and_trigger_empty() -> None:
    """Agents set impact and trigger. Analyzer groups have none to reconcile."""
    result = rollup_repeated_findings([_semgrep(31), _semgrep(65)])
    assert result[0].impact == ""
    assert result[0].trigger == ""


def test_non_semgrep_analyzer_still_needs_more_than_the_global_threshold() -> None:
    assert len(rollup_repeated_findings(_make_repeated(2))) == 2
    assert len(rollup_repeated_findings(_make_repeated(ROLLUP_THRESHOLD + 1))) == 1


def test_a_group_of_the_same_message_is_not_marked_as_rolled_up_by_rule() -> None:
    group = [
        Finding(severity="Low", confidence=90, finding="Missing doc comment", source="phpcs",
                file="a.php", line=n, category="lint")
        for n in range(1, 8)
    ]
    (rolled,) = rollup_repeated_findings(group)
    assert "occurrences" in rolled.finding
    assert rolled.rolled_up_by_rule is False
