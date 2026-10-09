"""Precision and recall of the review pipeline on a labeled corpus.

``consistency_eval.py`` asks whether repeated runs agree. This asks whether a
run is right. Each fixture in ``tests/canary/eval_corpus/`` is a diff plus a
``<name>.labels.json`` that says which bugs are in it (and which findings are
also true but are not the target bug). The harness runs the real review pipeline
over each diff once, matches the findings to the labels, and reports precision
and recall with Wilson 95% intervals. With about 20 fixtures the intervals are
wide, so they are printed, never hidden.

Cost is kept low on purpose:

  * One model, one agent (``code-reviewer``), one run, no repeats. Precision does
    not need repeats, and the consistency question was closed on 2026-07-21.
  * Every model response is saved by ``_llm_cache.py``. A later experiment
    (a judge change, a risk score, a label) replays the saved text through the
    new code at no cost. Only a changed prompt, diff, or model re-calls.
  * Every billed call goes through ``_spend_guard.py``: $5.00 per run and $9.00
    across runs, a pre-flight estimate, and ``--yes`` to start.

Usage:

    python tests/canary/precision_eval.py --dry-run          # estimate, no call
    python tests/canary/precision_eval.py --yes              # record what is missing
    AI_EVAL_CACHE_MODE=replay python tests/canary/precision_eval.py   # no call, ever
    python tests/canary/precision_eval.py --yes --json out.json

Environment: ``AI_EVAL_MODELS`` (default ``claude-sonnet-5-5``),
``AI_EVAL_AGENTS`` (default ``code-reviewer``), ``AI_EVAL_CACHE_MODE``
(``auto``, ``record``, ``replay``), ``AI_EVAL_STATE_DIR``, and the guard's
``AI_EVAL_MAX_COST_USD`` and ``AI_EVAL_CAMPAIGN_CAP_USD``.

Label file format (see ``eval_corpus/README.md``):

    {
      "clean": false,
      "source": "known-fix",            # known-fix, seeded, or harvested
      "evidence": "strong",             # strong, or weak for a clean-by-absence fixture
      "agents": ["code-reviewer"],      # optional, overrides AI_EVAL_AGENTS
      "bugs": [{"file": "a.py", "line_start": 10, "line_end": 12,
                "category": "edge-case", "summary": "unchecked None"}],
      "accepted_extra": [],             # true findings that are not the target bug
      "verified_by": "greg"             # who checked it, or "unverified"
    }
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
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

CORPUS_DIR = CANARY_DIR / "eval_corpus"


def resolve_corpus_dir() -> Path:
    """The fixture directory: ``AI_EVAL_CORPUS_DIR`` if set (for example a corpus
    made by ``mine_fixtures.py``), else the hand-built corpus in the repository."""
    override = os.environ.get("AI_EVAL_CORPUS_DIR", "").strip()
    return Path(override).expanduser() if override else CORPUS_DIR
DEFAULT_MODELS = ("claude-sonnet-5-5",)
DEFAULT_AGENTS = ("code-reviewer",)
LINE_WINDOW = 5  # the harness documents that a model anchors one finding a few lines apart
KEYWORD_JACCARD = 0.4  # same threshold consistency_eval uses to call two findings one issue
PROVIDER = "anthropic"


def _load_consistency_eval():  # type: ignore[no-untyped-def]
    """Load consistency_eval.py by path (tests/canary is not a package)."""
    if "consistency_eval" in sys.modules:
        return sys.modules["consistency_eval"]
    spec = importlib.util.spec_from_file_location("consistency_eval", CANARY_DIR / "consistency_eval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["consistency_eval"] = module
    spec.loader.exec_module(module)
    return module


# --- labels ---------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    file: str
    line_start: int
    line_end: int
    category: str
    summary: str


@dataclass
class Fixture:
    name: str
    diff_path: Path
    clean: bool
    source: str
    evidence: str
    verified_by: str
    bugs: list[Label]
    accepted_extra: list[Label]
    agents: tuple[str, ...] | None = None


def _labels(raw: object, where: str) -> list[Label]:
    if not isinstance(raw, list):
        raise ValueError(f"{where}: expected a list")
    out = []
    for item in raw:
        try:
            out.append(Label(
                file=str(item["file"]),
                line_start=int(item["line_start"]),
                line_end=int(item["line_end"]),
                category=str(item["category"]),
                summary=str(item["summary"]),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{where}: bad label {item!r}: {exc}") from exc
    return out


def load_fixtures(corpus_dir: Path | None = None) -> list[Fixture]:
    fixtures = []
    directory = corpus_dir if corpus_dir is not None else resolve_corpus_dir()
    for diff in sorted(directory.glob("*.diff")):
        label_path = diff.with_suffix(".labels.json")
        if not label_path.is_file():
            raise ValueError(f"{diff.name} has no {label_path.name}")
        data = json.loads(label_path.read_text())
        bugs = _labels(data.get("bugs", []), f"{label_path.name} bugs")
        clean = bool(data.get("clean", False))
        if clean and bugs:
            raise ValueError(f"{label_path.name}: a clean fixture cannot list bugs")
        if not clean and not bugs:
            raise ValueError(f"{label_path.name}: a fixture with no bugs must set clean=true")
        agents = data.get("agents")
        fixtures.append(Fixture(
            name=diff.stem,
            diff_path=diff,
            clean=clean,
            source=str(data.get("source", "")),
            evidence=str(data.get("evidence", "strong")),
            verified_by=str(data.get("verified_by", "unverified")),
            bugs=bugs,
            accepted_extra=_labels(data.get("accepted_extra", []), f"{label_path.name} accepted_extra"),
            agents=tuple(agents) if agents else None,
        ))
    return fixtures


# --- matching -------------------------------------------------------------


def _same_file(finding_file: str, label_file: str) -> bool:
    def norm(path: str) -> str:
        # Strip a literal "./" prefix or leading slashes, not every "." or "/" character.
        return path[2:] if path.startswith("./") else path.lstrip("/")

    a, b = norm(finding_file), norm(label_file)
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def matches(finding: Finding, label: Label, *, window: int = LINE_WINDOW,
            jaccard: float = KEYWORD_JACCARD) -> bool:
    """True when *finding* is about *label*: same file, near the lines (a
    finding with no line never matches), and the same category or enough shared keywords."""
    ce = _load_consistency_eval()
    if not _same_file(finding.file, label.file):
        return False
    line = finding.line or finding.start_line
    start = finding.start_line or finding.line
    if line is None or start is None:
        # No line anchor: it cannot be matched to a bug, so it goes to the
        # "needs adjudication" list instead of inflating precision or recall.
        return False
    low, high = min(start, line), max(start, line)
    if high < label.line_start - window or low > label.line_end + window:
        return False
    same_topic = finding.category == label.category or (
        ce._jaccard(ce._keywords(finding.finding), ce._keywords(label.summary)) >= jaccard
    )
    return same_topic


@dataclass
class FixtureScore:
    name: str
    clean: bool
    evidence: str
    findings: int
    bugs_total: int
    bugs_found: int
    true_positive_findings: int
    accepted_findings: int
    unlabeled: list[Finding] = field(default_factory=list)
    missed: list[Label] = field(default_factory=list)


def score_fixture(findings: list[Finding], fixture: Fixture) -> FixtureScore:
    tp = accepted = 0
    unlabeled: list[Finding] = []
    found_bug_idx: set[int] = set()
    for f in findings:
        hit = [i for i, bug in enumerate(fixture.bugs) if matches(f, bug)]
        if hit:
            tp += 1
            found_bug_idx.update(hit)
        elif any(matches(f, extra) for extra in fixture.accepted_extra):
            accepted += 1
        else:
            unlabeled.append(f)
    return FixtureScore(
        name=fixture.name,
        clean=fixture.clean,
        evidence=fixture.evidence,
        findings=len(findings),
        bugs_total=len(fixture.bugs),
        bugs_found=len(found_bug_idx),
        true_positive_findings=tp,
        accepted_findings=accepted,
        unlabeled=unlabeled,
        missed=[b for i, b in enumerate(fixture.bugs) if i not in found_bug_idx],
    )


# --- statistics -----------------------------------------------------------


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion. ``(0, 1)`` when there is no data."""
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Summary:
    fixtures: int
    clean_fixtures: int
    bugs_total: int
    bugs_found: int
    findings: int
    true_positives: int
    accepted: int
    unlabeled: int

    @property
    def precision(self) -> tuple[float, tuple[float, float]]:
        denominator = self.true_positives + self.unlabeled
        value = self.true_positives / denominator if denominator else 0.0
        return value, wilson(self.true_positives, denominator)

    @property
    def recall(self) -> tuple[float, tuple[float, float]]:
        value = self.bugs_found / self.bugs_total if self.bugs_total else 0.0
        return value, wilson(self.bugs_found, self.bugs_total)


