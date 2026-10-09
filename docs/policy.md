---
layout: default
title: Policies
parent: Configuration
nav_order: 1
---

# Policies

The `.ai-pr-review/policy.yml` file lets a repository route the review depth by changed-file path, base branch, or head branch. The review depth is which agents and analyzers run, and whether the review uses quick or full mode. You do not need to write a GitHub Actions expression for each repository, and you do not need to hard-code one global choice for every PR.

This feature solves the common case of mixed PR traffic. For example, a repository merges content-only changes straight to `main`. It collects feature work on a `staging` branch. It then tests that branch with a full review once for each batch, not on every push.

A ready-to-copy starting point is at [`examples/policy.yml.example`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/policy.yml.example). Copy it to `.ai-pr-review/policy.yml` and edit it. The action still reads `.github/ai-pr-review/policy.yml` as a fallback when `.ai-pr-review/policy.yml` is absent, for existing adopters. The action never merges the two files, and the first match wins. New adopters should use the neutral path. This also applies to repositories on GitLab or Bitbucket, which have no reason to have a `.github/` directory.

## Example

```yaml
# .ai-pr-review/policy.yml
version: 1

policies:
  content:                       # near-zero cost: content-only changes
    agents: []
    analyzers: []
  feature:                       # default: today's quick mode
    extends: quick
  integration:                   # staging smoke test
    extends: quick
    agents: [code-reviewer, silent-failure-hunter, edge-case-hunter]
  deep:
    extends: full                # today's full mode, unchanged

routes:
  - when: {paths: ['docs/**', '_data/**']}
    policy: content
  - when: {base-branch: 'staging-*'}
    policy: integration
  - when: {head-branch: 'feature/*'}
    policy: feature
default: feature
```

