"""Native Python implementation of the suppression-check analyzer.

Flags a line that a PR adds when a comment on it turns off a security check.
Examples are ``# noqa: S301``, ``# nosec``, ``# nosemgrep``, ``// phpcs:ignore
Drupal.Security...`` and ``// eslint-disable-next-line security/...``.

A reviewer reads the code, not the comment that hides it from the linters. On a
seeded-flaw PR, a ``# noqa: S301`` comment on an unsafe deserialization call
turned off the exact rule that reports the defect, and ruff then reported only
that the ``noqa`` was unused (#1077).

The analyzer is pure Python and offline. It reads the staged diff and looks at
added lines only. A directive counts only after a comment marker on the same
line. The finding names the directive and its rule codes, and never quotes
other text from the comment, because the comment is PR content.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ai_pr_review.diff.parse import parse_diff
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 85
_SOURCE = "suppression-check"

# A comment marker. A directive counts only in the text after the first marker
# on the line. The marker is found once and the directive patterns run on the
# rest of the line, so no pattern needs a lazy prefix. A lazy prefix after every
# marker is quadratic on a line made of many markers, and a PR author controls
# that line.
_MARKER = re.compile(r"#|//|/\*|<!--")

# Rule names and codes are copied into the finding text. Keep only characters
# that a rule name can hold, so a comment cannot smuggle prose into a review.
_RULE_TOKEN = re.compile(r"[A-Za-z0-9_.\-/@:]+")

_NOQA = re.compile(r"\bnoqa\s*:\s*([A-Za-z0-9_, \t]+)", re.IGNORECASE)
_NOSEC = re.compile(r"\bnosec\b", re.IGNORECASE)
_NOSEMGREP = re.compile(r"\bnosemgrep\b", re.IGNORECASE)
_PHPCS = re.compile(r"\b(phpcs:(?:ignore|disable)\w*)[ \t]+([^\n*]+)", re.IGNORECASE)
_ESLINT = re.compile(r"\beslint-disable(?:-next-line|-line)?[ \t]+([^\n*]+)", re.IGNORECASE)

_RUFF_SECURITY_CODE = re.compile(r"^S\d{3}$")
# A rule is a security rule when one segment of its name is in these sets, or when a
# segment contains the word "security" (the PHPCS_SecurityAudit standard and the
# eslint-plugin-security-node plugin name themselves that way). A segment is a part
# between "." or "/". The other names must match a whole segment, so a word such as
# "retrieval" is not read as the "eval" sniff.
_SECURITY_WORD = "security"
_PHPCS_SECURITY_SEGMENTS = frozenset({"forbiddenfunctions", "eval"})
_ESLINT_SECURITY_SEGMENTS = frozenset({
    "no-eval",
    "no-implied-eval",
    "no-new-func",
    "no-unsanitized",
    "no-danger",
    "no-script-url",
})

# The reason that phpcs and eslint accept after the rule list starts with two or more
# dashes, after whitespace and before whitespace or the end of the line. That is the
# separator the tools use. " --reason" is not one, so ESLint reads "no-console --x, no-eval"
# as two rules and disables no-eval. A wider splitter would hide the second rule from this
# analyzer. A tab, "---" and a bare trailing "--" all count as a separator.
_REASON = re.compile(r"\s-{2,}(?:\s|$)")


def _tokens(text: str) -> list[str]:
    """Return the rule names in a ``phpcs:ignore`` or ``eslint-disable`` rule list.

    Both tools separate rules with commas and put a reason after two dashes. The first
    word of each entry is the rule name. Words after it are prose, and prose is never read
    as a rule and never copied into the finding.
    """
    rules = _REASON.split(text, maxsplit=1)[0]
    found: list[str] = []
    for part in rules.split(","):
        match = _RULE_TOKEN.match(part.strip())
        if match:
            found.append(match.group(0))
    return found


def _is_security_rule(token: str, segments: frozenset[str]) -> bool:
    return any(
        part in segments or _SECURITY_WORD in part for part in re.split(r"[./]", token.lower())
    )


def _code(tokens: list[str]) -> str:
    # Inline code, so that an `@scope/rule` name cannot mention a user or a team in a comment.
    return ", ".join(f"`{t}`" for t in tokens)


def _describe(text: str) -> list[str]:
    """Return one short label for each security directive found in *text*."""
    labels: list[str] = []
    # The whole line is read, with no length cap: a cap would let a PR author hide a
    # directive behind padding. Every pattern below is linear in the line length.
    marker = _MARKER.search(text)
    if marker is None:
        return labels
    text = text[marker.end() :]

    noqa = _NOQA.search(text)
    if noqa:
        codes = [c for c in re.split(r"[,\s]+", noqa.group(1)) if _RUFF_SECURITY_CODE.match(c.upper())]
        if codes:
            labels.append("noqa: " + ", ".join(c.upper() for c in codes))
    if _NOSEC.search(text):
        labels.append("nosec")
    if _NOSEMGREP.search(text):
        labels.append("nosemgrep")

    phpcs = _PHPCS.search(text)
    if phpcs:
        rules = [t for t in _tokens(phpcs.group(2)) if _is_security_rule(t, _PHPCS_SECURITY_SEGMENTS)]
        if rules:
            labels.append(f"{phpcs.group(1).lower()} {_code(rules)}")

    eslint = _ESLINT.search(text)
    if eslint:
        rules = [t for t in _tokens(eslint.group(1)) if _is_security_rule(t, _ESLINT_SECURITY_SEGMENTS)]
        if rules:
            labels.append("eslint-disable " + _code(rules))

    return labels


def _run_suppression_check(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Flag added lines whose comment turns off a security rule."""
    source_files = set(changed_files.source)
    if not source_files:
        return []
    try:
        # The diff holds PR content, so it can hold any byte. A strict decode would
        # raise on one bad byte and skip the whole analyzer, which a PR author could
        # use to hide a directive. Replace what cannot be decoded and keep reading.
        diff_text = diff_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: suppression-check could not read the diff: %s", exc)
        return []

    findings: list[Finding] = []
    for file_diff in parse_diff(diff_text):
        path = file_diff.new_path
        if path is None or path not in source_files:
            continue
        for hunk in file_diff.hunks:
            for row in hunk.rows:
                if row.marker != "+" or row.new_no is None:
                    continue
                labels = _describe(row.text)
                if not labels:
                    continue
                try:
                    findings.append(
                        Finding(
                            severity="Medium",
                            confidence=_CONFIDENCE,
                            source=_SOURCE,
                            file=path,
                            line=row.new_no,
                            finding=(
                                f"A comment on this line turns off a security check ({'; '.join(labels)}). "
                                "The code stays and the check no longer reports it."
                            ),
                            remediation=(
                                "Fix the code that the rule reports. If the rule is wrong for this line, "
                                "say why in the comment and have a person confirm it."
                            ),
                            category="lint",
                        )
                    )
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "[ai-pr-review] WARNING: suppression-check dropped malformed finding: %s", exc
                    )
    return findings
