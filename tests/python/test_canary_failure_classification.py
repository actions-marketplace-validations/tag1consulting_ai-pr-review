"""Tests for tests/canary/live_model_canary.py's failure classification.

Filed alongside the #636 fix: the live-model canary's GitHub issue body used to
unconditionally claim every failure was "the same class as #592" (a model-behavior
regression). #636 itself was an Anthropic workspace API usage limit, not a model
bug, and the hardcoded text sent whoever triaged it chasing the wrong root cause.
These tests cover the classification and output-writing logic that now lets the
workflow tell the two apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/ has no __init__.py, so tests.canary isn't importable as a regular
# package by name alone -- add the repo root to sys.path first, same as
# live_model_canary.py does for its own ai_pr_review imports.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

import tests.canary.live_model_canary as canary  # noqa: E402
from ai_pr_review.agents.dispatch import AgentResult, TokenUsage  # noqa: E402
from tests.canary.live_model_canary import (  # noqa: E402
    _CLEAN_STOP_REASONS,
    PROVIDER_MODELS,
    CanaryResult,
    _findings_block_error,
    _is_quota_error,
    _save_output,
    _write_github_output,
)


def _parse_github_output_multiline(content: str, key: str) -> str:
    """Parse a `key<<DELIM\\n...\\nDELIM\\n` block the way GitHub Actions does.

    Mirrors the runner's actual behavior: the value ends at the first line that
    matches the opening delimiter verbatim, whatever that delimiter is. Raises
    AssertionError if the key isn't present or the block is malformed, so a
    delimiter collision (the F2 finding this guards against) fails loudly
    instead of silently truncating.
    """
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}<<"):
            delimiter = line[len(f"{key}<<") :]
            for j in range(i + 1, len(lines)):
                if lines[j] == delimiter:
                    return "\n".join(lines[i + 1 : j])
            raise AssertionError(f"no closing delimiter {delimiter!r} found for key {key!r}")
    raise AssertionError(f"key {key!r} not found in GITHUB_OUTPUT content")


class TestIsQuotaError:
    def test_workspace_usage_limit_is_quota(self) -> None:
        detail = (
            "agent failed (exit_code=1): SystemExit: 1 | caused by LLMError: "
            'Anthropic returned HTTP 400: {"type":"error","error":{"type":'
            '"invalid_request_error","message":"You have reached your specified '
            'workspace API usage limits. You will regain access on 2026-08-01 '
            'at 00:00 UTC."}}'
        )
        assert _is_quota_error(detail)

    def test_rate_limit_error_is_quota(self) -> None:
        assert _is_quota_error("rate_limit_error: too many requests")

    def test_low_credit_balance_is_quota(self) -> None:
        assert _is_quota_error("Your credit balance is too low to access the API.")

    def test_stop_reason_anomaly_is_not_quota(self) -> None:
        # The actual #592 shape: a real model-behavior surprise, not a billing block.
        detail = "stop_reason='max_tokens' (expected end_turn), thinking_tokens=16384, output_tokens=0"
        assert not _is_quota_error(detail)

    def test_unexpected_exception_is_not_quota(self) -> None:
        assert not _is_quota_error("run_tier raised: TimeoutError()")

    def test_case_insensitive(self) -> None:
        assert _is_quota_error("USAGE LIMIT reached")

    def test_openai_insufficient_quota_is_quota(self) -> None:
        detail = (
            "agent failed (exit_code=1): SystemExit: 1 | caused by LLMTransientError: OpenAI "
            'returned HTTP 429 after 3 retries: {"error": {"message": "You exceeded your '
            'current quota, please check your plan and billing details.", '
            '"type": "insufficient_quota"}}'
        )
        assert _is_quota_error(detail)

    def test_gemini_resource_exhausted_is_quota(self) -> None:
        detail = (
            "agent failed (exit_code=1): SystemExit: 1 | caused by LLMTransientError: Google "
            'returned HTTP 429 after 3 retries: {"error": {"code": 429, "message": '
            '"Quota exceeded.", "status": "RESOURCE_EXHAUSTED"}}'
        )
        assert _is_quota_error(detail)

    def test_missing_findings_block_is_not_quota(self) -> None:
        assert not _is_quota_error("stop_reason=stop but no json-findings block, output_tokens=12")


class TestWriteGithubOutput:
    def test_noop_without_github_output_env(self, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        # Must not raise even with failures present and no env var set.
        _write_github_output([CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, "usage limit")])

    def test_noop_with_no_failures(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        _write_github_output([])
        assert out_path.read_text() == ""

    def test_all_quota_exhausted_true(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        failed = [
            CanaryResult("anthropic", "claude-opus-4-8", "code-reviewer", False, "usage limit reached"),
            CanaryResult("anthropic", "claude-sonnet-5", "silent-failure-hunter", False, "usage limit reached"),
        ]
        _write_github_output(failed)
        content = out_path.read_text()
        assert "all_quota_exhausted=true" in content
        detail_value = _parse_github_output_multiline(content, "failure_detail")
        assert "anthropic/claude-opus-4-8/code-reviewer: usage limit reached" in detail_value

    def test_delimiter_is_random_per_call(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        result = [CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, "usage limit")]

        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        _write_github_output(result)
        first_delimiter = next(
            line[len("failure_detail<<") :]
            for line in out_path.read_text().splitlines()
            if line.startswith("failure_detail<<")
        )

        out_path.write_text("")
        _write_github_output(result)
        second_delimiter = next(
            line[len("failure_detail<<") :]
            for line in out_path.read_text().splitlines()
            if line.startswith("failure_detail<<")
        )

        assert first_delimiter != second_delimiter

    def test_failure_detail_containing_old_fixed_delimiter_does_not_truncate(self, tmp_path, monkeypatch) -> None:
        # F2 regression: a fixed delimiter could appear inside provider error
        # text/tracebacks and silently truncate the value. Embed the literal
        # string this script used to hardcode as the delimiter, inside the
        # failure detail itself, and confirm parsing still recovers it whole
        # rather than cutting off at that line.
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        poisoned_detail = "anthropic/claude-sonnet-5/code-reviewer: error containing CANARY_FAILURE_EOF verbatim"
        _write_github_output([CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, poisoned_detail)])

        detail_value = _parse_github_output_multiline(out_path.read_text(), "failure_detail")
        assert poisoned_detail in detail_value

    def test_mixed_failures_not_all_quota(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        failed = [
            CanaryResult("anthropic", "claude-opus-4-8", "code-reviewer", False, "stop_reason='max_tokens'"),
            CanaryResult("anthropic", "claude-sonnet-5", "silent-failure-hunter", False, "usage limit reached"),
        ]
        _write_github_output(failed)
        assert "all_quota_exhausted=false" in out_path.read_text()

    def test_appends_rather_than_overwrites(self, tmp_path, monkeypatch) -> None:
        out_path = tmp_path / "gh_output"
        out_path.write_text("existing_output=1\n")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
        _write_github_output([CanaryResult("anthropic", "claude-sonnet-5", "code-reviewer", False, "usage limit")])
        content = out_path.read_text()
        assert content.startswith("existing_output=1\n")
        assert "all_quota_exhausted=true" in content


# ---------------------------------------------------------------------------
# Per-provider stop reasons, findings-block validation, saved output
# ---------------------------------------------------------------------------

_GOOD = "Review.\n```json-findings\n[]\n```\n"


def test_clean_stop_reasons_cover_every_canary_provider() -> None:
    assert set(_CLEAN_STOP_REASONS) == set(PROVIDER_MODELS)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (_GOOD, ""),
        ("No block at all.", "no json-findings block"),
        ("```json-findings\n[{\"a\": 1,\n```\n", "not valid JSON"),
        ("```json-findings\n{\"a\": 1}\n```\n", "not a JSON array"),
    ],
)
def test_findings_block_error(output: str, expected: str) -> None:
    error = _findings_block_error(output)
    if expected:
        assert expected in error
    else:
        assert error == ""


def _stub_run_tier(monkeypatch: pytest.MonkeyPatch, *, output: str, stop_reason: str) -> None:
    result = AgentResult(
        name="code-reviewer",
        output=output,
        token_log=TokenUsage(input=10, output=20, cache_creation=0, cache_read=0, model="m"),
        truncated=False,
        stop_reason=stop_reason,
    )

    async def fake_run_tier(*_args: object, **_kwargs: object) -> tuple[list[AgentResult], list[object]]:
        return [result], []

    monkeypatch.setattr(canary, "run_tier", fake_run_tier)


@pytest.mark.parametrize(
    ("provider", "stop_reason", "ok"),
    [
        ("openai", "stop", True),
        ("openai", "length", False),
        ("google", "STOP", True),
        ("google", "MAX_TOKENS", False),
        ("anthropic", "end_turn", True),
        ("anthropic", "max_tokens", False),
    ],
)
def test_run_one_checks_each_providers_own_stop_reason(
    monkeypatch: pytest.MonkeyPatch, provider: str, stop_reason: str, ok: bool
) -> None:
    import asyncio

    monkeypatch.delenv("CANARY_OUTPUT_DIR", raising=False)
    _stub_run_tier(monkeypatch, output=_GOOD, stop_reason=stop_reason)
    result = asyncio.run(canary._run_one(provider, "m", "code-reviewer"))
    assert result.ok is ok


def test_run_one_fails_clean_stop_with_malformed_block(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.delenv("CANARY_OUTPUT_DIR", raising=False)
    _stub_run_tier(monkeypatch, output="```json-findings\n{}\n```\n", stop_reason="stop")
    result = asyncio.run(canary._run_one("openai", "m", "code-reviewer"))
    assert result.ok is False
    assert "not a JSON array" in result.detail


def test_save_output_writes_flat_file_and_is_noop_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CANARY_OUTPUT_DIR", raising=False)
    _save_output("google", "models/x", "code-reviewer", "text")
    assert list(tmp_path.iterdir()) == []

    out = tmp_path / "out"
    monkeypatch.setenv("CANARY_OUTPUT_DIR", str(out))
    _save_output("google", "models/x", "code-reviewer", "text")
    files = list(out.iterdir())
    assert [f.name for f in files] == ["google__models_x__code-reviewer.md"]
    assert files[0].read_text() == "text"


def test_save_output_write_error_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("CANARY_OUTPUT_DIR", str(blocker / "sub"))
    _save_output("openai", "m", "a", "text")
    assert "could not save canary output" in capsys.readouterr().err


class TestQuotaClassificationOfRealRetryErrors:
    """The markers only help if the exception text the retry layer really
    produces contains them. An exhausted OpenAI or Gemini key is a 429, and
    429 is retried, so the canary sees LLMTransientError, never the body of
    the non-retried LLMError path. These tests build the exception text
    through the real client instead of writing the string by hand."""

    @staticmethod
    async def _exhausted(provider: str, env_var: str, url: str, body: str, monkeypatch) -> str:
        import httpx
        import respx

        from ai_pr_review.agents.dispatch import _format_exception_chain
        from ai_pr_review.llm import call_llm
        from tests.python.llm.conftest import make_request

        monkeypatch.setenv(env_var, "test-key")
        monkeypatch.setenv("LLM_RETRY_COUNT", "1")
        monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")
        with respx.mock:
            respx.post(url).mock(return_value=httpx.Response(429, text=body))
            with pytest.raises(SystemExit) as exc:
                await call_llm(make_request(model_id=PROVIDER_MODELS[provider][1]), provider)
        # The same rendering the dispatcher gives the canary as a failure detail.
        return _format_exception_chain(exc.value)

    @pytest.mark.anyio
    async def test_openai_exhausted_key_is_classified_as_quota(self, monkeypatch) -> None:
        detail = await self._exhausted(
            "openai", "OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions",
            '{"error": {"message": "You exceeded your current quota, please check your '
            'plan and billing details.", "type": "insufficient_quota"}}',
            monkeypatch,
        )
        assert _is_quota_error(detail)

    @pytest.mark.anyio
    async def test_a_429_without_a_body_is_not_misread_as_quota(self, monkeypatch) -> None:
        detail = await self._exhausted(
            "openai", "OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions",
            "", monkeypatch,
        )
        assert "HTTP 429" in detail
        assert not _is_quota_error(detail)
