"""Findings deduplication and merging.

Proximity-based dedup: findings in the same file within PROXIMITY_LINES of
each other are clustered; the highest-severity finding survives with a union
of sources. Style findings (`docs`, and findings that an analyzer marks
`style_only`) cluster only with style findings of their own category, and a cluster counts as corroborated only when an analyzer
member and an agent member describe the same kind of defect (#1074). When the
cluster mixes two different analyzer tools, the other tool's message is kept in
`Finding.also_reported` so it is not lost (#1044).

Closes #185 (dedup preserves distinct nearby findings; merges exact
cross-source duplicates).
"""

from __future__ import annotations

import logging

from ai_pr_review.findings.models import Finding, Severity, combine_also_reported
from ai_pr_review.findings.provenance import boosted_confidence, is_corroborated_cluster
from ai_pr_review.findings.scope import is_analyzer_source

logger = logging.getLogger(__name__)

_SEVERITY_ORDER: dict[Severity, int] = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
}

# Findings within this many lines of each other in the same file are
# candidates for dedup.
PROXIMITY_LINES = 3

def _is_style(f: Finding) -> bool:
    """Does `f` report style or documentation only, and not a defect in how the code behaves?

    A `docs` finding does. So does a finding that an analyzer marked `style_only`
    (a phpcs style sniff, a ruff formatting or naming code, an eslint formatting
    rule). ruff, eslint and phpcs use the category `lint` for both style and real
    defects, so the analyzers decide per rule and the category cannot.

    A style finding clusters only with style findings of its own category, and a
    finding that is not style-only never joins a cluster that has one. Without
    this rule, the "other" wildcard below let a phpcs "Missing doc comment"
    finding absorb a nearby security finding (#1074).
    """
    return f.category == "docs" or f.style_only


# A finding from one of these sources never joins a cluster. The
# suppression-check finding names a comment that turns off a security rule on
# a line. If it clustered with the agent finding for that line, the agent
# finding (higher severity) would win the headline and the suppression would
# vanish from the review (#1077).
_STANDALONE_SOURCES = frozenset({"suppression-check"})


def _is_standalone(f: Finding) -> bool:
    return any(s in _STANDALONE_SOURCES for s in (f.sources or ([f.source] if f.source else [])))


def merge_findings(
    findings: list[Finding],
    *,
    confidence_threshold: int = 75,
) -> list[Finding]:
    """Filter by confidence, then deduplicate by proximity.

    Returns a list of unique findings sorted by severity (highest first)
    then by file+line.
    """
    # 1. Filter by confidence
    filtered = [f for f in findings if f.confidence >= confidence_threshold]

    # 2. Proximity-based dedup within each file
    return _deduplicate(filtered)


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    """Cluster findings within PROXIMITY_LINES of each other in the same file."""
    # Group by file
    by_file: dict[str, list[Finding]] = {}
    no_file: list[Finding] = []
    for f in findings:
        if f.file:
            by_file.setdefault(f.file, []).append(f)
        else:
            no_file.append(f)

    result: list[Finding] = []
    for file_findings in by_file.values():
        result.extend(_dedup_file(file_findings))

    # Findings without file go through text-based dedup (no proximity merging)
    result.extend(_dedup_no_file(no_file))

    # Sort: severity (highest first), then file, then line
    result.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 99),
            f.file,
            f.line or 0,
        )
    )
    return result


def categories_compatible(a: str | None, b: str) -> bool:
    """Are two categories compatible for clustering/cross-run matching?

    `"other"` (the default for findings with no categorization signal) and
    `None` (an unrecoverable category — e.g. a legacy inline comment posted
    before the per-comment metadata marker existed) are both wildcards, not a
    deliberate "this is genuinely different" signal: treating either as
    blocking causes false-negative matches. Two known, distinct real
    categories are never compatible. Shared by the within-run proximity
    clustering below and the cross-run fuzzy match in
    `ai_pr_review.vcs._canonical` — both need the identical wildcard
    semantics so a `secret` finding can never be silently matched against an
    incompatible `injection` finding just because one side's category could
    not be determined.
    """
    if a is None or a == "other":
        return True
    if b == "other":
        return True
    return a == b


def _style_flags_agree(m: Finding, f: Finding) -> bool:
    """Do two findings agree on style, where both can carry the flag?

    Only analyzers set `style_only`, and extract resets it on agent output. An
    agent finding therefore reads as not style whatever it reports. Comparing
    the flag against an agent finding would keep an agent `lint` finding away
    from a style hit on the same line that it describes. The category match in
    the caller still keeps such a finding out of a style cluster of another
    category.
    """
    if _analyzer_tools(m) is None or _analyzer_tools(f) is None:
        return True
    return _is_style(m) == _is_style(f)


def _cluster_category_compatible(cluster: list[Finding], f: Finding) -> bool:
    """Can `f` join `cluster` without introducing a second, conflicting real category?

    Compatibility is checked against the cluster's established real category
    (the first non-"other" category seen so far), not just the tail finding.
    Comparing only against the tail would let an "other"-tagged finding bridge
    two incompatible real categories (e.g. secret -> other -> injection all
    within PROXIMITY_LINES of their neighbor), silently dropping one of them
    when the cluster collapses.
    """
    if _is_style(f) or any(_is_style(m) for m in cluster):
        # `categories_compatible` is not used here on purpose: it also drives
        # the cross-run thread match in `vcs/_canonical.py`, and a change to it
        # would change which old threads a new finding updates. Only the
        # within-run clustering is strict.
        return all(m.category == f.category and _style_flags_agree(m, f) for m in cluster)
    established = next((m.category for m in cluster if m.category != "other"), None)
    return categories_compatible(established, f.category)


