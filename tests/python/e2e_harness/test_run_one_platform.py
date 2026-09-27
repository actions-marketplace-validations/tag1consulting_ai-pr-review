"""Unit tests for _run_one_platform's opened.json resolution control flow
(issue #959): the leave_genuinely_unresolved/finally logic added across
PR #954's F29/F33/F34 fixes. This is the part most likely to silently
regress -- it decides whether a run's opened.json entry ends up "closed",
"left_open", or genuinely unresolved, which in turn decides whether
e2e.yml's cancel-on-signal fallback step (or a manual `cleanup` run) is
allowed to touch that PR/MR.

`build_adapter`, `_clone_workspace`, and `_run_container` are all stubbed:
no network calls, no real container run, no real credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_pr_review.vcs.marker import build_summary_marker
from tests.e2e.config import DEFAULT_MODELS, PlatformConfig
from tests.e2e.models import AdapterError, PullRequest, RawEvidence, RunCommit
from tests.e2e.run_e2e import _opened_prs, _run_one_platform

_COMMIT_SHA = "deadbeef1234"
_MARKER = build_summary_marker(_COMMIT_SHA)


class _StubAdapter:
    """A fully-scripted adapter: every method's behavior for one test is
    set via constructor flags, so each test only turns on the one failure
    mode it's exercising."""

    def __init__(
        self, *, config: PlatformConfig,
        open_pr_fails: bool = False,
        close_fails: bool = False,
        delete_branch_fails: bool = False,
    ) -> None:
        self.config = config
        self._open_pr_fails = open_pr_fails
        self._close_fails = close_fails
        self._delete_branch_fails = delete_branch_fails
        self.closed = False
        self.deleted_branch: str | None = None

    def preflight(self) -> None:
        return

    def create_run_commit(self, run_id: str) -> RunCommit:
        return RunCommit(platform=self.config.name, run_id=run_id, branch=f"e2e/{run_id}", commit_sha=_COMMIT_SHA)

    def open_pr(self, run_commit: RunCommit) -> PullRequest:
        if self._open_pr_fails:
            raise AdapterError("open_pr failed")
        return PullRequest(
            platform=self.config.name, number=1, url="https://example.invalid/1",
            branch=run_commit.branch, run_commit=run_commit,
        )

    def fetch_summary(self, pr: PullRequest) -> RawEvidence:
        return RawEvidence(summary_body=_MARKER)

    def fetch_inline(self, pr: PullRequest, evidence: RawEvidence) -> None:
        evidence.inline_comments.append({"body": "fake finding"})

    def fetch_annotations(self, pr: PullRequest, evidence: RawEvidence) -> None:
        return

    def close(self, pr: PullRequest) -> None:
        if self._close_fails:
            raise AdapterError("close failed")
        self.closed = True

    def delete_branch(self, branch: str) -> None:
        if self._delete_branch_fails:
            raise AdapterError("delete_branch failed")
        self.deleted_branch = branch


def _platform_config() -> PlatformConfig:
    return PlatformConfig(
        name="testplat", repo_slug="x/y", base_ref="main", seed_sha="deadbeef",
        findings_floor=0, expected_findings=(), per_finding_surface="inline",
    )


def _write_telemetry(platform_out_dir: Path, *, outcome: str = "APPROVE") -> None:
    platform_out_dir.mkdir(parents=True, exist_ok=True)
    telemetry = {
        "outcome": outcome, "findings_count": 0, "findings_by_severity": {},
        "failed_agents": [], "token_usage_by_agent": {}, "provider": "anthropic",
        "model_standard": DEFAULT_MODELS["anthropic"][0],
        "model_premium": DEFAULT_MODELS["anthropic"][1], "review_mode": "quick",
    }
    (platform_out_dir / "telemetry.json").write_text(json.dumps(telemetry), encoding="utf-8")


def _stub_run_container(*, returncode: int = 0, event: str = "APPROVE", telemetry_outcome: str = "APPROVE"):
    """Builds a _run_container replacement matching its real signature
    (platform, pr, workspace, diff_base_sha, out_dir, *, mode, max_cost_usd)
    -- writes telemetry.json to out_dir (the same platform_out_dir the real
    container writes it to) and returns a "Review complete: ..." stderr
    line matching parse_review_log_line's expected format."""
    def _run(name, pr, workspace, diff_base_sha, out_dir, *, mode, max_cost_usd):  # noqa: ANN001, ANN202
        _write_telemetry(out_dir, outcome=telemetry_outcome)
        stderr = f"Review complete: 0 findings, 0 failed agents, event={event}, base=abc123"
        return returncode, stderr
    return _run


@pytest.fixture(autouse=True)
def _clean_opened_prs():
    _opened_prs.clear()
    yield
    _opened_prs.clear()


