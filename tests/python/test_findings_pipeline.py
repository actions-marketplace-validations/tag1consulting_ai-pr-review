"""The findings pipeline end to end on the findings of ai-pr-review-test PR #70.

The merge (#1074), the semgrep rollup (#1081) and the suppression-check analyzer
(#1077) all change the same list of findings. Each has unit tests. This test
runs them in the order `orchestrate.py` calls them (merge, then rollup) so that a
change in one cannot silently break another.
"""

from __future__ import annotations

from ai_pr_review.findings.merge import merge_findings
from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.scope import rollup_repeated_findings

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
        _f(file=user, line=48, source="ruff", severity="Low", category="lint",
           finding="RUF100: Unused `noqa` directive (non-enabled: `S301`)"),
        _f(file=user, line=48, source="suppression-check", severity="Medium", confidence=85,
           category="lint", finding="A comment on this line turns off a security check (noqa: S301)."),
        # lib/data_processor.php: six docblock findings, two semgrep rules, agent findings, md5.
        *(
            _f(file=php, line=n, source="phpcs", category="lint",
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
    assert by_line[37].source == "security-reviewer" and by_line[37].corroborated
    assert "md5" in by_line[47].finding and not by_line[47].corroborated


def test_pr70_pipeline_loses_no_source_text() -> None:
    """Every source that reported something is still named somewhere in the output."""
    result = _run_pipeline()
    sources = {s for f in result for s in f.sources or [f.source]}
    assert {"security-reviewer", "semgrep", "ruff", "phpcs", "suppression-check"} <= sources
