---
layout: default
title: Suppression Rules
parent: Configuration
nav_order: 3
---

# Suppression Rules

You can suppress known false positives in `config/suppressions.json`. Each entry matches findings by file, line, code prefix, or regex pattern:

```json
[
  {
    "id": "descriptive-id",
    "reason": "Why this is a false positive",
    "match": {
      "file": "specific-file.sh",
      "pattern": "regex.*to.*match.*finding.*text"
    }
  }
]
```

Match fields (all optional, combined with AND logic):
- `file`: A case-insensitive regex that the engine matches against the file path (`re.search`). A plain string still works as a substring match. The characters `.` and other regex metacharacters have their special meaning.
- `line`: Exact line number match.
- `line_start`: Lower bound of the line window (inclusive).
- `line_end`: Upper bound of the line window (inclusive).
- `code`: The finding text starts with this prefix.
- `pattern`: A case-insensitive regex that the engine matches against the finding text and its remediation text.

`line_start` and `line_end` are both optional. If you use them together, they target a specific line window in a file. A finding matches when its line span (from `start_line` to `line`) overlaps the window `[line_start, line_end]`. You can omit either bound to leave that end open. For example, `line_end` alone means "up to line N from the start of the file". A range rule never matches a finding that has no line number.

Example: suppress findings only in the unmodified upstream lines of a vendored patch file:

```json
{
  "id": "accept-upstream-vuln-in-patches",
  "reason": "Lines 1–200 are unmodified upstream code; our patch starts at line 201",
  "match": {
    "file": "patches/upstream-lib.c",
    "line_start": 1,
    "line_end": 200
  }
}
```

## Local suppressions

Consuming repositories can add their own suppression rules without a change to the action. Create `.ai-pr-review/suppressions.json` in your repository with the same schema. `.github/ai-pr-review/suppressions.json` still works as a fallback for existing adopters. The engine never merges the two files. If both exist, the neutral path wins:

```json
[
  {
    "id": "my-repo-specific-rule",
    "reason": "Why this finding is not relevant to this repo",
    "match": {
      "pattern": "regex.*to.*match.*finding.*text"
    }
  }
]
```

The engine merges local rules with the global suppression rules at runtime. You need no action input or other configuration.

The engine rejects a local rule that has an empty or missing `match` object and logs a warning. The local file is PR-controlled, and a rule without a constraint would match every finding. Only the global configuration that ships with the action can contain such catch-all rules.

## Suppressing CVE findings

To accept a specific CVE (for example, a library that only a test fixture uses), add a suppression rule that matches the CVE or GHSA ID:

```json
{
  "id": "accept-risk-CVE-2025-12345",
  "reason": "Library used only in test fixtures, not production",
  "match": {
    "pattern": "CVE-2025-12345|GHSA-xxxx-yyyy-zzzz"
  }
}
```

## Verify-gated suppressions

A suppression rule can include an optional `verify` field. The field tells the action to query an authoritative registry before it suppresses the finding. The query confirms that the version exists. If the registry says that the version is missing, the engine restores the finding. A mis-scoped suppression therefore cannot hide a real typo or a malicious downgrade.

| `verify` value | Extracts from finding | Authoritative source |
|---|---|---|
| `github-releases` | `owner/repo@vN.N.N` | `GET /repos/{owner}/{repo}/releases/tags/{tag}` (a published release must exist) |
| `npm` | `pkg@version` or `@scope/pkg@version` | `registry.npmjs.org` |
| `pypi` | `pkg==version` | `pypi.org/pypi/{pkg}/{version}/json` |
| `go` | `module@vX.Y.Z` | `proxy.golang.org/{module}/@v/{version}.info` |
| `cargo` | `pkg version` (name, whitespace, version) | `crates.io` |
| `docker-hub` | `image:tag` or `ns/image:tag` | `hub.docker.com` |

Example:

```json
{
  "id": "allow-serde-1.0.197",
  "reason": "Model keeps flagging serde 1.0.197 as unreleased; verify against crates.io",
  "match": {
    "pattern": "serde.*1\\.0\\.197.*not.*(valid|released|exist)"
  },
  "verify": "cargo"
}
```

Private registries (GHCR, GCR, ECR) are not supported because they need authentication.

## Why the bot flagged a valid version as "unreleased"

Every LLM has a **training-data cutoff**. To the model, anything released after that cutoff looks like a hallucination. Suppose a reviewer agent sees `ruby/setup-ruby@v1`, `actions/checkout@v5`, or `ruby-4.0.3`, and it never saw the version during training. The agent can wrongly flag the version as "unreleased", "invalid", or "not a valid release". This can happen even if the version shipped months earlier.

The action protects against this in two layers:

1. **Prompt-level guard.** All 7 finding-producing agents receive the `_knowledge-cutoff.md` shared trailer. This hard constraint forbids "unreleased version" findings that are based on training-data recall. The model must omit such findings completely. The exceptions are a malformed version string, an explicit downgrade, or a version that a cited CVE covers.
2. **Verify-gated suppression.** If a repeat hallucination gets through, add a suppression rule with a `verify` field (table above). The rule takes effect only if the authoritative registry confirms that the version exists. This process is deterministic and safe. Suppose the suppression pattern accidentally matches a real typo (for example, `ruby-9.9.9`). The registry lookup fails, and the engine restores the finding.

The CVE check is the complement. It queries OSV.dev for the changed dependency manifests. It emits a new finding when a declared version has a known vulnerability. It does not emit "does not exist" findings.
