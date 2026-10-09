"""A short label that says how much evidence stands behind a finding.

The confidence score alone is a number the reader cannot weigh. The label names
where the evidence came from. It is built only from facts the pipeline already
holds. It is display text and never enters an approval decision.

Labels, in order:

- ``corroborated by analyzer``: a static analyzer and an agent reported the same
  place.
- ``analyzer finding``: a static analyzer reported it alone.
- ``judge kept``: the judge pass ran and kept it.
- ``single agent, unverified``: one agent reported it and the judge did not keep
  it on a verdict (the judge downranked it).
- ``single agent, not judged``: one agent reported it and no judge verdict exists.
"""

from __future__ import annotations

from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.provenance import is_corroborated
from ai_pr_review.findings.scope import is_analyzer_source


def finding_label(finding: Finding) -> str:
    sources = finding.sources or ([finding.source] if finding.source else [])
    if finding.corroborated or is_corroborated(sources):
        return "corroborated by analyzer"
    if sources and all(is_analyzer_source(s) for s in sources):
        return "analyzer finding"
    if finding.judge_verdict == "keep":
        return "judge kept"
    if finding.judge_verdict is None:
        return "single agent, not judged"
    return "single agent, unverified"


def finding_badge(finding: Finding) -> str:
    """The text shown beside a finding, for example ``judge kept, confidence 80``."""
    return f"{finding_label(finding)}, confidence {finding.confidence}"
