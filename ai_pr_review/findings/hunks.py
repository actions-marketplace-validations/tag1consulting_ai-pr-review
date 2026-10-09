"""Cut the code around a finding out of a unified diff.

The judge pass can read the code a finding cites. It needs only a few lines, not
the whole diff, so this module returns a short window of the hunk that holds the
cited line. The text is untrusted data from the pull request. Callers must pass it
to a model as data and never as instructions.
"""

from __future__ import annotations

import re

_FILE_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

DEFAULT_CONTEXT_LINES = 6
DEFAULT_MAX_CHARS = 1500

# (new-file line number or None for a removed line, marker, text)
_Entry = tuple[int | None, str, str]


def _hunks_for_file(diff_text: str, file: str) -> list[list[_Entry]]:
    hunks: list[list[_Entry]] = []
    in_file = False
    current: list[_Entry] | None = None
    new_no = 0
    for raw in diff_text.splitlines():
        header = _FILE_HEADER.match(raw)
        if header:
            in_file = header.group(2) == file
            current = None
            continue
        if not in_file:
            continue
        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            current = []
            hunks.append(current)
            new_no = int(hunk.group(1))
            continue
        if current is None or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            current.append((new_no, "+", raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            current.append((None, "-", raw[1:]))
        else:
            current.append((new_no, " ", raw[1:]))
            new_no += 1
    return hunks


def extract_hunk(
    diff_text: str,
    file: str,
    line: int | None,
    *,
    context: int = DEFAULT_CONTEXT_LINES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Return the lines around *line* of *file*, or an empty string when the line
    is not in the diff. Each row starts with its new-file line number, then ``+``
    (added), ``-`` (removed), or a space (context). The result is cut at a line
    boundary to *max_chars*."""
    if not file or line is None or line < 1:
        return ""
    for entries in _hunks_for_file(diff_text, file):
        target = next((i for i, (no, _m, _t) in enumerate(entries) if no == line), None)
        if target is None:
            continue
        window = entries[max(target - context, 0): target + context + 1]
        out: list[str] = []
        size = 0
        for no, marker, text in window:
            row = f"{'' if no is None else no:>5} {marker} {text}"
            if size + len(row) + 1 > max_chars and out:
                break
            out.append(row)
            size += len(row) + 1
        return "\n".join(out)
    return ""
