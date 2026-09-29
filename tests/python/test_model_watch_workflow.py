"""Structural tests for .github/workflows/model-watch.yml.

The workflow holds `issues: write` and passes an API key to one step, so these pin
the properties that keep that narrow: it never runs on pull requests, its permissions
stay minimal, every action is pinned to a commit SHA, and the key reaches only the
step that needs it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "model-watch.yml"


def _load() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(_WORKFLOW.read_text())
    return loaded


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load()["jobs"]["watch"]["steps"]
    return steps


def test_runs_only_on_a_schedule_or_manually() -> None:
    # YAML 1.1 parses the bare key `on` as boolean True.
    assert set(_load()[True]) == {"schedule", "workflow_dispatch"}


def test_permissions_are_contents_read_and_issues_write_only() -> None:
    assert _load()["permissions"] == {"contents": "read", "issues": "write"}
    assert "permissions" not in _load()["jobs"]["watch"]


def test_only_runs_in_the_upstream_repository() -> None:
    assert "tag1consulting/ai-pr-review" in _load()["jobs"]["watch"]["if"]


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    uses = [step["uses"] for step in _steps() if "uses" in step]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref


def test_checkout_does_not_persist_credentials() -> None:
    checkout = next(step for step in _steps() if "uses" in step)
    assert checkout["with"]["persist-credentials"] is False


def test_run_scripts_never_interpolate_expressions() -> None:
    scripts = [step["run"] for step in _steps() if "run" in step]
    assert scripts
    for script in scripts:
        assert "${{" not in script


_KEY_SECRETS = {
    "ANTHROPIC_API_KEY": "${{ secrets.AI_REVIEW_API_KEY }}",
    "OPENAI_API_KEY": "${{ secrets.OPENAI_API_KEY }}",
    "GOOGLE_API_KEY": "${{ secrets.GOOGLE_API_KEY }}",
}


def test_each_api_key_reaches_only_the_watcher_step() -> None:
    for env_var, secret in _KEY_SECRETS.items():
        holders = [step for step in _steps() if env_var in (step.get("env") or {})]
        assert len(holders) == 1, env_var
        assert "model_watch.py" in holders[0]["run"]
        assert holders[0]["env"][env_var] == secret


def test_no_job_level_env_exposes_a_key_to_every_step() -> None:
    assert "env" not in _load()
    assert "env" not in _load()["jobs"]["watch"]


def test_dry_run_input_is_passed_through_env_not_the_script() -> None:
    step = next(s for s in _steps() if "model_watch.py" in s.get("run", ""))
    assert step["env"]["DRY_RUN"] == "${{ inputs.dry-run }}"
    assert "--dry-run" in step["run"]
