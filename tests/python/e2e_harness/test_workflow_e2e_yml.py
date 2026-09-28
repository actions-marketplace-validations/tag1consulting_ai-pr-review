"""Structural tests for .github/workflows/e2e.yml.

The workflow's release-eligibility and gate logic is inline bash inside YAML,
so it has no unit tests of its own. These tests pin the properties the
required `e2e-gate` check on `main` depends on, by reading the YAML directly.
No network calls, no real credentials.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "e2e.yml"

# `${{ github.event_name == 'pull_request' && 'A' || 'B' }}`
_TERNARY = re.compile(
    r"^\$\{\{\s*github\.event_name == '(?P<event>[^']+)'\s*&&\s*'(?P<yes>[^']*)'\s*\|\|\s*'(?P<no>[^']*)'\s*\}\}$"
)


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _resolve_gate_name(expr: str, event_name: str) -> str:
    """Resolve the gate job's `name:` for a given trigger event."""
    match = _TERNARY.match(expr)
    if match is None:
        return expr
    return match["yes"] if event_name == match["event"] else match["no"]


def _run_scripts(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_pull_request_types_include_synchronize_and_ready_for_review(wf: dict[str, Any]) -> None:
    # YAML 1.1 parses the bare key `on` as boolean True.
    types = wf[True]["pull_request"]["types"]
    assert "synchronize" in types
    assert "ready_for_review" in types


def test_gate_reports_required_context_only_for_pull_request(wf: dict[str, Any]) -> None:
    expr = wf["jobs"]["e2e-gate"]["name"]
    assert _resolve_gate_name(expr, "pull_request") == "e2e-gate"
    for event in ("workflow_dispatch", "schedule"):
        resolved = _resolve_gate_name(expr, event)
        assert resolved != "e2e-gate"
        assert resolved == "e2e-gate (manual)"


def test_gate_always_evaluates_and_needs_every_upstream_job(wf: dict[str, Any]) -> None:
    gate = wf["jobs"]["e2e-gate"]
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {"eligibility", "build", "e2e"}


def test_concurrency_cancels_in_progress(wf: dict[str, Any]) -> None:
    assert wf["concurrency"]["cancel-in-progress"] is True


def test_concurrency_has_no_sync_live_split(wf: dict[str, Any]) -> None:
    group = wf["concurrency"]["group"]
    assert "'sync'" not in group
    assert "'live'" not in group
    assert "synchronize" not in group


def test_concurrency_separates_pull_request_from_manual_runs(wf: dict[str, Any]) -> None:
    group = " ".join(wf["concurrency"]["group"].split())
    assert "github.event_name == 'pull_request' && 'pr' || 'manual'" in group
    assert "github.head_ref || github.ref_name" in group


def test_eligibility_has_no_synchronize_skip_path(wf: dict[str, Any]) -> None:
    script = _run_scripts(wf["jobs"]["eligibility"])
    assert "synchronize" not in script
    assert "EVENT_ACTION" not in script


def test_gate_script_has_no_release_push_pass_path(wf: dict[str, Any]) -> None:
    script = _run_scripts(wf["jobs"]["e2e-gate"])
    assert "synchronize" not in script
    assert "is_release_ref" not in script
    assert "NOT been live-validated" not in script


def test_gate_fails_unless_eligibility_build_and_e2e_all_succeeded(wf: dict[str, Any]) -> None:
    script = _run_scripts(wf["jobs"]["e2e-gate"])
    for job in ("eligibility", "build", "e2e"):
        assert f'needs.{job}.result }}}}" != "success"' in script
