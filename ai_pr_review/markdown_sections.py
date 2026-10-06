"""Markdown section helpers: find, mask and remove top-level `## ` sections.

Pure string utilities with no dependency on the rest of the package, so both
the pr-summarizer (`agents/summarizer.py`) and the VCS adapters (`vcs/`) can
import them without a cycle. A `## ` line inside a fenced code block is never
treated as a section boundary.
"""

from __future__ import annotations

import re

SECTION_HEADING = re.compile(r"^##\s+(?P<name>.+?)\s*$", re.MULTILINE)
CODE_FENCE = re.compile(r"^(?P<indent>\s*)(?P<fence>`{3,}|~{3,})", re.MULTILINE)


def _is_closing_fence(line_body: str, opener: str) -> bool:
    """Return True if line_body closes a fence opened with `opener`.

    CommonMark: the closing fence must use the same fence character as the
    opener, be at least as long, be indented at most 3 spaces, and have
    nothing but optional trailing whitespace after the fence run.
    """
    char = opener[0]
    min_len = len(opener)
    stripped = line_body.lstrip()
    if not stripped.startswith(char):
        return False
    run_len = 0
    while run_len < len(stripped) and stripped[run_len] == char:
        run_len += 1
    if run_len < min_len:
        return False
    return stripped[run_len:].strip() == ""


def strip_fenced_code(raw: str) -> str:
    """Mask content inside fenced code blocks, preserving byte offsets.

    Each masked character is replaced with a space (newlines preserved) so
    regex positions computed on the masked string map 1:1 to positions in
    `raw`. This lets the section splitter safely slice `raw` using
    offsets derived from the masked scan.

    CommonMark: a fenced block is closed by a line of the same fence
    character whose length is at least the opener's length. We preserve
    the full opener so a ````markdown`-opened block doesn't get closed
    by a triple-backtick line used inside it.
    """
    out: list[str] = []
    fence: str | None = None
    for line in raw.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        body = line[: -len(newline)] if newline else line
        if fence is None:
            match = CODE_FENCE.match(line)
            if match:
                fence = match.group("fence")
                out.append(line)
            else:
                out.append(line)
        elif _is_closing_fence(body, fence):
            fence = None
            out.append(line)
        else:
            out.append(" " * len(body) + newline)
    return "".join(out)


def find_section_span(raw: str, name: str) -> tuple[int, int] | None:
    """Return the (start, end) char offsets of a top-level `## <name>` section.

    The span runs from the start of the heading line through the character
    before the next top-level heading, or through the end of `raw` if it's
    the last section. Matching is done on a fenced-code-masked copy (via
    `strip_fenced_code`) so a `## ` line inside a code fence can't be
    mistaken for a section boundary, but the returned offsets index into the
    original `raw` string. Returns None if no section named `name` (matched
    case-insensitively) is found.
    """
    masked = strip_fenced_code(raw)
    matches = list(SECTION_HEADING.finditer(masked))
    for idx, match in enumerate(matches):
        if match.group("name").strip().lower() != name:
            continue
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        return start, end
    return None


def strip_walkthrough_section(markdown: str) -> str:
    """Remove the `## Walkthrough` section (heading and table) from `markdown`.

    Used when `AI_SUPPRESS_WALKTHROUGH` is on. Applied as a deterministic
    post-process on the model's output, not as a prompt instruction, for the
    same reason as `wrap_walkthrough_in_details`. Returns `markdown` unchanged
    when there is no `## Walkthrough` heading, and also when removing the
    section would leave nothing: an empty summary reads as "no summary" to the
    orchestrator, which then treats the run as incremental and posts nothing.
    """
    span = find_section_span(markdown, "walkthrough")
    if span is None:
        return markdown
    start, end = span
    head = markdown[:start].rstrip()
    tail = markdown[end:].lstrip("\n")
    if not head and not tail.strip():
        return markdown
    if tail:
        return f"{head}\n\n{tail}" if head else tail
    return head + ("\n" if markdown.endswith("\n") else "")
