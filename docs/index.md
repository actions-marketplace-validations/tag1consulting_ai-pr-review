---
layout: home
title: Home
nav_exclude: true
permalink: /
render_with_liquid: false
hero_title: AI PR Review
hero_tagline: "AI-powered pull request review using multiple LLM agents. Posts a summary comment and inline findings directly on your PRs."
---

<div class="features">
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> Multi-Agent Review</h3>
    <p>Up to 9 specialized AI agents analyze your code from different perspectives: architecture, security, edge cases, and more.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> 17 Static Analyzers</h3>
    <p>Shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, phpstan, kube-linter, and tflint ship as binaries in the container image. ESLint, cve-check, dep-exists, and three documentation analyzers (docs-api-check, docs-ref-check, and docs-drift-check) also run. All 17 run as native Python.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> Works Everywhere</h3>
    <p>GitHub Actions, Bitbucket Cloud Pipelines, and GitLab CI/CD. Anthropic, OpenAI, Google, and Bedrock proxy providers.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> One-Click Fixes</h3>
    <p>Code suggestion buttons let PR and MR authors accept fixes with one click. They use the suggestion block syntax of GitHub and GitLab.</p>
  </div>
</div>

## What it does

On every push to a pull request, AI PR Review runs a roster of LLM agents and deterministic static analyzers against the diff, then posts a structured review. The review has a summary comment and inline findings with "Apply suggestion" buttons where they apply. The review is incremental: later pushes review only what changed. A JSON rules file suppresses known false positives. The tool continues without error if a model times out or a scanner is missing. It runs on GitHub Actions, Bitbucket Cloud Pipelines, or GitLab CI/CD. It works with Anthropic, OpenAI, Google, or any OpenAI-compatible endpoint.

## Quick start

Get AI reviews on your PRs in two steps:

**1. Add your LLM API key** as a repository secret named `ANTHROPIC_API_KEY` (or the equivalent for your [provider](configuration)).

**2. Create `.github/workflows/ai-review.yml`** with this minimal workflow:

```yaml
name: AI PR Review
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: tag1consulting/ai-pr-review/container-action@main
        with:
          api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          pr-number: ${{ github.event.pull_request.number }}
          base-ref: ${{ github.event.pull_request.base.ref }}
          head-sha: ${{ github.event.pull_request.head.sha }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

Reviews start on the next PR.

## What's new in v2.21.0

**Each finding shows where its evidence came from.** A short label and the confidence score follow the finding text, for example `(corroborated by analyzer, confidence 88)`. It is display text only. Set `finding-badges: 'false'` to hide it.

**A new analyzer checks new dependencies.** `dep-exists` asks the public registry whether each dependency that a diff adds really exists. An unknown name is a High finding, because a model can invent a package name and an attacker can register it. Turn it off with `exclude-analyzers: dep-exists`.

**The review comment says what it did not look at.** A short `Not reviewed:` note lists your `exclude-patterns`, the agents that did not run, and whether the review is incremental.

**Behavior change: the cost estimate is about 2.2 times higher.** The estimate now uses a measured ratio of characters to tokens. A repository that sets `AI_MAX_COST_USD` can see a review skipped that passed before.

See [Version History → v2.21.0](version-history/v2.21.0) for details.

## What's new in v2.20.0

**`/ai-pr-review full` now works.** It is an alias for `/ai-pr-review review-full`. Both start the same full-mode review and get the same reply, and the `help` reply lists the alias.

**A Critical finding that two sources reported now gets an inline comment.** On a review with many findings, a Critical finding that an agent and an analyzer both reported could fall past the 25-comment cap and get no inline comment. On Bitbucket that meant no Code Insights annotation. Findings now keep their severity order, so the most severe ones get the inline slots first.

See [Version History → v2.20.0](version-history/v2.20.0) for details.

## What's new in v2.19.0

**You can now leave the Walkthrough table out of the summary comment.** Set `suppress-walkthrough: 'true'` (the `AI_SUPPRESS_WALKTHROUGH` environment variable on Bitbucket and GitLab). The Summary text, Type and Effort stay. This is most useful on Bitbucket, which cannot collapse the table the way GitHub and GitLab do. The default is `false`, so nothing changes unless you set it. On Bitbucket a summary comment that already has a table loses it on the next run.

See [Version History → v2.19.0](version-history/v2.19.0) for details.

## Learn more

**Start here**

- [Getting started](getting-started): Installation, requirements, secrets and variables
- [Configuration](configuration): Action inputs and LLM provider options

**Optional capabilities:** Three independent features. They are off by default, except context enrichment, which is on in the container action. They use the Python engine (the default since v1.0.0):

- [Tree-sitter context enrichment](configuration#opt-in-capabilities): Adds the symbol definitions that the diff references to the agent prompts. This reduces invented "should check X" findings.
- [SARIF 2.1.0 ingestion](static-analyzers#sarif-ingestion): Merges findings from external scanners (CodeQL, Semgrep, Trivy, Bandit) into the same dedup and post pipeline as native analyzers.
- [Learning loop](learning-loop): Reviewers post `/ai-pr-review false-positive | wont-fix | feedback` to save verdicts to a dedicated git branch. Later reviews see them as a `<repo-feedback>` block.

**Reference**

- [Features](features): Code suggestions, incremental reviews, resilience, token usage
- [Version History](version-history): What changed in each release
- [Agents & profiles](agents): Review agents, severity icons, review modes, language profiles
- [Static analyzers](static-analyzers): Analyzer table, dependency vulnerability check, SARIF ingestion
- [Suppression rules](suppression): Suppress false positives with JSON rules. Scope a rule to a line range with `match.line_start` and `match.line_end` (v1.1.0)
- [Diff-scope severity cap](configuration#static-analyzer-options): Use `analyzer-diff-scope` to control how the tool handles out-of-diff native-analyzer findings (v1.2.0)
- [Slash commands](slash-commands): PR-comment commands (rescan, review-full, skip, dismiss, help, plus learning-loop commands)

**Internals**

- [Architecture](architecture): Directory tree, data flow, dependencies
- [Local development](local-development): Run the container locally against any PR

**Contributing**

- [Contributing guide](https://github.com/tag1consulting/ai-pr-review/blob/main/CONTRIBUTING.md): Step-by-step recipes for adding analyzers, agents, language profiles, and VCS providers
- [Internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/architecture-internals.md): Deep implementation details for maintainers
