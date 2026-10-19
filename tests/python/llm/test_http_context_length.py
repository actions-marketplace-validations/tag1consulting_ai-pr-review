"""#1056: a verified provider "prompt is too long" 400 becomes exit code 4."""

from __future__ import annotations

import httpx
import pytest
import respx

from ai_pr_review.llm._http import (
    LLMContextLengthError,
    LLMError,
    is_context_length_rejection,
    retry_post,
)
from ai_pr_review.llm.base import LLMRequest
from ai_pr_review.llm.client import EXIT_PROMPT_TOO_LONG, call_llm

URL = "https://api.anthropic.com/v1/messages"

# Body shape of the Anthropic error seen in issue #1056.
PROMPT_TOO_LONG_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"prompt is too long: 4128365 tokens > 1000000 maximum"}}'
)
OTHER_400_BODY = (
    '{"type":"error","error":{"type":"invalid_request_error",'
    '"message":"max_tokens: 99999 > 64000, which is the maximum allowed"}}'
)


async def _post() -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await retry_post(
            client, URL, headers={}, json={}, provider_label="Anthropic",
            retry_count=0, retry_base_delay=0,
        )


class TestMatcher:
    def test_matches_a_400_with_the_anthropic_text(self) -> None:
        assert is_context_length_rejection(400, PROMPT_TOO_LONG_BODY) is True

    def test_match_ignores_case(self) -> None:
        assert is_context_length_rejection(400, "Prompt Is Too Long: 5 > 4") is True

    def test_other_400_text_does_not_match(self) -> None:
        assert is_context_length_rejection(400, OTHER_400_BODY) is False

    def test_same_text_with_another_status_does_not_match(self) -> None:
        assert is_context_length_rejection(413, PROMPT_TOO_LONG_BODY) is False
        assert is_context_length_rejection(401, PROMPT_TOO_LONG_BODY) is False

    def test_unverified_provider_texts_do_not_match(self) -> None:
        # OpenAI, Google and Bedrock texts are not confirmed, so they keep the
        # permanent LLMError behavior.
        assert not is_context_length_rejection(
            400, "This model's maximum context length is 128000 tokens."
        )
        assert not is_context_length_rejection(
            400, "The input token count exceeds the maximum number of tokens allowed"
        )
        assert not is_context_length_rejection(400, "Input is too long for requested model.")


class TestRetryPost:
    @pytest.mark.anyio
    async def test_raises_context_length_error_on_match(self) -> None:
        with respx.mock:
            route = respx.post(URL).mock(return_value=httpx.Response(400, text=PROMPT_TOO_LONG_BODY))
            with pytest.raises(LLMContextLengthError, match="prompt is too long"):
                await _post()
        assert route.call_count == 1  # permanent: never retried

    @pytest.mark.anyio
    async def test_context_length_error_is_an_llm_error(self) -> None:
        with respx.mock:
            respx.post(URL).mock(return_value=httpx.Response(400, text=PROMPT_TOO_LONG_BODY))
            with pytest.raises(LLMError):
                await _post()

    @pytest.mark.anyio
    async def test_other_400_stays_a_plain_llm_error(self) -> None:
        with respx.mock:
            respx.post(URL).mock(return_value=httpx.Response(400, text=OTHER_400_BODY))
            with pytest.raises(LLMError) as excinfo:
                await _post()
        assert not isinstance(excinfo.value, LLMContextLengthError)


class TestCallLlmExitCode:
    @pytest.mark.anyio
    async def test_prompt_too_long_exits_4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        req = LLMRequest(
            model_id="claude-sonnet-5-5", system_prompt="s", user_message="u", max_tokens=100,
        )
        with respx.mock:
            respx.post(URL).mock(return_value=httpx.Response(400, text=PROMPT_TOO_LONG_BODY))
            with pytest.raises(SystemExit) as excinfo:
                await call_llm(req, "anthropic")
        assert excinfo.value.code == EXIT_PROMPT_TOO_LONG == 4

    @pytest.mark.anyio
    async def test_other_400_still_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        req = LLMRequest(
            model_id="claude-sonnet-5-5", system_prompt="s", user_message="u", max_tokens=100,
        )
        with respx.mock:
            respx.post(URL).mock(return_value=httpx.Response(400, text=OTHER_400_BODY))
            with pytest.raises(SystemExit) as excinfo:
                await call_llm(req, "anthropic")
        assert excinfo.value.code == 1
