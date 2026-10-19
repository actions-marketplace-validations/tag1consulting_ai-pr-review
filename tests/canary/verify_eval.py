"""Does a verifier that re-reads the code catch unsupported findings and fix wrong lines?

Issue #1082. The Claude bot attaches a Verification section to each finding. Its own text
sometimes corrects the line number of its headline, so re-reading the code may help or
may not. #837 measured a code-aware *judge* as worse than the text-only judge. This
harness measures a different step: a verifier that re-reads a window of the diff around
each finding and answers two questions.

  1. Is the finding supported by the code (``confirmed``) or not (``unsupported``)?
  2. On which line is the problem?

It runs on the labeled corpus of ``precision_eval.py`` and, like ``judge_eval.py``, does
not call the reviewer again. It replays the saved reviewer responses (a missing one is an
error, never a billed call) and runs each verifier condition over the findings that come
out of the merge and suppression steps.

Conditions (``AI_EVAL_VERIFY_CONDITIONS`` picks a subset):

  none            no verifier: every finding stays as the reviewer wrote it
  hunk-sonnet     the verifier with the narrow window that the code-aware judge used
  reread-sonnet   the same verifier with a wide window around the cited line

Metrics per condition, using the corpus labels:

  kept real          findings that match a labeled bug and are still ``confirmed``
  dropped real       findings that match a labeled bug but were marked ``unsupported``
  caught unlabeled   findings with no label that were marked ``unsupported``
  anchor exact       findings that match a labeled bug whose line is inside the labeled
                     range, before the verifier and after it (the verifier's line replaces
                     the finding's line)

A finding with no label counts as unlabeled, as in ``precision_eval.py``, and is listed for
a human to adjudicate. The numbers move by whole findings, so the report prints every one.

No billed call is part of the test suite. Every billed call goes through
``_spend_guard.py`` and every response is saved by ``_llm_cache.py``.

    python tests/canary/verify_eval.py --dry-run
    python tests/canary/verify_eval.py --yes --json out.json   # billed, needs approval first
    AI_EVAL_CACHE_MODE=replay python tests/canary/verify_eval.py   # no call, no key
"""

from __future__ import annotations

import asyncio
import dataclasses
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
jc = _load("judge_code")
je = _load("judge_eval")

SONNET = "claude-sonnet-5-5"
PROMPT = CANARY_DIR / "verify_prompts" / "finding-verify.md"

CONFIRMED = "confirmed"
UNSUPPORTED = "unsupported"
_KNOWN_VERDICTS = frozenset({CONFIRMED, UNSUPPORTED})


@dataclass(frozen=True)
class Condition:
    name: str
    model: str
    context: int      # rows of diff before and after the cited line
    max_chars: int    # cut for the code window


CONDITIONS = (
    Condition("hunk-sonnet", SONNET, jc.DEFAULT_CONTEXT_LINES, jc.DEFAULT_MAX_CHARS),
    Condition("reread-sonnet", SONNET, 25, 6000),
)
_ALL_NAMES = ("none",) + tuple(c.name for c in CONDITIONS)

# Rough sizes for the pre-flight estimate (a verifier call is small).
_BASE_TOKENS = 900
_TOKENS_PER_FINDING = 450
_TOKENS_PER_CODE_CHAR = 0.3
_EXPECTED_OUTPUT = 400
_MAX_OUTPUT = 4096


class VerifyError(Exception):
    """The verifier reply cannot be read. The eval stops: a wrong score would follow."""


@dataclass(frozen=True)
class Verdict:
    verdict: str
    line: int | None
    reason: str = ""
    # False when the code window sent with the finding was empty. A verdict on such a
    # finding reflects missing context, not a judgment of the code.
    had_code: bool = True


# --- the verifier call -------------------------------------------------------


