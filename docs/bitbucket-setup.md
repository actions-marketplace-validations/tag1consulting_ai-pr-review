---
layout: default
title: Bitbucket Setup
parent: Configuration
nav_order: 4
render_with_liquid: false
---

# Bitbucket Cloud Pipelines setup

`ai-pr-review` supports Bitbucket Cloud PRs. It uses the same container image as GitHub Actions. The Bitbucket path posts one summary comment per PR and updates it in place on later runs. Findings that can have an inline anchor render as [Code Insights](https://support.atlassian.com/bitbucket-cloud/docs/code-insights/) annotations on the PR diff. All other findings render as markdown bullets in the comment body.

The Bitbucket Cloud API *can* create real inline PR comments, threaded replies, and thread resolution. This is not a platform limitation. `ai-pr-review` does not use them on purpose. Code Insights annotations are idempotent by nature, so a repeated post of the same finding never creates a duplicate. They also need no thread-resolution logic, because annotations cannot receive replies. See [ADR 0005](adr/0005-bitbucket-code-insights-not-inline-comment-threads) for the full reasoning.

## What works

- Summary comment upsert (one comment per PR, updated on each run)
- Leaving out the Walkthrough table: Bitbucket cannot collapse the table the way GitHub and GitLab do, so the per-file table shows in full by default. Set `AI_SUPPRESS_WALKTHROUGH=true` (a Pipelines variable) to leave it out. The Summary text, Type, and Effort stay. A summary comment that already has a table loses it on the next run.
- Inline findings through Code Insights annotations, rebuilt from scratch on every run (`AI_BITBUCKET_CODE_INSIGHTS`, default `true`). Every active finding also renders as a bullet in the summary comment. A finding that has an annotation gets a shorter bullet, with no remediation sub-bullet, because that detail is already on the diff. Each annotation has the same `[F<n>]` token as the findings in the summary comment, so you can refer to it there. If Code Insights is not available on the plan of your workspace (a 403 or 404 on the report API), every finding still renders with its full bullet in the summary comment body, as it did before this feature existed. The review drops no finding.
- Setting the reviewer state of the PR (approved or changes requested) to match the decided outcome of the review (`AI_BITBUCKET_REVIEW_STATE`, default `true`). A downgrade to `COMMENT` clears any prior approve or request-changes state, so an old badge from an earlier run does not stay. **Upgrading into this version:** This setting is on by default, so you get real approve and request-changes state at once, with no opt-in step. If a branch restriction counts the approval of this bot toward a merge requirement, or blocks merges on "changes requested", review that rule before you upgrade. Set `AI_BITBUCKET_REVIEW_STATE=false` to keep the prior behavior (heading text only).
- Cross-run dedup: the next run excludes a finding that was dismissed through the hidden verdicts marker of the summary comment from its Code Insights report. The finding must stay at the same file and line. This is part of the `AI_BITBUCKET_CODE_INSIGHTS` flag (see [issue #839](https://github.com/tag1consulting/ai-pr-review/issues/839)). GitHub uses a fuzzy match (same file, within 3 lines, compatible category). Bitbucket has no comment threads to match against, so suppression here uses the exact fingerprint only. If the line of a dismissed finding moves by even one line on a later push, the review treats it as a new finding.
- Dismissing or suppressing a finding with a comment command (`AI_BITBUCKET_VERDICTS`, default `false`, see [Dismissing findings](#dismissing-findings) below)
- Incremental-diff SHA watermark (a hidden reference-link marker). The Bitbucket renderer shows an HTML comment as literal text and does not hide it, unlike GitHub and GitLab. Bitbucket therefore uses a different marker form. See [Version History → v2.6.1](version-history/archive#v261).
- All existing AI agents and static analyzers (same container image, same review logic)
- Automatic retry on transient Bitbucket API errors (429, 502, 503, 504, and transient network errors)

### Code Insights annotation category mapping

Each finding category maps to one of the three `annotation_type` values of Code Insights. The rules are: `VULNERABILITY` means an attacker can exploit the finding. `BUG` means the code is wrong at runtime. `CODE_SMELL` means a maintainability problem. `test-gap` is in the `BUG` group (not `CODE_SMELL`) because Bitbucket shows `BUG` more prominently. A missing test on a security-relevant path is not only cosmetic.

| `annotation_type` | Categories |
|---|---|
| `VULNERABILITY` | `authz`, `injection`, `secret`, `dependency-cve` |
| `BUG` | `edge-case`, `test-gap` |
| `CODE_SMELL` | `architecture-coupling`, `observability`, `docs`, `lint`, `other` |

Severity maps 1:1: Critical→`CRITICAL`, High→`HIGH`, Medium→`MEDIUM`, Low→`LOW`.

### Concurrency

Each run deletes the Code Insights report and rebuilds it from scratch (delete, then recreate, then post the annotations). Two runs on the same PR can overlap closely enough to interleave their delete and recreate cycles. This can leave the report in an inconsistent state. GitHub and GitLab reviews have the same class of risk (see the Concurrency note in [Features → Quiet reruns](features#quiet-reruns-github)). If your pipeline pushes often enough for this to matter, configure a Bitbucket Pipelines concurrency setting keyed on the PR. Then only one review runs against the PR at a time.

## Dismissing findings

Enable this feature with `AI_BITBUCKET_VERDICTS=true` (default `false`). You can also set `AI_BITBUCKET_VERDICT_MIN_ROLE` (`read`, `write`, or `admin`, default `write`).

**Bitbucket Pipelines has no `issue_comment`-equivalent trigger** (see "What does not work" below). GitHub responds at once through a webhook. Bitbucket does not. A verdict command takes effect only on **the next review run**, because the review run polls the PR comments for pending commands. To apply a command at once, re-run the pipeline manually from **Pipelines → \[run\] → Rerun** in the Bitbucket UI. Do not wait for the next push.

**Syntax:** Always use a **top-level PR comment** that refers to the `F<n>` token of a finding in the summary comment. Bitbucket has no inline-comment-reply form like GitHub. Bitbucket findings anchor to the diff through Code Insights annotations, and annotations cannot receive replies:

```
/ai-pr-review false-positive F3 not exploitable, input is sanitized upstream
/ai-pr-review wont-fix F7 accepted risk for this release
/ai-pr-review fixed F12 a1b2c3d
```

`dismiss` is an alias for `false-positive`. You can put more than one command in a comment (one per line). The review applies all of them, as on GitHub and GitLab.

**Authorization is fail-closed.** Before the review applies a command, it must confirm that the commenter may use it. It checks two sources in this order:

1. **The owners file.** The review reads `.bitbucket/CODEOWNERS`. This is the file that Bitbucket uses for code owners. Only a line with the pattern `*` counts. It is a global list of people who may use `/ai-pr-review` commands. A commenter who is on that list is authorized. No API call is needed, so this works when the bot account has only write access.
2. **The repository permission.** A commenter who is not on the list is checked against the Bitbucket repository permission and `AI_BITBUCKET_VERDICT_MIN_ROLE`. Bitbucket allows that lookup only for repository admins, so it works only if the bot account is an admin of the repository.

If neither source confirms the commenter, the review rejects the command. A missing, oversized, or unreadable owners file is the same as an empty one. The review never applies a command on a guess.

Write the list like this:

```
# People who may dismiss AI review findings
* 557058:9aa59bdf-2b71-431a-af18-286802c44d56 {9f49742f-b2a6-491a-84aa-6bbf3a935756}
```

- An Atlassian `account_id` or a `{uuid}` matches the account. These never change. Use one of them for every owner. The `account_id` of a commenter is the `account_id` field of the comment `user` in the Bitbucket pull request API.
- `@nickname` entries are ignored and logged as a warning. A nickname is not a unique identifier, so it can not authorize a commenter. Unverified: whether Bitbucket lets a user change a nickname. Version 2.23.1 matched on `@nickname`. Version 2.23.2 removed that, so replace each `@nickname` line with an `account_id` or a `{uuid}`. A command from a person who is listed only by nickname gets the permission lookup, as if the file did not list them.
- Email addresses, `workspace/group` names, and `@teams/...` entries are ignored and logged at INFO level. A comment carries no email address, and a group lookup also needs admin.
- Lines with another pattern (for example `*.py` or `docs/`) are ignored by this feature. Bitbucket still uses them for its own reviewer suggestions. The review does not scope owners by path, because the owner of `docs/` must not be able to dismiss a security finding in code.
- If `AI_BITBUCKET_VERDICT_MIN_ROLE` is `admin`, the owners file is not used. An operator who asked for admins only did not ask for a list of names.
- Bitbucket reads `.bitbucket/CODEOWNERS` to suggest reviewers when a pull request opens. Names on a `*` line are suggested on every pull request where that Bitbucket feature is on. Unverified: which Bitbucket plan has that feature, and how Bitbucket treats an `account_id` or `{uuid}` entry it does not know.

**Where the owners file is read from.** The review reads the file from the target branch only, with `git show origin/<BASE_REF>:.bitbucket/CODEOWNERS`. It never reads the file in the checkout, and `AI_POLICY_SOURCE` has no effect on it. A pull request therefore cannot add its own author to the list. A change to the file takes effect after it is merged into the target branch.

- Version 2.23.1 followed `AI_POLICY_SOURCE`. With `workspace`, a pull request could add its own author to the list. Version 2.23.2 removed that option for this file.
- The pipeline must set `BASE_REF` to the destination branch, and the clone must contain `origin/<BASE_REF>`. If `BASE_REF` is empty, the review logs a warning and uses no owners file. If `origin/<BASE_REF>` is missing, `git show` fails, the review logs a warning, and it uses no owners file. In both cases the permission lookup decides.
- Push access to the repository is still the trust boundary. A person who can push can retarget a pull request to a branch with a different owners file, and can edit `bitbucket-pipelines.yml`.

The bot replies to every command it processes. The reply says if the command was applied, rejected, or could not be checked. Two replies need your action:

- "could not verify your repository permissions right now" means that the permission lookup failed for a temporary reason. The review tries again on the next run.
- "you are not listed on a `*` line", "was not found, could not be read, or is larger than 64 KiB", and "`AI_BITBUCKET_VERDICT_MIN_ROLE` is `admin`" all mean that the permission lookup returned HTTP 403. The bot account is not an admin, or its token lacks the permission. A retry does not help. The reply names the fix for your case: add the commenter to the owners file on the target branch, add the file, or make the bot account an admin. The command stays pending, and the next review run applies it after you fix this.

Earlier versions answered that HTTP 403 with "could not verify your repository permissions right now. Try again". That reply was wrong, because the error does not go away on its own. A command that already has that reply gets one new reply with the new text.

The review never ignores a command silently.

**Finding numbers stay stable across runs.** Bitbucket keeps one summary comment and rewrites it on each run, so the hidden id-map marker is the only record of finding numbers. The review keeps the entries of earlier runs in that marker. A finding that an incremental run does not report again keeps its `F<n>`, so a waiting command can still name it. A run with no findings does not restart the numbering at F1. A finding that returns after a run without it keeps its number and does not get the "new" badge. The earlier entries have a size budget of 4,000 bytes of JSON (about 5.4 KB in the comment). If the budget is full, the lowest numbers go first and the highest number always stays. If the summary comment is too large, the review keeps fewer earlier entries and writes a WARNING to the job log.

**Feedback command (issue #933):** `/ai-pr-review feedback [F<n>] <text>` saves free-form feedback to the learning-loop store, with no change to any finding. It uses the same authorization as the verdict commands and the same polling. The text is required, and a bare command gets a usage reply. The text passes the same sanitization as on GitHub before it reaches later review prompts. It needs both `AI_FEEDBACK_LOOP=true` and `AI_BITBUCKET_VERDICTS=true`. Without the learning loop, the bot replies that it saved nothing. Text that looks like a secret, such as a token, is removed. The command then gets the usage reply and nothing is saved.

**Learning loop (issue #906):** A `false-positive` or `wont-fix` verdict (including the `dismiss` alias) also writes to the cross-repo learning-loop store. A `fixed` verdict is never saved, on either provider. The store supplies the `<repo-feedback>` context that the review injects into future review prompts. This works the same way as a GitHub dismiss. It needs both `AI_FEEDBACK_LOOP=true` and `AI_BITBUCKET_VERDICTS=true`. See [Learning loop → Required setup](learning-loop#required-setup) for the full checklist. The checklist includes the **Repository:Write** token scope. This is a step up from the Repository:Read and Pull request:Write scopes that the base setup needs. See the security note under [Grant PR scopes](#3-grant-pr-scopes) below. If the token does not have that scope, or the write fails for any other reason, the review still suppresses the finding on this PR. The reply of the bot then says that it did **not** save the entry and that future reviews will not learn from it. If `AI_FEEDBACK_LOOP` is off, the reply says that instead. A missing scope produces exactly one "not saved" reply for each verdict (issue #941). The reply does not repeat on later pipeline runs. After you add the scope, the next review run saves the same verdict automatically. You do not need to post the command again.

## What does not work on Bitbucket

- Slash commands other than dismiss, false-positive, wont-fix, fixed, and feedback (`explain`, `revise`, `rescan`, `review-full`, `skip`): Bitbucket Pipelines has no `issue_comment` equivalent, so these commands have no trigger. Only the verdict commands work, and only because the review run polls for them (see [Dismissing findings](#dismissing-findings) above). The review does not react when the comment is posted.
- Collapsed and expandable sections: the PR-summary Walkthrough and the token-usage table (under `token-usage-display: full`) render as flat, always-expanded content on Bitbucket. Bitbucket Cloud renders no HTML in comments, so `<details>` is not an option (see [BCLOUD-20231](https://jira.atlassian.com/browse/BCLOUD-20231)). The default `token-usage-display: compact` is one plain line, so this matters only if you choose `full`.

## One-time setup

### 1. Create a bot user and Atlassian API token

Use a dedicated service account or your personal account. Go to <https://id.atlassian.com/manage-profile/security/api-tokens> and create a new API token. Store the token in a safe place. Atlassian shows it only one time.

### 2. Set repository variables

In your Bitbucket repo, go to **Repository settings → Pipelines → Repository variables** and add:

| Name | Secured? | Value |
|---|---|---|
| `BITBUCKET_EMAIL` | No | Atlassian account email of the bot user |
| `BITBUCKET_API_TOKEN` | Yes | The API token from step 1 |
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key (or swap for your provider) |
| `AI_PROVIDER` | No | `anthropic` (default). Alternatives: `openai`, `google`, `bedrock-proxy` |
| `AI_REVIEW_MODE` | No | Empty (default): defer to `.ai-pr-review/policy.yml` route matching if present, else `quick`. Set to `full` to override. |
| `AI_FAIL_ON_FINDINGS` | No | `true` (default, see the starter pipeline). Exit code 2 when Critical/High findings block approval, failing the pipeline step. Set to `false` to always exit 0. |
| `AI_REVIEW_IMAGE_TAG` | No | Container tag to pull, for example `latest`. Required, no default. The `image.name` field of the starter pipeline uses this variable through the Bitbucket `${{VAR}}` syntax. That syntax cannot resolve a secured variable at all, so this variable must stay non-secured. |
| `AI_BITBUCKET_VERDICTS` | No | `false` (default). Set to `true` to enable the `dismiss`, `false-positive`, `wont-fix`, `fixed`, and `feedback` comment commands. See [Dismissing findings](#dismissing-findings). |
| `AI_BITBUCKET_VERDICT_MIN_ROLE` | No | `write` (default). Minimum Bitbucket repository permission (`read`, `write`, or `admin`) required to apply a verdict command through the permission lookup. With `admin`, the owners file `.bitbucket/CODEOWNERS` is not used. It has an effect only when `AI_BITBUCKET_VERDICTS=true`. |
| `AI_BITBUCKET_REVIEW_STATE` | No | `true` (default). Set to `false` to stop the bot from calling the approve and request-changes endpoints of Bitbucket. The decided outcome still renders as heading text in the summary comment in both cases. |
| `AI_FEEDBACK_LOOP` | No | `false` (default). Set to `true` to save `false-positive`, `wont-fix`, and `feedback` entries to the cross-repo learning-loop store. See [Dismissing findings → Learning loop](#dismissing-findings). It also needs `AI_BITBUCKET_VERDICTS=true` and the **Repository:Write** scope below. |
| `AI_FEEDBACK_BRANCH` | No | `ai-pr-review-bot` (default). Branch that holds the JSONL file of the learning-loop store. GitHub reads the same variable. |

### 3. Grant PR scopes

The effective scopes of the API token follow the permissions of the user. The bot user must have at least these permissions:

- **Repository:Read** on the repo that the review covers
- **Pull request:Write** on the repo (to create and update comments)
- **Repository admin** is needed only for the permission-lookup fallback of verdict commands (see [Dismissing findings](#dismissing-findings)). If you list your owners in `.bitbucket/CODEOWNERS`, the bot account does not need it.
- **Account:Read** (`read:user:bitbucket` on a scoped API token). The review calls `GET /2.0/user` to find the account that it runs as. It trusts only summary comments that this account wrote (anti-spoofing). Without this scope, the lookup fails and the run logs a WARNING. Incremental review, in-place summary update, and verdict polling are then all off for that run. The result is a new summary comment on every push.

**If `AI_FEEDBACK_LOOP=true`**, the token also needs **Repository:Write**. Read this before you grant it. Bitbucket cannot limit the write access of a token to one branch. GitHub can do this with branch protection rules on `ai-pr-review-bot`, even for a `contents:write` token. Repository:Write on this token means the token can push to *any* branch in the repo, not only the feedback-store branch. The added risk starts when the token *has* the scope, not only when `AI_FEEDBACK_LOOP=true` is set. A token with Repository:Write that is not in use is still a larger blast radius if it leaks. This token is a secured pipeline variable (see [Secret exposure to pipeline contributors](#secret-exposure-to-pipeline-contributors) below). Anyone who can change `bitbucket-pipelines.yml` on a branch they can push to can read it out. With this scope granted, that access also lets them push to your default and release branches, not only post comments and reactions. Grant Repository:Write only when you are ready to enable the learning loop. If your workspace has branch restrictions, first protect your default and release branches against this token. Use the same caution as for any bot token that gains write access.

This scope has another effect. Anyone who can push directly to `ai-pr-review-bot` (not only through the slash-command flow) can write raw entries into its JSONL file. These entries bypass the write-time sanitization of the slash-command parser (HTML-escaping and secret-pattern rejection). By design, the `<repo-feedback>` render step does not escape `reason` a second time. It assumes that every entry already passed through that sanitization once. Restrict push access to `ai-pr-review-bot` wherever the branch-restriction settings of your workspace allow it. Do the same as for any other branch that a bot token can reach.

### 4. Copy the starter pipeline

Copy [`examples/pipelines/bitbucket-pipelines.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/pipelines/bitbucket-pipelines.yml) to the root of your repo as `bitbucket-pipelines.yml`. Commit and push the file. The review runs on every PR open and update. If you edit the `image:` block by hand, keep the `${{AI_REVIEW_IMAGE_TAG}}` template form. The Bitbucket `image.name` field requires that syntax and rejects a bare `$AI_REVIEW_IMAGE_TAG`.

### 5. Enable Pipelines

In **Repository settings → Pipelines → Settings**, turn on Pipelines if it is not already on.

## Environment variables the review reads

The starter pipeline translates the native Bitbucket environment variables to the canonical variables of the review. If you write your own pipeline, make sure these variables are set:

| Review var | Source (Bitbucket Pipelines) |
|---|---|
| `VCS_PROVIDER` | Must be set to `bitbucket` |
| `PR_NUMBER` | `$BITBUCKET_PR_ID` |
| `BASE_REF` | `$BITBUCKET_PR_DESTINATION_BRANCH` |
| `HEAD_REF` | `$BITBUCKET_BRANCH` |
| `HEAD_SHA` | `$BITBUCKET_COMMIT` |
| `GITHUB_REPOSITORY` | `${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO_SLUG}` |
| `BITBUCKET_EMAIL` | Repo variable |
| `BITBUCKET_API_TOKEN` | Repo variable (secured) |
| `AI_PROVIDER` | Repo variable (default `anthropic`) |
| `ANTHROPIC_API_KEY` (or equivalent) | Repo variable (secured) |
| `AI_IGNORE_MERGE_COMMITS` | Repo variable (default `true`) |
| `AI_FAIL_ON_FINDINGS` | Repo variable (default `true`) |
| `AI_CONTEXT_ENRICHMENT` | Repo variable (default `false`) |
| `AI_SARIF_PATHS` | Repo variable (default empty) |

> **Note:** `GITHUB_REPOSITORY` is reused as a generic `owner/repo` identifier, so the same environment contract works for both providers. You can also set `BITBUCKET_WORKSPACE` and `BITBUCKET_REPO_SLUG` explicitly. The script prefers those if both are set.

## `clone.depth: full` is required

`ai_pr_review` never fetches from `origin` itself. It expects `origin/<BASE_REF>` to exist already as a local ref (see issue #702). On GitHub Actions, `actions/checkout` with `fetch-depth: 0` creates that ref as part of its own full clone. Bitbucket Pipelines does a single-branch PR clone. The script step of the starter template therefore fetches the base branch before it starts the tool:

```
git fetch origin "$BITBUCKET_PR_DESTINATION_BRANCH":"refs/remotes/origin/$BITBUCKET_PR_DESTINATION_BRANCH"
```

For this fetch to succeed, the history of the base branch must be in the clone. The default shallow Pipelines clone does not guarantee this. Set `clone.depth: full` at the top of `bitbucket-pipelines.yml` to prevent this (the starter does this).

If `origin/<BASE_REF>` is still missing when the tool runs (for example, in a custom pipeline that skips this fetch step), `git diff` against it fails. The tool then raises a `GitDiffError` and stops the pipeline with a non-zero exit. It does not report "no changed files" and skip the review without a reason.

## Security considerations

### Secret exposure to pipeline contributors

`BITBUCKET_API_TOKEN` and `ANTHROPIC_API_KEY` (or your provider key) are exposed as secured repo variables to **any pipeline run triggered from a branch in this repository**. This includes PRs opened by any user with branch-push access.

A contributor with push access to any branch can change `bitbucket-pipelines.yml` in their PR and steal these secrets. This is the classic "pwn-request" pattern. The Bitbucket Cloud setting "do not expose secured variables to forks" protects against external forks, but **not against in-repo branches**.

Mitigations:
- **Use a dedicated bot user** with minimum scope: Pull request:Write on the reviewed repo only. Do not give workspace-wide admin access.
- **Restrict who can push branches** in **Repository settings → Branch restrictions**. Pipelines runs are limited to users who can push the triggering branch.
- **Enable manual approval** for pipelines that non-maintainer contributions trigger (**Repository settings → Pipelines → Settings → "Require manual step approval"**).
- **Do not use this setup on a public open-source repo** without additional safeguards. Any fork contributor could open a PR against your repo.

### `BITBUCKET_API_TOKEN` scope

Use the minimum scope required (Repository:Read + Pull request:Write + Account:Read). If the bot user has broader Workspace or Project admin rights, a token compromise has a much larger blast radius.

**If you enable `AI_FEEDBACK_LOOP`** (issue #906), the token also needs Repository:Write. That scope is not limited to the feedback-store branch (see the note under [Grant PR scopes](#3-grant-pr-scopes) above). This is a real and deliberate increase in what a compromised token can do (push to any branch, not only comment and react). In return, the learning loop can persist on Bitbucket. The feature is off by default and stays off unless you turn it on.

## Troubleshooting

### `ERROR: bb_api POST /repositories/.../comments -> 401`

The API token of the bot user is missing or wrong, or the user does not have Pull request:Write on the repo. Check **Repository settings → Access management** for the bot user.

### `ERROR: bb_api POST /repositories/.../comments -> 403`

The API token exists but the user does not have write access to comments. Check **Workspace settings → Members** and **Repository settings → User and group access**.

### `WARNING ... could not resolve the bot account (GET /2.0/user -> HTTP ...)`

The review calls `GET /2.0/user` to find the account that it runs as. If the call fails, the review fails closed. It does not trust any existing summary comment. It posts a new summary on every push and reviews the full diff each time. The summary comment also has a note when this happens. The warning and the note depend on the HTTP status:

- **HTTP 401:** Bitbucket rejected the credentials, or the token has no scopes at all (the body says `API Token provided has no Bitbucket scopes.`). Check `BITBUCKET_EMAIL` and the token. Make sure the token has scopes.
- **HTTP 403:** The token works but does not have the Account:Read scope (`read:user:bitbucket`). The warning lists the required and the granted scopes when Bitbucket sends them. Add the missing scope and re-run.
- **Any other HTTP error:** The warning gives the status and the start of the body. Check the token and its scopes.
- **A 200 reply that is not JSON or has no `account_id`:** The warning says so, without a body excerpt. Check that the request reached the Bitbucket API.

### `ERROR: git diff against 'origin/<ref>...<sha>' failed`

The `git fetch` step of the starter template (see "`clone.depth: full` is required" above) did not run or did not find the base branch. Make sure `clone.depth: full` is set. Make sure `BITBUCKET_PR_DESTINATION_BRANCH` is the branch you expect.

### Nothing posts, review exits 0

The diff is probably larger than `MAX_DIFF_LINES` (default 5000). The pipeline log shows `Review skipped: diff too large (N lines > M)`. The review also posts or updates an "AI Review skipped." comment on the PR to explain the skip.