def summarize(scores: list[FixtureScore]) -> Summary:
    return Summary(
        fixtures=len(scores),
        clean_fixtures=sum(1 for s in scores if s.clean),
        bugs_total=sum(s.bugs_total for s in scores),
        bugs_found=sum(s.bugs_found for s in scores),
        findings=sum(s.findings for s in scores),
        true_positives=sum(s.true_positive_findings for s in scores),
        accepted=sum(s.accepted_findings for s in scores),
        unlabeled=sum(len(s.unlabeled) for s in scores),
    )


def format_report(scores: list[FixtureScore]) -> str:
    s = summarize(scores)
    p, (plo, phi) = s.precision
    r, (rlo, rhi) = s.recall
    lines = [
        "",
        f"fixtures: {s.fixtures} ({s.clean_fixtures} clean), bugs labeled: {s.bugs_total}, "
        f"findings: {s.findings}",
        f"recall:    {s.bugs_found}/{s.bugs_total} = {r:.2f}  (95% interval {rlo:.2f} to {rhi:.2f})",
        f"precision: {s.true_positives}/{s.true_positives + s.unlabeled} = {p:.2f}  "
        f"(95% interval {plo:.2f} to {phi:.2f})",
        f"findings matching a true but non-target label (not counted either way): {s.accepted}",
        f"findings with no label (counted as false positives, adjudicate them): {s.unlabeled}",
        "",
        "Unlabeled findings are false positives only if no human says otherwise. Clean fixtures",
        "marked evidence=weak mean 'no known bug', so a finding there may be real.",
        "",
    ]
    for sc in scores:
        flag = "clean" if sc.clean else f"{sc.bugs_found}/{sc.bugs_total} bugs"
        lines.append(f"  {sc.name:48} {flag:12} findings={sc.findings} tp={sc.true_positive_findings} "
                     f"accepted={sc.accepted_findings} unlabeled={len(sc.unlabeled)}")
        for f in sc.unlabeled:
            lines.append(f"      ? [{f.severity}] {f.file}:{f.line} {f.finding[:110]}")
        for b in sc.missed:
            lines.append(f"      MISSED {b.file}:{b.line_start}-{b.line_end} {b.summary[:90]}")
    return "\n".join(lines)


