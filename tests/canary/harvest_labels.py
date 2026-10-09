"""Harvest what happened to this tool's own past review findings. Read-only, free.

For each merged pull request in an allowed repository, find the inline findings
the review bot posted and record what became of each one:

- ``dismissed``: a reply used a dismiss command (``dismiss``, ``false-positive``,
  ``wont-fix``).
- ``line-changed``: the commit the finding was posted on and the merge commit
  differ in the cited lines.
- ``untouched``: neither of the above.

The output is JSON lines, one per finding. It measures the old bot's precision
in practice, not recall. A finding left "untouched" can be a true problem the
author ignored. Read it as a signal, not a verdict.

This script only reads, through ``gh api``. It makes no model call and spends no
money. It refuses any repository that is not in ``ALLOWED_REPOS``. A client
repository needs Greg's approval and local-only handling, so it is not listed.

    python tests/canary/harvest_labels.py --limit 20 --out harvest.jsonl
    python tests/canary/harvest_labels.py --repo tag1consulting/ai-pr-review
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from typing import Any

ALLOWED_REPOS = (
    "tag1consulting/ai-pr-review",
    "tag1consulting/ai-pr-review-test",
)

_FINDING_MARKER = re.compile(r"<!-- ai-pr-review-finding:([A-Za-z0-9+/=]+) -->")
_FINDING_ID = re.compile(r"\*\*\[F(\d+)\]\*\*")
_DISMISS = re.compile(r"/ai-pr-review\s+(dismiss|false-positive|wont-fix)\b", re.IGNORECASE)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))?", re.MULTILINE)
_LINE_SLACK = 3


def decode_marker(body: str) -> dict[str, Any] | None:
    """The finding metadata the bot hides in each inline comment, or None."""
    match = _FINDING_MARKER.search(body)
    if not match:
        return None
    try:
        data = json.loads(base64.b64decode(match.group(1)))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def dismiss_command(replies: Iterable[str]) -> str | None:
    for text in replies:
        found = _DISMISS.search(text)
        if found:
            return found.group(1).lower()
    return None


def patch_touches(patch: str, line: int, slack: int = _LINE_SLACK) -> bool:
    """True when a unified-diff patch changes a line within *slack* of *line*."""
    for match in _HUNK.finditer(patch):
        start = int(match.group(1))
        length = int(match.group(2)) if match.group(2) is not None else 1
        if start - slack <= line <= start + max(length, 1) - 1 + slack:
            return True
    return False


def _gh(path: str) -> Any:
    out = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    pages = json.loads(out)
    merged: list[Any] = []
    for page in pages:
        merged.extend(page) if isinstance(page, list) else merged.append(page)
    return merged


def harvest_repo(repo: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pulls = _gh(f"repos/{repo}/pulls?state=closed&per_page=50&sort=updated&direction=desc")
    merged = [p for p in pulls if p.get("merged_at")][:limit]
    for pr in merged:
        number = pr["number"]
        try:
            rows += _harvest_pr(repo, pr)
        except (subprocess.SubprocessError, ValueError) as exc:
            # One bad PR must not discard the rows already collected.
            print(f"warning: skipped {repo}#{number}: {exc}", file=sys.stderr)
    return rows


def _harvest_pr(repo: str, pr: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    number = pr["number"]
    comments = _gh(f"repos/{repo}/pulls/{number}/comments?per_page=100")
    bot = [c for c in comments if c.get("user", {}).get("type") == "Bot" and decode_marker(c.get("body", ""))]
    replies: dict[int, list[str]] = {}
    for c in comments:
        parent = c.get("in_reply_to_id")
        if parent:
            replies.setdefault(parent, []).append(c.get("body", ""))
    compare_cache: dict[str, dict[str, str]] = {}
    for c in bot:
        marker = decode_marker(c["body"]) or {}
        commit = c.get("original_commit_id") or c.get("commit_id") or ""
        if commit not in compare_cache:
            # Compare to the PR head, not the merge commit. A squash merge commit
            # holds the whole PR, so nearly every cited line would look changed.
            compare_cache[commit] = _changed_patches(repo, commit, (pr.get("head") or {}).get("sha") or "")
        # The compare range starts at the commit the finding was posted on, so the
        # line number must be the one in that commit.
        line = c.get("original_line") or c.get("line") or 0
        patch = compare_cache[commit].get(c.get("path", ""), "")
        command = dismiss_command(replies.get(c["id"], []))
        fid = _FINDING_ID.search(c.get("body", ""))
        rows.append({
            "repo": repo, "pr": number, "finding": f"F{fid.group(1)}" if fid else "",
            "file": c.get("path", ""), "line": line,
            "severity": marker.get("sev", ""), "category": marker.get("cat", ""),
            "confidence": marker.get("conf"), "judge": marker.get("jv", ""),
            "outcome": ("dismissed" if command else "line-changed" if line and patch_touches(patch, line) else "untouched"),
            "dismiss_command": command or "",
        })
    return rows


COMPARE_FAILURES: list[str] = []


def _changed_patches(repo: str, base: str, head: str) -> dict[str, str]:
    if not base or not head:
        COMPARE_FAILURES.append(f"{repo} {base[:9] or '?'}...{head[:9] or '?'}: missing commit")
        return {}
    try:
        data = subprocess.run(
            ["gh", "api", f"repos/{repo}/compare/{base}...{head}"],
            capture_output=True, text=True, check=True, timeout=120,
        ).stdout
        files = json.loads(data).get("files", [])
    except (subprocess.SubprocessError, ValueError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        COMPARE_FAILURES.append(f"{repo} {base[:9]}...{head[:9]}: {detail[:120]}")
        print(f"warning: compare failed for {repo} {base[:9]}...{head[:9]}: {detail[:200]}", file=sys.stderr)
        return {}
    return {f["filename"]: f.get("patch", "") for f in files if isinstance(f, dict) and "filename" in f}


def summarize(rows: list[dict[str, Any]]) -> str:
    total = len(rows)
    if not total:
        return "no findings found"
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    parts = [f"{k}={v} ({100 * v / total:.0f}%)" for k, v in sorted(counts.items())]
    text = f"{total} findings: " + ", ".join(parts)
    if COMPARE_FAILURES:
        text += (f". {len(COMPARE_FAILURES)} compare call(s) failed, so some findings may read "
                 "as untouched because the diff was not available")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", action="append", help="repeatable, default: every allowed repo")
    parser.add_argument("--limit", type=int, default=20, help="merged PRs per repo")
    parser.add_argument("--out", default="-", help="JSON lines file, default stdout")
    args = parser.parse_args(argv)

    repos = args.repo or list(ALLOWED_REPOS)
    refused = [r for r in repos if r not in ALLOWED_REPOS]
    if refused:
        print(f"error: not an allowed repository: {', '.join(refused)}. A client repository needs "
              "explicit approval and local-only handling.", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    for repo in repos:
        rows += harvest_repo(repo, args.limit)
    text = "".join(json.dumps(r) + "\n" for r in rows)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w") as handle:
            handle.write(text)
    print(summarize(rows), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
