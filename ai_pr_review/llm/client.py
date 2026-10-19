"""Multi-provider LLM router.

Usage:
    response = asyncio.run(call_llm(request))
    response.emit_stderr(request.model_id)
    print(response.text)
"""

from __future__ import annotations

import sys

from ._config import resolve_prompt_caching
from ._http import LLMContentError, LLMContextLengthError, LLMError, LLMTransientError
from .base import LLMRequest, LLMResponse

# Exit code for a provider "prompt is too long" rejection (#1056). agents/dispatch.py
# and orchestrate.py read it from FailedAgent.exit_code.
EXIT_PROMPT_TOO_LONG = 4


async def call_llm(req: LLMRequest, provider: str) -> LLMResponse:
    """Dispatch to the correct provider and return an LLMResponse.

    Exit codes:
      SystemExit(1) — permanent error
      SystemExit(2) — transient (retries exhausted)
      SystemExit(3) — content filter
      SystemExit(4) - the provider rejected the prompt as too long (#1056)
    """
    try:
        return await _dispatch(req, provider)
    except LLMContentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
    except LLMContextLengthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_PROMPT_TOO_LONG) from exc
    except LLMTransientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except LLMError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _dispatch(req: LLMRequest, provider: str) -> LLMResponse:
    if provider == "anthropic":
        from . import anthropic

        caching = resolve_prompt_caching(provider)
        return await anthropic.call(req, caching=caching)

    if provider in ("openai", "openai-compatible"):
        from . import openai

        return await openai.call(req, provider=provider)

    if provider == "google":
        from . import google

        return await google.call(req)

    if provider == "bedrock-proxy":
        from . import bedrock

        return await bedrock.call(req)

    raise LLMError(
        f"Unknown AI_PROVIDER '{provider}'. "
        "Valid values: anthropic | openai | openai-compatible | google | bedrock-proxy"
    )
