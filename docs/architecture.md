---
layout: default
title: Architecture
nav_order: 7
---

# Architecture

The **Python engine** (`ai_pr_review/` package) implements the action. It runs compute, agent dispatch, and VCS posting in a single process. See [Configuration](configuration) for the configuration reference.

```
ai-pr-review/
├── action.yml              # GitHub Actions composite action definition
│
├── ai_pr_review/           # Python engine
│   ├── cli.py              # Click entrypoint: `python3 -m ai_pr_review review`
│   ├── config.py           # Typed config (ReviewConfig.from_env(), resolve_models())
│   ├── orchestrate.py      # run_review(): agent dispatch, findings merge, post
│   ├── review/             # Assembly layer
│   │   ├── runtime.py      # build_review_runtime(): env → orchestration seam
│   │   ├── compute.py      # Diff computation, manifest, handoff payload (run_compute)
│   │   ├── outcome.py      # Review outcome classification
│   │   └── ...             # Also: cost_ceiling, pr_context, preflight, reporting, watermark (partial list)
│   ├── agents/             # Agent roster, eligibility gates, prompt composition
│   ├── llm/                # Multi-provider LLM clients (Anthropic, OpenAI, Google, Bedrock)
│   ├── vcs/                # VCS provider clients (GitHub, GitLab, Bitbucket)
│   ├── findings/           # Findings pipeline: merge, suppress, deduplicate
│   ├── analyzers/          # Native static analyzer wrappers (16 Python implementations + bridge dispatcher)
│   ├── context/            # Context enrichment (tree-sitter symbol injection, opt-in)
│   ├── feedback/           # Learning loop: feedback store, injection (opt-in)
│   └── slash/              # Slash command handling (/ai-pr-review false-positive, etc.)
│
├── prompts/                # Agent system prompts + shared trailers (see deep reference)
├── config/                 # Configuration and data files
│   ├── model-pricing.json  # Per-model token pricing for cost estimation
│   └── suppressions.json   # Declarative false-positive suppression rules
├── language-profiles/      # Per-language review context (markdown, injected into prompts)
├── scripts/                # Release and model tooling (release_notes.py, model_watch.py)
├── tests/python/           # pytest suite (see deep reference)
└── .github/workflows/
    ├── ai-review.yml       # Self-test: runs the action on its own PRs
    ├── draft-release.yml   # Creates a draft GitHub release when a release tag is pushed
    ├── e2e.yml             # Live end-to-end harness (e2e-gate check)
    ├── lint.yml            # pytest + mypy + ruff
    ├── model-canary.yml    # Live model canary against the pinned defaults
    ├── model-watch.yml     # Weekly check for Anthropic/OpenAI/Google models newer than the pinned defaults
    ├── pages.yml           # GitHub Pages documentation site build
    ├── publish-image.yml   # Container image build, push, and signing
    └── slash-commands.yml  # /ai-pr-review slash command handling
```

For the full directory listing with per-file annotations, see the [internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/architecture-internals.md).

## Data flow

1. **action.yml** invokes `python3 -m ai_pr_review review`
2. **`ai_pr_review/review/runtime.py`** (`build_review_runtime`) resolves config, builds the VCS provider, fetches the last-reviewed SHA, computes the diff, detects languages, runs static analyzers, loads SARIF findings, and loads suppression rules
3. **`ai_pr_review/orchestrate.py`** (`run_review`) dispatches the eligible agents in parallel, merges LLM and pre-computed findings, deduplicates, applies suppressions, and posts the summary and findings via the VCS provider client (all in one process)
4. The engine composes each agent prompt at dispatch time. It adds shared trailers: a **governance preamble** (`prompts/_governance.md`, which holds the Three Laws, severity calibration, verify-before-naming, secret redaction, and maintainer-verdict obedience), a knowledge-cutoff guard, a findings-schema instruction, and optionally a code-suggestion addendum

## Dependencies

The action requires `jq`, `git`, `gh`, and `python3`. Standard GitHub-hosted runners have all four pre-installed.

The **container action** (recommended) includes all static analyzer binaries at pinned versions, so it needs no runner setup. The **direct action reference** and **git submodule** paths do not install analyzer binaries. See [runtime dependencies](installation-direct-action#runtime-dependencies) for the optional install-in-workflow snippet.

## Deep reference

For implementation internals, see the [internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/architecture-internals.md) (architecture-internals.md). It covers findings pipeline phases, parallel agent execution, prompt caching, code suggestion validation, test architecture, and the Dockerfile multi-stage layout.