def _dedup_file(findings: list[Finding]) -> list[Finding]:
    """Deduplicate findings within a single file using proximity clustering."""
    # Sort by line number (None → 0)
    sorted_fs = sorted(findings, key=lambda f: f.line or 0)

    clusters: list[list[Finding]] = []
    for f in sorted_fs:
        f_line = f.line or 0
        # Compare proximity against the last (nearest) finding in the cluster
        # so that a chain of findings within PROXIMITY_LINES all merge
        # together, even if the first finding is outside the window. Of the
        # clusters that accept `f`, join the one whose tail is nearest, and the
        # earlier one on a tie. First-fit let an agent `lint` finding join an
        # earlier style cluster instead of the analyzer finding on its own
        # line, because an agent finding cannot carry `style_only`.
        best_cluster: list[Finding] | None = None
        best_distance = PROXIMITY_LINES + 1
        if not _is_standalone(f):
            for cluster in clusters:
                distance = abs((cluster[-1].line or 0) - f_line)
                if (
                    distance < best_distance
                    and not any(_is_standalone(m) for m in cluster)
                    and _cluster_category_compatible(cluster, f)
                ):
                    best_cluster, best_distance = cluster, distance
        if best_cluster is not None:
            best_cluster.append(f)
        else:
            clusters.append([f])

    return [_collapse_cluster(c) for c in clusters]


_MAX_NOTE_CHARS = 300


def _analyzer_tools(f: Finding) -> frozenset[str] | None:
    """Return the analyzer tool names behind *f*, or None if any source is not an analyzer."""
    sources = f.sources or ([f.source] if f.source else [])
    if not sources or not all(is_analyzer_source(s) for s in sources):
        return None
    return frozenset(s.lower() for s in sources)


def _also_reported_notes(cluster: list[Finding], best: Finding) -> list[str]:
    """Compose one note per distinct message that the collapse would otherwise drop.

    This applies only when `best` is an analyzer finding:

    - An analyzer member from a different tool set than `best` qualifies. Two
      different tools report different rules, so their messages are not
      duplicates.
    - An agent member qualifies too (#1074). The agent text usually names the
      impact in the context of the code, and the analyzer text is a generic
      rule message. The analyzer won the headline only because its severity
      was higher.

    Same-tool members stay collapsed silently. When `best` is an agent
    finding, nothing is added: the badge already says an analyzer agreed.
    """
    best_tools = _analyzer_tools(best)
    if best_tools is None:
        return []
    notes: list[str] = []
    seen: set[str] = set()
    for f in sorted(cluster, key=lambda m: m.line or 0):
        if f is best:
            continue
        tools = _analyzer_tools(f)
        if tools == best_tools:
            continue
        if tools is None:
            names = sorted({s for s in (f.sources or ([f.source] if f.source else [])) if s})
            if not names:
                continue
            label = ", ".join(names)
        else:
            label = ", ".join(sorted(tools))
        text = " ".join(f.finding.split())
        if len(text) > _MAX_NOTE_CHARS:
            text = text[: _MAX_NOTE_CHARS - 1] + "…"
        where = f" (line {f.line})" if f.line else ""
        note = f"{label}{where}: {text}"
        if note not in seen:
            seen.add(note)
            notes.append(note)
    return combine_also_reported([notes])


def _collapse_cluster(cluster: list[Finding]) -> Finding:
    """Collapse a cluster to the highest-severity representative."""
    best = min(cluster, key=lambda f: _SEVERITY_ORDER.get(f.severity, 99))
    all_sources: list[str] = []
    seen: set[str] = set()
    for f in cluster:
        for s in (f.sources or ([f.source] if f.source else [])):
            if s and s not in seen:
                seen.add(s)
                all_sources.append(s)

    update: dict[str, object] = {"sources": sorted(all_sources)}
    # impact and trigger come from agents only. When an analyzer finding wins the
    # headline, keep the text of the first member that has one (#1078). When an agent
    # wins, its own fields stand: another agent's text may describe another defect.
    if _analyzer_tools(best) is not None:
        for detail in ("impact", "trigger"):
            value = next((getattr(m, detail) for m in cluster if getattr(m, detail)), "")
            if value:
                update[detail] = value
    also_reported = _also_reported_notes(cluster, best)
    if also_reported:
        update["also_reported"] = also_reported
    # _cluster_category_compatible guarantees a surviving cluster has at most
    # one distinct non-"other" category. Preserve it even if `best` (chosen by
    # severity alone) happens to be the "other"-tagged member.
    non_other_categories = {f.category for f in cluster if f.category != "other"}
    if len(non_other_categories) == 1:
        update["category"] = non_other_categories.pop()
    elif len(non_other_categories) > 1:
        # Should be unreachable: _cluster_category_compatible() gates cluster
        # membership so a surviving cluster never has more than one distinct
        # non-"other" category. Log loudly rather than silently discarding the
        # conflict if that invariant is ever broken by a future change.
        logger.warning(
            "merge: cluster invariant violated — %d distinct non-'other' "
            "categories in one cluster (%s); keeping best.category=%r",
            len(non_other_categories),
            sorted(non_other_categories),
            best.category,
        )
    if is_corroborated_cluster(cluster):
        update["corroborated"] = True
        update["confidence"] = boosted_confidence(best.confidence)
    return best.model_copy(update=update)


def _dedup_no_file(findings: list[Finding]) -> list[Finding]:
    """Deduplicate body-only findings by exact finding-text match."""
    seen: dict[str, Finding] = {}
    for f in findings:
        key = f.finding.strip()
        if key not in seen:
            seen[key] = f
        else:
            existing = seen[key]
            merged_sources = sorted(
                set((existing.sources or []) + (f.sources or []))
            )
            seen[key] = existing.model_copy(update={"sources": merged_sources})
    return list(seen.values())
