"""Parse a unified diff into files, hunks, and rows.

One parser for the code that needs the text of a change and not only the added
line numbers (``analyzers/native/dep_exists.py`` and the code-aware judge experiment
in ``tests/canary/judge_code.py``). Before this module each of them read the headers
its own way, so a rename or a quoted path was handled differently by each.

The parser reads the paths from the ``diff --git`` header, the ``rename from`` and
``rename to`` lines, and the ``---`` and ``+++`` lines. It decodes the C-style
quoting that git uses for a path with a special character. It uses the line counts
in each ``@@`` header to know where a hunk ends, so a removed line whose text
begins with ``-- `` is not read as a file header.

It expects the standard ``a/`` and ``b/`` prefixes. The engine pins them when it
builds a diff (see ``diff/compute.py``). A header with any other prefix gives a
file with no path, and the caller must handle that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SIMPLE_HEADER = re.compile(r"^diff --git a/(.+) b/\1$")
_QUOTED = re.compile(r'^"((?:[^"\\]|\\.)*)"')
_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


@dataclass(frozen=True)
class Row:
    """One line of a hunk. ``old_no`` and ``new_no`` are None where the line is absent."""

    old_no: int | None
    new_no: int | None
    marker: str  # "+", "-", or " "
    text: str


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    rows: tuple[Row, ...] = ()

    @property
    def old_side(self) -> list[str]:
        """The text of the hunk before the change (context and removed lines)."""
        return [r.text for r in self.rows if r.marker != "+"]

    @property
    def new_side(self) -> list[str]:
        """The text of the hunk after the change (context and added lines)."""
        return [r.text for r in self.rows if r.marker != "-"]


@dataclass
class FileDiff:
    """One file of a diff. A path is None for ``/dev/null`` or when it could not be read."""

    old_path: str | None = None
    new_path: str | None = None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str | None:
        """The path the file has after the change, or before it when the file is deleted."""
        return self.new_path or self.old_path

    @property
    def renamed(self) -> bool:
        return bool(self.old_path and self.new_path and self.old_path != self.new_path)


def decode_git_path(raw: str) -> str:
    """Undo git's C-style quoting of a path, for example ``"pkg-\\303\\251/x"``.

    A path without quotes is returned as it is. Octal escapes are bytes of a UTF-8
    sequence, so they are collected as bytes and decoded together.
    """
    match = _QUOTED.match(raw)
    if not match:
        return raw
    body = match.group(1)
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[i + 1 :])
            assert digits is not None
            out.append(int(digits.group(0), 8) & 0xFF)
            i += 1 + len(digits.group(0))
        elif nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
        else:
            out += nxt.encode("utf-8")
            i += 2
    return out.decode("utf-8", errors="replace")


def safe_decode_git_path(raw: str) -> str:
    """Decode a git path, unless the result holds a control character.

    A path is attacker-controlled text. A decoded newline would let a file name start a new
    line in a log, for example a line that begins with ``::error::`` in GitHub Actions. A path
    with a control character is returned in its quoted, escaped form, which is safe to print
    and matches no file on disk.
    """
    decoded = decode_git_path(raw)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        return raw
    return decoded


def _strip_prefix(raw: str, prefix: str) -> str | None:
    """A ``---`` or ``+++`` path without its ``a/`` or ``b/`` prefix, or None for /dev/null."""
    raw = raw.split("\t", 1)[0].strip() if not raw.startswith('"') else raw.strip()
    path = safe_decode_git_path(raw)
    if path == "/dev/null":
        return None
    return path[len(prefix) :] if path.startswith(prefix) else None


def _header_path(line: str) -> str | None:
    """The shared path of a ``diff --git a/X b/X`` header, decoded, or None when it differs."""
    simple = _SIMPLE_HEADER.match(line)
    if simple:
        return simple.group(1)
    rest = line[len("diff --git ") :]
    quoted = re.match(r'^("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")$', rest)
    if quoted:
        old, new = safe_decode_git_path(quoted.group(1)), safe_decode_git_path(quoted.group(2))
        if old.startswith("a/") and new.startswith("b/") and old[2:] == new[2:]:
            return new[2:]
    return None


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse *diff_text* into one :class:`FileDiff` per ``diff --git`` block."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    rows: list[Row] = []
    header: tuple[int, int, int, int] | None = None
    old_no = new_no = old_left = new_left = 0

    def close_hunk() -> None:
        nonlocal header, rows
        if current is not None and header is not None:
            current.hunks.append(Hunk(*header, rows=tuple(rows)))
        header = None
        rows = []

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            close_hunk()
            shared = _header_path(raw)
            current = FileDiff(old_path=shared, new_path=shared)
            files.append(current)
            continue
        if current is None:
            continue
        in_hunk = header is not None
        # Inside a hunk the counts say whether a "---" or "+++" line is a row or a header.
        if in_hunk and (old_left > 0 or new_left > 0 or not raw.startswith(("--- ", "+++ ", "@@", "diff "))):
            if raw.startswith("\\"):
                continue
            marker = raw[:1]
            if marker == "+":
                rows.append(Row(None, new_no, "+", raw[1:]))
                new_no += 1
                new_left -= 1
            elif marker == "-":
                rows.append(Row(old_no, None, "-", raw[1:]))
                old_no += 1
                old_left -= 1
            else:
                rows.append(Row(old_no, new_no, " ", raw[1:]))
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
            continue
        match = _HUNK_HEADER.match(raw)
        if match:
            close_hunk()
            old_start = int(match.group(1))
            old_count = 1 if match.group(2) is None else int(match.group(2))
            new_start = int(match.group(3))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            header = (old_start, old_count, new_start, new_count)
            old_no, new_no, old_left, new_left = old_start, new_start, old_count, new_count
            continue
        close_hunk()
        if raw.startswith("rename from "):
            current.old_path = safe_decode_git_path(raw[len("rename from ") :])
        elif raw.startswith("rename to "):
            current.new_path = safe_decode_git_path(raw[len("rename to ") :])
        elif raw.startswith("--- "):
            current.old_path = _strip_prefix(raw[4:], "a/")
        elif raw.startswith("+++ "):
            current.new_path = _strip_prefix(raw[4:], "b/")
        elif raw.startswith("new file mode"):
            current.old_path = None
        elif raw.startswith("deleted file mode"):
            current.new_path = None
    close_hunk()
    return files