# --- running --------------------------------------------------------------


class _CachedGuard:
    """What ``consistency_eval._one_run`` calls instead of the provider directly.

    Cache outside, guard inside: a replayed response never reserves or charges.
    """

    def __init__(self, cache: _llm_cache.LLMCache, guard: object) -> None:
        self._cache = cache
        self._guard = guard

    async def call(self, llm_call, request):  # type: ignore[no-untyped-def]
        return await self._cache.call(lambda r: self._guard.call(llm_call, r), request)  # type: ignore[attr-defined]


class _NoCallsAllowed:
    """Stands in for the guard in replay mode: any attempt to bill is a bug."""

    async def call(self, llm_call, request):  # type: ignore[no-untyped-def]
        raise _llm_cache.CacheMiss("replay mode: no billed call is allowed")


async def run_fixtures(
    fixtures: list[Fixture], models: tuple[str, ...], default_agents: tuple[str, ...],
    cache: _llm_cache.LLMCache, guard: _spend_guard.SpendGuard | None,
) -> dict[str, list[FixtureScore]]:
    ce = _load_consistency_eval()
    # Cache outside, guard (or a guard that forbids any call) inside.
    ce._GUARD = _CachedGuard(cache, guard if guard is not None else _NoCallsAllowed())
    results: dict[str, list[FixtureScore]] = {}
    for model in models:
        scores = []
        for fx in fixtures:
            agents = fx.agents or default_agents
            print(f"  {fx.name} :: {model} :: {','.join(agents)} ...", flush=True)
            outcome = await ce._one_run(model, agents, fx.diff_path, "baseline", REPO_ROOT)
            if not outcome.ok:
                raise RuntimeError(f"{fx.name}: the run failed, so its score would be wrong: {outcome.detail}")
            scores.append(score_fixture(outcome.findings, fx))
        results[model] = scores
    return results


