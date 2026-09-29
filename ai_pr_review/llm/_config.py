"""Runtime config helpers shared by all provider modules."""

from __future__ import annotations

import os
import re
import sys

# Matches "opus-5" or "opus-5-5"/"opus-5.5" as a whole version token -- not a
# hypothetical future "opus-5-6", "opus-5-9", etc. Used by resolve_temperature()
# and resolve_effort() below, which previously did a bare `"opus-5" in lower`
# substring check that also (correctly, but only by luck) matched
# "claude-opus-5-5". A future claude-opus-5-N would have matched the same way
# whether or not its actual API behavior warranted it -- this makes the match
# explicit instead of relying on substring luck (see issue tracking the Opus
# 5.5 rollout).
#
# The trailing lookahead is `(?![-.]\d(?!\d))`, not the simpler `(?![-.\d])`:
# a plain `(?![-.\d])` also rejects a dated snapshot suffix like
# "-20260915" (Anthropic's own model-naming convention, and AI_MODEL_STANDARD/
# AI_MODEL_PREMIUM are free-form env vars a user could set to one), which is a
# real multi-digit run, not a single sibling-version digit -- that would
# silently stop stripping temperature and stop capping effort for a
# dated Opus 5 snapshot, reintroducing the HTTP-400/180s-timeout failures this
# module exists to prevent. `(?![-.]\d(?!\d))` excludes only a single trailing
# version digit (matches opus-5-9, rejects it) while still accepting a
# multi-digit date suffix (opus-5-20260915 matches the opus-5 family).
_OPUS_5_FAMILY_RE = re.compile(r"opus-5(?:[-.]5)?(?![-.]\d(?!\d))")


def _is_opus_5_family(lower_model_id: str) -> bool:
    """True for claude-opus-5 or claude-opus-5-5/claude-opus-5.5 specifically."""
    return _OPUS_5_FAMILY_RE.search(lower_model_id) is not None


def _clamp_int(env_var: str, default: int, min_val: int, max_val: int) -> int:
    raw = os.environ.get(env_var, str(default)).strip()
    try:
        val = int(raw)
    except ValueError:
        print(
            f"WARNING: {env_var} '{raw}' is not a valid number; defaulting to {default}.",
            file=sys.stderr,
        )
        return default
    if val > max_val:
        print(
            f"WARNING: {env_var} '{val}' exceeds maximum ({max_val}); clamping.",
            file=sys.stderr,
        )
        return max_val
    if val < min_val:
        print(
            f"WARNING: {env_var} '{val}' is below minimum ({min_val}); clamping.",
            file=sys.stderr,
        )
        return min_val
    return val


def get_retry_count() -> int:
    return _clamp_int("LLM_RETRY_COUNT", default=3, min_val=0, max_val=10)


def get_retry_base_delay() -> int:
    return _clamp_int("LLM_RETRY_BASE_DELAY", default=2, min_val=0, max_val=30)


def resolve_temperature(raw: float, model_id: str) -> float | None:
    """Return None when the model doesn't accept temperature; else the clamped value.

    Returning None (which makes the provider modules omit the `temperature`
    field entirely) is REQUIRED for the models listed below, not merely
    conservative. Verified against the live Anthropic Messages API on
    2026-07-21 (tests/canary/temperature_verify.py) for claude-sonnet-5 and
    claude-opus-4-8, and re-verified 2026-09-03 for claude-opus-5 (identical
    behavior confirmed live ahead of the Opus 5 default-model bump):

        temperature omitted -> HTTP 200
        temperature=0.0     -> HTTP 400  "`temperature` is deprecated for this model."
        temperature=1.0     -> HTTP 200  (1.0 is the model's default, so a no-op)

    claude-sonnet-5-5 is covered by the "sonnet-5" substring check below, on
    Anthropic's own published model docs (non-default sampling values are a 400,
    same as Sonnet 5), NOT yet live-verified against the real Messages API.

    claude-opus-5-5 is included below on Anthropic's own published model docs
    (sampling params removed, same as Opus 5/4.8/4.7), NOT yet live-verified
    against the real Messages API -- that verification is required before
    claude-opus-5-5 becomes the default (see the model-change verification
    process in this repo's CLAUDE.md).

    gpt-5.6-* (luna/terra/sol) and gpt-6* (luna/sol/astra, gpt-6.1-sol) are
    included for the same reason as gpt-5.5: these reasoning models accept only
    the default temperature of 1, per third-party reports, NOT live-verified
    (the canary never sends a temperature to them).

    gemini-3* is included for a different reason: the API accepts a
    temperature, but Google's Gemini 3 developer guide "strongly recommend[s]
    keeping the temperature parameter at its default value of 1.0" and warns
    that lowering it "may lead to unexpected behavior, such as looping or
    degraded performance". The judge's temperature=0.0 is exactly that case,
    so omission is the safe choice here too.

    Consequences, do not "fix" this by making temperature apply:
      * An explicit temperature is a hard 400 for these models -- including the
        temperature=0.0 the judge pass is coded for (judge.py). Sending it would
        break the judge entirely. This strip is what keeps that call valid.
      * There is no lever below 1.0. Only omission (or the default 1.0) is
        accepted, so AI_TEMPERATURE cannot be made to lower sampling entropy for
        these models. Review-consistency work must lean on prompt guidance and
        the confidence threshold instead, not on temperature.
    """
    lower = model_id.lower()
    if (
        _is_opus_5_family(lower)
        or "opus-4-8" in lower
        or "opus-4.8" in lower
        or "opus-4-7" in lower
        or "opus-4.7" in lower
        or "sonnet-5" in lower
        or lower.startswith("o1")
        or lower.startswith("o3")
        or lower.startswith("o4")
        or lower.startswith("gpt-5.5")
        or lower.startswith("gpt-5.6")
        or lower.startswith("gpt-6")
        or lower.startswith("gemini-3")
        or lower.startswith("gpt-5-")
        or lower == "gpt-5"
    ):
        return None
    return min(raw, 2.0)


