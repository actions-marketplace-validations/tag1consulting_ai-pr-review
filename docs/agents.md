---
layout: default
title: Agents & Profiles
nav_order: 6
render_with_liquid: false
---

# Agents & Profiles

On every PR push, this action:

1. Computes the diff (full on the first run, incremental on later pushes)
2. Detects languages from changed file extensions
3. Runs a roster of AI review agents against the diff
4. Runs deterministic checks on changed files: shellcheck, CVE lookups ([OSV.dev](https://osv.dev/)), semgrep SAST, trufflehog secret scanning, ruff (Python), golangci-lint (Go), hadolint (Dockerfiles), checkov (Terraform/K8s/IaC), phpcs (PHP/Drupal), eslint (JS/TS), dep-exists (new dependencies that no public registry knows), phpstan (PHP static analysis), kube-linter (Kubernetes), tflint (Terraform), and three documentation checks (docs-api-check, docs-ref-check, docs-drift-check). See [Static Analyzers](static-analyzers)
5. Posts a summary comment (first run only) and a review with inline findings
6. Auto-resolves stale bot threads and dismisses superseded reviews

## Review agents

**Quick mode** (default) runs one or two finding agents. On the first run it also runs a summary agent:

| Agent | Purpose |
|-------|---------|
| **pr-summarizer** | Generates a walkthrough summary (first run only) |
| **code-reviewer** | Finds bugs, logic errors, and code quality issues |
| **silent-failure-hunter** | Detects swallowed errors and unsafe fallbacks (runs when the diff has error-handling patterns) |

**Full mode** adds five more agents:

| Agent | Purpose |
|-------|---------|
| **architecture-reviewer** (conditional) | Evaluates design patterns, coupling, and scalability. Runs when the diff touches code or infra files |
| **security-reviewer** (conditional) | Checks for injection, auth, crypto, and supply chain issues. Runs when the diff has security-relevant patterns |
| **blind-hunter** | Context-free review (zero project knowledge, finds issues that familiarity hides) |
| **edge-case-hunter** (conditional) | Traces every branching path for unhandled gaps. Runs when the diff has control-flow constructs |
| **adversarial-general** | Cynical adversarial review |

Full mode also runs **issue-linker** (GitHub only). It finds related issues and PRs and assesses whether the current changes resolve them.

## Controlling which agents run

The `agents` (allowlist) and `exclude-agents` (denylist) settings control which agents run. Both accept a comma-separated list of the agent names above. An empty value (default) means all eligible agents run. When `agents` is set, the action ignores `exclude-agents` (the allowlist takes precedence). Existing gates still apply:

- A tier-2 agent in the allowlist does not run in quick mode.
- A conditional agent does not run if its trigger did not fire.

If you exclude `pr-summarizer`, the action does not post the PR summary comment. The action rejects unknown names with an error and a suggestion.

You can set these in three places. The list shows them in precedence order (highest wins):

1. **`/ai-pr-review review-full` slash command**: always runs the full roster and ignores any allowlist or denylist for that one run.
2. **Action input or repo variable**: set it directly in the calling workflow, or with the `AI_AGENTS`/`AI_EXCLUDE_AGENTS` env vars (see [Configuration → Analyzer and agent selection](configuration#analyzer-and-agent-selection)):

   ```yaml
   - uses: tag1consulting/ai-pr-review@main
     with:
       agents: 'code-reviewer,security-reviewer'      # Run only these two
       # exclude-agents: 'edge-case-hunter,adversarial-general'  # or: run everything except these two
   ```

3. **`.github/ai-pr-review/policy.yml`**: the `agents` and `exclude-agents` fields of a named policy. The action routes by changed-file path, base branch, or head branch. Different PRs can get different agent rosters without a change to the workflow file. See [Policies](policy) for the full schema.

An explicit action input or repo variable always wins over a `policy.yml` route match. A route match wins over the engine default (all eligible agents run). The same three-place precedence applies to the `analyzers`/`exclude-analyzers` allowlist and denylist for static analyzers. See [Static Analyzers → Controlling which analyzers run](static-analyzers#controlling-which-analyzers-run).

## Severity icons

Findings use color-distinct icons:

| Icon | Severity | Review action |
|------|----------|---------------|
| 🚨 | Critical | REQUEST_CHANGES |
| 🔴 | High | REQUEST_CHANGES |
| 🟡 | Medium | APPROVE (informational) |
| 🔵 | Low | APPROVE (informational) |

## Review modes

**Quick mode** (default): Runs the code-reviewer and, if its trigger fires, silent-failure-hunter. This mode is fast and cheap, so it suits every push.

**Full mode**: Runs up to 9 agents. These are the 6 finding agents in the tables above (code-reviewer, architecture-reviewer, security-reviewer, blind-hunter, edge-case-hunter, adversarial-general), and 3 of them depend on diff content. Full mode also runs silent-failure-hunter (conditional), pr-summarizer (first run), and issue-linker (GitHub only). Start full mode in one of these ways:
- Adding the `ai-review-full` label to the PR
- Using `workflow_dispatch` with `review_mode: full`
- Setting the `review-mode` input to `full`
- Routing by changed-file path, base branch, or head branch via a repo-local `.github/ai-pr-review/policy.yml` (see below)

### Routing review depth by branch or path

For anything beyond a single global quick/full choice (for example, near-zero-cost review for content-only PRs, one full smoke-test review when features land on a staging branch, and quick review on other feature branches), add `.github/ai-pr-review/policy.yml` to your repo. See **[Policies](policy)** for the full schema, the precedence chain (an explicit `ai-review-full` label or `review-mode` input always wins over policy routing), and worked examples that include a release-branch escalation.

The `ai-review-full` label and `review_mode` input of `workflow_dispatch` are the way to force a one-time full review, whatever the policy file says.

**Bitbucket Pipelines and GitLab CI** work the same way, because `policy.yml` is engine-side and not specific to GitHub. Export `BASE_REF` and `HEAD_REF` (see the example pipelines in `examples/pipelines/`).

## Language profiles

The action detects languages from file extensions and adds per-language context to agent prompts. Language profiles are markdown files in `language-profiles/`:

| Profile file | Covers |
|---|---|
| `go.md` | Go |
| `python.md` | Python |
| `javascript.md` | JavaScript |
| `typescript.md` | TypeScript |
| `php.md` | PHP / Drupal |
| `shell.md` | Shell / Bash |
| `ruby.md` | Ruby / Rails |
| `rust.md` | Rust |
| `java.md` | Java |
| `c++.md` | C and C++ |
| `terraform.md` | Terraform |
| `yaml.md` | YAML |
| `kotlin.md` | Kotlin |
| `swift.md` | Swift |
| `csharp.md` | C# |
| `scala.md` | Scala |
| `sql.md` | SQL |
| `lua.md` | Lua |
| `perl.md` | Perl |

To add a language, create a `language-profiles/<language>.md` file. The filename (without extension) must match the lowercase language key that `detect_language()` in `ai_pr_review/languages.py` returns for the relevant file extensions.
