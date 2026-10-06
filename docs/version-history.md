---
layout: default
title: Version History
nav_order: 8
has_children: true
render_with_liquid: false
---

# Version History

What changed in each release, newest first. Every release from v2.8.0 onward has its own page; everything before that is combined into [Older releases](version-history/archive).

| Version | Highlights |
|---------|-----------|
| [v2.19.0](version-history/v2.19.0) | New `AI_SUPPRESS_WALKTHROUGH` (`suppress-walkthrough`) leaves the per-file Walkthrough table out of the summary comment (useful on Bitbucket), and release PRs of this repository now get a full review of the whole code diff since the last release |
| [v2.18.3](version-history/v2.18.3) | Bitbucket errors keep the granted scopes, the account lookup warning depends on the HTTP status (a 401 is no longer blamed on Account:Read), and the dismiss help line is hidden when the lookup failed |
| [v2.18.2](version-history/v2.18.2) | Bitbucket comment fixes (one Summary heading, annotation note after the list, both finding counts), a visible warning when the Bitbucket account lookup fails (needs the Account:Read scope), dismiss help links, and a `policy.yml ignored` note |
| [v2.18.1](version-history/v2.18.1) | Cost ceiling now reports or blocks when a model has no pricing row (new `cost-ceiling-unpriced`, default `warn`, #977), and the `ai-pr-review/policy-gate` check no longer flips back to `action_required` after `review-full` (#979) |
| [v2.18.0](version-history/v2.18.0) | New default models: OpenAI `gpt-6-luna` / `gpt-6.1-sol` and Google `gemini-3.5-flash-lite` / `gemini-3.8-flash` (behavior changes, live-canary verified), Gemini 3 thinking capped at `low`, weekly model watcher now covers OpenAI and Google |
| [v2.17.0](version-history/v2.17.0) | Default Anthropic standard model is now `claude-sonnet-5-5` (behavior change, live-canary verified), draft GitHub release created on tag push, token table labels Sonnet 5.5 correctly |
| [v2.16.2](version-history/v2.16.2) | GitHub Marketplace metadata in `action.yml` (new name `Tag1 Multi-Agent PR Review`, short description, orange `git-pull-request` badge, author), plus two stale-statement fixes |
| [v2.16.1](version-history/v2.16.1) | Every third-party GitHub Action pinned to a commit SHA (#955), `e2e-gate` now required and runs the live legs on `release/*` pushes, `deep-preflight` credential isolation (#962) and a verifier false-failure fixed |
| [v2.16.0](version-history/v2.16.0) | `deep-preflight` subcommand replaces the weekly credential check's inline bash (#956), plus e2e harness follow-ups (#957, #958, #959) |
| [v2.15.0](version-history/v2.15.0) | Deterministic Python e2e harness replaces the LLM-orchestrated e2e script |
| [v2.14.1](version-history/v2.14.1) | Bitbucket duplicate verdict-reply spam on a permanently-failing feedback-store write or degraded permission check fixed (#941) |
| [v2.14.0](version-history/v2.14.0) | Bitbucket learning-loop store (#906): `false-positive`/`wont-fix` verdicts now persist and feed future reviews; found and fixed several follow-up issues before and after tagging |
| [v2.13.2](version-history/v2.13.2) | Compute-phase skip crash fixed (#927); Bitbucket brand-new-summary-comment race fixed (#930) |
| [v2.13.1](version-history/v2.13.1) | Two v2.13.0 release-doc gaps fixed (stale approval-ceiling wording, missing Behavior-change label); docs-only |
| [v2.13.0](version-history/v2.13.0) | Bitbucket reaches finding-lifecycle parity (dedup, Code Insights, verdict polling); `approval-ceiling`; 5 security fixes (#886/#887/#894/#913/#914); slash-command input-forwarding gaps closed |
| [v2.12.0](version-history/v2.12.0) | PR title/description now visible to review agents; GitLab findings update in place; per-agent token budgets; pre-flight cost ceiling; judge-verdict persistence |
| [v2.11.0](version-history/v2.11.0) | GitLab now renders out-of-diff findings instead of dropping them; `REVIEW_TARGET` case-sensitivity fixed; phpstan Semgrep false positive fixed |
| [v2.10.0](version-history/v2.10.0) | Dropped-verdict bug fixed; resolved-without-verdict duplicate comments now explained instead of silently reposted; silent slash-command failures now signal clearly |
| [v2.9.0](version-history/v2.9.0) | Token usage moved out of the review comment by default; container-action env passthrough gaps closed |
| [v2.8.0](version-history/v2.8.0) | GitLab cross-run finding dedup; canonical-review empty-body/dismiss fixes; Opus 5 default |

See [Older releases](version-history/archive) for v2.7.0 and earlier, back to v0.7.0.

For the underlying commit-level changelog, see [CHANGELOG.md](https://github.com/tag1consulting/ai-pr-review/blob/main/CHANGELOG.md) on GitHub.
