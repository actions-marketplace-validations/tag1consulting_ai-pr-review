# Precision and recall corpus

Labeled diffs for `tests/canary/precision_eval.py`. Each `NN_name.diff` has an
`NN_name.labels.json` that says which bugs it contains. The harness runs the real
review pipeline once per diff and matches findings to labels.

| Group | Fixtures | Where the label comes from |
|---|---|---|
| Real bugs from this repo's history | 01 to 08 | The diff is the commit that introduced the bug, limited to its source files (no tests or docs). The label is the lines that a later `fix:` commit changed, found with `git blame` on the fix's parent. The `verified_by` field names both commits. |
| Seeded bugs | 09 to 13 | Copies of fixtures 10 to 14 in `tests/canary/corpus/`. The bugs are planted by construction, so the label follows from the code. |
| Clean by absence | 14 to 21 | Merged commits that no later `fix` commit blames (checked 2026-10-08). `evidence` is `weak`: it means "no known bug", not "no bug". A finding on one of these may be real. |

## Label format

```json
{
  "clean": false,
  "source": "known-fix",
  "evidence": "strong",
  "verified_by": "history: introduced by <sha>, fixed by <sha>",
  "agents": ["code-reviewer", "security-reviewer"],
  "bugs": [{"file": "a.py", "line_start": 10, "line_end": 12,
            "category": "edge-case", "summary": "what is wrong, in plain words"}],
  "accepted_extra": []
}
```

- `line_start` and `line_end` are line numbers in the new version of `file`. A finding matches when it names the same file, is within 5 lines of the range, and has the same category or shares enough words with `summary` (Jaccard 0.4, the threshold `consistency_eval.py` uses).
- `accepted_extra` lists true findings that are not the target bug, so they count as neither a hit nor a false positive.
- `agents` is optional. It replaces the default agent list for that fixture (use `security-reviewer` on a security fixture).
- A finding that matches nothing is counted as a false positive and listed for a human to adjudicate. If a human decides it is real, add it to `accepted_extra` or `bugs`.

## Limits

- A fixture is one commit's source diff. Real pull requests also carry tests and docs, and a reviewer sees them.
- The real-bug labels come from the fix commits, so they cover the bugs someone found and fixed. A bug that was never found is not labeled, and a finding on it counts as a false positive.
- About 21 fixtures give wide confidence intervals. Only large differences are detectable.
- `verified_by` on the real and clean fixtures names history, not a human reading. A person should spot-check them before a result drives a decision.
- This repo's own code is Python. The seeded fixtures add Go, TypeScript, PHP, and Terraform.
