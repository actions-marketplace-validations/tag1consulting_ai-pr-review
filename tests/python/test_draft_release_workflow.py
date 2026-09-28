"""Structural tests for .github/workflows/draft-release.yml.

The workflow runs with `contents: write` on every release tag push, and the tag
name is chosen by whoever pushes it. These tests pin the properties that keep
that safe: it only ever creates a draft, it only triggers on tags, the tag name
never reaches a shell through expression interpolation, and every action is
pinned to a commit SHA.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "draft-release.yml"


def _load() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(_WORKFLOW.read_text())
    return loaded


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load()["jobs"]["draft-release"]["steps"]
    return steps


def _run_scripts() -> list[str]:
    return [step["run"] for step in _steps() if "run" in step]


def test_triggers_only_on_release_tag_pushes() -> None:
    # YAML 1.1 parses the bare key `on` as boolean True.
    trigger = _load()[True]
    assert set(trigger) == {"push"}
    assert set(trigger["push"]) == {"tags"}
    assert trigger["push"]["tags"] == ["v*.*.*"]


def test_permissions_are_contents_write_only() -> None:
    assert _load()["permissions"] == {"contents": "write"}


def test_the_job_does_not_widen_permissions() -> None:
    assert "permissions" not in _load()["jobs"]["draft-release"]


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    uses = [step["uses"] for step in _steps() if "uses" in step]
    assert uses, "expected at least the checkout step"
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref


def test_checkout_does_not_persist_credentials() -> None:
    checkout = next(step for step in _steps() if "uses" in step)
    assert checkout["with"]["persist-credentials"] is False


def test_run_scripts_never_interpolate_expressions() -> None:
    scripts = _run_scripts()
    assert scripts
    for script in scripts:
        assert "${{" not in script


def test_tag_reaches_the_script_only_through_env() -> None:
    step = next(step for step in _steps() if "run" in step)
    assert step["env"]["TAG"] == "${{ github.ref_name }}"


def test_release_is_only_ever_created_as_a_draft() -> None:
    script = "\n".join(_run_scripts())
    creations = re.findall(r"gh release create[^\n]*(?:\\\n[^\n]*)*", script)
    assert creations, "expected a gh release create call"
    for command in creations:
        assert "--draft" in command


def test_release_creation_requires_an_existing_tag() -> None:
    assert "--verify-tag" in "\n".join(_run_scripts())


def test_existing_release_is_left_alone_and_tag_shape_is_validated() -> None:
    script = "\n".join(_run_scripts())
    assert "gh release view" in script
    assert "exit 0" in script
    assert "is not a release tag" in script


def test_falls_back_to_generated_notes_instead_of_failing() -> None:
    script = "\n".join(_run_scripts())
    assert "scripts/release_notes.py" in script
    assert "--generate-notes" in script
