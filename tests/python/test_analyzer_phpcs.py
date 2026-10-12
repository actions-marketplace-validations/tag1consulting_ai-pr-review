"""Tests for the native phpcs analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.phpcs import _run_phpcs
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "phpcs"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(php_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=php_files, php=php_files)


class TestRunPhpcs:
    def test_no_php_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_phpcs(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value=None):
            result = _run_phpcs(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_phpcs(cf, Path("/dev/null"))
        assert "phpcs not found" in caplog.text

    def test_only_php_extension_files_passed(self, tmp_path: Path) -> None:
        php = tmp_path / "code.php"
        php.write_text("<?php\n")
        txt = tmp_path / "readme.txt"
        txt.write_text("hello\n")
        cf = _make_cf([str(php), str(txt)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr="")
            _run_phpcs(cf, Path("/dev/null"))
        # Only phpcs -i call (for standard detection) and the main call
        # The main phpcs call should include only php, not txt
        main_call = mock_run.call_args_list[-1]
        assert str(php) in main_call[0][0]
        assert str(txt) not in main_call[0][0]


class TestRunPhpcsFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path, returncode: int = 1) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            # First call = phpcs -i (standard detection), second = main scan
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Drupal,DrupalPractice", stderr=""),
                MagicMock(returncode=returncode, stdout=fixture, stderr=""),
            ]
            return _run_phpcs(cf, Path("/dev/null"))

    def test_warning_finding_maps_to_medium(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-warning.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 90
        assert f.source == "phpcs"
        assert "Generic.Files.LineLength.TooLong" in f.finding
        assert f.line == 15

    def test_error_finding_maps_to_high(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-error.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert "Generic.PHP.UpperCaseConstant" in findings[0].finding

    def test_category_is_lint(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-warning.json", tmp_path)
        assert findings[0].category == "lint"

    def test_empty_files_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("phpcs-malformed.json")
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                MagicMock(returncode=1, stdout=fixture, stderr=""),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []
        assert "non-JSON" in caplog.text

    def test_path_prefix_stripped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os as _os
        # strip_workspace_prefix() prefers GITHUB_WORKSPACE when set (#846);
        # real CI runners always set it, so this cwd-fallback case must
        # explicitly unset it, not just rely on it being absent locally.
        monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
        pwd = _os.getcwd()
        payload = json.dumps({
            "totals": {"errors": 1, "warnings": 0, "fixable": 0},
            "files": {
                f"{pwd}/src/module.php": {
                    "errors": 1, "warnings": 0,
                    "messages": [{
                        "message": "uppercase expected", "source": "Generic.PHP.UpperCaseConstant",
                        "severity": 5, "type": "ERROR", "line": 5, "column": 1, "fixable": False,
                    }],
                },
            },
        })
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                MagicMock(returncode=1, stdout=payload, stderr=""),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings[0].file == "src/module.php"

    def test_github_workspace_prefix_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#846: phpcs's own cwd-only stripping leaked an absolute path
        whenever cwd != GITHUB_WORKSPACE. Now consolidated onto
        _paths.strip_workspace_prefix, which prefers GITHUB_WORKSPACE."""
        monkeypatch.setenv("GITHUB_WORKSPACE", "/some/container/workspace")
        payload = json.dumps({
            "totals": {"errors": 1, "warnings": 0, "fixable": 0},
            "files": {
                "/some/container/workspace/src/module.php": {
                    "errors": 1, "warnings": 0,
                    "messages": [{
                        "message": "uppercase expected", "source": "Generic.PHP.UpperCaseConstant",
                        "severity": 5, "type": "ERROR", "line": 5, "column": 1, "fixable": False,
                    }],
                },
            },
        })
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                MagicMock(returncode=1, stdout=payload, stderr=""),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings[0].file == "src/module.php"

    def test_exitcode_2_with_real_output_parses_findings(self, tmp_path: Path) -> None:
        # phpcs 4.x exit code 2 means "non-fixable violations found" (a normal
        # successful run), not a fatal error. PHPCSStandards/PHP_CodeSniffer#184
        # redesigned exit codes in 4.0 as a bitmask: 1=fixable, 2=non-fixable,
        # 3=both. Verified empirically against phpcs 4.0.4 this session.
        findings = self._run_with_fixture("phpcs-warning.json", tmp_path, returncode=2)
        assert len(findings) == 1
        assert findings[0].severity == "Medium"

    def test_exitcode_3_mixed_fixable_and_nonfixable_parses_findings(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("phpcs-error.json", tmp_path, returncode=3)
        assert len(findings) == 1
        assert findings[0].severity == "High"

    def test_process_error_exitcode_returns_empty_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Genuine fatal/config errors use ExitCode::PROCESS_ERROR (16) or
        # ::REQUIREMENTS_NOT_MET (64) in phpcs 4.x — these never overlap the
        # 0-3 violation-bitmask range, so they're the real "something broke"
        # signal, not exit code 2.
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                MagicMock(returncode=16, stdout="", stderr="fatal error"),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 16" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                sp.TimeoutExpired(cmd="phpcs", timeout=120),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12", stderr=""),
                OSError("not found"),
            ]
            findings = _run_phpcs(cf, Path("/dev/null"))
        assert findings == []

    def test_drupal_standard_used_when_available(self, tmp_path: Path) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        (tmp_path / "module.info.yml").write_text("name: x\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="The following coding standards are installed: Drupal, DrupalPractice, PSR12", stderr=""),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        main_call_args = mock_run.call_args_list[-1][0][0]
        assert "--standard=Drupal,DrupalPractice" in main_call_args

    def test_standard_detection_timeout_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                sp.TimeoutExpired(cmd="phpcs", timeout=10),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        assert "phpcs -i timed out" in caplog.text

    def test_standard_detection_oserror_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.side_effect = [
                OSError("bad interpreter"),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        assert "phpcs -i failed" in caplog.text

    def test_psr12_standard_when_drupal_unavailable(self, tmp_path: Path) -> None:
        # The PSR12 fallback is upgraded to a temp-file ruleset (PSR12 plus
        # the Squiz docblock sniff) rather than the bare "PSR12" string —
        # phpcs's --standard flag cannot mix a named standard with a bare
        # sniff code, verified empirically this session — so the argument
        # passed to phpcs is a filesystem path, not the literal "PSR12".
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="PSR12, PEAR", stderr=""),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        main_call_args = mock_run.call_args_list[-1][0][0]
        standard_flag = next(a for a in main_call_args if a.startswith("--standard="))
        ruleset_path = standard_flag.removeprefix("--standard=")
        assert ruleset_path.endswith(".xml")
        # The remediation message shown to reviewers still says "PSR12" (the
        # human-readable label), not the temp-file path.
        assert "--standard=PSR12" not in main_call_args

    def test_psr12_ruleset_combines_psr12_and_docblock_sniff(self, tmp_path: Path) -> None:
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        cf = _make_cf([str(f)])
        written_ruleset = {}

        def _capture_run(args: list[str], **kwargs: object) -> MagicMock:
            standard_flag = next((a for a in args if a.startswith("--standard=")), None)
            if standard_flag and standard_flag.removeprefix("--standard=").endswith(".xml"):
                written_ruleset["content"] = Path(standard_flag.removeprefix("--standard=")).read_text()
            if args[0:2] == ["phpcs", "-i"]:
                return MagicMock(returncode=0, stdout="PSR12", stderr="")
            return MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr="")

        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run", side_effect=_capture_run),
        ):
            _run_phpcs(cf, Path("/dev/null"))
        assert '<rule ref="PSR12"/>' in written_ruleset["content"]
        assert '<rule ref="Squiz.Commenting.FunctionComment"/>' in written_ruleset["content"]

    def test_drupal_standard_does_not_use_temp_ruleset(self, tmp_path: Path) -> None:
        # The docblock-sniff upgrade is scoped to the PSR12 fallback only —
        # Drupal repos already get equivalent coverage via
        # Drupal.Commenting.FunctionComment, so the Drupal branch is untouched.
        f = tmp_path / "module.php"
        f.write_text("<?php\n")
        (tmp_path / "module.info.yml").write_text("name: x\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="Drupal, DrupalPractice", stderr=""),
                MagicMock(returncode=0, stdout='{"totals":{},"files":{}}', stderr=""),
            ]
            _run_phpcs(cf, Path("/dev/null"))
        main_call_args = mock_run.call_args_list[-1][0][0]
        assert "--standard=Drupal,DrupalPractice" in main_call_args


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_phpcs_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["module.php"], php=["module.php"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "phpcs" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_phpcs_skipped_when_no_php_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("phpcs", ["php"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called


# ---------------------------------------------------------------------------
# Repo ruleset handling (#1042): read as data only, phpcs never reads the repo file
# ---------------------------------------------------------------------------

_INSTALLED_FULL = (
    "The installed coding standards are PEAR, PSR1, PSR2, PSR12, Squiz, Zend, "
    "Drupal, DrupalPractice and VariableAnalysis"
)

# A trimmed copy of the tag1consulting/scolta-wp ruleset that triggered #1042.
_SCOLTA_RULESET = """<?xml version="1.0"?>
<ruleset name="Scolta WordPress">
  <arg name="extensions" value="php"/>
  <file>includes</file>
  <file>cli</file>
  <file>scolta.php</file>
  <rule ref="WordPress-Extra"/>
  <rule ref="WordPress-Docs">
    <exclude-pattern>scolta\\.php</exclude-pattern>
  </rule>
  <config name="minimum_supported_wp_version" value="6.1"/>
  <rule ref="Generic.Files.LineLength">
    <exclude-pattern>tests/*</exclude-pattern>
  </rule>
  <rule ref="Generic.Files.LineLength.MaxExceeded">
    <exclude-pattern>admin/*</exclude-pattern>
  </rule>
  <rule ref="WordPress.PHP.YodaConditions">
    <exclude-pattern>*</exclude-pattern>
  </rule>
</ruleset>
"""


class _Run:
    def __init__(self) -> None:
        self.xml: str | None = None
        self.main_args: list[str] = []
        self.calls: list[list[str]] = []


def _run_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    changed: list[str],
    *,
    installed: str = _INSTALLED_FULL,
    ruleset: tuple[str, str | bytes] | None = None,
    main_returncodes: list[int] | None = None,
) -> tuple[list[object], _Run]:
    """Run the analyzer in *tmp_path* with a mocked phpcs. Returns (findings, capture)."""
    monkeypatch.chdir(tmp_path)
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if ruleset is not None:
        name, body = ruleset
        path = tmp_path / name
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body)
    run = _Run()
    codes = list(main_returncodes or [0])

    def _fake(args: list[str], **kwargs: object) -> MagicMock:
        run.calls.append(args)
        if args[0:2] == ["phpcs", "-i"]:
            return MagicMock(returncode=0, stdout=installed, stderr="")
        run.main_args = args
        flag = next(a for a in args if a.startswith("--standard="))
        value = flag.removeprefix("--standard=")
        if value.endswith(".xml"):
            run.xml = Path(value).read_text()
        rc = codes.pop(0) if len(codes) > 1 else codes[0]
        return MagicMock(returncode=rc, stdout='{"totals":{},"files":{}}' if rc in (0, 1, 2, 3) else "", stderr="boom")

    with (
        patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
        patch("ai_pr_review.analyzers.native.phpcs.subprocess.run", side_effect=_fake),
    ):
        findings = _run_phpcs(_make_cf(changed), Path("/dev/null"))
    return list(findings), run


_PHP = "<?php\n"


class TestRepoRuleset:
    def test_no_ruleset_no_drupal_signal_uses_psr12_not_drupal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = _run_in(tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"])
        assert run.xml is not None
        assert 'ref="PSR12"' in run.xml
        assert "Drupal" not in " ".join(run.main_args)

    @pytest.mark.parametrize(
        "files",
        [
            {"a.php": _PHP, "composer.json": '{"type": "drupal-module"}'},
            {"a.php": _PHP, "composer.json": '{"require": {"drupal/core-recommended": "^11"}}'},
            {"mod/src/a.php": _PHP, "mod/mod.info.yml": "name: x\n"},
            {"mod.module": _PHP},
        ],
    )
    def test_drupal_signals_select_drupal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]
    ) -> None:
        changed = [next(k for k in files if k.endswith((".php", ".module")))]
        _, run = _run_in(tmp_path, monkeypatch, files, changed)
        assert "--standard=Drupal,DrupalPractice" in run.main_args

    def test_drupal7_info_file_selects_drupal_for_inc_only_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        files = {
            "sites/all/modules/mod/mod.info": "name = Mod\ndescription = x\ncore = 7.x\n",
            "sites/all/modules/mod/includes/helper.inc": _PHP,
        }
        _, run = _run_in(tmp_path, monkeypatch, files, ["sites/all/modules/mod/includes/helper.inc"])
        assert "--standard=Drupal,DrupalPractice" in run.main_args

    @pytest.mark.parametrize(
        "info_body",
        ["just some notes\n", "name = Thing\n", "core = 7.x\n"],
        ids=["unrelated", "name-only", "core-only"],
    )
    def test_info_file_without_drupal7_keys_is_not_a_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, info_body: str
    ) -> None:
        files = {"lib/thing.info": info_body, "lib/a.php": _PHP}
        _, run = _run_in(tmp_path, monkeypatch, files, ["lib/a.php"])
        assert run.xml is not None
        assert 'ref="PSR12"' in run.xml

    def test_symlinked_drupal7_info_is_not_a_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        real = tmp_path / "real.txt"
        real.write_text("name = Mod\ncore = 7.x\n")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "mod.info").symlink_to(real)
        _, run = _run_in(tmp_path, monkeypatch, {"lib/a.php": _PHP}, ["lib/a.php"])
        assert run.xml is not None
        assert 'ref="PSR12"' in run.xml

    def test_missing_wordpress_standard_uses_neutral_fallback_and_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        files = {"includes/y.php": _PHP, "tests/x.php": _PHP, "scolta.php": _PHP}
        with caplog.at_level("WARNING"):
            _, run = _run_in(
                tmp_path,
                monkeypatch,
                files,
                ["includes/y.php", "tests/x.php", "scolta.php"],
                ruleset=("phpcs.xml.dist", _SCOLTA_RULESET),
            )
        assert "WordPress-Extra" in caplog.text
        # `<file>` scope is applied here, because phpcs ignores it for CLI files.
        sent = run.main_args[run.main_args.index("--") + 1 :]
        assert sent == ["includes/y.php", "scolta.php"]
        assert run.xml is not None
        assert '<rule ref="Generic.PHP.Syntax"' in run.xml
        assert "WordPress" not in run.xml
        assert "PSR12" not in run.xml
        # Installed sniffs the repo named, with their excludes, are kept.
        assert '<rule ref="Generic.Files.LineLength">' in run.xml
        assert "<exclude-pattern type=\"absolute\">tests/*</exclude-pattern>" in run.xml
        assert "<config" not in run.xml
        assert "<arg" not in run.xml

    def test_all_changed_files_out_of_scope_skips_phpcs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        findings, run = _run_in(
            tmp_path,
            monkeypatch,
            {"tests/x.php": _PHP},
            ["tests/x.php"],
            ruleset=("phpcs.xml.dist", _SCOLTA_RULESET),
        )
        assert findings == []
        assert run.main_args == []

    def test_installed_standard_ruleset_keeps_standard_patterns_and_properties(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ruleset = """<?xml version="1.0"?>
<ruleset name="X">
  <exclude-pattern>vendor/*</exclude-pattern>
  <exclude-pattern type="relative">tests/*</exclude-pattern>
  <rule ref="PSR12">
    <exclude name="PSR12.Files.FileHeader"/>
    <severity>3</severity>
  </rule>
  <rule ref="Generic.Files.LineLength">
    <properties><property name="lineLimit" value="140"/></properties>
  </rule>
</ruleset>
"""
        _, run = _run_in(
            tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"], ruleset=("phpcs.xml", ruleset)
        )
        assert run.xml is not None
        assert '<rule ref="PSR12">' in run.xml
        assert '<exclude name="PSR12.Files.FileHeader"' in run.xml
        assert "<severity>3</severity>" in run.xml
        assert '<property name="lineLimit" value="140"' in run.xml
        assert '<exclude-pattern type="relative">tests/*</exclude-pattern>' in run.xml
        assert "Generic.PHP.Syntax" not in run.xml

    def test_ruleset_discovery_order_matches_phpcs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        low = '<ruleset name="a"><file>nope</file><rule ref="PSR12"/></ruleset>'
        high = '<ruleset name="b"><rule ref="PSR12"/></ruleset>'
        monkeypatch.chdir(tmp_path)
        (tmp_path / "phpcs.xml.dist").write_text(low)
        _, run = _run_in(
            tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"], ruleset=(".phpcs.xml", high)
        )
        # `.phpcs.xml` wins, and it has no `<file>` scope, so a.php is linted.
        assert run.main_args

    def test_ruleset_without_standard_keeps_scope_and_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ruleset = '<ruleset name="s"><file>src</file><exclude-pattern>src/gen/*</exclude-pattern></ruleset>'
        _, run = _run_in(
            tmp_path,
            monkeypatch,
            {"src/a.php": _PHP, "other/b.php": _PHP},
            ["src/a.php", "other/b.php"],
            ruleset=("phpcs.xml", ruleset),
        )
        assert run.main_args[run.main_args.index("--") + 1 :] == ["src/a.php"]
        assert run.xml is not None
        assert 'ref="PSR12"' in run.xml
        assert "src/gen/*" in run.xml

    def test_remediation_names_repo_ruleset_not_drupal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = json.dumps(
            {"files": {str(tmp_path / "a.php"): {"messages": [
                {"type": "ERROR", "line": 1, "source": "Generic.PHP.Syntax", "message": "bad"}
            ]}}}
        )
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.php").write_text(_PHP)
        (tmp_path / "phpcs.xml").write_text('<ruleset name="s"><rule ref="PSR12"/></ruleset>')

        def _fake(args: list[str], **kwargs: object) -> MagicMock:
            if args[0:2] == ["phpcs", "-i"]:
                return MagicMock(returncode=0, stdout=_INSTALLED_FULL, stderr="")
            return MagicMock(returncode=2, stdout=out, stderr="")

        with (
            patch("ai_pr_review.analyzers.native.phpcs.shutil.which", return_value="/usr/bin/phpcs"),
            patch("ai_pr_review.analyzers.native.phpcs.subprocess.run", side_effect=_fake),
        ):
            findings = _run_phpcs(_make_cf(["a.php"]), Path("/dev/null"))
        assert len(findings) == 1
        assert "drupal.org" not in findings[0].remediation
        assert "phpcs --standard=phpcs.xml a.php" in findings[0].remediation

    def test_process_error_with_repo_ruleset_retries_with_neutral_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ruleset = '<ruleset name="s"><rule ref="PSR12.Nope.Missing"/></ruleset>'
        with caplog.at_level("WARNING"):
            _, run = _run_in(
                tmp_path,
                monkeypatch,
                {"a.php": _PHP},
                ["a.php"],
                ruleset=("phpcs.xml", ruleset),
                main_returncodes=[16, 0],
            )
        assert "retrying with the style-neutral fallback" in caplog.text
        assert run.xml is not None
        assert "Generic.PHP.Syntax" in run.xml


    @pytest.mark.parametrize("entry", [".", "./", "./src/", "src", "src/"])
    def test_file_scope_entry_forms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
    ) -> None:
        ruleset = f'<ruleset name="s"><file>{entry}</file><rule ref="PSR12"/></ruleset>'
        _, run = _run_in(
            tmp_path, monkeypatch, {"src/a.php": _PHP}, ["src/a.php"], ruleset=("phpcs.xml", ruleset)
        )
        # Every form covers src/a.php. `.` and `./` mean the whole tree.
        assert run.main_args, entry

    def test_dot_scope_does_not_limit_other_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ruleset = '<ruleset name="s"><file>.</file><rule ref="PSR12"/></ruleset>'
        _, run = _run_in(
            tmp_path,
            monkeypatch,
            {"a/x.php": _PHP, "b/y.php": _PHP},
            ["a/x.php", "b/y.php"],
            ruleset=("phpcs.xml", ruleset),
        )
        assert run.main_args[run.main_args.index("--") + 1 :] == ["a/x.php", "b/y.php"]

    def test_retry_also_covers_the_neutral_fallback_with_repo_rules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A WordPress-style ruleset plus a Generic sniff that phpcs 4 removed.
        ruleset = (
            '<ruleset name="s"><rule ref="WordPress-Extra"/>'
            '<rule ref="Generic.Gone.Removed"><exclude-pattern>x</exclude-pattern></rule></ruleset>'
        )
        with caplog.at_level("WARNING"):
            _, run = _run_in(
                tmp_path,
                monkeypatch,
                {"a.php": _PHP},
                ["a.php"],
                ruleset=("phpcs.xml", ruleset),
                main_returncodes=[16, 0],
            )
        assert "retrying with the style-neutral fallback" in caplog.text
        assert run.xml is not None
        assert "Generic.Gone.Removed" not in run.xml
        assert "Generic.PHP.Syntax" in run.xml

    def test_dropped_exclude_pattern_is_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ruleset = '<ruleset name="s"><exclude-pattern>(a+)+$</exclude-pattern><rule ref="PSR12"/></ruleset>'
        with caplog.at_level("WARNING"):
            _run_in(tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"], ruleset=("phpcs.xml", ruleset))
        assert "dropping exclude-pattern" in caplog.text


class TestRepoRulesetIsDataOnly:
    """The repo ruleset must never reach phpcs. Mirrors the phpstan trust rule (#739)."""

    _HOSTILE = """<?xml version="1.0"?>
<ruleset name="evil">
  <config name="installed_paths" value="/workspace/evil"/>
  <config name="php_path" value="/workspace/evil/php"/>
  <arg name="bootstrap" value="/workspace/evil/boot.php"/>
  <autoload>/workspace/evil/autoload.php</autoload>
  <ini name="auto_prepend_file" value="/workspace/evil/p.php"/>
  <exclude-pattern>(a+)+$</exclude-pattern>
  <exclude-pattern></exclude-pattern>
  <rule ref="vendor/evil/ruleset.xml"/>
  <rule ref="../evil.xml"/>
  <rule ref="PSR12">
    <exclude-pattern>x</exclude-pattern><arg name="bootstrap" value="/workspace/evil/b2.php"/>
    <properties><property name="ok" value="1"/><property name="bad name" value="1"/></properties>
  </rule>
</ruleset>
"""

    def test_hostile_ruleset_never_reaches_phpcs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = _run_in(
            tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"], ruleset=("phpcs.xml.dist", self._HOSTILE)
        )
        standard = next(a for a in run.main_args if a.startswith("--standard="))
        assert "phpcs.xml.dist" not in standard
        assert str(tmp_path) not in standard
        assert run.xml is not None
        for needle in ("installed_paths", "php_path", "bootstrap", "autoload", "auto_prepend", "evil", "(a+)+", "<arg", "<ini"):
            assert needle not in run.xml, needle
        assert '<rule ref="PSR12">' in run.xml
        assert '<property name="ok" value="1"' in run.xml
        assert "bad name" not in run.xml

    @pytest.mark.parametrize(
        "body",
        [
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]><ruleset name="a"><rule ref="PSR12"/></ruleset>',
            '<?xml version="1.0" encoding="UTF-16"?><ruleset name="a"/>'.encode("utf-16"),
            b'<ruleset name="a">' + b"<!-- " + b"x" * 300_000 + b" --></ruleset>",
            b"<ruleset",
            b"<notaruleset/>",
        ],
        ids=["doctype", "utf16", "oversize", "malformed", "wrong-root"],
    )
    def test_unusable_ruleset_falls_back_to_neutral(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
    ) -> None:
        _, run = _run_in(
            tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"], ruleset=("phpcs.xml", body)
        )
        assert run.xml is not None
        assert "Generic.PHP.Syntax" in run.xml
        assert "PSR12" not in run.xml

    def test_symlinked_ruleset_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "elsewhere.xml"
        target.write_text('<ruleset name="a"><file>nope</file><rule ref="PSR12"/></ruleset>')
        (tmp_path / "phpcs.xml").symlink_to(target)
        _, run = _run_in(tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"])
        # The symlink is skipped, so no `<file>` scope applies and a.php is linted.
        assert run.main_args
        assert run.xml is not None
        assert "PSR12" in run.xml

    def test_pattern_text_with_markup_is_not_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ruleset = (
            '<ruleset name="a"><rule ref="PSR12"/>'
            "<exclude-pattern>&lt;/exclude-pattern&gt;&lt;arg name=&quot;bootstrap&quot;/&gt;</exclude-pattern>"
            "</ruleset>"
        )
        _, run = _run_in(
            tmp_path, monkeypatch, {"a.php": _PHP}, ["a.php"], ruleset=("phpcs.xml", ruleset)
        )
        assert run.xml is not None
        assert "bootstrap" not in run.xml
