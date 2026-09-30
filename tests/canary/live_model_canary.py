"""Live-API canary: exercises real model behavior against a genuinely
demanding diff, not a small representative one.

Background (#592): Claude Sonnet 5's adaptive thinking, on by default, was
able to consume an entire max_tokens budget on thinking alone, leaving no
room for a text response. Nothing in the model-onboarding process at the
time (unit tests with no network access, plus one live e2e run against a
310-line, 6-file diff) was demanding enough to trigger it. This script is
the tier of the #592 test plan meant to catch the *next* model-behavior
surprise: it runs the real dispatch path (real prompts, real
DispatchContext, real call_llm) against tests/canary/stress_diff.txt (the
actual PR #591 diff, ~1200 lines across 4 files, known to reliably demand
enough reasoning to matter) for every model this repo has a live API key
for, and asserts each call completes cleanly.

Not a pytest suite: this makes real, billed API calls and is intentionally
excluded from the default `pytest tests/python` run. Invoke directly:

    python tests/canary/live_model_canary.py

Exit code 0 on success (every call ended with its provider's clean stop
reason and a valid json-findings array), 1 on any failure. Intended for a scheduled GitHub Actions workflow
(.github/workflows/model-canary.yml), not per-PR CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ai_pr_review.agents.dispatch import DispatchContext, run_tier  # noqa: E402
from ai_pr_review.agents.roster import AGENTS  # noqa: E402
from ai_pr_review.findings.extract import _FENCE_RE, extract_findings  # noqa: E402
from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402
from ai_pr_review.llm.client import call_llm  # noqa: E402

STRESS_DIFF_PATH = Path(__file__).resolve().parent / "stress_diff.txt"

# The two agents #592 actually crashed on. Restricting the canary to these
# (rather than the full roster) keeps API cost bounded while still covering
# the agents with the largest max_output_tokens and the most demanding,
# analysis-heavy prompts -- the ones most likely to trigger a thinking-budget
# or similar model-behavior surprise on a future model.
TARGET_AGENT_NAMES = ("code-reviewer", "silent-failure-hunter")

# provider -> (env var holding the API key, standard model, premium model).
# The scheduled workflow (.github/workflows/model-canary.yml) passes the
# Anthropic, OpenAI and Google keys, so those three run weekly. bedrock-proxy
# runs only where BEDROCK_API_KEY and BEDROCK_API_URL are set (locally today).
# A SKIP line is not coverage: only an OK line is.
PROVIDER_MODELS: dict[str, tuple[str, str, str]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "claude-sonnet-5-5", "claude-opus-5-5"),
    "openai": ("OPENAI_API_KEY", "gpt-6-luna", "gpt-6.1-sol"),
    "google": ("GOOGLE_API_KEY", "gemini-3.5-flash-lite", "gemini-3.8-flash"),
    # Also needs BEDROCK_API_URL (see ai_pr_review/llm/bedrock.py).
    "bedrock-proxy": (
        "BEDROCK_API_KEY",
        "us.anthropic.claude-sonnet-5",
        "global.anthropic.claude-opus-4-7",
    ),
}

# The raw stop reason each provider module reports for a response that
# finished on its own. The provider modules pass the provider's own value
# through unchanged (OpenAI finish_reason, Gemini finishReason), so the
# canary compares against the provider's own "finished cleanly" value.
_CLEAN_STOP_REASONS: dict[str, str] = {
    "anthropic": "end_turn",
    "bedrock-proxy": "end_turn",
    "openai": "stop",
    "google": "STOP",
}


@dataclass
class CanaryResult:
    provider: str
    model: str
    agent: str
    ok: bool
    detail: str


# Substrings identifying an API quota/billing block rather than a genuine
# model-behavior surprise (#592's actual bug: empty-thinking, no text, wrong
# stop_reason). Filed 2026-07-28 after issue #636 turned out to be the CI
# key's Anthropic workspace usage limit (resets monthly) auto-labeled as a
# "#592-class" model regression by the workflow's hardcoded issue body --
# nothing to do with model behavior at all. Matched case-insensitively.
# OpenAI reports an exhausted quota as `insufficient_quota` ("You exceeded your
# current quota"), and the Gemini API as status `RESOURCE_EXHAUSTED`.
_QUOTA_ERROR_MARKERS = (
    "usage limit",
    "rate_limit_error",
    "credit balance is too low",
    "insufficient_quota",
    "exceeded your current quota",
    "resource_exhausted",
)


def _is_quota_error(detail: str) -> bool:
    """True if a failure detail looks like a quota/billing block, not a model bug."""
    lowered = detail.lower()
    return any(marker in lowered for marker in _QUOTA_ERROR_MARKERS)


async def _run_one(provider: str, model_id: str, agent_name: str) -> CanaryResult:
    async def llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, provider)

    agents = [a for a in AGENTS if a.name == agent_name]
    if not agents:
        return CanaryResult(provider, model_id, agent_name, False, f"unknown agent {agent_name!r}")

    context = DispatchContext(
        script_dir=REPO_ROOT,
        mode="full",
        diff_path=STRESS_DIFF_PATH,
        provider=provider,
        standard_model=model_id,
        premium_model=model_id,
        # Match the production ceiling so the canary tests what actually ships (#592, #642).
        max_tokens_per_agent=32768,
        changed_files=[
            ".github/workflows/slash-commands.yml",
            "ai_pr_review/slash/dismiss.py",
            "ai_pr_review/vcs/github.py",
            "ai_pr_review/cli.py",
        ],
    )

    started = time.monotonic()
    try:
        successes, failures = await run_tier(agents, llm_call, context, semaphore_size=1)
    except Exception as exc:  # noqa: BLE001 - canary must report, not crash
        return CanaryResult(provider, model_id, agent_name, False, f"run_tier raised: {exc!r}")

    if failures:
        f = failures[0]
        return CanaryResult(
            provider, model_id, agent_name, False,
            f"agent failed (exit_code={f.exit_code}): {f.reason[:300]}",
        )

    elapsed = time.monotonic() - started
    result = successes[0]
    _save_output(provider, model_id, agent_name, result.output)
    stop_reason = result.stop_reason
    thinking_tokens = result.token_log.thinking_tokens if result.token_log else 0
    expected = _CLEAN_STOP_REASONS.get(provider, "end_turn")
    if stop_reason != expected:
        # Stricter than "text is non-empty": a response that hit max_tokens
        # but still produced *some* text is a silently truncated, degraded
        # review, not a crash -- it would pass a looser check while still
        # being a real quality problem. See the #592 test plan, Tier 2.
        return CanaryResult(
            provider, model_id, agent_name, False,
            f"stop_reason={stop_reason!r} (expected {expected}), "
            f"thinking_tokens={thinking_tokens}, output_tokens="
            f"{result.token_log.output if result.token_log else 'n/a'}",
        )

    output_tokens = result.token_log.output if result.token_log else 0
    block_error = _findings_block_error(result.output)
    if block_error:
        # A clean stop with a missing or unparseable json-findings block is
        # still a lost review: extract_findings() would return [] with only a
        # stderr warning, which reads as "no findings", not as a failure.
        return CanaryResult(
            provider, model_id, agent_name, False,
            f"stop_reason={stop_reason} but {block_error}, "
            f"output_tokens={output_tokens}",
        )
    findings = extract_findings(result.output, agent_name)
    return CanaryResult(
        provider, model_id, agent_name, True,
        f"stop_reason={stop_reason}, findings={len(findings)}, "
        f"thinking_tokens={thinking_tokens}, output_tokens={output_tokens}, "
        f"elapsed={elapsed:.0f}s",
    )


def _findings_block_error(output: str) -> str:
    """Return why the output's json-findings block is unusable, or "" if it is fine.

    Uses the same fence regex as findings/extract.py, then checks that the
    block is a JSON array, so the canary fails on exactly the outputs the
    findings pipeline would silently drop.
    """
    match = _FENCE_RE.search(output)
    if not match:
        return "no json-findings block"
    try:
        parsed = json.loads(match.group(1).strip())
    except json.JSONDecodeError as exc:
        return f"json-findings block is not valid JSON ({exc.msg})"
    if not isinstance(parsed, list):
        return "json-findings block is not a JSON array"
    return ""


def _save_output(provider: str, model_id: str, agent_name: str, text: str) -> None:
    """Write the raw review text to $CANARY_OUTPUT_DIR, when set, so a person
    can judge review content and not only the stop reason. A write error is
    reported and ignored: it must not hide the canary result itself."""
    out_dir = os.environ.get("CANARY_OUTPUT_DIR")
    if not out_dir:
        return
    path = Path(out_dir) / f"{provider}__{model_id}__{agent_name}.md".replace("/", "_")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError as exc:
        print(f"WARNING: could not save canary output to {path}: {exc}", file=sys.stderr)


async def main() -> int:
    results: list[CanaryResult] = []
    for provider, (env_var, standard_model, premium_model) in PROVIDER_MODELS.items():
        if not os.environ.get(env_var):
            print(f"SKIP: {provider} (no {env_var} set)", file=sys.stderr)
            continue
        for model_id in {standard_model, premium_model}:
            for agent_name in TARGET_AGENT_NAMES:
                try:
                    result = await _run_one(provider, model_id, agent_name)
                except Exception as exc:  # noqa: BLE001 - one combo's crash must not hide the rest
                    result = CanaryResult(provider, model_id, agent_name, False, f"unexpected: {exc!r}")
                results.append(result)
                status = "OK  " if result.ok else "FAIL"
                print(f"{status} provider={provider} model={model_id} agent={agent_name}: {result.detail}")

    if not results:
        print("ERROR: no providers had a usable API key; nothing was tested", file=sys.stderr)
        return 1

    failed = [r for r in results if not r.ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} passed ===")

    _write_github_output(failed)
    return 1 if failed else 0


def _write_github_output(failed: list[CanaryResult]) -> None:
    """Expose failure classification to the calling workflow step.

    Lets .github/workflows/model-canary.yml compose an issue body that
    reflects what actually happened instead of always assuming a #592-class
    model-behavior regression (see _QUOTA_ERROR_MARKERS above). No-op outside
    GitHub Actions (GITHUB_OUTPUT unset) or when nothing failed.
    """
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path or not failed:
        return
    all_quota = all(_is_quota_error(r.detail) for r in failed)
    detail_lines = [f"{r.provider}/{r.model}/{r.agent}: {r.detail}" for r in failed]
    # Random per-run delimiter, not a fixed string: failure_detail can embed raw
    # provider error text/tracebacks we don't control, and a fixed delimiter that
    # happened to appear in that text would silently truncate the multiline
    # GITHUB_OUTPUT value at that point (GitHub Actions heredoc-style outputs
    # terminate on the first line matching the delimiter, verbatim).
    delimiter = f"CANARY_FAILURE_{uuid.uuid4().hex}"
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"all_quota_exhausted={'true' if all_quota else 'false'}\n")
        f.write(f"failure_detail<<{delimiter}\n")
        f.write("\n".join(detail_lines) + "\n")
        f.write(f"{delimiter}\n")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
