#!/usr/bin/env python3
"""Turn a docs/version-history/vX.Y.Z.md page into GitHub release notes.

Used by .github/workflows/draft-release.yml. The page is written for the docs
site, so three things are removed to leave only what belongs in a release body:

- the Jekyll front matter block at the top,
- the level-1 page title (the release already has a title),
- the closing "See [CHANGELOG.md](...)" line, which is a docs-site pointer.

Prints the notes to stdout. Exit codes: 0 notes printed, 1 the page produced
no notes, 2 the page could not be read.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_TITLE = re.compile(r"\A\s*# [^\n]*\n")
# Anchored to the end of the text so a CHANGELOG link in the middle of a
# section is never removed.
_CHANGELOG_LINE = re.compile(r"^See \[CHANGELOG\.md\]\([^)]*\)[^\n]*\s*\Z", re.MULTILINE)


def build_notes(text: str) -> str:
    """Return release notes for a version-history page, or "" if nothing is left."""
    body = _FRONT_MATTER.sub("", text, count=1)
    body = _TITLE.sub("", body, count=1)
    body = _CHANGELOG_LINE.sub("", body)
    body = body.strip()
    return f"{body}\n" if body else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes.py <version-history-page.md>", file=sys.stderr)
        return 2
    page = Path(argv[1])
    try:
        text = page.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {page}: {exc}", file=sys.stderr)
        return 2
    notes = build_notes(text)
    if not notes:
        print(f"{page} produced no release notes", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
