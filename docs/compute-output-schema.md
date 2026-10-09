# Compute Output Schema

> **Maintainer-only reference.** This file is not on the [GitHub Pages site](https://tag1consulting.github.io/ai-pr-review/) on purpose. The payload it documents is a legacy handoff format (`AI_PR_REVIEW_COMPUTE_OUTPUT`) that the action no longer uses (see [Configuration → Legacy compatibility](https://tag1consulting.github.io/ai-pr-review/configuration#legacy-compatibility)). The file stays in the repo for tooling that still reads the JSON directly.

This page describes the JSON payload that the `compute` subcommand (`python -m ai_pr_review compute`) writes when `AI_PR_REVIEW_COMPUTE_OUTPUT` (or `--output`) points at a file path. The `review` command does not write this payload.

> **Status (v0.9.0):** The Python engine handles compute, dispatch, and posting end to end. The action no longer needs the separate compute handoff that this file once described. The engine keeps `AI_PR_REVIEW_COMPUTE_OUTPUT` for external tooling that reads the compute payload on its own. Set the variable to a path and run the `compute` subcommand to get the JSON shape below.

## Schema

```json
{
  "skip": false,
  "reason": "",
  "diff": "<unified diff text>",
  "changed_files": ["path/to/file.py", "..."],
  "manifest": "BASE: main | DIFF: ... | LANGUAGES: Python | FILES: 3 | ...",
  "diff_label": "full (main..abc1234)",
  "base": "main",
  "head": "abc1234def5678",
  "is_incremental": false,
  "languages": ["Python", "Shell"],
  "merge_filter_fallback_reason": null,
  "findings": [],
  "token_log": []
}
```

## Fields

| Field | Type | Description |
|---|---|---|
| `skip` | bool | True if the review must be skipped (for example, the diff is too large or there are no changed files). |
| `reason` | string | Human-readable reason for the skip. Empty when `skip` is false. For a large diff the format is `diff too large (<lines> lines > <max>)`. The engine appends `; merge-commit filter failed: <reason>` when the merge-commit filter fell back. |
| `diff` | string | Full unified diff text. Empty when `skip` is true. |
| `changed_files` | string[] | List of changed file paths (after the built-in lockfile and vendor excludes). Empty for the "no changed files" skip. A "diff too large" skip still lists the files. |
| `manifest` | string | Formatted manifest line for agent context. |
| `diff_label` | string | Human-readable diff description (for example, "incremental (abc..def)"). |
| `base` | string | Base branch name. |
| `head` | string | Head commit SHA. |
| `is_incremental` | bool | True if this is an incremental (watermark) diff. |
| `languages` | string[] | Detected language labels. |
| `merge_filter_fallback_reason` | string \| null | Set when `ignore-merge-commits` filtering fell back to the unfiltered diff (for example, filtering would have produced an empty diff). Null otherwise. |
| `findings` | Finding[] | Reserved. `run_compute` always writes an empty array. |
| `token_log` | TokenEntry[] | Reserved. `run_compute` always writes an empty array. |

Skip payloads (`skip` is true) include only `skip`, `reason`, `diff`, `changed_files`, `manifest`, `findings`, and `token_log`. They omit `diff_label`, `base`, `head`, `is_incremental`, `languages`, and `merge_filter_fallback_reason`.

## Finding schema

`run_compute` does not fill `findings` today. If a future version fills it, each entry matches the agent output schema:

```json
{
  "severity": "High",
  "confidence": 85,
  "file": "path/to/file.py",
  "line": 42,
  "start_line": 40,
  "finding": "Description",
  "remediation": "How to fix",
  "suggested_code": "replacement",
  "source": "security-reviewer",
  "sources": ["security-reviewer", "code-reviewer"]
}
```
