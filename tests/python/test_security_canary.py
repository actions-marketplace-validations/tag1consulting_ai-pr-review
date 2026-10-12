"""Security canary tests: verify the action never executes checked-out workspace files.

Each test exercises a code path that reads files from $GITHUB_WORKSPACE as data and
asserts that none of the sentinel files in CANARY_DIR were written. A sentinel file
appearing means the code path *executed* a workspace file rather than just reading it.

These tests are marked @pytest.mark.security. A failure is a security regression, not
a flaky test — see SECURITY.md and CONTRIBUTING.md for the reporting and remediation
process.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

CANARY_WORKSPACE = Path(__file__).parent.parent / "security" / "canary-workspace"

SENTINEL_NAMES = [
    "makefile-executed",
    "setup-py-imported",
    "npm-preinstall-executed",
    "npm-postinstall-executed",
    "pre-commit-hook-executed",
    "conftest-imported",
    "semgrep-rule-executed",
    "phpcs-ruleset-executed",
]


def _assert_no_canary_fired(canary_dir: Path) -> None:
    """Assert no sentinel file was written under canary_dir."""
    if not canary_dir.exists():
        return
    fired = [f.name for f in canary_dir.iterdir() if f.name in SENTINEL_NAMES]
    assert not fired, (
        f"SECURITY REGRESSION: workspace execution sentinels triggered: {fired}. "
        "The action executed checked-out code from the workspace, violating the "
        "'never execute checked-out code' invariant. "
        "See SECURITY.md and CONTRIBUTING.md for the reporting process."
    )


@pytest.mark.security
class TestNeverExecuteCheckedOutCode:
    """Verify each workspace-reading code path treats files as data, never as code."""

    def test_language_detection_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """languages.py detects language by extension/basename — must not exec any file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.languages import detect_language, is_test_file

        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file():
                detect_language(f.suffix.lstrip("."))
                is_test_file(str(f))

        _assert_no_canary_fired(canary_dir)

    def test_manifest_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """manifest.py classifies files by path/extension — must not exec any file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.manifest import build_changed_files

        file_list = [
            str(f.relative_to(CANARY_WORKSPACE))
            for f in CANARY_WORKSPACE.iterdir()
            if f.is_file()
        ]
        result = build_changed_files(file_list)
        assert result is not None

        _assert_no_canary_fired(canary_dir)

    def test_analyzer_bridge_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """analyzer bridge runs regex/AST analysis on file content — must not exec files."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.analyzers.bridge import run_analyzers
        from ai_pr_review.manifest import build_changed_files

        file_list = [
            str(f.relative_to(CANARY_WORKSPACE))
            for f in CANARY_WORKSPACE.iterdir()
            if f.is_file()
        ]
        changed_files = build_changed_files(file_list)

        # Write a minimal diff file so the analyzer has something to read as data.
        diff_file = str(tmp_path / "test.diff")
        Path(diff_file).write_text("", encoding="utf-8")

        # Only suppress expected infrastructure errors (missing deps, missing files).
        # Unexpected exceptions — including anything raised during workspace execution —
        # must not be swallowed, so the test fails rather than passing vacuously.
        with contextlib.suppress(ImportError, FileNotFoundError, OSError):
            asyncio.run(
                run_analyzers(changed_files=changed_files, diff_file=diff_file)
            )

        _assert_no_canary_fired(canary_dir)

    def test_sarif_ingestor_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SARIF ingestor reads JSON as data — must not execute any workspace file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.analyzers.sarif import load_sarif_files

        # load_sarif_files is fail-soft — a missing path returns [] and logs a warning.
        findings, _elapsed = load_sarif_files(["nonexistent.sarif"])
        assert findings == []

        _assert_no_canary_fired(canary_dir)

    def test_context_enrichment_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tree-sitter parses file content as AST — must not exec any workspace file."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))

        try:
            from ai_pr_review.context.treesitter import extract_symbol_refs
        except ImportError:
            pytest.skip("tree-sitter-language-pack not installed")

        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file() and f.suffix == ".py":
                # Pass file content as a diff hunk string — purely data, never executed.
                hunk = f.read_text(errors="replace")
                extract_symbol_refs(hunk, language="python")

        _assert_no_canary_fired(canary_dir)

    def test_language_profile_loading_does_not_execute_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """language_profiles.py reads markdown from the image install — not workspace."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        from ai_pr_review.language_profiles import load_language_profiles

        # Point script_dir at a temp dir with no language-profiles/ subdir — the
        # function is fail-soft and returns "" when the directory doesn't exist.
        # This verifies it never falls back to reading from CANARY_WORKSPACE.
        result = load_language_profiles(["Python", "JavaScript"], script_dir=tmp_path)
        assert isinstance(result, str)

        # Also verify with a real script_dir that has profiles (if available).
        # Either way, no workspace files should be executed.
        import ai_pr_review

        real_script_dir = Path(ai_pr_review.__file__).parent
        load_language_profiles(["Python"], script_dir=real_script_dir)

        _assert_no_canary_fired(canary_dir)

    def test_reading_workspace_files_as_bytes_does_not_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading canary files as raw bytes (simulating diff content reads) fires no sentinel."""
        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))

        # Simulate what diff/compute.py does: open and read file bytes for diffing.
        for f in CANARY_WORKSPACE.iterdir():
            if f.is_file():
                _ = f.read_bytes()

        _assert_no_canary_fired(canary_dir)


@pytest.mark.security
class TestPhpcsRulesetIsData:
    """The phpcs analyzer reads a repo ruleset as data and never hands it to phpcs (#1042)."""

    def test_phpcs_never_receives_the_workspace_ruleset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess
        from unittest.mock import MagicMock, patch

        from ai_pr_review.analyzers.native.phpcs import _run_phpcs
        from ai_pr_review.manifest import ChangedFiles

        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        monkeypatch.chdir(CANARY_WORKSPACE)
        seen: list[list[str]] = []
        generated: list[str] = []

        def _fake(args: list[str], **kwargs: object) -> MagicMock:
            seen.append(args)
            if args[0:2] == ["phpcs", "-i"]:
                return MagicMock(returncode=0, stdout="PSR12, PSR2, PSR1", stderr="")
            flag = next(a for a in args if a.startswith("--standard="))
            generated.append(Path(flag.removeprefix("--standard=")).read_text())
            return MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr="")

        files = ["canary-bootstrap.php"]
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch.object(subprocess, "run", side_effect=_fake),
        ):
            _run_phpcs(ChangedFiles(all_files=files, php=files), tmp_path / "x.diff")

        main = seen[-1]
        standard = next(a for a in main if a.startswith("--standard="))
        assert "phpcs.xml.dist" not in standard
        assert str(CANARY_WORKSPACE) not in standard
        assert generated, "phpcs should receive a generated ruleset"
        for needle in ("installed_paths", "bootstrap", "autoload", "canary-bootstrap"):
            assert needle not in generated[-1], needle
        _assert_no_canary_fired(canary_dir)

    def test_real_phpcs_does_not_load_workspace_php(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        if not (shutil.which("phpcs") and shutil.which("php")):
            pytest.skip("phpcs and php are not installed")
        from ai_pr_review.analyzers.native.phpcs import _run_phpcs
        from ai_pr_review.manifest import ChangedFiles

        canary_dir = tmp_path / "canary"
        monkeypatch.setenv("CANARY_DIR", str(canary_dir))
        monkeypatch.chdir(CANARY_WORKSPACE)
        files = ["canary-bootstrap.php"]
        _run_phpcs(ChangedFiles(all_files=files, php=files), tmp_path / "x.diff")
        _assert_no_canary_fired(canary_dir)

