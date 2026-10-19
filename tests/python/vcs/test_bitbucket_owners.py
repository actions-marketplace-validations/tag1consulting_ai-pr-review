"""Verdict owners from `.bitbucket/CODEOWNERS` (the owners-file path of the
Bitbucket verdict poller). Parser, loader, and the wiring into
`apply_pending_verdicts` through `BitbucketProvider.post_findings`.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from ai_pr_review.vcs import _bitbucket_owners as owners_mod
from ai_pr_review.vcs._bitbucket_owners import (
    OWNERS_PATH,
    OwnersLoad,
    VerdictOwners,
    load_verdict_owners,
    parse_owners,
)
from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import extract_acks, extract_verdicts
from ai_pr_review.vcs.protocol import DiffContext
from tests.python.vcs.test_bitbucket_verdicts import (
    _DIFF,
    _FINDING,
    _FP,
    _HEAD,
    _build_handler,
    _command_comment,
    _summary_body,
    _summary_comment,
)

_ACCOUNT_ID = "712020:589217f7-409e-4b3c-b1ba-d323ee2022ed"
_UUID = "{9f49742f-b2a6-491a-84aa-6bbf3a935756}"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_reads_nicknames_account_ids_and_uuids_from_star_lines() -> None:
    owners = parse_owners(
        "# who may dismiss findings\n"
        f"* @Alice {_ACCOUNT_ID} {_UUID} 61b8f771ef5b4600710cf39f\n"
    )
    assert owners.nicknames == frozenset({"alice"})
    assert owners.ids == frozenset(
        {
            _ACCOUNT_ID.lower(),
            "9f49742f-b2a6-491a-84aa-6bbf3a935756",
            "61b8f771ef5b4600710cf39f",
        }
    )
    assert owners.ignored_owners == ()


def test_parse_ignores_path_scoped_lines_and_counts_them() -> None:
    owners = parse_owners("*.py @alice\ndocs/ @bob\n* @carol\n")
    assert owners.nicknames == frozenset({"carol"})
    assert owners.ignored_lines == 2


def test_parse_ignores_emails_groups_and_teams_but_records_them() -> None:
    owners = parse_owners("* a@example.com ws/group @teams/core @dave\n")
    assert owners.nicknames == frozenset({"dave"})
    assert owners.ids == frozenset()
    assert set(owners.ignored_owners) == {"a@example.com", "ws/group", "@teams/core"}


def test_parse_strips_inline_comments_and_blank_lines() -> None:
    owners = parse_owners("\n   \n* @alice  # the lead\n\n#* @mallory\n")
    assert owners.nicknames == frozenset({"alice"})


def test_parse_line_with_a_pattern_and_no_owner_is_skipped() -> None:
    assert parse_owners("*\n") == VerdictOwners()


def test_parse_bare_at_sign_is_ignored() -> None:
    owners = parse_owners("* @ @alice\n")
    assert owners.nicknames == frozenset({"alice"})
    assert owners.ignored_owners == ("@",)


def test_parse_empty_text() -> None:
    assert parse_owners("") == VerdictOwners()


@pytest.mark.parametrize("sep", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_parse_does_not_split_a_line_on_a_hidden_line_separator(sep: str) -> None:
    """str.splitlines() splits on these, Bitbucket and a diff viewer do not.
    A path-scoped line must not turn into a global one that nobody sees."""
    owners = parse_owners(f"docs/ @alice{sep}* @mallory\n* @carol\n")
    assert owners.nicknames == frozenset({"carol"})
    assert owners.ignored_lines == 1


def test_parse_accepts_crlf_tabs_and_a_leading_bom() -> None:
    owners = parse_owners("\ufeff# header\r\n*\t@alice\t@bob\r\n")
    assert owners.nicknames == frozenset({"alice", "bob"})


def test_parse_ignores_non_ascii_nicknames() -> None:
    owners = parse_owners("* @J\u00f6rg @alice\n")
    assert owners.nicknames == frozenset({"alice"})
    assert owners.ignored_owners == ("@J\u00f6rg",)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_matches_account_id_case_insensitively() -> None:
    owners = parse_owners(f"* {_ACCOUNT_ID}\n")
    assert owners.matches(account_id=_ACCOUNT_ID.upper(), uuid=None, nickname=None) == "id"


def test_matches_uuid_with_or_without_braces() -> None:
    owners = parse_owners(f"* {_UUID}\n")
    assert owners.matches(account_id="x", uuid=_UUID, nickname=None) == "id"
    assert owners.matches(account_id="x", uuid=_UUID.strip("{}").upper(), nickname=None) == "id"


def test_matches_nickname_case_insensitively() -> None:
    owners = parse_owners("* @Alice\n")
    assert owners.matches(account_id="x", uuid=None, nickname="ALICE") == "nickname"


def test_an_id_match_wins_over_a_nickname_match() -> None:
    owners = parse_owners(f"* @alice {_ACCOUNT_ID}\n")
    assert owners.matches(account_id=_ACCOUNT_ID, uuid=None, nickname="alice") == "id"


def test_no_match() -> None:
    owners = parse_owners("* @alice\n")
    assert owners.matches(account_id="x", uuid="{nope}", nickname="mallory") is None
    assert owners.matches(account_id=None, uuid=None, nickname=None) is None


@pytest.mark.parametrize(
    ("owner", "commenter"),
    [("kate", "\u212aate"), ("fiona", "\ufb01ona"), ("strasse", "Stra\u00dfe")],
)
def test_a_look_alike_unicode_nickname_does_not_match(owner: str, commenter: str) -> None:
    """casefold() would make these compare equal. ASCII-only matching must not."""
    owners = parse_owners(f"* @{owner}\n")
    assert owners.matches(account_id="x", uuid=None, nickname=commenter) is None


def test_a_nickname_does_not_match_as_a_substring() -> None:
    owners = parse_owners("* @alice\n")
    assert owners.matches(account_id="x", uuid=None, nickname="alice2") is None
    assert owners.matches(account_id="x", uuid=None, nickname="malice") is None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _fake_reader(files: dict[str, str | None]) -> Callable[..., str | None]:
    def reader(path: str, *, workspace: str, base_ref: str, source: str = "base-ref") -> str | None:
        return files.get(source)

    return reader


def test_load_base_ref_source_reads_once_and_has_no_pr_only_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def reader(path: str, *, workspace: str, base_ref: str, source: str = "base-ref") -> str | None:
        calls.append(source)
        return "* @alice\n"

    monkeypatch.setattr(owners_mod, "read_repo_file", reader)
    load = load_verdict_owners(workspace=".", base_ref="develop", source="base-ref")
    assert calls == ["base-ref"]
    assert load.owners is not None and load.owners.nicknames == frozenset({"alice"})
    assert not load.granted_only_by_this_pr(account_id="x", uuid=None, nickname="alice")


def test_load_workspace_source_flags_a_grant_that_only_this_pr_makes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        owners_mod, "read_repo_file",
        _fake_reader({"workspace": "* @alice @mallory\n", "base-ref": "* @alice\n"}),
    )
    load = load_verdict_owners(workspace=".", base_ref="develop", source="workspace")
    assert load.granted_only_by_this_pr(account_id="x", uuid=None, nickname="mallory")
    assert not load.granted_only_by_this_pr(account_id="x", uuid=None, nickname="alice")
    # Someone the file does not list is not "granted" at all.
    assert not load.granted_only_by_this_pr(account_id="x", uuid=None, nickname="eve")


def test_load_workspace_source_with_no_file_on_the_target_branch_is_a_pr_only_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        owners_mod, "read_repo_file", _fake_reader({"workspace": "* @mallory\n", "base-ref": None})
    )
    load = load_verdict_owners(workspace=".", base_ref="develop", source="workspace")
    assert load.base_owners is None
    assert load.granted_only_by_this_pr(account_id="x", uuid=None, nickname="mallory")


def test_load_missing_file_yields_no_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(owners_mod, "read_repo_file", _fake_reader({}))
    load = load_verdict_owners(workspace=".", base_ref="develop", source="base-ref")
    assert load.owners is None


def test_load_unknown_source_falls_back_to_base_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def reader(path: str, *, workspace: str, base_ref: str, source: str = "base-ref") -> str | None:
        seen.append(source)
        return None

    monkeypatch.setattr(owners_mod, "read_repo_file", reader)
    load = load_verdict_owners(workspace=".", base_ref="develop", source="bogus")
    assert seen == ["base-ref"]
    assert load.source == "base-ref"


def test_load_oversized_file_is_ignored_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        owners_mod, "read_repo_file", _fake_reader({"base-ref": "* @alice\n" + "# x\n" * 40_000})
    )
    with caplog.at_level(logging.WARNING, logger=owners_mod.__name__):
        load = load_verdict_owners(workspace=".", base_ref="develop", source="base-ref")
    assert load.owners is None
    assert any("larger than" in m for m in caplog.messages)


def test_load_is_fail_soft_when_the_reader_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(path: str, *, workspace: str, base_ref: str, source: str = "base-ref") -> str | None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(owners_mod, "read_repo_file", boom)
    with caplog.at_level(logging.WARNING, logger=owners_mod.__name__):
        load = load_verdict_owners(workspace=".", base_ref="develop", source="base-ref")
    assert load.owners is None and load.base_owners is None
    assert any("disk on fire" in m for m in caplog.messages)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo, check=True, capture_output=True,
    )


def test_load_reads_the_real_file_from_the_target_branch_and_from_the_checkout(
    tmp_path: Path,
) -> None:
    """End to end through git: the destination branch is `origin/develop`, the
    checkout carries a changed file."""
    _git(tmp_path, "init", "-q", "-b", "develop")
    (tmp_path / ".bitbucket").mkdir()
    (tmp_path / OWNERS_PATH).write_text("* @alice\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "owners")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    (tmp_path / OWNERS_PATH).write_text("* @alice @mallory\n", encoding="utf-8")

    base = load_verdict_owners(workspace=str(tmp_path), base_ref="develop", source="base-ref")
    assert base.owners is not None and base.owners.nicknames == frozenset({"alice"})

    head = load_verdict_owners(workspace=str(tmp_path), base_ref="develop", source="workspace")
    assert head.owners is not None and head.owners.nicknames == frozenset({"alice", "mallory"})
    assert head.granted_only_by_this_pr(account_id="x", uuid=None, nickname="mallory")


def test_load_missing_file_in_a_real_repo_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q", "-b", "develop")
    (tmp_path / "README").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "x")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    load = load_verdict_owners(workspace=str(tmp_path), base_ref="develop", source="base-ref")
    assert load.owners is None
    assert "WARNING" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Wiring through BitbucketProvider.post_findings
# ---------------------------------------------------------------------------


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    monkeypatch: pytest.MonkeyPatch,
    *,
    owners: OwnersLoad | None,
    verdict_min_role: str = "write",
    loader_kwargs: list[dict] | None = None,
    owners_source: str = "base-ref",
    base_ref: str = "",
) -> BitbucketProvider:
    def loader(**kw: object) -> OwnersLoad | None:
        if loader_kwargs is not None:
            loader_kwargs.append(kw)
        return owners

    monkeypatch.setattr("ai_pr_review.vcs.bitbucket.load_verdict_owners", loader)
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bitbucket.org/2.0"
    )
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    return BitbucketProvider(
        config=BitbucketConfig(
            workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t",
            code_insights=True, verdicts=True, verdict_min_role=verdict_min_role,
            verdict_owners_source=owners_source, base_ref=base_ref,
        ),
        client=client,
    )


def _owners(text: str, *, base: str | None = None, source: str = "base-ref") -> OwnersLoad:
    return OwnersLoad(
        owners=parse_owners(text),
        base_owners=parse_owners(base if base is not None else text),
        source=source,
    )


def _with_permission_calls(
    inner: Callable[[httpx.Request], httpx.Response],
    *,
    forbidden: bool,
    calls: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/permissions/repositories/" in str(req.url):
            calls.append(str(req.url))
            if forbidden:
                return httpx.Response(403, text='{"error":{"message":"admin only"}}')
        return inner(req)

    return handler


def _post(prov: BitbucketProvider) -> object:
    return prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )


def _cmd(**user: str) -> dict:
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 not exploitable", account_id=_ACCOUNT_ID)
    cmd["user"].update(user)
    return cmd


def test_an_owner_listed_by_account_id_is_authorized_without_any_permission_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    replies: list[dict] = []
    put_bodies: list[str] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd()],
        permissions={}, replies=replies, put_bodies=put_bodies,
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=calls),
        monkeypatch, owners=_owners(f"* {_ACCOUNT_ID}\n"),
    )
    result = _post(prov)
    assert result.ok, result.error  # type: ignore[attr-defined]
    assert result.suppressed == 1  # type: ignore[attr-defined]
    assert calls == [], "an owner must not trigger the admin-only lookup"
    assert extract_verdicts(put_bodies[-1])[_FP] == "dismissed"
    assert "marked **F1**" in replies[0]["content"]["raw"]
    assert "Note: this pull request changes" not in replies[0]["content"]["raw"]


def test_an_owner_listed_by_nickname_is_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    put_bodies: list[str] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="Alice")],
        permissions={}, replies=[], put_bodies=put_bodies,
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=calls),
        monkeypatch, owners=_owners("* @alice\n"),
    )
    assert _post(prov).suppressed == 1  # type: ignore[attr-defined]
    assert calls == []


def test_a_commenter_who_is_not_listed_gets_the_forbidden_reply_and_no_verdict(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []
    replies: list[dict] = []
    put_bodies: list[str] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="mallory")],
        permissions={}, replies=replies, put_bodies=put_bodies,
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=calls),
        monkeypatch, owners=_owners("* @alice\n"),
    )
    with caplog.at_level(logging.WARNING):
        result = _post(prov)
    assert result.ok, result.error  # type: ignore[attr-defined]
    assert result.suppressed == 0  # type: ignore[attr-defined]
    assert len(calls) == 1, "a non-owner falls back to the permission lookup"
    assert _FP not in extract_verdicts(put_bodies[-1])
    assert len(replies) == 1
    text = replies[0]["content"]["raw"]
    assert OWNERS_PATH in text and "A retry does not help" in text
    assert "could not verify" not in text
    # The log line carries the response body so the 403 explains itself.
    assert any("403" in m and "admin only" in m for m in caplog.messages)


def test_a_forbidden_command_stays_unacked_and_applies_once_the_owner_file_lists_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run 1: not listed, lookup refused.
    put1: list[str] = []
    inner1 = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="alice")],
        permissions={}, replies=[], put_bodies=put1,
    )
    prov1 = _provider(
        _with_permission_calls(inner1, forbidden=True, calls=[]),
        monkeypatch, owners=None,
    )
    assert _post(prov1).suppressed == 0  # type: ignore[attr-defined]
    assert 200 not in extract_acks(put1[-1]), "a forbidden command must be retried"

    # Run 2: the owners file was merged. The same command now applies.
    put2: list[str] = []
    replies2: list[dict] = []
    inner2 = _build_handler(
        comments=[_summary_comment(100, put1[-1]), _cmd(nickname="alice")],
        permissions={}, replies=replies2, put_bodies=put2,
    )
    prov2 = _provider(
        _with_permission_calls(inner2, forbidden=True, calls=[]),
        monkeypatch, owners=_owners("* @alice\n"),
    )
    result = _post(prov2)
    assert result.suppressed == 1  # type: ignore[attr-defined]
    assert extract_verdicts(put2[-1])[_FP] == "dismissed"
    assert 200 in extract_acks(put2[-1])


def test_the_owners_file_is_ignored_when_the_minimum_role_is_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    replies: list[dict] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="alice")],
        permissions={}, replies=replies, put_bodies=[],
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=calls),
        monkeypatch, owners=_owners("* @alice\n"), verdict_min_role="admin",
    )
    assert _post(prov).suppressed == 0  # type: ignore[attr-defined]
    assert len(calls) == 1
    text = replies[0]["content"]["raw"]
    # The owners file is off, so the reply must not send the user to it.
    assert "AI_BITBUCKET_VERDICT_MIN_ROLE" in text and "Make the bot account an admin" in text
    assert OWNERS_PATH not in text


def test_without_an_owners_file_the_permission_lookup_still_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd()],
        permissions={_ACCOUNT_ID: "write"}, replies=[], put_bodies=[],
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=False, calls=calls), monkeypatch, owners=None
    )
    assert _post(prov).suppressed == 1  # type: ignore[attr-defined]
    assert len(calls) == 1


def test_a_commenter_listed_only_in_this_pr_is_authorized_with_a_notice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    replies: list[dict] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="mallory")],
        permissions={}, replies=replies, put_bodies=[],
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=[]),
        monkeypatch,
        owners=_owners("* @alice @mallory\n", base="* @alice\n", source="workspace"),
    )
    with caplog.at_level(logging.INFO):
        result = _post(prov)
    assert result.suppressed == 1  # type: ignore[attr-defined]
    text = replies[0]["content"]["raw"]
    assert "marked **F1**" in text
    assert f"changes `{OWNERS_PATH}`" in text and "AI_POLICY_SOURCE=base-ref" in text
    assert any("listed only in this pull request's file" in m for m in caplog.messages)


def test_a_commenter_listed_on_the_target_branch_gets_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: list[dict] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="alice")],
        permissions={}, replies=replies, put_bodies=[],
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=[]),
        monkeypatch,
        owners=_owners("* @alice @mallory\n", base="* @alice\n", source="workspace"),
    )
    assert _post(prov).suppressed == 1  # type: ignore[attr-defined]
    assert "Note: this pull request changes" not in replies[0]["content"]["raw"]


def test_an_owner_may_use_the_fixed_command_too(monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = _command_comment(200, "/ai-pr-review fixed F1 abc1234", account_id=_ACCOUNT_ID)
    put_bodies: list[str] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), cmd],
        permissions={}, replies=[], put_bodies=put_bodies,
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=[]),
        monkeypatch, owners=_owners(f"* {_ACCOUNT_ID}\n"),
    )
    _post(prov)
    assert extract_verdicts(put_bodies[-1])[_FP] == "fixed"


def test_a_forbidden_command_with_no_owners_file_says_the_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: list[dict] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="alice")],
        permissions={}, replies=replies, put_bodies=[],
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=[]), monkeypatch, owners=None
    )
    assert _post(prov).suppressed == 0  # type: ignore[attr-defined]
    text = replies[0]["content"]["raw"]
    assert "was not found or could not be read" in text
    assert "its token lacks the permission" in text


def test_the_three_forbidden_replies_use_three_distinct_dedup_keys() -> None:
    """The reply key hashes the context, so a changed situation (a file was
    added, but the commenter is not on it) gets a fresh reply."""
    from ai_pr_review.vcs._bitbucket_verdicts import _forbidden_context, _reply_for_authority

    listed = _owners("* @alice\n")
    assert _forbidden_context(listed, "write") == "not-listed"
    assert _forbidden_context(None, "write") == "no-file"
    assert _forbidden_context(OwnersLoad(None, None, "base-ref"), "write") == "no-file"
    assert _forbidden_context(listed, "admin") == "admin-only"
    texts = {_reply_for_authority("a", "forbidden", c) for c in ("not-listed", "no-file", "admin-only")}  # type: ignore[arg-type]
    assert len(texts) == 3


def test_the_provider_passes_its_config_to_the_owners_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    inner = _build_handler(
        comments=[_summary_comment(100, _summary_body()), _cmd(nickname="alice")],
        permissions={}, replies=[], put_bodies=[],
    )
    prov = _provider(
        _with_permission_calls(inner, forbidden=True, calls=[]),
        monkeypatch, owners=None, loader_kwargs=seen,
        owners_source="workspace", base_ref="develop",
    )
    _post(prov)
    assert seen == [{"workspace": ".", "base_ref": "develop", "source": "workspace"}]
