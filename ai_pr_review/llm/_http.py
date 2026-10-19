"""Shared httpx retry helper for all LLM providers.

Retries with exponential back-off and jitter on transient HTTP codes (408, 429,
500, 502, 503, 504, 520–524) and transient httpx exceptions (ConnectError,
TimeoutException, NetworkError).
"""

from __future__ import annotations

import logging
import random
from typing import Any

import anyio
import httpx

logger = logging.getLogger(__name__)

# HTTP status codes worth retrying.
_TRANSIENT_HTTP: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})

# httpx exception types considered transient.
_TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class LLMError(Exception):
    """Permanent LLM error (bad API key, invalid request, unknown provider)."""


class LLMContextLengthError(LLMError):
    """The provider rejected the prompt as longer than the model context window.

    A permanent error (a retry sends the same prompt), kept as an `LLMError`
    subclass so a caller that does not know about it still sees a permanent
    failure. `llm/client.py` maps it to exit code 4.
    """


class LLMTransientError(Exception):
    """Transient LLM error — all retries exhausted on 429/5xx/timeout."""


class LLMContentError(Exception):
    """Response blocked by provider safety/recitation filter."""


# Provider 400 texts that mean "the prompt is longer than the context window".
# Matched in lower case against the response body. Only Anthropic is covered:
# the text "prompt is too long" was observed in a real run (issue #1056, for
# example "prompt is too long: 4128365 tokens > 1000000 maximum"). The OpenAI,
# Google and Bedrock texts are not matched, because the provider docs do not
# state the exact 400 message. A 400 with any other text keeps the permanent
# `LLMError` behavior. Add a text here only after you see it in a real response.
_CONTEXT_LENGTH_MARKERS: tuple[str, ...] = ("prompt is too long",)


def is_context_length_rejection(status_code: int, body: str) -> bool:
    """True when a non-2xx response is a verified "prompt too long" 400."""
    if status_code != 400:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _CONTEXT_LENGTH_MARKERS)


async def retry_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json: Any,
    provider_label: str,
    retry_count: int,
    retry_base_delay: int,
) -> httpx.Response:
    """POST with exponential back-off retry on transient errors.

    Returns the successful Response. Raises LLMTransientError if all retries
    are exhausted, LLMContextLengthError on a verified "prompt too long" 400, and
    LLMError on other permanent failures.
    """
    attempt = 0

    while True:
        try:
            response = await client.post(url, headers=headers, json=json)
        except _TRANSIENT_EXCEPTIONS as exc:
            if attempt < retry_count:
                attempt += 1
                delay = retry_base_delay * (2 ** (attempt - 1))
                jitter = random.random()
                logger.warning(
                    "%s request failed (%s: %s), retrying in %.1fs (attempt %d/%d)...",
                    provider_label, type(exc).__name__, exc, delay + jitter, attempt, retry_count,
                )
                await anyio.sleep(delay + jitter)
                continue
            raise LLMTransientError(
                f"{provider_label} request failed after {retry_count} retries: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{provider_label} HTTP error: {exc}") from exc

        if response.status_code in _TRANSIENT_HTTP:
            if attempt < retry_count:
                attempt += 1
                delay = retry_base_delay * (2 ** (attempt - 1))
                jitter = random.random()
                logger.warning(
                    "%s returned HTTP %d (%s), retrying in %.1fs (attempt %d/%d)...",
                    provider_label, response.status_code,
                    response.text[:200] if response.content else "",
                    delay + jitter, attempt, retry_count,
                )
                await anyio.sleep(delay + jitter)
                continue
            # Keep the provider's error body (as the non-retried path below
            # does): an exhausted OpenAI or Gemini key is a 429, and the only
            # thing that says "quota" and not "rate limit" is in the body.
            # Without it the live model canary cannot tell a billing block
            # from a model regression.
            body = response.text[:500] if response.content else ""
            raise LLMTransientError(
                f"{provider_label} returned HTTP {response.status_code} after "
                f"{retry_count} retries" + (f": {body}" if body else "")
            )

        if response.status_code < 200 or response.status_code >= 300:
            logger.error("%s API returned HTTP %d", provider_label, response.status_code)
            try:
                logger.warning("%s error response body: %s", provider_label, response.text[:500])
            except Exception as log_exc:
                logger.warning(
                    "could not read error response body: %s; raw bytes: %r",
                    log_exc, response.content[:200],
                )
            error_cls = (
                LLMContextLengthError
                if is_context_length_rejection(response.status_code, response.text)
                else LLMError
            )
            raise error_cls(
                f"{provider_label} returned HTTP {response.status_code}: {response.text[:500]}"
            )

        return response
