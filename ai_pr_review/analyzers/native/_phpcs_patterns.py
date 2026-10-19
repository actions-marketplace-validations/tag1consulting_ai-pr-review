"""Exclude-pattern matcher for the phpcs analyzer.

Applies a `type="relative"` exclude pattern from a repo ruleset without a
backtracking regex, because a hostile repo controls both the pattern and the
file names (ReDoS). Split out of `phpcs.py` as a pure move.
"""

from __future__ import annotations

import re


def _segment_regex(segment: str) -> re.Pattern[str]:
    """Compile one `*`-free piece of a pattern. No quantifier, so matching is linear per position."""
    out: list[str] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "\\" and i + 1 < len(segment):
            out.append(re.escape(segment[i + 1]))
            i += 2
            continue
        out.append("." if ch == "." else re.escape(ch))
        i += 1
    return re.compile("".join(out), re.IGNORECASE)


def _alternative_matches(alt: str, path: str) -> bool:
    """Match one `|`-free alternative the way phpcs reads it: `*` is `.*`, search semantics.

    Matches the `*`-separated pieces left to right with the earliest match of
    each. A piece has a fixed length, so the earliest match is also the
    earliest end, which is what makes this greedy walk exact. The cost is
    linear in the path length per piece.
    """
    anchored_start = alt.startswith("^")
    anchored_end = alt.endswith("$") and not alt.endswith("\\$")
    body = alt[1:] if anchored_start else alt
    body = body[:-1] if anchored_end else body
    pieces = body.split("*")
    pos = 0
    for index, piece in enumerate(pieces):
        regex = _segment_regex(piece)
        is_last = index == len(pieces) - 1
        if is_last and anchored_end:
            # The final piece must end at the end of the path. A fixed-length
            # piece can only do that as a suffix of what is left.
            if len(pieces) == 1:
                return (regex.fullmatch(path) if anchored_start else _suffix_match(regex, path, pos)) is not None
            return _suffix_match(regex, path, pos) is not None
        m = regex.match(path) if index == 0 and anchored_start else regex.search(path, pos)
        if m is None:
            return False
        pos = m.end()
    return True


def _suffix_match(regex: re.Pattern[str], path: str, pos: int) -> re.Match[str] | None:
    """Match *regex* so that it ends exactly at the end of *path* and starts at or after *pos*.

    A piece has a fixed length: one character for each literal, `.` or
    escaped character. So there is exactly one possible start.
    """
    length = len(regex.pattern.replace("\\", ""))
    begin = len(path) - length
    if begin < pos:
        return None
    return regex.fullmatch(path, begin)


def _relative_pattern_matches(pattern: str, relative_path: str) -> bool:
    """Apply one `type="relative"` exclude pattern the way phpcs does for a directory scan.

    Only patterns that passed `_pattern_ok` get here. The match never uses a
    backtracking regex over the whole pattern, so a hostile pattern cannot
    stall the review.
    """
    return any(_alternative_matches(alt.replace("\\,", ","), relative_path) for alt in pattern.split("|"))
