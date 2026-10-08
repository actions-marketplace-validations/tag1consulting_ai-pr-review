"""Check the cost estimator against real token counts.

``tests/data/token_ratio_measurements.json`` holds the output of
``tests/canary/measure_token_ratio.py``: for each sample file, its SHA-256, its
character count, and the input tokens Anthropic's token counting endpoint
reported for each model. This test makes no network call. It reads the file,
skips any sample that has changed since it was measured, and checks that
``estimate_billed_tokens`` never counts fewer tokens than were measured.

A failure means the estimator under-counts a file that was measured, so a cost
ceiling or a spend cap built on it could be too loose. Re-run
``python tests/canary/measure_token_ratio.py`` after a tokenizer change, then
adjust ``_BILLED_CHARS_PER_TOKEN`` in ``ai_pr_review/review/cost_ceiling.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_pr_review.context.budget import estimate_tokens
from ai_pr_review.review.cost_ceiling import estimate_billed_tokens

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA = _REPO_ROOT / "tests" / "data" / "token_ratio_measurements.json"

# Files that other commits edit (source and docs) drop out of the check when
# their hash no longer matches. The corpus diffs, prompts, and profiles are the
# stable core, so require enough of them to make the check mean something.
_MIN_CHECKED_SAMPLES = 30

# The estimate may be above the real count (that is the safe direction) but a
# huge gap would make the ceiling refuse reviews that are well inside it. Only
# the current models are held to this. Claude Sonnet 4.6 uses the older
# tokenizer, which gives about 30% fewer tokens, so the estimate is further
# above it by design (it still must not be below).
_MAX_OVERESTIMATE = 2.0
_CURRENT_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")


def _load() -> list[tuple[dict[str, object], str]]:
    if not _DATA.is_file():
        pytest.skip("tests/data/token_ratio_measurements.json not present")
    data = json.loads(_DATA.read_text())
    checked: list[tuple[dict[str, object], str]] = []
    for sample in data["samples"]:
        path = _REPO_ROOT / sample["path"]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != sample["sha256"]:
            continue
        checked.append((sample, text))
    if len(checked) < _MIN_CHECKED_SAMPLES:
        pytest.skip(
            f"only {len(checked)} measured files are unchanged; "
            "re-run tests/canary/measure_token_ratio.py"
        )
    return checked


def test_measurement_file_covers_the_current_models() -> None:
    data = json.loads(_DATA.read_text())
    assert {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"} <= set(data["models"])


def test_estimate_never_counts_fewer_tokens_than_measured() -> None:
    under = []
    for sample, text in _load():
        estimate = estimate_billed_tokens(text)
        for model, tokens in sample["tokens"].items():  # type: ignore[attr-defined]
            if estimate < tokens:
                under.append(f"{sample['path']} on {model}: estimate {estimate} < measured {tokens}")
    assert not under, "estimator under-counts measured files:\n" + "\n".join(under)


def test_estimate_is_not_wildly_above_measured() -> None:
    over = []
    for sample, text in _load():
        estimate = estimate_billed_tokens(text)
        for model, tokens in sample["tokens"].items():  # type: ignore[attr-defined]
            if model in _CURRENT_MODELS and tokens > 0 and estimate / tokens > _MAX_OVERESTIMATE:
                over.append(
                    f"{sample['path']} on {model}: estimate {estimate} is "
                    f"{estimate / tokens:.2f}x measured {tokens}"
                )
    assert not over, "estimator is far above measured counts:\n" + "\n".join(over)


def test_the_old_default_estimate_under_counted_diffs() -> None:
    """Why this estimator exists. ``estimate_tokens`` with its defaults (4
    characters per token) counted fewer tokens than the endpoint reported for
    most corpus diffs. It stays as it is, because the context budget uses it."""
    diffs = [(s, t) for s, t in _load() if s["class"] == "diff"]
    assert diffs
    under = sum(
        1
        for sample, text in diffs
        if estimate_tokens(text) < sample["tokens"]["claude-sonnet-5-5"]  # type: ignore[index]
    )
    assert under >= 0.8 * len(diffs)


def test_billed_estimate_has_no_cjk_regression() -> None:
    """CJK text is still charged near one token per character."""
    assert estimate_billed_tokens("日本語" * 100) == int(300 * 1.1)
