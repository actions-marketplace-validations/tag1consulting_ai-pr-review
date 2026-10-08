"""Model pricing and token-table rendering.

Ports lib/pricing.sh: model_pricing(), model_display_name(),
format_cost(), emit_token_table().
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass
class LongPromptRates:
    """Rates that replace a model's base rates when one call's prompt is
    longer than ``threshold`` tokens. Only Claude Haiku 5.5 is priced this
    way today (up to 100,000 prompt tokens, then higher rates)."""

    threshold: int
    input_rate: int
    output_rate: int
    cache_write_rate: int = 0
    cache_read_rate: int = 0


@dataclass
class ModelRates:
    display_name: str
    input_rate: int
    output_rate: int
    cache_write_rate: int = 0
    cache_read_rate: int = 0
    long_prompt: LongPromptRates | None = None


@dataclass
class TokenEntry:
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    max_output_tokens: int = 0  # 0 means no cap shown


@cache
def load_pricing(pricing_file: str) -> list[dict[str, object]]:
    """Load and parse the model-pricing JSON file, cached per path (#802).

    The pricing file is static config bundled with the image; a single
    process never needs to re-read it from disk more than once per unique
    path. Before this cache, every token-usage rendering call (the review
    comment's usage block, the high-usage warning, the job-log echo, the
    step-summary table) re-read and re-parsed the same file independently —
    up to four disk reads per review run. Callers that need to defeat the
    cache in tests should patch this function directly (as the existing
    reporting/pricing tests already do) rather than rely on cache eviction.

    Safety: `ai_pr_review.cli`'s `review`/`compute`/`slash` commands are each
    a fresh, single-shot process (Click entry points that `sys.exit()` when
    done; see cli.py) — the file cannot change mid-process, so caching by
    path introduces no staleness within a run. Different tests use different
    `pricing_file` paths (typically a fresh `tmp_path` per test), so the
    cache never serves one test's data to another; the only tests that share
    a path (`_REAL_PRICING_FILE` in `tests/python/test_pricing.py`) read it
    read-only. A hypothetical future long-lived server mode reusing this
    process across repos would need to revisit this (as
    `context/symbols.py`'s per-run `_reset_cache()` already anticipates for
    its own cache), but no such mode exists today.
    """
    path = Path(pricing_file)
    if not path.is_file():
        print(
            f"WARNING: model_pricing: pricing file not found '{pricing_file}'; "
            "cost estimates will show as $0.",
            file=sys.stderr,
        )
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: model_pricing: could not load pricing file: {exc}", file=sys.stderr)
        return []


def _as_int(v: object, default: int = 0) -> int:
    try:
        return int(v)  # type: ignore[call-overload, no-any-return]
    except (TypeError, ValueError):
        return default


def _parse_long_prompt(entry: dict[str, object]) -> tuple[LongPromptRates | None, bool]:
    """Read an entry's optional ``long_prompt`` object.

    Returns ``(rates, ok)``. A missing key is ``(None, True)``. A key that is
    present but not a usable tier (not an object, no positive threshold, or no
    positive input and output rate) is ``(None, False)``: the caller must then
    treat the whole entry as unpriced, because silently using the base rates
    would under-count a model that is dearer for long prompts.
    """
    raw = entry.get("long_prompt")
    if raw is None:
        return None, True
    if not isinstance(raw, dict):
        return None, False
    rates = LongPromptRates(
        threshold=_as_int(raw.get("threshold", 0)),
        input_rate=_as_int(raw.get("input_rate", 0)),
        output_rate=_as_int(raw.get("output_rate", 0)),
        cache_write_rate=_as_int(raw.get("cache_write_rate", 0)),
        cache_read_rate=_as_int(raw.get("cache_read_rate", 0)),
    )
    if rates.threshold <= 0 or rates.input_rate <= 0 or rates.output_rate <= 0:
        return None, False
    return rates, True


def model_pricing(model_id: str, pricing_data: list[dict[str, object]]) -> ModelRates:
    """Return ModelRates for model_id, defaulting to zero rates if unknown.

    A matching entry whose ``long_prompt`` tier is malformed also returns zero
    rates (unpriced), with a warning on stderr, so ``token_cost_units`` returns
    ``None`` and callers that fail closed on ``None`` stop.
    """
    for entry in pricing_data:
        patterns = entry.get("patterns", [])
        if not isinstance(patterns, list):
            continue
        for pat in patterns:
            if re.search(str(pat), model_id):
                display_name = str(entry.get("display_name", model_id))
                long_prompt, ok = _parse_long_prompt(entry)
                if not ok:
                    print(
                        f"WARNING: model_pricing: '{display_name}' has a malformed "
                        "long_prompt tier in the pricing file; treating it as unpriced.",
                        file=sys.stderr,
                    )
                    return ModelRates(display_name=display_name, input_rate=0, output_rate=0)
                return ModelRates(
                    display_name=display_name,
                    input_rate=_as_int(entry.get("input_rate", 0)),
                    output_rate=_as_int(entry.get("output_rate", 0)),
                    cache_write_rate=_as_int(entry.get("cache_write_rate", 0)),
                    cache_read_rate=_as_int(entry.get("cache_read_rate", 0)),
                    long_prompt=long_prompt,
                )
    return ModelRates(display_name=model_id, input_rate=0, output_rate=0)


def format_cost(microdollars: int) -> str:
    """Format an integer in $0.0001 units as a dollar string."""
    whole = microdollars // 10000
    frac = microdollars % 10000
    return f"${whole}.{frac:04d}"


def token_cost_units(
    rates: ModelRates,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> int | None:
    """Return cost in $0.0001 units for the given token counts, or None if
    *rates* has no pricing entry (both input_rate and output_rate are 0).

    The single source of truth for this arithmetic (#848 item 4) -- both
    ``_row_cost`` (the token-table row cost) and
    ``review.cost_ceiling``'s pre-flight estimators call this rather than
    each re-deriving the same formula, which had drifted into two
    independently-maintained copies.

    The token counts must describe ONE model call. A model with a
    ``long_prompt`` tier (Claude Haiku 5.5) is dearer when a call's prompt is
    longer than the tier threshold, and the prompt is counted as
    input + cache-write + cache-read tokens of that call. Each token-log row for
    an Anthropic model is one call (``agents/dispatch.py`` builds it from a
    single response, and ``llm/anthropic.py`` makes one request per call), so
    ``_row_cost`` meets this. Summing several calls first would push a total
    over the threshold that no single call reached. Anthropic's pricing page
    says "prompts over 100,000 tokens" and does not say whether cached tokens
    count, so cached tokens are included here (the higher, safer estimate).
    """
    if rates.input_rate == 0 and rates.output_rate == 0:
        return None
    input_rate = rates.input_rate
    output_rate = rates.output_rate
    cache_write_rate = rates.cache_write_rate
    cache_read_rate = rates.cache_read_rate
    long_prompt = rates.long_prompt
    if (
        long_prompt is not None
        and input_tokens + cache_creation_tokens + cache_read_tokens > long_prompt.threshold
    ):
        input_rate = long_prompt.input_rate
        output_rate = long_prompt.output_rate
        cache_write_rate = long_prompt.cache_write_rate
        cache_read_rate = long_prompt.cache_read_rate
    return (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_creation_tokens * cache_write_rate
        + cache_read_tokens * cache_read_rate
    ) // 100_000_000


def _row_cost(entry: TokenEntry, rates: ModelRates) -> int | None:
    """Return cost in $0.0001 units, or None if rates are unknown."""
    return token_cost_units(
        rates,
        input_tokens=entry.input_tokens,
        output_tokens=entry.output_tokens,
        cache_creation_tokens=entry.cache_creation_tokens,
        cache_read_tokens=entry.cache_read_tokens,
    )


@dataclass(frozen=True)
class TokenTotals:
    """Aggregate totals across a token log, shared by the full table's Total
    row and the compact review-comment line (#758) so the two can never
    silently disagree about the numbers they report for the same run.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    grand_total: int
    cost_units: int  # $0.0001 units, same as _row_cost/format_cost
    any_unknown: bool  # True if any row's model had no pricing entry
    agent_count: int  # excludes the synthetic "judge-pass" row
    models: tuple[str, ...]  # unique display names, first-seen order


def compute_totals(
    token_log: list[TokenEntry], pricing_data: list[dict[str, object]]
) -> TokenTotals:
    """Compute the same totals emit_token_table's Total row renders.

    Pulled out as its own function (rather than left inline in
    emit_token_table's row loop) so review/reporting.py's compact usage line
    can report the identical cost/token/model figures the full table shows,
    without a second, independently-maintained accumulation loop that could
    drift from this one.
    """
    total_in = total_out = total_cw = total_cr = total_cost = 0
    any_unknown = False
    agent_count = 0
    seen_models: list[str] = []

    for entry in token_log:
        rates = model_pricing(entry.model, pricing_data)
        cost_units = _row_cost(entry, rates)
        if cost_units is None:
            any_unknown = True
        else:
            total_cost += cost_units

        total_in += entry.input_tokens
        total_out += entry.output_tokens
        total_cw += entry.cache_creation_tokens
        total_cr += entry.cache_read_tokens

        if entry.agent != "judge-pass":
            agent_count += 1
        if rates.display_name not in seen_models:
            seen_models.append(rates.display_name)

    return TokenTotals(
        input_tokens=total_in,
        output_tokens=total_out,
        cache_creation_tokens=total_cw,
        cache_read_tokens=total_cr,
        grand_total=total_in + total_out + total_cw + total_cr,
        cost_units=total_cost,
        any_unknown=any_unknown,
        agent_count=agent_count,
        models=tuple(seen_models),
    )


def emit_token_table(
    token_log: list[TokenEntry],
    pricing_data: list[dict[str, object]],
    *,
    context_tokens: int = 0,
    profile_tokens: int = 0,
    sarif_elapsed_s: float | None = None,
) -> str:
    """Render the token-usage markdown table."""
    any_cache = any(
        e.cache_creation_tokens > 0 or e.cache_read_tokens > 0
        for e in token_log
    )

    lines: list[str] = []

    if any_cache:
        lines.append(
            "| Agent | Model | Input | Output | Cache Write | Cache Read | Total | Est. Cost |"
        )
        lines.append(
            "|-------|-------|------:|-------:|------------:|-----------:|------:|----------:|"
        )
    else:
        lines.append("| Agent | Model | Input | Output | Total | Est. Cost |")
        lines.append("|-------|-------|------:|-------:|------:|----------:|")

    # Totals (Total row + potential "+" suffix) come from compute_totals()
    # rather than being accumulated a second time in this loop, so this
    # table and reporting.py's compact usage line can never report
    # different numbers for the same run (#758).
    totals = compute_totals(token_log, pricing_data)

    for entry in token_log:
        rates = model_pricing(entry.model, pricing_data)
        cost_units = _row_cost(entry, rates)
        cost_display = "n/a" if cost_units is None else format_cost(cost_units)

        row_total = (
            entry.input_tokens
            + entry.output_tokens
            + entry.cache_creation_tokens
            + entry.cache_read_tokens
        )

        output_cell = (
            f"{entry.output_tokens} / {entry.max_output_tokens}"
            if entry.max_output_tokens > 0
            else f"{entry.output_tokens}"
        )

        if any_cache:
            lines.append(
                f"| {entry.agent} | {rates.display_name} | {entry.input_tokens} | "
                f"{output_cell} | {entry.cache_creation_tokens} | "
                f"{entry.cache_read_tokens} | {row_total} | {cost_display} |"
            )
        else:
            lines.append(
                f"| {entry.agent} | {rates.display_name} | {entry.input_tokens} | "
                f"{output_cell} | {row_total} | {cost_display} |"
            )

    total_cost_str = format_cost(totals.cost_units) + ("+" if totals.any_unknown else "")

    if any_cache:
        lines.append(
            f"| **Total** | | **{totals.input_tokens}** | **{totals.output_tokens}** | "
            f"**{totals.cache_creation_tokens}** | **{totals.cache_read_tokens}** | "
            f"**{totals.grand_total}** | **{total_cost_str}** |"
        )
    else:
        lines.append(
            f"| **Total** | | **{totals.input_tokens}** | **{totals.output_tokens}** | "
            f"**{totals.grand_total}** | **{total_cost_str}** |"
        )

    if context_tokens > 0:
        if any_cache:
            # 8-col: Agent | Model | Input(value) | Output | CW | CR | Total | Cost
            lines.append(
                f"| Context enrichment | *(context)* | {context_tokens}"
                " | — | — | — | — | — |"
            )
        else:
            # 6-col: Agent | Model | Input(value) | Output | Total | Cost
            lines.append(
                f"| Context enrichment | *(context)* | {context_tokens}"
                " | — | — | — |"
            )

    if profile_tokens > 0:
        if any_cache:
            # 8-col: Agent | Model | Input(value) | Output | CW | CR | Total | Cost
            lines.append(
                f"| Language profiles | *(profile)* | {profile_tokens}"
                " | — | — | — | — | — |"
            )
        else:
            # 6-col: Agent | Model | Input(value) | Output | Total | Cost
            lines.append(
                f"| Language profiles | *(profile)* | {profile_tokens}"
                " | — | — | — |"
            )

    if sarif_elapsed_s is not None and math.isfinite(sarif_elapsed_s) and sarif_elapsed_s >= 0:
        if any_cache:
            # 8-col: Agent | Model | Input | Output(value) | CW | CR | Total | Cost
            lines.append(
                f"| SARIF ingestion | *(timing)* | — | {sarif_elapsed_s:.2f}s"
                " | — | — | — | — |"
            )
        else:
            # 6-col: Agent | Model | Input | Output(value) | Total | Cost
            lines.append(
                f"| SARIF ingestion | *(timing)* | — | {sarif_elapsed_s:.2f}s"
                " | — | — |"
            )

    return "\n".join(lines)


def parse_token_log_entry(entry: str) -> TokenEntry:
    """Parse a TOKEN_LOG entry string into a TokenEntry."""

    def _extract(pattern: str, text: str, default: int = 0) -> int:
        m = re.search(pattern, text)
        return int(m.group(1)) if m else default

    agent = entry.split(":")[0] if ":" in entry else "unknown"
    model_m = re.search(r"(?:^| )model=(\S+)", entry)
    model = model_m.group(1) if model_m else "unknown"

    return TokenEntry(
        agent=agent,
        model=model,
        input_tokens=_extract(r"(?:^| )input=(\d+)", entry),
        output_tokens=_extract(r"(?:^| )output=(\d+)", entry),
        cache_creation_tokens=_extract(r"(?:^| )cache_creation=(\d+)", entry),
        cache_read_tokens=_extract(r"(?:^| )cache_read=(\d+)", entry),
    )
