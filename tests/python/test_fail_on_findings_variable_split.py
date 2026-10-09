"""The slash-command path and the review job must not share one repo variable.

A shared `AI_REVIEW_FAIL_ON_FINDINGS` turned the rescan gate on whenever a
repository set the review job's gate to 'true', which fails a rescan run that
only found something and misfires the failure-reaction step. The caller
workflows therefore read `AI_REVIEW_SLASH_FAIL_ON_FINDINGS` on the slash path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_VAR = "AI_REVIEW_FAIL_ON_FINDINGS"
_SLASH_VAR = "AI_REVIEW_SLASH_FAIL_ON_FINDINGS"

# (file, whether the review job in the same file reads the review variable)
_CALLERS = [
    (".github/workflows/ai-review.yml", True),
    ("examples/workflows/pr-review.yml", True),
    ("examples/workflows/comment-triggers.yml", False),
]


def _fail_on_findings_exprs(path: str) -> list[str]:
    data: Any = yaml.safe_load((_ROOT / path).read_text())
    found: list[str] = []
    for job in (data.get("jobs") or {}).values():
        values = [job.get("with") or {}]
        values += [step.get("with") or {} for step in job.get("steps") or []]
        for with_block in values:
            if "fail-on-findings" in with_block:
                found.append(str(with_block["fail-on-findings"]))
    return found


@pytest.mark.parametrize(("path", "has_review_job"), _CALLERS)
def test_slash_path_uses_its_own_variable(path: str, has_review_job: bool) -> None:
    exprs = _fail_on_findings_exprs(path)
    slash = [e for e in exprs if _SLASH_VAR in e]
    review = [e for e in exprs if _REVIEW_VAR in e and _SLASH_VAR not in e]
    assert len(slash) == 1, f"{path}: expected one slash-path expression, got {exprs}"
    assert "|| 'false'" in slash[0]
    assert bool(review) == has_review_job, f"{path}: review-job expression mismatch in {exprs}"
    if review:
        assert "|| 'true'" in review[0]
