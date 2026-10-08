"""Measure real characters-per-token on the text this tool sends to a model.

``ai_pr_review.context.budget.estimate_tokens`` guesses tokens from characters.
This script replaces the guess with a measurement. It sends each sample to
Anthropic's token counting endpoint (``POST /v1/messages/count_tokens``) for
each model and writes the counts to ``tests/data/token_ratio_measurements.json``.
``tests/python/test_token_estimate_calibration.py`` then checks the estimator
against that file with no network access.

Token counting is free to use (Anthropic's token counting page, fetched
2026-10-08). It is rate limited per usage tier and does not bill tokens, so this
script does not go through the eval spend guard and does not need the live-harness
approval that ``consistency_eval.py`` needs. It still needs an API key.

The key is read from ``ANTHROPIC_API_KEY``. If that is not set, the script reads
``ANTHROPIC_API_TEST_KEY``. It never prints the key.

Run it from the repository root:

    python tests/canary/measure_token_ratio.py
    python tests/canary/measure_token_ratio.py --models claude-haiku-5-5
    python tests/canary/measure_token_ratio.py --dry-run     # list samples only

Each sample is plain repository text (corpus diffs, prompts, language profiles,
source files, and docs), so no customer data is sent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_PATH = REPO_ROOT / "tests" / "data" / "token_ratio_measurements.json"

API_URL = "https://api.anthropic.com/v1/messages/count_tokens"
API_VERSION = "2023-06-01"

# The three current models, plus Sonnet 4.6, which uses the older tokenizer.
# The estimator must not under-count on the newer tokenizer, and the older one
# shows how much the ratio moved.
DEFAULT_MODELS = (
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-sonnet-4-6",
)

# Source files that stand for the code a review sees. Chosen for size and
# variety, not for any property of the content.
CODE_FILES = (
    "ai_pr_review/pricing.py",
    "ai_pr_review/findings/judge.py",
    "ai_pr_review/findings/merge.py",
    "ai_pr_review/config.py",
    "ai_pr_review/vcs/github.py",
    "ai_pr_review/agents/dispatch.py",
)
PROSE_FILES = ("README.md", "docs/configuration.md", "docs/architecture-internals.md")

# Anthropic rejects an empty text block, so the overhead probe uses one letter.
_PROBE_TEXT = "a"


def _classify_and_collect() -> list[tuple[str, Path]]:
    samples: list[tuple[str, Path]] = []
    samples += [("diff", p) for p in sorted((REPO_ROOT / "tests/canary/corpus").glob("*.diff"))]
    samples += [("prompt", p) for p in sorted((REPO_ROOT / "prompts").glob("*.md"))]
    samples += [("profile", p) for p in sorted((REPO_ROOT / "language-profiles").glob("*.md"))]
    samples += [("code", REPO_ROOT / rel) for rel in CODE_FILES]
    samples += [("prose", REPO_ROOT / rel) for rel in PROSE_FILES]
    return [(cls, p) for cls, p in samples if p.is_file()]


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        key = os.environ.get("ANTHROPIC_API_TEST_KEY", "").strip()
    return key


def _count(client: httpx.Client, key: str, model: str, text: str) -> int:
    body = {"model": model, "messages": [{"role": "user", "content": text}]}
    headers = {
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    last_error = ""
    for attempt in range(4):
        response = client.post(API_URL, headers=headers, json=body, timeout=60.0)
        if response.status_code == 200:
            return int(response.json()["input_tokens"])
        last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        if response.status_code in (429, 500, 502, 503, 529):
            time.sleep(2**attempt)
            continue
        break
    raise RuntimeError(f"count_tokens failed for {model}: {last_error}")


def _summary(measurements: list[dict[str, object]], models: tuple[str, ...]) -> str:
    lines = ["", "characters per token (net of the per-request overhead), by class and model:", ""]
    header = f"{'class':8} {'model':20} {'n':>3} {'min':>6} {'median':>7} {'max':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for cls in ("diff", "prompt", "profile", "code", "prose", "all"):
        for model in models:
            ratios = []
            for m in measurements:
                if cls != "all" and m["class"] != cls:
                    continue
                tokens = m["tokens"][model]  # type: ignore[index]
                if tokens > 0:
                    ratios.append(int(m["chars"]) / tokens)  # type: ignore[call-overload]
            if ratios:
                lines.append(
                    f"{cls:8} {model:20} {len(ratios):>3} {min(ratios):>6.2f} "
                    f"{statistics.median(ratios):>7.2f} {max(ratios):>6.2f}"
                )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--dry-run", action="store_true", help="list the samples and exit")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    args = parser.parse_args(argv)

    models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    samples = _classify_and_collect()
    print(f"{len(samples)} samples x {len(models)} models = {len(samples) * len(models)} requests")
    if args.dry_run:
        for cls, path in samples:
            print(f"  {cls:8} {path.relative_to(REPO_ROOT)} ({path.stat().st_size} bytes)")
        return 0

    key = _api_key()
    if not key:
        print("error: set ANTHROPIC_API_KEY (or ANTHROPIC_API_TEST_KEY)", file=sys.stderr)
        return 1

    overhead: dict[str, int] = {}
    measurements: list[dict[str, object]] = []
    with httpx.Client() as client:
        for model in models:
            # A one-letter message is one token, so the rest is fixed request overhead.
            overhead[model] = max(_count(client, key, model, _PROBE_TEXT) - 1, 0)
        print("per-request overhead tokens:", overhead)
        for cls, path in samples:
            text = path.read_text(encoding="utf-8")
            tokens = {
                model: max(_count(client, key, model, text) - overhead[model], 0)
                for model in models
            }
            measurements.append({
                "path": str(path.relative_to(REPO_ROOT)),
                "class": cls,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "chars": len(text),
                "non_ascii_chars": sum(1 for ch in text if ord(ch) > 127),
                "tokens": tokens,
            })

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endpoint": "POST /v1/messages/count_tokens",
                "models": list(models),
                "overhead_tokens": overhead,
                "samples": measurements,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")
    print(_summary(measurements, models))
    return 0


if __name__ == "__main__":
    sys.exit(main())