def _install_stubs(monkeypatch, tmp_path: Path, stub: _StubAdapter, config: PlatformConfig, *, run_container=None) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("tests.e2e.run_e2e.build_adapter", lambda name, env: stub)
    monkeypatch.setattr("tests.e2e.run_e2e.PLATFORMS", {"testplat": config})
    monkeypatch.setattr("tests.e2e.run_e2e._clone_workspace", lambda name, run_commit, base_ref: (workspace, "basesha123"))
    if run_container is not None:
        monkeypatch.setattr("tests.e2e.run_e2e._run_container", run_container)
    return workspace


def _entry(out_dir: Path, platform: str = "testplat") -> dict[str, object]:
    entries = json.loads((out_dir / "opened.json").read_text(encoding="utf-8"))
    matches = [e for e in entries if e["platform"] == platform]
    assert len(matches) == 1
    return matches[0]


def test_pass_path_resolves_closed(monkeypatch, tmp_path: Path):
    config = _platform_config()
    stub = _StubAdapter(config=config)
    _install_stubs(monkeypatch, tmp_path, stub, config, run_container=_stub_run_container())

    result = _run_one_platform("testplat", "run1", tmp_path, mode="quick", max_cost_usd=2.0)

    assert result.ok, result.reasons
    assert stub.closed
    assert stub.deleted_branch == "e2e/run1"
    entry = _entry(tmp_path)
    assert entry["resolution"] == "closed"


def test_verdict_failure_resolves_left_open(monkeypatch, tmp_path: Path):
    config = _platform_config()
    stub = _StubAdapter(config=config)
    # Telemetry's own intent was APPROVE, but the posted event was COMMENT
    # -- a real self-approval degrade, which verify_event_not_degraded must
    # fail. A genuine product failure, not an infra one.
    _install_stubs(monkeypatch, tmp_path, stub, config,
                    run_container=_stub_run_container(event="COMMENT", telemetry_outcome="APPROVE"))

    result = _run_one_platform("testplat", "run1", tmp_path, mode="quick", max_cost_usd=2.0)

    assert not result.ok
    assert not stub.closed
    entry = _entry(tmp_path)
    assert entry["resolution"] == "left_open"


def test_unexpected_exception_resolves_left_open(monkeypatch, tmp_path: Path):
    config = _platform_config()
    stub = _StubAdapter(config=config)
    _install_stubs(monkeypatch, tmp_path, stub, config)

    def _boom(name, run_commit, base_ref):  # noqa: ANN001, ANN202
        raise KeyError("malformed API response")

    monkeypatch.setattr("tests.e2e.run_e2e._clone_workspace", _boom)

    with pytest.raises(KeyError):
        _run_one_platform("testplat", "run1", tmp_path, mode="quick", max_cost_usd=2.0)

    entry = _entry(tmp_path)
    assert entry["resolution"] == "left_open"


def test_system_exit_leaves_entry_genuinely_unresolved(monkeypatch, tmp_path: Path):
    # Simulates the in-process signal handler interrupting mid-run and
    # calling sys.exit() -- SystemExit must NOT be stamped "left_open" by
    # the finally, since that would tell e2e.yml's `if: cancelled()`
    # cleanup fallback to skip an entry whose in-process cleanup attempt
    # may have failed or not finished (see run_e2e.py's own comment on
    # this except clause).
    config = _platform_config()
    stub = _StubAdapter(config=config)
    _install_stubs(monkeypatch, tmp_path, stub, config)

    def _signal_exit(name, run_commit, base_ref):  # noqa: ANN001, ANN202
        raise SystemExit(2)

    monkeypatch.setattr("tests.e2e.run_e2e._clone_workspace", _signal_exit)

    with pytest.raises(SystemExit):
        _run_one_platform("testplat", "run1", tmp_path, mode="quick", max_cost_usd=2.0)

    entry = _entry(tmp_path)
    assert "resolution" not in entry


def test_cleanup_failure_after_pass_leaves_entry_genuinely_unresolved(monkeypatch, tmp_path: Path):
    # A close()/delete_branch() failure on the pass path must be left
    # genuinely unresolved (neither "closed" nor "left_open"), so `cleanup`
    # still retries it -- "left_open" would tell cleanup to skip it.
    config = _platform_config()
    stub = _StubAdapter(config=config, close_fails=True)
    _install_stubs(monkeypatch, tmp_path, stub, config, run_container=_stub_run_container())

    result = _run_one_platform("testplat", "run1", tmp_path, mode="quick", max_cost_usd=2.0)

    assert not result.ok
    assert result.category == "infra_failure"
    entry = _entry(tmp_path)
    assert "resolution" not in entry