def _estimate(fixtures: list[Fixture], models: tuple[str, ...], default_agents: tuple[str, ...],
              guard: _spend_guard.SpendGuard) -> tuple[int, int, int]:
    ce = _load_consistency_eval()
    expected = worst = calls = 0
    for fx in fixtures:
        e, w, c = ce._estimate_invocation(
            [fx.diff_path], 1, fx.agents or default_agents, ("baseline",), models, guard)
        expected, worst, calls = expected + e, worst + w, calls + c
    return expected, worst, calls


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    items = tuple(s.strip() for s in raw.split(",") if s.strip())
    return items or default


async def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    json_path = argv[argv.index("--json") + 1] if "--json" in argv else ""
    models = _csv("AI_EVAL_MODELS", DEFAULT_MODELS)
    agents = _csv("AI_EVAL_AGENTS", DEFAULT_AGENTS)
    try:
        fixtures = load_fixtures()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not fixtures:
        print(f"ERROR: no fixtures in {resolve_corpus_dir()}", file=sys.stderr)
        return 1
    try:
        cache = _llm_cache.LLMCache.from_env(namespace=PROVIDER)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"precision eval: {len(fixtures)} fixtures, models={list(models)}, "
          f"agents={list(agents)}, cache mode={cache.mode}")

    guard: _spend_guard.SpendGuard | None = None
    if cache.mode != "replay":
        if not dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
            print("ERROR: ANTHROPIC_API_KEY not set (use AI_EVAL_CACHE_MODE=replay to run "
                  "from saved responses with no key).", file=sys.stderr)
            return 1
        try:
            guard = _spend_guard.SpendGuard.from_env("precision_eval")
            expected, worst, calls = _estimate(fixtures, models, agents, guard)
            print(f"pre-flight: up to {calls} billed calls (fewer if responses are already saved)")
            guard.preflight(expected_units=expected, worst_case_units=worst,
                            yes=dry_run or _spend_guard.wants_yes(argv))
        except _spend_guard.SpendGuardError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
    if dry_run:
        print("dry run: no billed calls were made")
        return 0

    try:
        results = await run_fixtures(fixtures, models, agents, cache, guard)
    except (RuntimeError, _llm_cache.CacheError, _spend_guard.SpendGuardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for model, scores in results.items():
        print(f"\n=== {model} ===")
        print(format_report(scores))
    print(f"\ncache: {cache.stats.hits} replayed, {cache.stats.misses} called, {cache.stats.saved} saved")
    if guard is not None:
        print(f"spend guard: this run counted {_spend_guard.usd(guard.run_spent_units)}, campaign "
              f"{_spend_guard.usd(guard.campaign_spent_units())} of {_spend_guard.usd(guard.campaign_cap_units)}")
    if json_path:
        out = {
            model: {
                "summary": summarize(scores).__dict__,
                "fixtures": [
                    {"name": s.name, "clean": s.clean, "findings": s.findings, "bugs_total": s.bugs_total,
                     "bugs_found": s.bugs_found, "true_positive_findings": s.true_positive_findings,
                     "accepted_findings": s.accepted_findings,
                     "unlabeled": [f.model_dump() for f in s.unlabeled],
                     "missed": [m.__dict__ for m in s.missed]}
                    for s in scores
                ],
            }
            for model, scores in results.items()
        }
        Path(json_path).write_text(json.dumps(out, indent=2) + "\n")
        print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
