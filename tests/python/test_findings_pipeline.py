"""The findings pipeline end to end on the findings of ai-pr-review-test PR #70.

The merge (#1074), the semgrep rollup (#1081) and the suppression-check analyzer
(#1077) all change the same list of findings. Each has unit tests. This test
runs the merge and then the rollup, in the order `orchestrate.py` runs them, so that
a change in one cannot silently break the other. It does not run the suppression or
diff-scope steps that sit between them in `orchestrate.py`.
"""

from __future__ import annotations

from ai_pr_review.findings.merge import merge_findings
from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.scope import rollup_repeated_findings
from ai_pr_review.vcs._canonical import classify
from ai_pr_review.vcs._finding_ids import fingerprint
from tests.python.vcs.test_canonical import _thread

_SQL_RULE = "python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query"
_PICKLE_RULE = "python.lang.security.deserialization.pickle.avoid-pickle"


def _f(**kw: object) -> Finding:
    base: dict[str, object] = {"severity": "High", "confidence": 90, "finding": "x", "line": 1}
    return Finding.model_validate(base | kw)


def _pr70_findings() -> list[Finding]:
    user, php = "api/user.py", "lib/data_processor.php"
    findings = [
        # api/user.py: SQL injection by the agent, plus the same semgrep rule at four sites.
        _f(file=user, line=22, source="security-reviewer", severity="Critical", category="injection",
           finding="get_user concatenates user_id into the query"),
        *(
            _f(file=user, line=n, source="semgrep", category="injection",
               finding=f"{_SQL_RULE}: Avoiding SQL string concatenation ({n})")
            for n in (31, 65, 93, 101)
        ),
        # api/user.py:48, the unsafe deserialization call and its noqa comment.
        _f(file=user, line=48, source="security-reviewer", severity="Critical", category="injection",
           finding="pickle.loads on a caller-supplied blob gives code execution"),
        _f(file=user, line=48, source="semgrep", category="injection",
           finding=f"{_PICKLE_RULE}: Avoid pickle"),
        _f(file=user, line=48, source="ruff", severity="Low", category="lint", style_only=True,
           finding="RUF100: Unused `noqa` directive (non-enabled: `S301`)"),
        _f(file=user, line=48, source="suppression-check", severity="Medium", confidence=85,
           category="lint", finding="A comment on this line turns off a security check (noqa: S301)."),
        # lib/data_processor.php: six docblock findings, two semgrep rules, agent findings, md5.
        *(
            _f(file=php, line=n, source="phpcs", category="lint", severity="Low", style_only=True,
               finding=f"Squiz.Commenting.FunctionComment.Missing: Missing doc comment ({n})")
            for n in (7, 14, 21, 29, 35, 45)
        ),
        _f(file=php, line=31, source="semgrep", category="injection",
           finding="php.lang.security.exec-use.exec-use: Executing non-constant commands."),
        _f(file=php, line=31, source="security-reviewer", severity="Critical", category="injection",
           finding="runDiagnostic passes $host to shell_exec"),
        _f(file=php, line=37, source="semgrep", category="injection",
           finding="php.lang.security.unserialize-use.unserialize-use: Calling unserialize()."),
        _f(file=php, line=37, source="security-reviewer", category="injection",
           finding="unserialize() on $data enables object injection"),
        _f(file=php, line=47, source="security-reviewer", category="other",
           finding="generateToken uses md5 of the user id and time()"),
    ]
    return findings


def _run_pipeline() -> list[Finding]:
    return rollup_repeated_findings(merge_findings(_pr70_findings()))


