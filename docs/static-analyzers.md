---
layout: default
title: Static Analyzers
parent: Configuration
nav_order: 2
render_with_liquid: false
---

# Static Analyzers

The action runs deterministic analyzers alongside the LLM agents. Their findings flow through the same dedup, suppress, and render pipeline as LLM findings. All analyzers run concurrently in the parallel path. They run one at a time when `parallel: false`. If a binary is missing, the native analyzer writes a WARNING to stderr and returns an empty findings array. A missing binary never blocks the review.

The container action includes all analyzer binaries. For the direct-action or submodule paths, install the binaries you need. See [runtime dependencies](installation-direct-action#runtime-dependencies).

## Controlling which analyzers run

The `analyzers` (allowlist) and `exclude-analyzers` (denylist) settings control which analyzers run. Both accept a comma-separated list of the names in the **Analyzer** column below. An empty value (default) means all eligible analyzers run. When `analyzers` is set, the action ignores `exclude-analyzers` (the allowlist takes precedence). The action rejects unknown names with an error and a suggestion.

You can set these in three places. The list shows them in precedence order (highest wins):

1. **`/ai-pr-review review-full` slash command**: always runs the full roster and ignores any allowlist or denylist for that one run.
2. **Action input or repo variable**: set it directly in the calling workflow, or with the `AI_ANALYZERS`/`AI_EXCLUDE_ANALYZERS` env vars (see [Configuration → Analyzer and agent selection](configuration#analyzer-and-agent-selection)):

   ```yaml
   - uses: tag1consulting/ai-pr-review@main
     with:
       analyzers: 'semgrep,trufflehog'      # Run only semgrep and trufflehog
       # exclude-analyzers: 'checkov,tflint'  # or: run everything except checkov and tflint
   ```

3. **`.github/ai-pr-review/policy.yml`**: the `analyzers` and `exclude-analyzers` fields of a named policy. The action routes by changed-file path, base branch, or head branch. Different PRs can get different analyzer sets without a change to the workflow file. See [Policies](policy) for the full schema.

An explicit action input or repo variable always wins over a `policy.yml` route match. A route match wins over the engine default (all eligible analyzers run). The same three-place precedence applies to the `agents` and `exclude-agents` allowlist and denylist for review agents.

## Category mapping

Each analyzer maps its findings to a severity and to one of the 11 categories that the LLM agents also use (`authz`, `injection`, `dependency-cve`, `secret`, `architecture-coupling`, `test-gap`, `edge-case`, `observability`, `docs`, `lint`, `other`). An analyzer finding can then corroborate an LLM-agent finding on the same issue. The LLM judge pass does not down-rank corroborated findings, and they get a confidence boost. If the analyzer cannot classify a finding with confidence, it maps the finding to `"other"`. That category never blocks corroboration with a real category, and it never matches one by mistake.

| Analyzer | Language gate | Severity mapping | Confidence | Source tag |
|----------|--------------|-----------------|------------|------------|
| **shellcheck** | `.sh`, `.bash` | `error`→High, `warning`→Medium | 95 | `shellcheck` |
| **semgrep** | Any file | `ERROR`→High, `WARNING`→Medium, else→Low | 90 | `semgrep` |
| **trufflehog** | Any file | Verified secret→Critical, Unverified→High | 95 / 85 | `trufflehog` |
| **ruff** | `.py` files | `F`/`E` prefix→High, `W`/`C`→Medium, else→Low | 90 | `ruff` |
| **golangci-lint** | `.go` files | `errcheck`/`govet`/`staticcheck`→High, others→Medium | 90 | `golangci-lint` |
| **hadolint** | `Dockerfile*`, `*.dockerfile` | `error`→High, `warning`→Medium, else→Low | 90 | `hadolint` |
| **checkov** | `.tf`, `.tfvars`, `.yaml`, `.yml`, `Dockerfile*`, `.json` | `CKV2_*` and `CKV_SECRET_*`→High, all other checks→Medium | 80 | `checkov` |
| **phpcs** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | `ERROR`→High, `WARNING`→Medium. Uses Drupal+DrupalPractice standard when available, else PSR12 | 90 | `phpcs` |
| **eslint** | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` | severity 2→High, severity 1→Medium. Uses the consumer's config, and does nothing if no `eslint.config.*` or `.eslintrc.*` exists | 90 | `eslint` |
| **phpstan** | `.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile` | Severity depends on `PHPSTAN_LEVEL`: 8–9→High, 5–7→Medium, 0–4→Low (default level 3→Low). The analyzer always runs at that level. It never auto-discovers a project's own `phpstan.neon` or `phpstan.neon.dist`, because the analyzed workspace can hold untrusted fork-PR content | 85 | `phpstan` |
| **kube-linter** | `.yaml`, `.yml`, `.json` with `apiVersion:` + `kind:` headers | All findings→Medium (reliability-focused: missing probes, resource limits, etc.) | 85 | `kube-linter` |
| **tflint** | `.tf`, `.tfvars` | `error`→High, `warning`→Medium, `notice`→Low. Runs per Terraform module directory | 90 | `tflint` |
| **dep-exists** | Newly added dependencies in `package.json`, `requirements*.txt`, `Cargo.toml`, `composer.json`, `Gemfile` | A dependency the public registry does not know→High, a package first published under 30 days ago (npm, PyPI, crates.io)→Medium | 85 / 65 | `dep-exists` |
| **docs-api-check** | Python + `@param`-family languages (JS/TS, Java, Kotlin, C#, Ruby, C++, Scala) | A documented parameter not in the signature, or vice versa, always→Medium | 90 (Python, via ruff) / 80 (tree-sitter engine) | `docs-api-check` |
| **docs-ref-check** | Changed `.md` files | A broken relative link or heading anchor, always→Medium | 80 | `docs-ref-check` |
| **docs-drift-check** | Any file (runs unconditionally, like semgrep/trufflehog) | A doc reference to a file this PR deletes, always→Low | 80 | `docs-drift-check` |
| **cve-check** | Dependency manifests and lockfiles (see [Dependency vulnerability check](#dependency-vulnerability-check)) | CVSS >= 9.0→Critical, 7.0–8.9→High, 4.0–6.9→Medium, below 4.0→Low, unscored→High | 90 (70 when unscored) | `osv` |

## Dependency existence check

`dep-exists` asks the public registry whether each dependency this diff adds really exists. A model can invent a package name, and an attacker can register that name and ship malicious code under it. The check makes network calls to `registry.npmjs.org`, `pypi.org`, `crates.io`, `repo.packagist.org`, and `rubygems.org`. It sends only the package name.

- It checks only dependencies on added lines of a direct-dependency manifest. It does not read lockfiles.
- It does not check Go modules, because a private Go module returns "not found" from the public proxy and would raise a false finding.
- It skips an ecosystem when the repository sets a private registry (`.npmrc` registry, pip index flags, Cargo registries, composer `repositories`, a non-default Gemfile `source`).
- It fails open. A timeout, a rate limit, or any status other than 200 or 404 gives no finding.
- It reads only files in the repository, so it cannot see a private registry that is set in CI settings or a user-level config. A private scoped npm package, or a monorepo package that another package lists by plain version, returns "not found" from the public registry. The result is a High finding, and a High finding makes the review request changes. Set the registry in a file in the repository, or turn the analyzer off.
- It checks at most 25 new dependencies per run.
- Turn it off with `exclude-analyzers: dep-exists`.

## Documentation checks

Three analyzers find documentation mismatch and drift for zero LLM tokens. None can block a PR alone. `docs-api-check` and `docs-ref-check` report Medium, `docs-drift-check` reports Low, and Medium and Low both resolve to `APPROVE`.

- **`docs-api-check`** compares the documented parameters of a function with its actual signature. Python uses the installed `ruff` binary with `--isolated`, so the results never depend on the consumer's ruff config. Every other supported language uses a shared tree-sitter traversal. The traversal recognizes the JSDoc, JavaDoc, KDoc, YARD, Doxygen, and C# XML doc comment styles. The analyzer skips a function with a destructured or rest parameter and does not guess.
- **`docs-ref-check`** finds broken relative links and heading anchors in changed Markdown.
- **`docs-drift-check`** finds doc references to a file that the current PR deletes.

`docs-ref-check` and `docs-drift-check` are pure Python and offline. They never make network calls.

`docs-api-check` excludes PHP on purpose, because `phpcs` already covers doc-comment mismatch on both the Drupal and PSR12 paths (see the `phpcs` row above). See `docs/adr/0001-tree-sitter-not-node-for-doc-mismatch.md` and `docs/adr/0002-hand-rolled-doc-ref-checker-not-lychee.md` in the repo for the reasons these analyzers are hand-written and do not use an existing tool.

A fourth analyzer, **`docs-missing-check`**, flagged a newly added public function or method that had no doc comment. It checked only symbols that the current diff added. It checked Go with a dedicated `golangci-lint --enable-only=godoclint` invocation. The maintainers removed it in issue #815. It reported Low severity only, and its maintenance cost (a separate ruff rule set, the dedicated Go linter invocation, and a second tree-sitter presence check) was too high for its review value. It was not redundant with the other three analyzers. `docs-api-check` fires only when a doc comment already exists and does not match the signature. See the #815 PR description for the full reasoning. The `analyzers` and `exclude-analyzers` settings still accept the name as a deprecated no-op. The v3.0.0 release removes it.

## Dependency vulnerability check

The analyzer name for `analyzers` and `exclude-analyzers` is `cve-check`. When a PR modifies a supported dependency manifest or lockfile, the action queries [OSV.dev](https://osv.dev/) for known vulnerabilities in the declared versions. It reports them as findings next to the LLM review. The action prefers lockfiles to range manifests when both exist.

| Ecosystem | Supported files |
|-----------|-----------------|
| Go | `go.mod` |
| npm | `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml` |
| Python | `poetry.lock`, `Pipfile.lock`, `uv.lock`, `requirements*.txt` (exact pins only) |
| PHP | `composer.lock`, `composer.json` |
| Rust | `Cargo.lock` |
| Ruby | `Gemfile.lock` |

The analyzer maps CVSS scores to severity: >= 9.0 → Critical, 7.0–8.9 → High, 4.0–6.9 → Medium, below 4.0 → Low. Unscored or unparseable CVEs map to **High** as a fail-safe (see `_cvss_to_severity` in `ai_pr_review/analyzers/native/cve_check.py`). The source tag is `osv`. Confidence is 90, or 70 when the CVE has no score. Critical and High findings trigger `REQUEST_CHANGES` on the PR review, like any other high-severity finding.

The check needs no configuration. It runs when a manifest file is in the diff. The OSV.dev API is unauthenticated and free. If the API is unreachable, the check logs a warning and continues. A CVE-lookup failure never blocks the review.

To accept a specific CVE (for example, a library that only a test fixture uses), add a suppression rule that matches the CVE or GHSA ID. See [Suppression rules](suppression#suppressing-cve-findings) for the schema and a worked example.

## SARIF ingestion

The Python engine can also read SARIF 2.1.0 output from any external tool (CodeQL, Semgrep Pro, Trivy, Snyk, custom scanners).

### Setup

1. Run your SARIF-producing tool as an earlier step (for example, CodeQL or Trivy).
2. Pass the output paths in the `sarif-paths` input:

```yaml
- uses: tag1consulting/ai-pr-review@main
  with:
    sarif-paths: 'results/codeql.sarif,results/trivy.sarif'
    api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

See `examples/workflows/sarif-codeql.yml` for a complete CodeQL + AI review pipeline.

### Severity mapping

| SARIF level | AI review severity |
|------------|-------------------|
| `error` | High |
| `warning` | Medium |
| `note` | Low |
| `none` | Low |

### Behavior

- Source tag: `sarif:<driver.name>` (for example, `sarif:CodeQL`, `sarif:trivy`).
- Default confidence: 90.
- Remediation text: taken from the rule's `help.text` field when present.
- File URI prefixes (`file:///`, `file://`) are stripped from location paths.
- Findings from SARIF files go through the same dedup and suppress pipeline as findings from native analyzers and LLM agents.
- The engine logs a `WARNING` for an unreadable or malformed SARIF file and skips it (fail-soft).

---

## Implementation reference

All 17 analyzers are native Python functions in `ai_pr_review/analyzers/native/`. The `analyzers/bridge.py` dispatcher maps each tool name to its Python callable. Each analyzer runs the tool binary with `subprocess.run` and parses the JSON output in Python. Test coverage is in `tests/python/test_analyzer_<tool>.py`. The three docs analyzers use the files `test_analyzer_docs_comments.py` (docs-api-check), `test_analyzer_docs_ref_check.py`, and `test_analyzer_docs_drift_check.py`.