def build_payload(findings: list[Finding], diff_text: str, cond: Condition) -> str:
    """The JSON list the verifier reads. ``id`` is the index in *findings*."""
    items = []
    for idx, f in enumerate(findings):
        items.append({
            "id": idx,
            "severity": f.severity,
            "file": f.file,
            "line": f.line,
            "finding": f.finding,
            "code": jc.extract_hunk(
                diff_text, f.file, f.start_line or f.line, context=cond.context, max_chars=cond.max_chars
            ),
        })
    return json.dumps(items, ensure_ascii=False)


def parse_verdicts(text: str, count: int) -> dict[int, Verdict]:
    """Read the verifier reply. Raises ``VerifyError`` when it is not the expected JSON.

    An id that is out of range, or a verdict that is not known, is left out: that
    finding has no verdict, and the report shows it as unverified. A reply that
    cannot be parsed at all stops the run.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        verdicts = json.loads(body)["verdicts"]
        if not isinstance(verdicts, list):
            raise TypeError(f"verdicts is a {type(verdicts).__name__}, not a list")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise VerifyError(f"cannot read the verifier reply ({exc}): {text[:200]!r}") from exc
    out: dict[int, Verdict] = {}
    for item in verdicts:
        try:
            vid = int(item["id"])
            verdict = str(item["verdict"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= vid < count or verdict not in _KNOWN_VERDICTS:
            continue
        raw_line = item.get("line")
        line = raw_line if isinstance(raw_line, int) and not isinstance(raw_line, bool) and raw_line >= 1 else None
        out[vid] = Verdict(verdict, line, str(item.get("reason", ""))[:200])
    return out


async def verify_findings(
    findings: list[Finding], *, llm_call, cond: Condition, prompt_text: str, diff_text: str  # type: ignore[no-untyped-def]
) -> list[Verdict | None]:
    """One verifier call for *findings*. A finding without a verdict gets ``None``."""
    if not findings:
        return []
    request = LLMRequest(
        model_id=cond.model,
        system_prompt=prompt_text,
        user_message=build_payload(findings, diff_text, cond),
        max_tokens=4096,
        temperature=0.0,
    )
    response: LLMResponse = await llm_call(request)
    verdicts = parse_verdicts(response.text, len(findings))
    codes = [item["code"] for item in json.loads(request.user_message)]
    return [
        dataclasses.replace(verdicts[i], had_code=bool(codes[i])) if i in verdicts else None
        for i in range(len(findings))
    ]


# --- scoring -----------------------------------------------------------------


def _in_range(line: int | None, bug) -> bool:  # type: ignore[no-untyped-def]
    return line is not None and bug.line_start <= line <= bug.line_end


@dataclass
class Row:
    fixture: str
    where: str
    kind: str                 # bug, accepted, unlabeled
    verdict: str              # confirmed, unsupported, "-" (none) or "none" (no verdict)
    no_code: bool             # the verifier saw an empty code window
    no_line: bool             # the verifier gave a verdict but no usable line
    line_before: int | None
    line_after: int | None
    anchor_before: bool       # only meaningful for kind == "bug"
    anchor_after: bool
    summary: str


@dataclass
class ConditionResult:
    name: str
    rows: list[Row] = field(default_factory=list)

    def _count(self, kind: str, verdict: str) -> int:
        return sum(1 for r in self.rows if r.kind == kind and r.verdict == verdict)

    @property
    def bugs(self) -> int:
        return sum(1 for r in self.rows if r.kind == "bug")

    @property
    def kept_real(self) -> int:
        """Real findings that the verifier confirmed. A finding with no verdict is not counted:
        counting it as kept would make a verifier that returned nothing look perfect."""
        return sum(1 for r in self.rows if r.kind == "bug" and r.verdict in (CONFIRMED, "-"))

    @property
    def dropped_real(self) -> int:
        return self._count("bug", UNSUPPORTED)

    @property
    def caught_unlabeled(self) -> int:
        return self._count("unlabeled", UNSUPPORTED)

    @property
    def unlabeled(self) -> int:
        return sum(1 for r in self.rows if r.kind == "unlabeled")

    @property
    def anchor_before(self) -> int:
        return sum(1 for r in self.rows if r.kind == "bug" and r.anchor_before)

    @property
    def anchor_after(self) -> int:
        return sum(1 for r in self.rows if r.kind == "bug" and r.anchor_after)

    @property
    def unverified(self) -> int:
        return sum(1 for r in self.rows if r.verdict == "none")

    @property
    def unsupported_without_code(self) -> int:
        return sum(1 for r in self.rows if r.verdict == UNSUPPORTED and r.no_code)

    @property
    def no_line(self) -> int:
        return sum(1 for r in self.rows if r.no_line)


def score_condition(
    name: str, per_fixture: list[tuple[object, list[Finding], list[Verdict | None]]]
) -> ConditionResult:
    """*per_fixture* holds (fixture, findings, verdicts) in fixture order. A condition
    with no verifier passes ``None`` for each verdict."""
    result = ConditionResult(name=name)
    for fixture, findings, verdicts in per_fixture:
        for f, v in zip(findings, verdicts, strict=True):
            kind = je.classify(f, fixture)
            matched = next((b for b in fixture.bugs if pe.matches(f, b)), None)  # type: ignore[attr-defined]
            after_line = v.line if (v is not None and v.line is not None) else f.line
            result.rows.append(Row(
                fixture=fixture.name,  # type: ignore[attr-defined]
                where=f"{f.file}:{f.line}", kind=kind,
                verdict="-" if name == "none" else (v.verdict if v is not None else "none"),
                no_code=v is not None and not v.had_code,
                no_line=v is not None and v.line is None,
                line_before=f.line, line_after=after_line,
                anchor_before=matched is not None and _in_range(f.line, matched),
                anchor_after=matched is not None and _in_range(after_line, matched),
                summary=f.finding[:100],
            ))
    return result


def format_report(results: list[ConditionResult]) -> str:
    lines = [
        "",
        f"{'condition':14} {'findings':>8} {'kept real':>10} {'dropped real':>13} "
        f"{'caught unlabeled':>17} {'anchor exact':>14} {'no verdict':>11} {'no line':>8} {'unsup, no code':>15}",
        "-" * 118,
    ]
    for r in results:
        lines.append(
            f"{r.name:14} {len(r.rows):>8} {f'{r.kept_real}/{r.bugs}':>10} {r.dropped_real:>13} "
            f"{f'{r.caught_unlabeled}/{r.unlabeled}':>17} "
            f"{f'{r.anchor_before}->{r.anchor_after}/{r.bugs}':>14} {r.unverified:>11} "
            f"{r.no_line:>8} {r.unsupported_without_code:>15}"
        )
    base = results[0]
    lines += ["", "Verdict and line for every finding (b = matches a labeled bug, ? = no label, a = accepted):", ""]
    lines.append(f"{'finding':44} {'kind':>4} " + " ".join(f"{r.name:>16}" for r in results[1:]))
    for i, row in enumerate(base.rows):
        marks = []
        for r in results[1:]:
            other = r.rows[i]
            moved = "" if other.line_after == other.line_before else f" ->{other.line_after}"
            marks.append(f"{other.verdict + moved:>16}")
        tag = {"bug": "b", "unlabeled": "?", "accepted": "a"}[row.kind]
        lines.append(f"{(row.fixture[:20] + ' ' + row.where)[:44]:44} {tag:>4} " + " ".join(marks))
    return "\n".join(lines)


# --- running -----------------------------------------------------------------


def estimate(per_fixture, conditions, guard):  # type: ignore[no-untyped-def]
    """Expected units, worst-case units and call count for the pre-flight check."""
    expected = worst = calls = 0
    for _fx, findings in per_fixture:
        if not findings:
            continue
        for c in conditions:
            code = int(_TOKENS_PER_CODE_CHAR * c.max_chars * len(findings))
            tokens = _BASE_TOKENS + _TOKENS_PER_FINDING * len(findings) + code
            expected += guard.estimate_units(c.model, input_tokens=tokens, output_tokens=_EXPECTED_OUTPUT)
            worst += guard.estimate_units(c.model, input_tokens=tokens, output_tokens=_MAX_OUTPUT, worst_case=True)
            calls += 1
    return expected, worst, calls


async def run_conditions(per_fixture, conditions, cache, guard):  # type: ignore[no-untyped-def]
    ce = pe._load_consistency_eval()
    cached = pe._CachedGuard(cache, guard if guard is not None else pe._NoCallsAllowed())
    prompt_text = PROMPT.read_text()
    results = [score_condition("none", [(fx, fs, [None] * len(fs)) for fx, fs in per_fixture])]

    async def llm_call(req: LLMRequest) -> LLMResponse:
        return await cached.call(lambda r: ce.call_llm(r, pe.PROVIDER), req)

    for cond in conditions:
        scored = []
        for fx, findings in per_fixture:
            print(f"  verify {cond.name} :: {fx.name} ({len(findings)} findings)", flush=True)
            verdicts = await verify_findings(
                findings, llm_call=llm_call, cond=cond, prompt_text=prompt_text,
                diff_text=fx.diff_path.read_text() if findings else "",
            )
            scored.append((fx, findings, verdicts))
        results.append(score_condition(cond.name, scored))
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
        conditions = _selected(os.environ.get("AI_EVAL_VERIFY_CONDITIONS", ""))
        if not conditions:
            raise ValueError(f"no verifier condition selected; choose from {_ALL_NAMES[1:]}")
        fixtures = pe.load_fixtures()
        cache = _llm_cache.LLMCache.from_env(namespace=pe.PROVIDER)
        reviewer_cache = _llm_cache.LLMCache(mode="replay", namespace=pe.PROVIDER)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    models = pe._csv("AI_EVAL_MODELS", pe.DEFAULT_MODELS)
    agents = pe._csv("AI_EVAL_AGENTS", pe.DEFAULT_AGENTS)
    print(f"verify eval: {len(fixtures)} fixtures, reviewer {models[0]} (replayed), "
          f"conditions={[c.name for c in conditions]}, cache mode={cache.mode}")
    try:
        per_fixture = await je.collect_findings(fixtures, models, agents, reviewer_cache)
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
            guard = _spend_guard.SpendGuard.from_env("verify_eval")
            expected, worst, calls = estimate(per_fixture, conditions, guard)
            print(f"pre-flight: up to {calls} billed verifier calls (fewer if responses are saved)")
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
    except (VerifyError, RuntimeError, _llm_cache.CacheError, _llm_cache.CacheMiss,
            _spend_guard.SpendGuardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(format_report(results))
    incomplete = [r for r in results if r.unverified]
    if incomplete:
        print("\nERROR: the verifier gave no usable verdict for some findings "
              f"({', '.join(f'{r.name}: {r.unverified}' for r in incomplete)}). The 'kept real' "
              "counts leave them out. Fix the prompt or the reply format before you trust this run.",
              file=sys.stderr)
    print(f"\ncache: {cache.stats.hits} replayed, {cache.stats.misses} called, {cache.stats.saved} saved")
    if guard is not None:
        print(f"spend guard: this run counted {_spend_guard.usd(guard.run_spent_units)}, campaign "
              f"{_spend_guard.usd(guard.campaign_spent_units())} of {_spend_guard.usd(guard.campaign_cap_units)}")
    if json_path:
        Path(json_path).write_text(json.dumps([
            {"condition": r.name, "findings": len(r.rows), "bugs": r.bugs, "kept_real": r.kept_real,
             "dropped_real": r.dropped_real, "caught_unlabeled": r.caught_unlabeled,
             "anchor_before": r.anchor_before, "anchor_after": r.anchor_after,
             "no_verdict": r.unverified, "rows": [row.__dict__ for row in r.rows]}
            for r in results
        ], indent=2) + "\n")
        print(f"wrote {json_path}")
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
