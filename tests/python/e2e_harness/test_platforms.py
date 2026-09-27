"""Unit tests for tests/e2e/platforms.py's pure/file-local helper
functions. No network calls; no real credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.e2e.config import PlatformConfig
from tests.e2e.models import AdapterError
from tests.e2e.platforms import _BaseAdapter, _record_orphan_branch


def _config(name: str = "github") -> PlatformConfig:
    return PlatformConfig(name=name, repo_slug="example/repo", base_ref="main", seed_sha="deadbeef")


class _FailingDeleteAdapter(_BaseAdapter):
    def delete_branch(self, branch: str) -> None:
        raise AdapterError("delete failed: 403 forbidden")


class _SucceedingDeleteAdapter(_BaseAdapter):
    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config)
        self.deleted: list[str] = []

    def delete_branch(self, branch: str) -> None:
        self.deleted.append(branch)


# --- _record_orphan_branch -------------------------------------------------------

def test_record_orphan_branch_appends_jsonl_entry(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "orphan-branches.jsonl"
    monkeypatch.setattr("tests.e2e.platforms._ORPHAN_LOG_PATH", log_path)
    _record_orphan_branch("github", "e2e/run1", "delete failed: 403 forbidden")
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["platform"] == "github"
    assert entry["branch"] == "e2e/run1"
    assert entry["error"] == "delete failed: 403 forbidden"
    assert "timestamp" in entry


def test_record_orphan_branch_appends_multiple_entries(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "orphan-branches.jsonl"
    monkeypatch.setattr("tests.e2e.platforms._ORPHAN_LOG_PATH", log_path)
    _record_orphan_branch("github", "e2e/run1", "err1")
    _record_orphan_branch("gitlab", "e2e/run2", "err2")
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["branch"] == "e2e/run1"
    assert json.loads(lines[1])["branch"] == "e2e/run2"


def test_record_orphan_branch_creates_parent_directory(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "nested" / "orphan-branches.jsonl"
    monkeypatch.setattr("tests.e2e.platforms._ORPHAN_LOG_PATH", log_path)
    _record_orphan_branch("bitbucket", "e2e/run3", "err")  # must not raise
    assert log_path.exists()


# --- _delete_branch_best_effort ---------------------------------------------------

def test_delete_branch_best_effort_records_orphan_on_failure(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "orphan-branches.jsonl"
    monkeypatch.setattr("tests.e2e.platforms._ORPHAN_LOG_PATH", log_path)
    adapter = _FailingDeleteAdapter(_config("github"))
    adapter._delete_branch_best_effort("e2e/orphan1")  # must not raise
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["platform"] == "github"
    assert entry["branch"] == "e2e/orphan1"


def test_delete_branch_best_effort_does_not_record_on_success(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "orphan-branches.jsonl"
    monkeypatch.setattr("tests.e2e.platforms._ORPHAN_LOG_PATH", log_path)
    adapter = _SucceedingDeleteAdapter(_config("github"))
    adapter._delete_branch_best_effort("e2e/fine1")
    assert adapter.deleted == ["e2e/fine1"]
    assert not log_path.exists()