- **`policies`**: Named policies. `extends` is either a built-in base (`quick` or `full`, which reproduce today's roster exactly) or another policy that this file defines. The fields `agents`, `exclude-agents`, `analyzers`, and `exclude-analyzers` override the value of the extended base for that field only. A field that you leave unset inherits from `extends`. If you omit `extends`, the policy extends `quick`.
- **`routes`**: An ordered list. The first route whose `when` matches the PR wins. `when.paths` matches if *any* changed file matches *any* glob (standard shell-style globs, for example `src/**` or `*.md`). `when.base-branch` and `when.head-branch` each match a single glob against the base or head branch name of the PR. A route must constrain at least one of the three. An unconstrained route would match every PR without notice and shadow all the routes after it. The engine therefore rejects the file if it finds one.
- **`default`**: The policy to use when no route matches. If you omit it and nothing matches, the engine uses its hard-coded default (`quick`, all agents and analyzers eligible).

## Precedence

The list below goes from highest to lowest. Each level applies only when the level above it left a field unset:

1. **Slash command**: `/ai-pr-review review-full` always runs full mode. `/ai-pr-review rescan` uses the `AI_REVIEW_MODE_DEFAULT` repository variable if it is set.
2. **Explicit action input or repository variable**: This is the `ai-review-full` PR label, the `review-mode` input set to a real value, or `agents`, `analyzers`, and similar inputs set to a non-empty value (through `vars.AI_REVIEW_*` in the shipped template).
3. **`policy.yml` route match**: This page.
4. **Engine default**: Today's hard-coded behavior (`quick` mode, no agent or analyzer restriction). The engine uses it unchanged when no `policy.yml` exists. **Adoption of this feature is therefore opt-in and does not change behavior until you add the file**.

A repository with no `policy.yml` sees no change. Suppose a repository already sets `review-mode` (or `agents`, and so on) to an explicit value in its own workflow. That value stays, whatever a policy file says. Level 2 always wins over level 3.

Suppose your existing workflow hardcodes a fallback like `... || 'quick'` at the end of a `review-mode:` expression. That hardcoded value is itself an explicit override, and it always wins over `policy.yml`. If you want the repository to defer to policy routing, replace the trailing `'quick'` with `''`. You can also remove the special case for branch names. The shipped [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) template already does this.

## Security: loaded from the base ref, never the PR head, but only when your trigger needs it {#security-loaded-from-the-base-ref-never-the-pr-head--but-only-when-your-trigger-needs-it}

By default, the engine reads the policy file with `git show origin/{base-ref}:.ai-pr-review/policy.yml`. If the neutral path is not present, it falls back to `.github/ai-pr-review/policy.yml`. It **never** reads the file from the working tree of the checked-out PR branch. A PR that edits its own `policy.yml` to weaken its own review (for example, to drop `security-reviewer`) has no effect. The file that governs the review of a PR is the file that is committed on the *target* branch at review time. Merge a `policy.yml` change to your base branch, and it applies to every later PR review. The PR that introduced the change gets it on its *next* run, after the target branch of that PR (not the PR branch) has the file.

**This protection closes a real gap only when the workflow itself runs from the base branch, that is, with the GitHub `pull_request_target` trigger.** The far more common trigger is `pull_request`, which is the default in the shipped [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) template. With that trigger, the workflow *file* already runs from the PR head. A PR can already delete the review job, set `exclude-agents`, or force `review-mode: quick` in its own copy of the workflow, whatever the read location of `policy.yml` is. In that setup, the base-ref read guards a side door while the PR already controls the front door. Its only practical effect is that a `policy.yml` change needs one extra merge before it applies.

If your workflow is head-controlled (plain `pull_request`), you can set `policy-source: workspace` (`AI_POLICY_SOURCE`, default `base-ref`). Use it if you want a `policy.yml` change to apply immediately and do not need protection that your trigger does not provide. **Never set `policy-source: workspace` under `pull_request_target`.** That setting reopens exactly the gap that the base-ref read closes. A PR could then weaken its own review with its own `policy.yml` without notice.

A malformed or invalid `policy.yml` never blocks a review. Examples are bad YAML, an unknown agent or analyzer name, a cyclic `extends` chain, or an unconstrained route. The engine prints one warning to the run log, and the review continues with the hard-coded engine defaults, as if there was no policy file. The posted review comment also has a short `policy.yml ignored: <reason>` note, so a misnamed policy is visible on the PR. An invalid `policy-source` value (anything other than `base-ref` or `workspace`) has the same effect: one warning, and the engine falls back to `base-ref`.

## Release-branch escalation (replacing a hand-rolled workflow expression)

Earlier versions of the shipped template selected full mode automatically for `release/*` branches. They used a hardcoded `startsWith()` expression. You can express the same behavior as a policy route:

```yaml
policies:
  release:
    extends: full
routes:
  - when: {head-branch: 'release/*'}
    policy: release
default: feature
```

This route also works for any other branch convention (`hotfix/*`, merges into a specific base branch, and so on). You do not need to edit the workflow file again.

## Requiring `head-ref`

Route matching on `when.head-branch` needs the action to know the head branch name of the PR. The `head-ref` action input (optional, default `''`) carries this name. The shipped templates for GitHub, GitLab, and Bitbucket all wire it. If you use an older copy of a template that has no `head-ref`, add it before you use `head-branch` routes (see [Configuration](configuration)). Routes on `paths` and `base-branch` work without it.

## Requiring a review tier before merge

A route can name a policy that must have run (at least) before merge. Use `require` for this. It is a **manual-trigger merge gate**. The automatic push does not need to run that tier itself. A required status check on the target branch blocks the merge until *some* run satisfies it. That run can be automatic or `/ai-pr-review review-full`.

```yaml
policies:
  integration:
    extends: quick
  deep:
    extends: full

routes:
  - when: {base-branch: 'staging-*'}
    policy: integration
    require: deep
```

Every automatic push to a PR that targets `staging-*` runs the (cheap) `integration` tier. It also posts a GitHub check run with the name `ai-pr-review/policy-gate`:
- **`action_required`** if only `integration` has run so far. The check summary tells the reviewer to comment `/ai-pr-review review-full` (or add the `ai-review-full` label) to satisfy the requirement. The status is `action_required` and not `failure`, because an unmet requirement on an ordinary automatic push is not a defect. It is a manual step that nobody has done yet. It still blocks the merge. The pass set of GitHub for a required status check is `success`, `neutral`, and `skipped` only. `action_required` is therefore excluded in the same way as `failure`. (An earlier version of this check posted `neutral` for the unmet state. `neutral` is in that pass set, so it satisfied the requirement without notice and did not block it. Version v2.5.0 fixed this, #688.)
- **`success`** after a run at the required tier (or in full mode, which satisfies any requirement) has completed for the current commit. This includes a later `/ai-pr-review review-full` run. That run posts the check again for the same SHA, and GitHub evaluates branch protection again automatically.

After the requirement is satisfied for a commit, it stays satisfied for that commit. A slower automatic run on the same SHA never downgrades it. Before the run posts `action_required`, it looks for an existing `success` from the GitHub Actions app on that SHA. If it finds one, it posts a new `success` instead. After it posts, it checks once more in case `review-full` finished in between (#979). If those lookups fail, the run posts the result that it computed, as before. A new push is a new SHA and starts at `action_required` again. "Satisfied" means a completed `success` from the GitHub Actions app for that check name on that commit. The `GITHUB_TOKEN` of every workflow posts as that app. This trusts anyone who can run a workflow in the repository, as a required `ai-pr-review/policy-gate` check already does.

To make this a real merge gate, add a branch protection rule on the target branch that requires the `ai-pr-review/policy-gate` check (**Settings → Branches → Branch protection rules**). The shipped GitHub templates grant the `checks: write` permission that the check needs.

**GitHub only for now.** GitLab and Bitbucket have no equivalent yet. On those providers, `require` does nothing (the engine logs it at `info` level). Routing (`policies` and `routes` without `require`) works the same way everywhere.

[`examples/policy.yml.example`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/policy.yml.example) includes this exact `staging-*` → `integration`, `require: deep` route as a working starting point. Copy it and add the branch-protection rule above to turn it on.

**`AI_APPROVAL_CEILING` (issue #858) is deliberately not part of this schema.** It is a workflow-level environment variable and input, not a `policy.yml` field. This is on purpose. `policy.yml` fields inherit through `extends` and the route selects them by path glob. A ceiling for each route would let a route rule restore real approvals for a subset of paths without notice. A safety setting like this must never have that failure mode. You set it once, above the content of the reviewed repository (loaded from the base ref, but still controlled by the repository). See [Configuration: Approval ceiling](configuration#approval-ceiling).

## Live example

This repository uses its own feature. [`.github/ai-pr-review/policy.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/.github/ai-pr-review/policy.yml) routes docs-only PRs (`docs/**`, `language-profiles/**`) to the near-zero-cost tier. It runs a full-mode review automatically on every push to a `release/*` PR (the `deep` policy, which also satisfies the `ai-pr-review/policy-gate` merge gate). Feature and issue PRs that target a `release/*` branch keep the default quick review.

## Verifying a route matched

Check the `Token usage by agent` breakdown in the CI job log. The log always has it, on every provider (see [Features: Token usage](features#token-usage)). Do not use the posted review comment for this check. By default, the comment has only a compact summary line with an agent *count*, not names. It cannot tell you *which* agents ran. The full table in the job log names every agent that ran. Suppose a route has `agents: []`. If the job log has no `Token usage by agent` table (not an empty one), the route matched and suppressed the roster as expected. On GitHub, the `GITHUB_STEP_SUMMARY` page works the same way. When you verify, check the *first* automatic review of a route on a fresh PR. If you trigger `/ai-pr-review rescan` many times on the same PR, it can interact with incremental-diff caching. A single unusual result is then hard to interpret by itself.
