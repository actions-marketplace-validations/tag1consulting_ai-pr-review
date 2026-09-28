"""Structural test for .github/workflows/lint.yml.

`Python (3.14)` is a required status check on `main`. A required check whose
workflow is skipped by a `paths:` filter never reports, so docs-only and
workflow-only PRs would sit at "Expected -- waiting for status" forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

LINT = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "lint.yml"


def test_pull_request_trigger_has_no_path_filter() -> None:
    wf: dict[str, Any] = yaml.safe_load(LINT.read_text())
    # YAML 1.1 parses the bare key `on` as boolean True.
    pull_request = wf[True]["pull_request"]
    assert not pull_request or "paths" not in pull_request
    assert not pull_request or "paths-ignore" not in pull_request
