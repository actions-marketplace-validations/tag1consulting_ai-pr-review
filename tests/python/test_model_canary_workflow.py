"""Structural tests for .github/workflows/model-canary.yml.

The canary step holds three provider API keys, so these pin that each key
reaches only that step and comes from the expected repository secret.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "model-canary.yml"

_KEY_SECRETS = {
    "ANTHROPIC_API_KEY": "${{ secrets.AI_REVIEW_API_KEY }}",
    "OPENAI_API_KEY": "${{ secrets.OPENAI_API_KEY }}",
    "GOOGLE_API_KEY": "${{ secrets.GOOGLE_API_KEY }}",
}


def _load() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(_WORKFLOW.read_text())
    return loaded


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load()["jobs"]["canary"]["steps"]
    return steps


def test_each_provider_key_reaches_only_the_canary_step() -> None:
    for env_var, secret in _KEY_SECRETS.items():
        holders = [step for step in _steps() if env_var in (step.get("env") or {})]
        assert len(holders) == 1, env_var
        assert "live_model_canary.py" in holders[0]["run"]
        assert holders[0]["env"][env_var] == secret


def test_no_workflow_or_job_level_env_exposes_a_key() -> None:
    assert "env" not in _load()
    assert "env" not in _load()["jobs"]["canary"]


def test_issue_text_is_not_anthropic_specific() -> None:
    issue_step = next(s for s in _steps() if "gh issue create" in s.get("run", ""))
    assert "Anthropic API quota" not in issue_step["run"]


def test_job_timeout_leaves_room_for_every_provider() -> None:
    # 12 calls run one after another (3 providers x 2 models x 2 agents). Four
    # Anthropic calls alone took 5 to 6 minutes, so 20 minutes was too short and
    # the cancelled job filed no issue.
    assert _load()["jobs"]["canary"]["timeout-minutes"] >= 45


def test_issue_step_also_runs_when_the_job_is_cancelled() -> None:
    # A job that hits timeout-minutes is cancelled, not failed, so a bare
    # failure() would file nothing for a canary that ran out of time.
    step = next(s for s in _steps() if s.get("name") == "File an issue on failure")
    condition = step["if"]
    assert "failure()" in condition
    assert "cancelled()" in condition
    assert "schedule" in condition


def test_empty_detail_explanation_does_not_guess_a_single_cause() -> None:
    step = next(s for s in _steps() if s.get("name") == "File an issue on failure")
    script = step["run"]
    assert "cause is not known from this issue alone" in script
    assert "no provider key was set" in script
