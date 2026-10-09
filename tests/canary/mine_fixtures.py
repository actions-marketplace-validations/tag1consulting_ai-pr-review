"""Mine labeled eval fixtures from this repository's own fix commits. Read-only, free.

A ``fix:`` commit repairs a bug. ``git blame`` on the lines the fix removed or
changed, taken at the fix's parent, names the commit that introduced them. The
introducing commit's diff of that one file is a realistic review input, and the
blamed lines (in that commit's own numbering, which blame reports) are where the
bug sits. The fix commit's message is the plain-words description of the bug.

This gives many labeled fixtures with no model call. The labels are weaker than
the hand-built corpus:

- Blame can point at a refactor or a move, not the real origin of the bug.
- A fix that only adds code (a missing check) removes no line, so it gives no
  fixture. The set is biased toward bugs fixed by editing existing lines.
- The label summary is the fix commit's message, not a one-line bug statement.

Every fixture is marked ``source: mined`` and ``evidence: weak``. Findings the
reviewer makes on a mined fixture that match no label are NOT proven false
positives, so a person must read them (see ``judge_eval.py``).

    python tests/canary/mine_fixtures.py --out tests/canary/eval_corpus_mined --limit 80
    python tests/canary/mine_fixtures.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANARY_DIR = Path(__file__).resolve().parent

MAX_DIFF_BYTES = 9_000
MAX_LABEL_SPAN = 25
MAX_SUMMARY_CHARS = 260
# A fix that touches many files is usually a review-feedback sweep, so its message
# does not describe the bug in any one file.
MAX_FIX_FILES = 3
SOURCE_GLOB = "ai_pr_review/*.py"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_SHA = re.compile(r"^[0-9a-f]{40}$")


GIT_FAILURES: list[str] = []


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        if check:
            raise RuntimeError(f"git {' '.join(args[:3])} failed: {result.stderr[:200]}")
        # A caller that tolerates failure still gets a record of it, so a broken git
        # setup is not mistaken for "this commit has no data".
        GIT_FAILURES.append(f"git {' '.join(args[:3])}: {result.stderr.strip()[:120]}")
    return result.stdout


@dataclass(frozen=True)
class Mined:
    introducing: str
    fix: str
    file: str
    line_start: int
    line_end: int
    summary: str
    diff: str


def removed_ranges(patch: str) -> list[tuple[int, int]]:
    """Old-file ``(start, count)`` ranges that a ``-U0`` patch removes or changes."""
    out = []
    for match in _HUNK.finditer(patch):
        count = 1 if match.group(2) is None else int(match.group(2))
        if count > 0:
            out.append((int(match.group(1)), count))
    return out


def parse_blame(porcelain: str) -> list[tuple[str, int]]:
    """``(commit sha, line number in that commit's version of the file)`` per blamed line."""
    result = []
    for line in porcelain.splitlines():
        head = line.split(" ")
        if len(head) >= 3 and _SHA.match(head[0]) and head[1].isdigit() and head[2].isdigit():
            result.append((head[0], int(head[1])))
    return result


def added_line_numbers(diff: str) -> set[int]:
    """New-file numbers of the lines a unified diff adds."""
    numbers: set[int] = set()
    current = 0
    for raw in diff.splitlines():
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if match:
            current = int(match.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            numbers.add(current)
            current += 1
        elif raw.startswith("-") or raw.startswith("\\") or raw.startswith(("diff", "index", "---")):
            continue
        else:
            current += 1
    return numbers


def summary_of(message: str) -> str:
    """Subject plus the first paragraph of the body, in plain words."""
    paragraphs = [p.strip() for p in message.strip().split("\n\n") if p.strip()]
    text = " ".join(paragraphs[:2]) if paragraphs else ""
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_SUMMARY_CHARS].rstrip()


def mine_commit(fix: str) -> list[Mined]:
    mined: list[Mined] = []
    files = [
        f for f in _git("diff-tree", "--no-commit-id", "--name-only", "-r", fix).split()
        if f.startswith("ai_pr_review/") and f.endswith(".py")
    ]
    if len(files) > MAX_FIX_FILES:
        return []
    message = _git("show", "-s", "--format=%B", fix)
    for file in files:
        patch = _git("diff", "-U0", f"{fix}^", fix, "--", file, check=False)
        counts: dict[str, list[int]] = {}
        for start, count in removed_ranges(patch):
            porcelain = _git("blame", "--porcelain", "-L", f"{start},+{count}", f"{fix}^", "--", file, check=False)
            for sha, orig in parse_blame(porcelain):
                counts.setdefault(sha, []).append(orig)
        if not counts:
            continue
        introducing, lines = max(counts.items(), key=lambda kv: len(kv[1]))
        if introducing == fix or _git("show", "-s", "--format=%P", introducing).count(" ") >= 1:
            continue  # the fix itself, or a merge commit
        if max(lines) - min(lines) > MAX_LABEL_SPAN:
            continue
        diff = _git("show", "--format=", "--no-renames", "-U3", introducing, "--", file, check=False)
        if not diff or len(diff.encode()) > MAX_DIFF_BYTES:
            continue
        if not any(n in added_line_numbers(diff) for n in range(min(lines), max(lines) + 1)):
            continue  # blame points at a line this commit only moved or kept
        mined.append(Mined(introducing[:9], fix[:9], file, min(lines), max(lines), summary_of(message), diff))
    return mined


def known_introducers() -> set[str]:
    """Introducing-commit prefixes already used by the hand-built corpus."""
    found: set[str] = set()
    for path in (CANARY_DIR / "eval_corpus").glob("*.labels.json"):
        found.update(re.findall(r"\b[0-9a-f]{9}\b", json.loads(path.read_text()).get("verified_by", "")))
    return found


def default_ref() -> str:
    """``origin/main`` if it exists, else ``HEAD``. Use ``--pin`` for a repeatable run."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", "origin/main"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    return "origin/main" if result.returncode == 0 else "HEAD"


def fix_commits(limit: int) -> list[str]:
    out = _git("log", default_ref(), "--no-merges", "--format=%H", "-i", "--grep=^fix", "--", SOURCE_GLOB)
    return out.split()[: max(limit, 0)]


def write(fixtures: list[Mined], out_dir: Path, start_index: int = 1) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[str, str]] = set()
    n = start_index
    for m in fixtures:
        key = (m.introducing, m.file)
        if key in seen:
            continue
        seen.add(key)
        name = f"m{n:03d}_{m.introducing}"
        (out_dir / f"{name}.diff").write_text(m.diff)
        (out_dir / f"{name}.labels.json").write_text(json.dumps({
            "clean": False, "source": "mined", "evidence": "weak",
            "verified_by": f"git blame: introduced by {m.introducing}, fixed by {m.fix} (auto-mined, not human checked)",
            "bugs": [{"file": m.file, "line_start": m.line_start, "line_end": m.line_end,
                      "category": "edge-case", "summary": m.summary}],
            "accepted_extra": [],
        }, indent=2) + "\n")
        n += 1
    return n - start_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(CANARY_DIR / "eval_corpus_mined"))
    parser.add_argument("--limit", type=int, default=80, help="stop after this many fixtures")
    parser.add_argument("--scan", type=int, default=400, help="fix commits to examine")
    parser.add_argument("--dry-run", action="store_true", help="count, write nothing")
    parser.add_argument("--pin", help="JSON file of fix commit shas to use, so a later run mines the same set")
    parser.add_argument("--write-pin", help="write the fix commit shas that produced fixtures to this JSON file")
    args = parser.parse_args(argv)

    found: list[Mined] = []
    keys: set[tuple[str, str]] = set()
    skip = known_introducers()
    fix_shas: dict[str, None] = {}
    pinned = json.loads(Path(args.pin).read_text()) if args.pin else None
    try:
        candidates = pinned if pinned is not None else fix_commits(args.scan)
    except RuntimeError as exc:
        print(f"error: cannot list fix commits ({exc}). Run inside a clone of the repository.", file=sys.stderr)
        return 1
    for fix in candidates:
        for m in mine_commit(fix):
            if m.introducing in skip:
                continue
            if (m.introducing, m.file) not in keys:
                keys.add((m.introducing, m.file))
                found.append(m)
                fix_shas.setdefault(fix, None)
        if len(found) >= args.limit:
            break
    found = found[: args.limit]
    print(f"{len(found)} fixtures mined", file=sys.stderr)
    if GIT_FAILURES:
        print(f"warning: {len(GIT_FAILURES)} git call(s) failed and their commits were skipped. "
              f"First: {GIT_FAILURES[0]}", file=sys.stderr)
    if args.write_pin:
        Path(args.write_pin).write_text(json.dumps(list(fix_shas), indent=0) + "\n")
    if args.dry_run:
        for m in found[:10]:
            print(f"  {m.introducing} {m.file}:{m.line_start}-{m.line_end} fixed by {m.fix}: {m.summary[:70]}", file=sys.stderr)
        return 0
    print(f"wrote {write(found, Path(args.out))} fixtures to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
