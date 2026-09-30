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
    <p>Up to 8 specialized AI agents analyze your code from different perspectives — architecture, security, edge cases, and more.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> 13 Static Analyzers</h3>
    <p>Shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, eslint, phpstan, kube-linter, and tflint ship as binaries in the container image; cve-check runs as pure Python (OSV.dev HTTP queries, no external binary). All 13 run as native Python.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> Works Everywhere</h3>
    <p>GitHub Actions, Bitbucket Cloud Pipelines, and GitLab CI/CD. Anthropic, OpenAI, Google, and Bedrock proxy providers.</p>
  </div>
  <div class="feature">
    <h3><span class="feature-icon">&#9670;</span> One-Click Fixes</h3>
    <p>Code suggestion buttons let PR/MR authors accept fixes with a single click, powered by GitHub and GitLab's suggestion block syntax.</p>
  </div>
</div>

## What it does

On every push to a pull request, AI PR Review runs a roster of LLM agents and deterministic static analyzers against the diff, then posts a structured review — a summary comment plus inline findings with "Apply suggestion" buttons where applicable. It's incremental (subsequent pushes only review what changed), suppresses known false positives via a JSON rules file, and is designed to fail gracefully when a model times out or a scanner is missing. Runs on GitHub Actions, Bitbucket Cloud Pipelines, or GitLab CI/CD against Anthropic, OpenAI, Google, or any OpenAI-compatible endpoint.

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

That's it — reviews start firing on the next PR.

## What's new in v2.18.1

**The cost ceiling now tells you when it cannot bound a model.** A model with no pricing row used to be estimated at $0, so `max-cost-usd` could not stop it and nothing in the PR said so. The new `cost-ceiling-unpriced` input defaults to `warn`: the review runs and a "Cost ceiling not enforced" notice names the model. Set it to `block` to skip the review instead. Nothing changes unless `max-cost-usd` is above 0 and a model has no pricing row (an invalid `cost-ceiling-unpriced` value is always an error).

**The `ai-pr-review/policy-gate` check no longer flips back to `action_required` after `/ai-pr-review review-full` satisfied it.** A slower automatic review on the same commit could finish last and overwrite the `success`. It now keeps the gate satisfied for that commit.

See [Version History → v2.18.1](version-history/v2.18.1) for details.

## What's new in v2.18.0

**Behavior change: new default OpenAI and Google models.** OpenAI now uses `gpt-6-luna` / `gpt-6.1-sol`, which are cheaper than the previous pair. Google now uses `gemini-3.5-flash-lite` / `gemini-3.8-flash`, because Google stopped offering the old Gemini 2.5 defaults to new projects. The Google standard model costs the same as before, and the premium model has a higher input price and a lower output price than `gemini-2.5-pro` (its introductory rate is lower through 2026-12-31, then doubles). Both pairs were verified with the live model canary. To keep the previous models, set the `model-standard` and `model-premium` action inputs. A new weekly model watcher opens an issue when Anthropic, OpenAI or Google lists a newer model than the defaults.

See [Version History → v2.18.0](version-history/v2.18.0) for details.

## What's new in v2.17.0

**Behavior change: the default Anthropic `standard` model is now `claude-sonnet-5-5`**, at the same price, verified with the live model canary. To keep the previous model, set the `model-standard: claude-sonnet-5` action input (or the `AI_REVIEW_MODEL_STANDARD` repository variable in the example workflow). `AI_MODEL_STANDARD` works only when running the container directly, because the action sets it from its inputs. Pushing a release tag now also creates a draft GitHub release with the notes filled in, so publishing to the GitHub Marketplace is a single checkbox in the web form. The token table also labels Sonnet 5.5 correctly.

See [Version History → v2.17.0](version-history/v2.17.0) for details.

## Learn more

**Start here**

- [Getting started](getting-started) — Installation, requirements, secrets and variables
- [Configuration](configuration) — Action inputs and LLM provider options

**Opt-in capabilities** — three independent features, all default off, all require the Python engine (the default since v1.0.0):

- [Tree-sitter context enrichment](configuration#opt-in-capabilities) — inject symbol definitions referenced in the diff into agent prompts; reduces hallucinated "should check X" findings
- [SARIF 2.1.0 ingestion](static-analyzers#sarif-ingestion) — merge findings from external scanners (CodeQL, Semgrep, Trivy, Bandit) into the same dedup/post pipeline as native analyzers
- [Learning loop](learning-loop) — reviewers post `/ai-pr-review false-positive | wont-fix | feedback` to persist verdicts to a dedicated git branch; future reviews see them as a `<repo-feedback>` block

**Reference**

- [Features](features) — Code suggestions, incremental reviews, resilience, token usage
- [Version History](version-history) — What changed in each release
- [Agents & profiles](agents) — Review agents, severity icons, review modes, language profiles
- [Static analyzers](static-analyzers) — Analyzer table, dependency vulnerability check, SARIF ingestion
- [Suppression rules](suppression) — Suppress false positives with JSON rules; scope rules to a line range with `match.line_start` / `match.line_end` (v1.1.0)
- [Diff-scope severity cap](configuration#static-analyzer-options) — control how out-of-diff native-analyzer findings are handled via `analyzer-diff-scope` (v1.2.0)
- [Slash commands](slash-commands) — PR-comment commands (rescan, review-full, skip, dismiss, help, plus learning-loop commands)

**Internals**

- [Architecture](architecture) — Directory tree, data flow, dependencies
- [Local development](local-development) — Run the container locally against any PR

**Contributing**

- [Contributing guide](https://github.com/tag1consulting/ai-pr-review/blob/main/CONTRIBUTING.md) — Step-by-step recipes for adding analyzers, agents, language profiles, and VCS providers
- [Internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/architecture-internals.md) — Deep implementation details for maintainers
