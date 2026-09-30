"""GitHubProvider tests: post_check_run (the ai-pr-review/policy-gate merge gate)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder


@dataclass
class _Recorder:
    calls: list[tuple[str, str, dict | None]] = field(default_factory=list)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[GitHubProvider, _Recorder]:
    rec = _Recorder()

    def _wrap(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                import json

                body = json.loads(request.content)
            except Exception:
                body = {"_raw": request.content.decode("utf-8", errors="replace")}
        rec.calls.append((request.method, str(request.url), body))
        return handler(request)

    transport = httpx.MockTransport(_wrap)
    http = httpx.Client(transport=transport, base_url="https://api.github.com")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    config = GitHubConfig(owner="o", repo="r", pr_number=7, token="t")
    return GitHubProvider(config=config, client=client), rec


def test_post_check_run_success_posts_correct_payload() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    provider, rec = _make_provider(handler)
    ok = provider.post_check_run(
        head_sha="abc1234",
        name="ai-pr-review/policy-gate",
        conclusion="success",
        title="'deep' review tier satisfied",
        summary="This run satisfies the 'deep' review tier.",
    )
    assert ok is True
    assert len(rec.calls) == 1
    method, url, body = rec.calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/o/r/check-runs"
    assert body == {
        "name": "ai-pr-review/policy-gate",
        "head_sha": "abc1234",
        "status": "completed",
        "conclusion": "success",
        "output": {
            "title": "'deep' review tier satisfied",
            "summary": "This run satisfies the 'deep' review tier.",
        },
    }


def test_post_check_run_action_required_conclusion() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    provider, rec = _make_provider(handler)
    provider.post_check_run(
        head_sha="abc1234",
        name="ai-pr-review/policy-gate",
        conclusion="action_required",
        title="'deep' review tier required",
        summary="Comment /ai-pr-review review-full to satisfy it.",
    )
    _, _, body = rec.calls[0]
    assert body is not None
    assert body["conclusion"] == "action_required"


def test_post_check_run_api_error_returns_false_and_records_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Resource not accessible by integration")

    provider, _rec = _make_provider(handler)
    ok = provider.post_check_run(
        head_sha="abc1234",
        name="ai-pr-review/policy-gate",
        conclusion="success",
        title="t",
        summary="s",
    )
    assert ok is False
    assert any("post_check_run" in e for e in provider._errors)


def test_list_check_run_conclusions_queries_all_runs_for_the_name() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 0, "check_runs": []})

    provider, rec = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "ai-pr-review/policy-gate") == []
    method, url, _ = rec.calls[0]
    assert method == "GET"
    parsed = httpx.URL(url)
    assert parsed.path == "/repos/o/r/commits/abc1234/check-runs"
    # filter=all is load-bearing: the default (latest) hides the earlier success.
    assert dict(parsed.params) == {
        "check_name": "ai-pr-review/policy-gate",
        "filter": "all",
        "per_page": "100",
    }


def test_list_check_run_conclusions_keeps_only_completed_github_actions_runs() -> None:
    def run(conclusion: str | None, slug: str, status: str = "completed") -> dict:
        return {"conclusion": conclusion, "status": status, "app": {"slug": slug}}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 4,
                "check_runs": [
                    run("success", "github-actions"),
                    run("success", "some-other-app"),
                    run(None, "github-actions", status="in_progress"),
                    run("action_required", "github-actions"),
                ],
            },
        )

    provider, _ = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "n") == ["success", "action_required"]


def test_list_check_run_conclusions_api_error_returns_none_and_records_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Resource not accessible by integration")

    provider, _ = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "n") is None
    assert any("list_check_run_conclusions" in e for e in provider._errors)


def test_list_check_run_conclusions_persistent_5xx_returns_none_instead_of_raising() -> None:
    # RecordingClient.request raises RetryExhaustedError after persistent
    # 429/5xx rather than returning the response, so a status-code check
    # alone would let it escape.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    provider, _ = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "n") is None
    assert any("list_check_run_conclusions" in e for e in provider._errors)


def test_list_check_run_conclusions_transport_error_returns_none_instead_of_raising() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider, _ = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "n") is None


def test_list_check_run_conclusions_non_object_json_returns_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    provider, _ = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "n") is None


def test_list_check_run_conclusions_invalid_json_returns_none() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    provider, _ = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "n") is None


def test_list_check_run_conclusions_tolerates_null_app_and_null_conclusion() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "check_runs": [
                    {"status": "completed", "conclusion": None, "app": {"slug": "github-actions"}},
                    {"status": "completed", "conclusion": "success", "app": None},
                ],
            },
        )

    provider, _ = _make_provider(handler)
    assert provider.list_check_run_conclusions("abc1234", "n") == [""]
