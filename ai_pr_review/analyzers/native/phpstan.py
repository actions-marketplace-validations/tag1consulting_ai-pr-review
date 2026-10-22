"""Native Python implementation of the phpstan analyzer.

Replaces analyzers/run-phpstan.sh. Discovers config files and phpstan-drupal,
invokes phpstan, and converts its JSON output to Finding instances.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from ai_pr_review.analyzers.native._paths import strip_workspace_prefix
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 85
_SOURCE = "phpstan"
_TIMEOUT_SECS = 120
_PHP_EXTENSIONS = frozenset({".php", ".module", ".inc", ".theme", ".install", ".profile"})
_REMEDIATION = "Fix the type error reported by PHPStan. See https://phpstan.org/user-guide/getting-started"
_VALID_LEVEL_RE = re.compile(r"^[0-9]$")
_DEFAULT_LEVEL = "3"

# Absolute, image-baked composer install (see Dockerfile) — never a path
# relative to the analyzed workspace. Used only to give Drupal-aware analysis
# when the shipped container has phpstan-drupal installed; absent entirely in
# non-container (direct-action) deployments, where this simply evaluates False.
_BAKED_COMPOSER_AUTOLOAD = Path("/opt/composer/vendor/autoload.php")
_BAKED_PHPSTAN_DRUPAL = Path("/opt/composer/vendor/mglaman/phpstan-drupal")


# phpstan gets an allowlist environment. It runs PHP from untrusted files, so
# the job credentials must not reach it. Only the search path, the locale, a
# temp directory and the proxy and CA settings pass.
_ALLOWED_ENV_NAMES = frozenset({
    "PATH", "LANG", "TMPDIR", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # PHP runtime settings, so a runner-provided php still starts.
    "PHPRC", "PHP_INI_SCAN_DIR", "LD_LIBRARY_PATH",
})
# PHP version managers (asdf, mise, phpenv, phpbrew) find the installed php
# through their own variables or through paths under the real HOME. They are
# runner settings, not workspace content, so they pass.
_VERSION_MANAGER_PREFIXES = ("ASDF_", "MISE_", "PHPENV_", "PHPBREW_")
# Data directory each manager falls back to under the real HOME. Pinned when
# HOME is replaced, so a shim still finds its installed versions.
_VERSION_MANAGER_HOME_DEFAULTS = (
    ("ASDF_DATA_DIR", ".asdf"),
    ("PHPENV_ROOT", ".phpenv"),
    ("MISE_DATA_DIR", ".local/share/mise"),
)


def _phpstan_env(home: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k in _ALLOWED_ENV_NAMES or k.startswith("LC_") or k.startswith(_VERSION_MANAGER_PREFIXES)
    }
    real_home = os.environ.get("HOME")
    if real_home:
        for name, rel in _VERSION_MANAGER_HOME_DEFAULTS:
            candidate = os.path.join(real_home, rel)
            if name not in env and os.path.isdir(candidate):
                env[name] = candidate
    env["HOME"] = home
    return env


# With no project classes loaded, phpstan reports every symbol outside the
# changed files as not found. Those are artifacts of the isolation, so they are
# dropped. A message check covers phpstan versions with no identifier field.
_UNRESOLVED_MESSAGE_RE = re.compile(r"not found\.|unknown class|unknown (?:parent )?class", re.IGNORECASE)


def _is_unresolved_symbol(msg: dict[str, object]) -> bool:
    identifier = msg.get("identifier")
    if isinstance(identifier, str) and identifier.endswith(".notFound"):
        return True
    message = msg.get("message")
    return isinstance(message, str) and bool(_UNRESOLVED_MESSAGE_RE.search(message))


def _severity_for_level(level: str) -> Literal["High", "Medium", "Low"]:
    """Map PHPSTAN_LEVEL (0-9) to a finding severity.

    PHPStan level controls strictness — higher levels catch subtler type errors.
    Level 8-9 (max): enforce return types, mixed-type propagation → High.
    Level 5-7 (strict): template types, union narrowing → Medium.
    Level 0-4 (basic): obvious errors and undefined symbols → Low.
    """
    n = int(level)
    if n >= 8:
        return "High"
    if n >= 5:
        return "Medium"
    return "Low"


def _run_phpstan(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run phpstan on changed PHP files and return Finding instances."""
    target_files = [
        f for f in changed_files.php
        if Path(f).suffix in _PHP_EXTENSIONS and Path(f).is_file()
    ]
    if not target_files:
        return []

    if not shutil.which("phpstan"):
        logger.warning("[ai-pr-review] WARNING: phpstan not found; skipping.")
        return []

    level = os.environ.get("PHPSTAN_LEVEL", _DEFAULT_LEVEL).strip()
    if not _VALID_LEVEL_RE.match(level):
        logger.warning(
            "[ai-pr-review] WARNING: PHPSTAN_LEVEL='%s' is not a valid level (0-9); using 3.", level
        )
        level = _DEFAULT_LEVEL

    # Never let phpstan read anything from the analyzed workspace: the workspace
    # may be untrusted fork-PR content checked out under pull_request_target
    # (#739). `--configuration` stops phpstan.neon auto-discovery, but phpstan
    # also runs `vendor/autoload.php` from its current directory (and reads the
    # `extra.phpstan` block of `composer.json` there), so a stranger's PHP ran in
    # this job. The run therefore starts in an empty scratch directory with HOME
    # set to it, the trusted config sits inside it, and the files go in by
    # absolute path. The cost is that the project's own classes are not
    # resolved, so "not found" messages are dropped from the output. Verified against a hostile
    # `vendor/autoload.php`: from the workspace it ran, from an empty directory
    # it did not.
    try:
        scratch_cm = tempfile.TemporaryDirectory(prefix="phpstan-cwd-", ignore_cleanup_errors=True)
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: could not create trusted phpstan config: %s; skipping.", exc)
        return []

    with scratch_cm as scratch:
        trusted_config_path = os.path.join(scratch, "phpstan-trusted.neon")
        try:
            Path(trusted_config_path).write_text("# Deliberately empty; see #739.\n")
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: could not create trusted phpstan config: %s; skipping.", exc)
            return []

        args = [
            "phpstan", "analyse", "--error-format=json", "--no-progress", "--memory-limit=512M",
            f"--configuration={trusted_config_path}", f"--level={level}",
        ]
        if _BAKED_PHPSTAN_DRUPAL.is_dir() and _BAKED_COMPOSER_AUTOLOAD.is_file():
            args.append(f"--autoload-file={_BAKED_COMPOSER_AUTOLOAD}")

        # `--` stops option parsing so an attacker-controlled changed-file path
        # beginning with a dash (e.g. `--autoload-file=evil.php`) is treated as a
        # path, not a flag. Absolute paths also start with `/`, never with `-`.
        args.append("--")
        args += [os.path.abspath(f) for f in target_files]

        try:
            # List-form args, no shell=True: no shell to inject into. `level`
            # (the one env-derived value reaching args) is regex-validated to
            # a single digit above, so it can't smuggle an extra flag or path
            # either. See #789.
            result = subprocess.run(
                args,  # nosemgrep: dangerous-subprocess-use-tainted-env-args
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECS,
                cwd=scratch,
                env=_phpstan_env(scratch),
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning("[ai-pr-review] WARNING: phpstan timed out after %ss; skipping.", exc.timeout)
            return []
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: phpstan failed to start: %s", exc)
            return []

    if result.returncode >= 2:
        logger.warning(
            "[ai-pr-review] WARNING: phpstan exited %d; possible configuration error. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: phpstan produced no output. stderr: %s",
            result.stderr[:200],
        )
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: phpstan produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: phpstan produced unexpected output structure; skipping.")
        return []

    files_block = data.get("files") or {}
    if not isinstance(files_block, dict):
        logger.warning("[ai-pr-review] WARNING: phpstan 'files' is not a dict; skipping.")
        return []

    severity = _severity_for_level(level)
    findings: list[Finding] = []

    for file_path, file_data in files_block.items():
        if not isinstance(file_data, dict):
            continue
        messages = file_data.get("messages") or []
        if not isinstance(messages, list):
            continue
        # Strip workspace/CWD prefix so paths are repo-relative
        rel_path = strip_workspace_prefix(file_path)

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            message = msg.get("message")
            if not message or _is_unresolved_symbol(msg):
                continue
            line = msg.get("line") or 1

            try:
                findings.append(
                    Finding(
                        severity=severity,
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=rel_path,
                        line=line,
                        finding=message,
                        remediation=_REMEDIATION,
                        category="lint",
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: phpstan dropped malformed finding: %s; msg=%r",
                    exc, repr(msg)[:200],
                )

    return findings