def resolve_effort(model_id: str) -> str | None:
    """Return the output_config.effort value to send for this model, or None to omit it.

    Claude Sonnet 5 has adaptive thinking on by default (implicit effort="high"),
    which can consume max_tokens entirely on thinking before any text is produced
    (see issue #592). Capping effort at "low" bounds thinking without disabling it
    outright. Other models either don't support adaptive thinking (predate it, would
    400 on an unrecognized output_config) or have thinking off by default already
    (Opus 4.7/4.8 require an explicit thinking:{type:adaptive} opt-in), so they don't
    have this failure mode and must not receive the parameter.

    Claude Opus 5 was verified live (2026-09-03, ahead of its default-model bump)
    to ALSO have adaptive thinking on by default -- unlike Opus 4.7/4.8, this is
    a new default-on behavior for Opus 5 specifically. Against
    tests/canary/stress_diff.txt with no effort cap: 206s wall time, 11,433
    thinking tokens, comfortably exceeding llm/anthropic.py's 180s client
    timeout under real load. With effort="low": 67s wall time, 2,464 thinking
    tokens -- same mitigation as Sonnet 5, verified to actually work rather
    than assumed to carry over.

    claude-sonnet-5-5 also matches the "sonnet-5" check below and so inherits the
    "low" cap. Per Anthropic's docs its effort levels are recalibrated from Sonnet
    5's (default still "high"). Live canary 2026-09-28 (run 36500424509 of the Live Model Canary workflow, on commit 97f9bdf): with this "low"
    cap, code-reviewer and silent-failure-hunter both ended with
    stop_reason=end_turn and 0 thinking tokens against
    tests/canary/stress_diff.txt. That shows "low" is sufficient for that diff,
    NOT that it is necessary (nothing was run uncapped) and NOT that it is the
    best setting for review quality.

    claude-opus-5-5 cannot disable thinking at all (per Anthropic's docs,
    unlike Opus 5 which could disable it below effort "xhigh"), and its
    default effort is "medium" (one step below Opus 5's default "high") --
    Anthropic's own release notes describe it as thinking more per turn at a
    given effort level than Opus 5, so the 180s client timeout in
    llm/anthropic.py is a real regression candidate here. The live canary has now
    been run against it (2026-09-28, run 36500424509 of the Live Model Canary workflow, on commit 97f9bdf): with the "low" cap both
    code-reviewer and silent-failure-hunter ended with stop_reason=end_turn
    (830 and 591 thinking tokens) against tests/canary/stress_diff.txt, and the
    whole canary step finished in about 55 seconds. That is one diff and two
    agents, so treat it as "not a regression on the stress diff", not as a
    general guarantee.
    """
    lower = model_id.lower()
    if "sonnet-5" in lower or _is_opus_5_family(lower):
        return "low"
    return None


def resolve_gemini_thinking_level(model_id: str) -> str | None:
    """Return the generationConfig.thinkingConfig.thinkingLevel to send, or None to omit it.

    Same failure class as resolve_effort() above (#592): on Gemini 3.x,
    maxOutputTokens is a hard cutoff that includes thought tokens (Google's
    thinking guide). Live canary, 2026-09-29, tests/canary/stress_diff.txt,
    max_output_tokens=32768, no thinking control sent: gemini-3.8-flash
    (default level "medium") spent 30,938 thinking tokens on code-reviewer
    and 23,789 on silent-failure-hunter. Both still ended with STOP, but the
    first left at most about 1,830 tokens for the review itself.

    With "low", same diff and agents (2026-09-29): 0 thinking tokens, 190 and
    712 output tokens, 5s per call, 0 and 1 findings. An uncapped re-run in
    the same session: 22,860 and 20,141 thinking tokens, 54-83s per call, 0
    and 0 findings, with review text that said essentially the same thing.
    So "low" did not reduce findings on this diff. That is one diff and two
    agents, not a general guarantee about review quality.

    Capped at "low" for Gemini 3 models only. Flash-Lite is excluded: its
    default is "minimal" (0 thinking tokens on the same canary run), so "low"
    would raise its thinking, not bound it. Gemini 2.5 models use the older
    thinkingBudget mechanism, and Google returns a 400 if a request mixes the
    two, so nothing is sent for them.
    """
    lower = model_id.lower()
    if lower.startswith("gemini-3") and "flash-lite" not in lower:
        return "low"
    return None


def resolve_prompt_caching(provider: str) -> bool:
    """Resolve LLM_PROMPT_CACHING to a boolean for the given provider."""
    raw = os.environ.get("LLM_PROMPT_CACHING", "auto").strip().lower()
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    if raw not in ("auto", ""):
        print(
            f"WARNING: LLM_PROMPT_CACHING='{raw}' is not a valid value; defaulting to auto.",
            file=sys.stderr,
        )
    # auto: enable for anthropic and bedrock-proxy
    return provider in ("anthropic", "bedrock-proxy")