def test_pr70_pipeline_keeps_every_security_finding_visible() -> None:
    result = _run_pipeline()
    user = [f for f in result if f.file == "api/user.py"]
    php = [f for f in result if f.file == "lib/data_processor.php"]

    # api/user.py: the agent SQL finding, one rolled-up semgrep SQL finding,
    # the pickle cluster, the ruff finding and the suppression finding.
    assert len(user) == 5
    sql_rollup = next(f for f in user if f.sources == ["semgrep"] and "occurrences" in f.finding)
    assert sql_rollup.line == 31
    assert "lines: 31, 65, 93, 101" in sql_rollup.finding
    assert any(f.line == 22 and f.source == "security-reviewer" for f in user)

    pickle = next(f for f in user if f.line == 48 and "pickle.loads" in f.finding)
    assert pickle.severity == "Critical" and pickle.corroborated
    ruff = next(f for f in user if f.source == "ruff")
    assert ruff.sources == ["ruff"] and not ruff.corroborated
    suppression = next(f for f in user if f.source == "suppression-check")
    assert suppression.line == 48 and suppression.severity == "Medium"
    assert suppression.sources == ["suppression-check"]

    # lib/data_processor.php: six separate docblock findings, two merged
    # security clusters and the md5 finding.
    assert len(php) == 9
    docblocks = [f for f in php if f.sources == ["phpcs"]]
    assert len(docblocks) == 6 and not any(f.corroborated for f in docblocks)
    by_line = {f.line: f for f in php if f.sources != ["phpcs"]}
    assert by_line[31].source == "security-reviewer" and by_line[31].corroborated
    assert by_line[37].source == "semgrep" and by_line[37].corroborated
    assert any("security-reviewer" in note for note in by_line[37].also_reported)
    assert "md5" in by_line[47].finding and not by_line[47].corroborated


def test_pr70_pipeline_loses_no_source_text() -> None:
    """Every source that reported something is still named somewhere in the output."""
    result = _run_pipeline()
    sources = {s for f in result for s in f.sources or [f.source]}
    assert {"security-reviewer", "semgrep", "ruff", "phpcs", "suppression-check"} <= sources


def _sql_hit(line: int) -> Finding:
    return _f(file="api/user.py", line=line, source="semgrep", category="injection",
              finding=f"{_SQL_RULE}: Avoiding SQL string concatenation ({line})")


def test_a_dismissed_semgrep_finding_is_reported_again_when_a_second_site_rolls_it_up() -> None:
    """The rollup changes the text of the finding at the lowest line, so its fingerprint
    changes. The rolled-up finding stands for the new site as well, and a person dismissed
    only the first one. A fuzzy match on the lowest line must not hide a real second hit
    (#1073), so only an exact fingerprint verdict can suppress a rolled-up finding."""
    run1 = rollup_repeated_findings(merge_findings([_sql_hit(31)]))
    dismissed_fp = fingerprint(run1[0])
    thread = _thread(fp=dismissed_fp, severity="High", category="injection", is_resolved=True)

    run2 = rollup_repeated_findings(merge_findings([_sql_hit(31), _sql_hit(65)]))
    assert len(run2) == 1 and "2 occurrences" in run2[0].finding
    assert fingerprint(run2[0]) != dismissed_fp  # the churn is real

    assert run2[0].rolled_up_by_rule
    result = classify(run2[0], verdicts={dismissed_fp: "dismissed"}, all_threads=[thread])
    assert result.kind == "new"
    # The same dismissal still covers the lone finding, which is not rolled up.
    assert not run1[0].rolled_up_by_rule
    assert classify(run1[0], verdicts={dismissed_fp: "dismissed"}, all_threads=[thread]).kind == "suppressed"


def test_a_new_semgrep_site_far_above_a_dismissed_one_is_reported_again() -> None:
    """The rolled-up finding moves to the new lowest line. That is a new place to look,
    so it is not covered by the old dismissal. This documents the behavior."""
    dismissed_fp = fingerprint(_sql_hit(31))
    thread = _thread(fp=dismissed_fp, severity="High", category="injection", is_resolved=True)

    (rolled,) = rollup_repeated_findings(merge_findings([_sql_hit(5), _sql_hit(31)]))
    assert rolled.line == 5
    result = classify(rolled, verdicts={dismissed_fp: "dismissed"}, all_threads=[thread])
    assert result.kind == "new"
