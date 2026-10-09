"""Does a judge that reads the code beat a judge that reads only the finding text?

The judge pass never removes a finding. It lowers confidence and moves a finding
from the inline comments to the review body. So whole-set precision is the same
with or without it. The question is about the inline set: of the findings that
stay inline, how many are real, and how many real bugs did the judge push out of
view?

This script answers it on the labeled corpus of ``precision_eval.py``. It does
not call the reviewer again. It replays the saved reviewer responses (a missing
one is an error, never a billed call), then runs each judge condition over the
findings that came out of the merge and suppression steps.

Conditions (``AI_EVAL_JUDGE_CONDITIONS`` picks a subset):

  none          no judge, every finding stays inline
  text-sonnet   the production prompt (finding text only) on Sonnet 5.5
  code-sonnet   the code-aware prompt (finding plus cited hunk) on Sonnet 5.5
  text-haiku    the production prompt on Haiku 5.5
  code-haiku    the code-aware prompt on Haiku 5.5
  v2-sonnet     the text prompt with an extra keep rule and an extra downrank rule

Metrics per condition, using the corpus labels:

  inline precision   matched findings / (matched + unlabeled) among inline findings
  bugs inline        labeled bugs found by a finding that stays inline
  demoted real       findings that match a labeled bug but were moved out of inline

A finding with no label counts as a false positive, as in ``precision_eval.py``,
and is listed for a human to adjudicate. With about 16 findings the numbers move
by whole findings, so the report prints every verdict.

The judge calls are small. Each response is saved by ``_llm_cache.py`` and every
billed call goes through ``_spend_guard.py``.

    python tests/canary/judge_eval.py --dry-run
    python tests/canary/judge_eval.py --yes --json out.json
    AI_EVAL_CACHE_MODE=replay python tests/canary/judge_eval.py   # no call, no key
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

CANARY_DIR = Path(__file__).resolve().parent
REPO_ROOT = CANARY_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(CANARY_DIR))

import _llm_cache  # noqa: E402
import _spend_guard  # noqa: E402

from ai_pr_review.findings.judge import judge_findings  # noqa: E402
from ai_pr_review.findings.models import Finding  # noqa: E402
from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402


def _load(name: str):  # type: ignore[no-untyped-def]
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, CANARY_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pe = _load("precision_eval")

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
TEXT_PROMPT = REPO_ROOT / "prompts" / "finding-judge.md"
CODE_PROMPT = REPO_ROOT / "prompts" / "finding-judge-code.md"


@dataclass(frozen=True)
class Condition:
    name: str
    model: str
    with_code: bool
    prompt_file: str = ""

    @property
    def prompt(self) -> Path:
        if self.prompt_file:
            return REPO_ROOT / "prompts" / self.prompt_file
        return CODE_PROMPT if self.with_code else TEXT_PROMPT


CONDITIONS = (
    Condition("text-sonnet", SONNET, False),
    Condition("code-sonnet", SONNET, True),
    Condition("text-haiku", HAIKU, False),
    Condition("code-haiku", HAIKU, True),
    # A tweaked text-only prompt (more keep for concrete consequences). Sonnet only.
    Condition("v2-sonnet", SONNET, False, "finding-judge-v2.md"),
)
_ALL_NAMES = ("none",) + tuple(c.name for c in CONDITIONS)

# Rough sizes for the pre-flight estimate (a judge call is small).
_JUDGE_BASE_TOKENS = 900
_JUDGE_TOKENS_PER_FINDING = 450
_JUDGE_EXPECTED_OUTPUT = 300
_JUDGE_MAX_OUTPUT = 4096


# --- classification ---------------------------------------------------------


def classify(finding: Finding, fixture) -> str:  # type: ignore[no-untyped-def]
    """``bug``, ``accepted``, or ``unlabeled`` for one finding."""
    if any(pe.matches(finding, b) for b in fixture.bugs):
        return "bug"
    if any(pe.matches(finding, a) for a in fixture.accepted_extra):
        return "accepted"
    return "unlabeled"


@dataclass
class Row:
    fixture: str
    where: str
    severity: str
    kind: str          # bug, accepted, unlabeled
    inline: bool
    verdict: str       # keep, downrank (or "-" for the none condition)
    summary: str


@dataclass
class ConditionResult:
    name: str
    rows: list[Row] = field(default_factory=list)
    bugs_total: int = 0
    bugs_inline: int = 0

    @property
    def inline_rows(self) -> list[Row]:
        return [r for r in self.rows if r.inline]

    @property
    def inline_matched(self) -> int:
        return sum(1 for r in self.inline_rows if r.kind == "bug")

    @property
    def inline_unlabeled(self) -> int:
        return sum(1 for r in self.inline_rows if r.kind == "unlabeled")

    @property
    def inline_precision(self) -> tuple[float, float, float]:
        denom = self.inline_matched + self.inline_unlabeled
        if denom == 0:
            return (0.0, 0.0, 1.0)
        lo, hi = pe.wilson(self.inline_matched, denom)
        return (self.inline_matched / denom, lo, hi)

    @property
    def demoted_real(self) -> int:
        return sum(1 for r in self.rows if r.kind == "bug" and not r.inline)

    @property
    def demoted_total(self) -> int:
        return sum(1 for r in self.rows if not r.inline)


def score_condition(name: str, per_fixture: list[tuple[object, list[Finding]]]) -> ConditionResult:
    """*per_fixture* holds (fixture, findings after the judge) in fixture order."""
    result = ConditionResult(name=name)
    for fixture, findings in per_fixture:
        result.bugs_total += len(fixture.bugs)  # type: ignore[attr-defined]
        found_inline: set[int] = set()
        for f in findings:
            kind = classify(f, fixture)
            inline = not f.demoted_to_body
            if kind == "bug" and inline:
                for i, b in enumerate(fixture.bugs):  # type: ignore[attr-defined]
                    if pe.matches(f, b):
                        found_inline.add(i)
            result.rows.append(Row(
                fixture=fixture.name,  # type: ignore[attr-defined]
                where=f"{f.file}:{f.line}", severity=f.severity, kind=kind, inline=inline,
                verdict=f.judge_verdict or "-", summary=f.finding[:100],
            ))
        result.bugs_inline += len(found_inline)
    return result


def format_report(results: list[ConditionResult]) -> str:
    lines = [
        "",
        f"{'condition':14} {'inline':>6} {'inline prec':>12} {'95% interval':>14} "
        f"{'bugs inline':>12} {'demoted':>8} {'demoted real':>13}",
        "-" * 86,
    ]
    for r in results:
        p, lo, hi = r.inline_precision
        lines.append(
            f"{r.name:14} {len(r.inline_rows):>6} {p:>12.2f} {f'{lo:.2f} to {hi:.2f}':>14} "
            f"{f'{r.bugs_inline}/{r.bugs_total}':>12} {r.demoted_total:>8} {r.demoted_real:>13}"
        )
    base = results[0]
    lines += ["", "Verdict for every finding (b = matches a labeled bug, ? = no label, a = accepted):", ""]
    header = f"{'finding':44} {'kind':>4} " + " ".join(f"{r.name:>12}" for r in results)
    lines.append(header)
    for i, row in enumerate(base.rows):
        marks = []
        for r in results:
            other = r.rows[i]
            marks.append(f"{'inline' if other.inline else 'DEMOTED':>12}")
        tag = {"bug": "b", "unlabeled": "?", "accepted": "a"}[row.kind]
        lines.append(f"{(row.fixture[:20] + ' ' + row.where)[:44]:44} {tag:>4} " + " ".join(marks))
    return "\n".join(lines)


# --- running -----------------------------------------------------------------


async def collect_findings(fixtures, models, agents, reviewer_cache):  # type: ignore[no-untyped-def]
    """Reviewer output after merge and suppression, replayed from the cache only."""
    out = []
    ce = pe._load_consistency_eval()
    ce._GUARD = pe._CachedGuard(reviewer_cache, pe._NoCallsAllowed())
    for fx in fixtures:
        outcome = await ce._one_run(models[0], fx.agents or agents, fx.diff_path, "baseline", REPO_ROOT)
        if not outcome.ok:
            raise RuntimeError(f"{fx.name}: reviewer replay failed: {outcome.detail}")
        out.append((fx, list(outcome.findings)))
    return out


def estimate(per_fixture, conditions, guard):  # type: ignore[no-untyped-def]
    expected = worst = calls = 0
    for _fx, findings in per_fixture:
        if not findings:
            continue
        tokens = _JUDGE_BASE_TOKENS + _JUDGE_TOKENS_PER_FINDING * len(findings)
        for c in conditions:
            code_extra = 400 * len(findings) if c.with_code else 0
            expected += guard.estimate_units(c.model, input_tokens=tokens + code_extra,
                                             output_tokens=_JUDGE_EXPECTED_OUTPUT)
            worst += guard.estimate_units(c.model, input_tokens=tokens + code_extra,
                                          output_tokens=_JUDGE_MAX_OUTPUT, worst_case=True)
            calls += 1
    return expected, worst, calls


async def run_conditions(per_fixture, conditions, cache, guard):  # type: ignore[no-untyped-def]
    ce = pe._load_consistency_eval()
    cached = pe._CachedGuard(cache, guard if guard is not None else pe._NoCallsAllowed())
    results = [score_condition("none", per_fixture)]
    for cond in conditions:
        async def llm_call(req: LLMRequest) -> LLMResponse:
            return await cached.call(lambda r: ce.call_llm(r, pe.PROVIDER), req)

        judged = []
        for fx, findings in per_fixture:
            if not findings:
                judged.append((fx, findings))
                continue
            print(f"  judge {cond.name} :: {fx.name} ({len(findings)} findings)", flush=True)
            result = await judge_findings(
                findings, llm_call=llm_call, model=cond.model, prompt_path=cond.prompt,
                diff_text=fx.diff_path.read_text() if cond.with_code else None,
            )
            judged.append((fx, result.findings))
        results.append(score_condition(cond.name, judged))
    return results


def _selected(names: str) -> tuple[Condition, ...]:
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in _ALL_NAMES]
    if unknown:
        raise ValueError(f"unknown condition(s) {unknown}; choose from {_ALL_NAMES[1:]}")
    return tuple(c for c in CONDITIONS if not wanted or c.name in wanted)


async def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    json_path = argv[argv.index("--json") + 1] if "--json" in argv else ""
    try:
        conditions = _selected(os.environ.get("AI_EVAL_JUDGE_CONDITIONS", ""))
        fixtures = pe.load_fixtures()
        cache = _llm_cache.LLMCache.from_env(namespace=pe.PROVIDER)
        reviewer_cache = _llm_cache.LLMCache(mode="replay", namespace=pe.PROVIDER)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    models = pe._csv("AI_EVAL_MODELS", pe.DEFAULT_MODELS)
    agents = pe._csv("AI_EVAL_AGENTS", pe.DEFAULT_AGENTS)
    print(f"judge eval: {len(fixtures)} fixtures, reviewer {models[0]} (replayed), "
          f"conditions={[c.name for c in conditions]}, cache mode={cache.mode}")
    try:
        per_fixture = await collect_findings(fixtures, models, agents, reviewer_cache)
    except (RuntimeError, _llm_cache.CacheError, _llm_cache.CacheMiss) as exc:
        print(f"ERROR: {exc}\nRun precision_eval.py first so the reviewer responses are saved.",
              file=sys.stderr)
        return 1
    print(f"reviewer findings replayed: {sum(len(f) for _x, f in per_fixture)} "
          f"in {sum(1 for _x, f in per_fixture if f)} fixtures")

    guard: _spend_guard.SpendGuard | None = None
    if cache.mode != "replay":
        if not dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
            print("ERROR: ANTHROPIC_API_KEY not set (use AI_EVAL_CACHE_MODE=replay).", file=sys.stderr)
            return 1
        try:
            guard = _spend_guard.SpendGuard.from_env("judge_eval")
            expected, worst, calls = estimate(per_fixture, conditions, guard)
            print(f"pre-flight: up to {calls} billed judge calls (fewer if responses are saved)")
            guard.preflight(expected_units=expected, worst_case_units=worst,
                            yes=dry_run or _spend_guard.wants_yes(argv))
        except _spend_guard.SpendGuardError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
    if dry_run:
        print("dry run: no billed calls were made")
        return 0

    try:
        results = await run_conditions(per_fixture, conditions, cache, guard)
    except (RuntimeError, _llm_cache.CacheError, _llm_cache.CacheMiss, _spend_guard.SpendGuardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(format_report(results))
    print(f"\ncache: {cache.stats.hits} replayed, {cache.stats.misses} called, {cache.stats.saved} saved")
    if guard is not None:
        print(f"spend guard: this run counted {_spend_guard.usd(guard.run_spent_units)}, campaign "
              f"{_spend_guard.usd(guard.campaign_spent_units())} of {_spend_guard.usd(guard.campaign_cap_units)}")
    if json_path:
        Path(json_path).write_text(json.dumps([
            {"condition": r.name, "inline": len(r.inline_rows), "inline_matched": r.inline_matched,
             "inline_unlabeled": r.inline_unlabeled, "bugs_inline": r.bugs_inline, "bugs_total": r.bugs_total,
             "demoted": r.demoted_total, "demoted_real": r.demoted_real,
             "rows": [row.__dict__ for row in r.rows]}
            for r in results
        ], indent=2) + "\n")
        print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
