"""Tests for the native dep-exists analyzer. No test makes a network call."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ai_pr_review.analyzers.native import dep_exists as de
from ai_pr_review.manifest import ChangedFiles

Handler = Callable[[httpx.Request], httpx.Response]


def _cf(*paths: str) -> ChangedFiles:
    return ChangedFiles(all_files=list(paths), manifest_lockfile=list(paths))


def _diff(path: str, added: list[str], start: int = 1) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +{start},{len(added)} @@\n{body}"
    )


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Registry(list):  # type: ignore[type-arg]
    """Requests the analyzer sent (the list), plus the handler that answers them."""

    handler: Handler = staticmethod(lambda r: httpx.Response(404))  # type: ignore[assignment]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """Route every client the analyzer builds to a recorded mock. Default: 404."""
    seen = Registry()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return seen.handler(request)

    real = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(de.httpx, "Client", factory)
    return seen


def _set(registry: Registry, handler: Handler) -> None:
    registry.handler = staticmethod(handler)  # type: ignore[assignment]


def _run(work: Path, path: str, content: str, added: list[str]) -> list:
    (work / path).parent.mkdir(parents=True, exist_ok=True)
    (work / path).write_text(content)
    diff = work / "d.diff"
    diff.write_text(_diff(path, added))
    return de._run_dep_exists(_cf(path), diff)


class TestDiffParsing:
    def test_added_lines_carry_new_line_numbers(self) -> None:
        diff = (
            "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +10,3 @@\n ctx\n-old\n+new1\n+new2\n"
        )
        assert de.added_lines(diff) == {"f": [(11, "new1"), (12, "new2")]}

    def test_deleted_file_has_no_added_lines(self) -> None:
        assert de.added_lines("--- a/f\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n") == {}


class TestNameValidation:
    @pytest.mark.parametrize(
        ("eco", "name"),
        [
            (de._NPM, "left-pad"), (de._NPM, "@scope/pkg"), (de._PYPI, "requests"),
            (de._CRATES, "serde_json"), (de._PACKAGIST, "monolog/monolog"), (de._RUBYGEMS, "rails"),
        ],
    )
    def test_real_names_pass(self, eco: str, name: str) -> None:
        assert de.valid_name(eco, name)

    @pytest.mark.parametrize(
        ("eco", "name"),
        [
            (de._NPM, "../../etc/passwd"), (de._NPM, "a b"), (de._NPM, "x?y=1"), (de._NPM, "x#frag"),
            (de._NPM, "@scope"), (de._PYPI, "a/b"), (de._PYPI, "a%2fb"), (de._CRATES, "a/../b"),
            (de._PACKAGIST, "no-vendor"), (de._PACKAGIST, "a/b/c"), (de._RUBYGEMS, "a\nb"),
            (de._NPM, "a" * 300),
        ],
    )
    def test_injection_shaped_names_are_rejected(self, eco: str, name: str) -> None:
        assert not de.valid_name(eco, name)


class TestNameValidationSpeed:
    @pytest.mark.parametrize("eco", [de._NPM, de._PYPI, de._CRATES, de._PACKAGIST, de._RUBYGEMS])
    def test_a_hostile_name_is_rejected_fast(self, eco: str) -> None:
        import time

        hostile = ["a" * 200 + "!", "a/" + "a" * 200 + "!", "a-" * 100 + "!", "@" + "a" * 200 + "/!"]
        start = time.monotonic()
        for name in hostile:
            de.valid_name(eco, name)
        assert time.monotonic() - start < 0.5

    def test_packagist_accepts_the_same_real_names_as_before(self) -> None:
        for name in ("monolog/monolog", "symfony/polyfill-php80", "a.b/c_d", "vendor/pkg.name-x"):
            assert de.valid_name(de._PACKAGIST, name)
        for name in ("a--b/c", "-a/b", "a/b-", "a//b"):
            assert not de.valid_name(de._PACKAGIST, name)


class TestExtraction:
    def test_npm_only_new_names_are_checked(self, work: Path, registry: Registry) -> None:
        pkg = json.dumps({"dependencies": {"old-pkg": "1.0.0", "new-pkg": "^2.0.0"}}, indent=2)
        findings = _run(work, "package.json", pkg, ['    "new-pkg": "^2.0.0"'])
        assert [r.url.path for r in registry] == ["/new-pkg"]
        assert len(findings) == 1 and findings[0].line == 1

    def test_npm_non_registry_specs_are_skipped(self, work: Path, registry: Registry) -> None:
        pkg = json.dumps({"dependencies": {
            "a": "file:../a", "b": "github:u/b", "c": "u/c", "d": "git+https://x/d.git", "e": "npm:other@1",
        }})
        assert _run(work, "package.json", pkg, ['"a"', '"b"', '"c"', '"d"', '"e"']) == []
        assert registry == []

    def test_requirements_names_are_normalized_and_flags_skipped(
        self, work: Path, registry: Registry
    ) -> None:
        added = ["Foo_Bar==1.0", "# comment", "-r base.txt", "git+https://x/y.git", "baz[extra]>=2", ""]
        _run(work, "requirements.txt", "\n".join(added), added)
        assert sorted(r.url.path for r in registry) == ["/pypi/baz/json", "/pypi/foo-bar/json"]

    def test_private_pip_index_skips_the_file(self, work: Path, registry: Registry) -> None:
        content = "--extra-index-url https://pkgs.example/simple\nprivate-lib==1\n"
        assert _run(work, "requirements.txt", content, ["private-lib==1"]) == []
        assert registry == []

    def test_private_npmrc_skips_npm(self, work: Path, registry: Registry) -> None:
        (work / ".npmrc").write_text("@corp:registry=https://npm.corp.example/\n")
        pkg = json.dumps({"dependencies": {"@corp/lib": "1.0.0"}})
        assert _run(work, "package.json", pkg, ['"@corp/lib": "1.0.0"']) == []
        assert registry == []

    def test_cargo_path_and_git_deps_are_skipped(self, work: Path, registry: Registry) -> None:
        toml = '[dependencies]\nserde = "1"\nlocal = { path = "../local" }\ngitdep = { git = "https://x/y" }\n'
        _run(work, "Cargo.toml", toml, ['serde = "1"', 'local = { path = "../local" }', 'gitdep = { git = "https://x/y" }'])
        assert [r.url.path for r in registry] == ["/api/v1/crates/serde"]

    def test_composer_skips_platform_packages_and_private_repositories(
        self, work: Path, registry: Registry
    ) -> None:
        public = json.dumps({"require": {"php": ">=8.1", "ext-json": "*", "acme/lib": "^1"}})
        _run(work, "composer.json", public, ['"php": ">=8.1"', '"ext-json": "*"', '"acme/lib": "^1"'])
        assert [r.url.path for r in registry] == ["/p2/acme/lib.json"]
        registry.clear()
        private = json.dumps({"repositories": [{"type": "composer", "url": "https://x"}], "require": {"acme/lib": "^1"}})
        assert _run(work, "composer.json", private, ['"acme/lib": "^1"']) == []
        assert registry == []

    def test_gemfile_git_sources_and_private_source_are_skipped(
        self, work: Path, registry: Registry
    ) -> None:
        content = 'source "https://rubygems.org"\ngem "rails"\ngem "mine", git: "https://x/y"\n'
        _run(work, "Gemfile", content, ['gem "rails"', 'gem "mine", git: "https://x/y"'])
        assert [r.url.path for r in registry] == ["/api/v1/gems/rails.json"]
        registry.clear()
        private = 'source "https://gems.corp.example"\ngem "rails"\n'
        assert _run(work, "Gemfile", private, ['gem "rails"']) == []

    def test_go_mod_is_not_checked(self, work: Path, registry: Registry) -> None:
        assert _run(work, "go.mod", "module x\nrequire github.com/a/b v1.0.0\n", ["require github.com/a/b v1.0.0"]) == []
        assert registry == []

    def test_lockfiles_are_not_read(self, work: Path, registry: Registry) -> None:
        assert _run(work, "package-lock.json", "{}", ['"x": "1"']) == []
        assert registry == []


class TestFindings:
    def test_missing_package_is_a_high_finding_on_the_added_line(
        self, work: Path, registry: Registry
    ) -> None:
        pkg = json.dumps({"dependencies": {"invented-pkg": "1.0.0"}}, indent=2)
        findings = _run(work, "package.json", pkg, ['    "invented-pkg": "1.0.0"'])
        assert len(findings) == 1
        f = findings[0]
        assert (f.severity, f.source, f.file, f.line) == ("High", "dep-exists", "package.json", 1)
        assert "invented-pkg" in f.finding and "npm" in f.finding

    def test_existing_old_package_gives_no_finding(self, work: Path, registry: Registry) -> None:
        old = (datetime.now(UTC) - timedelta(days=900)).isoformat()
        _set(registry, lambda r: httpx.Response(200, json={"time": {"created": old}}))
        pkg = json.dumps({"dependencies": {"left-pad": "1"}})
        assert _run(work, "package.json", pkg, ['"left-pad": "1"']) == []

    def test_a_very_new_package_is_a_medium_finding(self, work: Path, registry: Registry) -> None:
        new = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        _set(registry, lambda r: httpx.Response(200, json={"time": {"created": new}}))
        pkg = json.dumps({"dependencies": {"fresh-pkg": "1"}})
        findings = _run(work, "package.json", pkg, ['"fresh-pkg": "1"'])
        assert [f.severity for f in findings] == ["Medium"]
        assert "3 days" in findings[0].finding

    def test_pypi_age_uses_the_earliest_upload(self, work: Path, registry: Registry) -> None:
        old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
        new = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        body = {"releases": {"1.0": [{"upload_time_iso_8601": old}], "2.0": [{"upload_time_iso_8601": new}]}}
        _set(registry, lambda r: httpx.Response(200, json=body))
        assert _run(work, "requirements.txt", "requests\n", ["requests"]) == []

    def test_crates_age_uses_created_at(self, work: Path, registry: Registry) -> None:
        new = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _set(registry, lambda r: httpx.Response(200, json={"crate": {"created_at": new}}))
        findings = _run(work, "Cargo.toml", '[dependencies]\nfresh = "1"\n', ['fresh = "1"'])
        assert [f.severity for f in findings] == ["Medium"]

    def test_the_same_name_in_two_ecosystems_is_two_lookups(self) -> None:
        assert de.valid_name(de._NPM, "requests") and de.valid_name(de._PYPI, "requests")


class TestVersionBumpsAreNotNew:
    """The base manifest is rebuilt from the diff. A name it already declared is not new."""

    @staticmethod
    def _real_diff(path: str, old: str, new: str) -> str:
        import difflib

        body = difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=1
        )
        return f"diff --git a/{path} b/{path}\n" + "\n".join(body) + "\n"

    def _run_change(self, work: Path, path: str, old: str, new: str) -> list:
        (work / path).write_text(new)
        diff = work / "d.diff"
        diff.write_text(self._real_diff(path, old, new))
        return de._run_dep_exists(_cf(path), diff)

    @staticmethod
    def _pkg(**deps: str) -> str:
        return json.dumps({"name": "app", "dependencies": deps}, indent=2) + "\n"

    def test_npm_version_bump_is_not_looked_up(self, work: Path, registry: Registry) -> None:
        found = self._run_change(work, "package.json", self._pkg(**{"@acme/private": "1.0.0"}), self._pkg(**{"@acme/private": "2.0.0"}))
        assert found == [] and len(registry) == 0

    def test_requirements_version_bump_is_not_looked_up(self, work: Path, registry: Registry) -> None:
        found = self._run_change(work, "requirements.txt", "acme-private==1.0\n", "acme-private==2.0\n")
        assert found == [] and len(registry) == 0

    def test_requirements_bump_matches_across_name_spelling(self, work: Path, registry: Registry) -> None:
        found = self._run_change(work, "requirements.txt", "acme.private==1.0\n", "Acme_Private==2.0\n")
        assert found == [] and len(registry) == 0

    def test_cargo_version_bump_is_not_looked_up(self, work: Path, registry: Registry) -> None:
        old = '[package]\nname = "app"\n\n[dependencies]\nacme-private = "1.0"\n'
        found = self._run_change(work, "Cargo.toml", old, old.replace('"1.0"', '"2.0"'))
        assert found == [] and len(registry) == 0

    def test_gemfile_version_bump_is_not_looked_up(self, work: Path, registry: Registry) -> None:
        found = self._run_change(work, "Gemfile", "gem 'acme-private', '1.0'\n", "gem 'acme-private', '2.0'\n")
        assert found == [] and len(registry) == 0

    def test_a_move_between_sections_is_not_new(self, work: Path, registry: Registry) -> None:
        old = json.dumps({"devDependencies": {"acme-private": "1"}}, indent=2) + "\n"
        new = json.dumps({"dependencies": {"acme-private": "1"}}, indent=2) + "\n"
        assert self._run_change(work, "package.json", old, new) == [] and len(registry) == 0

    def test_a_new_name_next_to_a_bump_is_still_checked(self, work: Path, registry: Registry) -> None:
        found = self._run_change(work, "package.json", self._pkg(**{"old-pkg": "1"}), self._pkg(**{"old-pkg": "2", "invented-pkg": "1"}))
        assert [f.finding.split("`")[1] for f in found] == ["invented-pkg"]
        assert {r.url.path for r in registry} == {"/invented-pkg"}

    def test_a_removed_script_with_the_same_key_does_not_hide_a_new_dependency(
        self, work: Path, registry: Registry
    ) -> None:
        old = json.dumps({"name": "app", "scripts": {"invented-pkg": "node build.js"}}, indent=2) + "\n"
        new = json.dumps({"name": "app", "dependencies": {"invented-pkg": "1"}}, indent=2) + "\n"
        found = self._run_change(work, "package.json", old, new)
        assert [f.severity for f in found] == ["High"]

    def test_a_removed_unrelated_key_does_not_hide_a_new_dependency_in_requirements(
        self, work: Path, registry: Registry
    ) -> None:
        found = self._run_change(work, "requirements.txt", "# invented-pkg is gone\nreal-pkg==1\n", "real-pkg==1\ninvented-pkg==1\n")
        assert [f.severity for f in found] == ["High"]

    @pytest.mark.parametrize("spec", ["workspace:*", "file:../lib", "link:../lib", "git+https://example.com/x.git"])
    def test_a_change_from_a_local_source_to_the_registry_is_checked(
        self, work: Path, registry: Registry, spec: str
    ) -> None:
        """Dependency confusion: the name was local, now it resolves on the public registry."""
        found = self._run_change(work, "package.json", self._pkg(**{"internal-lib": spec}), self._pkg(**{"internal-lib": "^1.0.0"}))
        assert [f.severity for f in found] == ["High"]

    def test_cargo_path_to_registry_is_checked(self, work: Path, registry: Registry) -> None:
        old = '[package]\nname = "app"\n\n[dependencies]\ninternal = { path = "../internal" }\n'
        new = '[package]\nname = "app"\n\n[dependencies]\ninternal = "1.0"\n'
        assert [f.severity for f in self._run_change(work, "Cargo.toml", old, new)] == ["High"]

    def test_gemfile_git_to_registry_is_checked(self, work: Path, registry: Registry) -> None:
        found = self._run_change(work, "Gemfile", "gem 'internal', git: 'https://example.com/i.git'\n", "gem 'internal', '1.0'\n")
        assert [f.severity for f in found] == ["High"]

    def test_requirements_url_to_registry_is_checked(self, work: Path, registry: Registry) -> None:
        found = self._run_change(work, "requirements.txt", "internal @ https://example.com/i.whl\n", "internal==1.0\n")
        assert [f.severity for f in found] == ["High"]

    def test_a_private_index_in_the_old_requirements_file_means_its_names_were_not_public(
        self, work: Path, registry: Registry
    ) -> None:
        old = "--extra-index-url https://pypi.internal.example/simple\ninternal-lib==1.0\n"
        new = "internal-lib==2.0\n"  # the private index is dropped in the same change
        assert [f.severity for f in self._run_change(work, "requirements.txt", old, new)] == ["High"]

    def test_a_private_source_in_the_old_gemfile_means_its_names_were_not_public(
        self, work: Path, registry: Registry
    ) -> None:
        old = "source 'https://gems.internal.example'\ngem 'internal', '1.0'\n"
        new = "source 'https://rubygems.org'\ngem 'internal', '2.0'\n"
        assert [f.severity for f in self._run_change(work, "Gemfile", old, new)] == ["High"]

    @pytest.mark.parametrize("config", [".npmrc", ".yarnrc.yml", ".cargo/config.toml"])
    def test_a_registry_config_change_in_the_same_diff_checks_every_added_dependency(
        self, work: Path, registry: Registry, config: str
    ) -> None:
        (work / config).parent.mkdir(parents=True, exist_ok=True)
        (work / config).write_text("")  # the change emptied the file, so no private registry remains
        old, new = self._pkg(**{"internal-lib": "1.0.0"}), self._pkg(**{"internal-lib": "2.0.0"})
        (work / "package.json").write_text(new)
        diff = work / "d.diff"
        diff.write_text(
            f"diff --git a/{config} b/{config}\n--- a/{config}\n+++ b/{config}\n@@ -1 +0,0 @@\n-registry=https://npm.internal.example/\n"
            + self._real_diff("package.json", old, new)
        )
        assert [f.severity for f in de._run_dep_exists(_cf("package.json"), diff)] == ["High"]

    def test_a_moved_manifest_checks_every_added_dependency(self, work: Path, registry: Registry) -> None:
        """The old directory's registry config no longer governs a manifest that moved."""
        old, new = self._pkg(**{"internal-lib": "1.0.0"}), self._pkg(**{"internal-lib": "2.0.0"})
        (work / "pkgs").mkdir()
        (work / "pkgs/package.json").write_text(new)
        diff = work / "d.diff"
        diff.write_text(self._real_diff("package.json", old, new).replace("b/package.json", "b/pkgs/package.json"))
        assert [f.severity for f in de._run_dep_exists(_cf("pkgs/package.json"), diff)] == ["High"]

    def test_a_diff_that_does_not_match_the_file_checks_everything(self, work: Path, registry: Registry) -> None:
        (work / "package.json").write_text(self._pkg(**{"old-pkg": "2"}))
        diff = work / "d.diff"
        diff.write_text(
            "diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n"
            '@@ -1,1 +1,1 @@\n-    "old-pkg": "1"\n+    "old-pkg": "9"\n'
        )
        found = de._run_dep_exists(_cf("package.json"), diff)
        assert [f.severity for f in found] == ["High"]


class TestFailOpen:
    @pytest.mark.parametrize("status", [301, 302, 403, 429, 500, 503])
    def test_other_statuses_give_no_finding(self, work: Path, registry: Registry, status: int) -> None:
        _set(registry, lambda r: httpx.Response(status, headers={"location": "https://evil.example/"}))
        pkg = json.dumps({"dependencies": {"some-pkg": "1"}})
        assert _run(work, "package.json", pkg, ['"some-pkg": "1"']) == []

    def test_a_timeout_gives_no_finding(self, work: Path, registry: Registry) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        _set(registry, boom)
        pkg = json.dumps({"dependencies": {"some-pkg": "1"}})
        assert _run(work, "package.json", pkg, ['"some-pkg": "1"']) == []

    def test_a_redirect_is_never_followed(self, work: Path, registry: Registry) -> None:
        _set(registry, lambda r: httpx.Response(302, headers={"location": "https://evil.example/x"}))
        pkg = json.dumps({"dependencies": {"some-pkg": "1"}})
        _run(work, "package.json", pkg, ['"some-pkg": "1"'])
        assert {r.url.host for r in registry} == {"registry.npmjs.org"}

    def test_a_non_json_body_means_exists_with_unknown_age(self, work: Path, registry: Registry) -> None:
        _set(registry, lambda r: httpx.Response(200, text="<html>"))
        pkg = json.dumps({"dependencies": {"some-pkg": "1"}})
        assert _run(work, "package.json", pkg, ['"some-pkg": "1"']) == []

    def test_an_unreadable_diff_gives_no_finding(self, work: Path) -> None:
        assert de._run_dep_exists(_cf("package.json"), work / "missing.diff") == []

    def test_failed_lookups_are_summarized_in_one_warning(
        self, work: Path, registry: Registry, caplog: pytest.LogCaptureFixture
    ) -> None:
        _set(registry, lambda r: httpx.Response(503))
        pkg = json.dumps({"dependencies": {"pkg-a": "1", "pkg-b": "1"}})
        with caplog.at_level("WARNING", logger=de.logger.name):
            assert _run(work, "package.json", pkg, ['"pkg-a": "1"', '"pkg-b": "1"']) == []
        assert "2 of 2 lookups got no usable answer" in caplog.text

    def test_a_clean_run_does_not_warn(self, work: Path, registry: Registry, caplog: pytest.LogCaptureFixture) -> None:
        _set(registry, lambda r: httpx.Response(200, text="{}"))
        pkg = json.dumps({"dependencies": {"some-pkg": "1"}})
        with caplog.at_level("WARNING", logger=de.logger.name):
            _run(work, "package.json", pkg, ['"some-pkg": "1"'])
        assert "no usable answer" not in caplog.text

    @pytest.mark.parametrize(
        ("path", "content"),
        [("package.json", "{<<<<<<< HEAD"), ("Cargo.toml", "[dependencies\nx = "), ("composer.json", "not json")],
    )
    def test_an_unparseable_manifest_is_reported(
        self, work: Path, registry: Registry, caplog: pytest.LogCaptureFixture, path: str, content: str
    ) -> None:
        with caplog.at_level("WARNING", logger=de.logger.name):
            assert _run(work, path, content, ["x"]) == []
        assert f"cannot parse {path}" in caplog.text
        assert len(registry) == 0


class TestRequestSafety:
    def test_urls_use_only_the_registry_host_and_encode_scoped_names(self) -> None:
        assert de._url(de._NPM, "@scope/pkg") == "https://registry.npmjs.org/@scope%2Fpkg"
        assert de._url(de._PACKAGIST, "a/b") == "https://repo.packagist.org/p2/a/b.json"

    def test_lookups_are_capped_and_deduplicated(self, work: Path, registry: Registry) -> None:
        names = [f"pkg-{i}" for i in range(40)]
        lines = [f"{n}==1" for n in names] + ["pkg-0==2"]
        _run(work, "requirements.txt", "\n".join(lines), lines)
        assert len(registry) == de._MAX_LOOKUPS

    def test_invalid_names_never_reach_the_network(self, work: Path, registry: Registry) -> None:
        pkg = json.dumps({"dependencies": {"x?y=1": "1", "../etc": "1"}})
        _run(work, "package.json", pkg, ['"x?y=1": "1"', '"../etc": "1"'])
        assert registry == []

    def test_oversize_body_means_exists_with_unknown_age(self, work: Path, registry: Registry) -> None:
        big = b"x" * (de._MAX_BODY_BYTES + 10)
        _set(registry, lambda r: httpx.Response(200, content=big))
        pkg = json.dumps({"dependencies": {"huge-pkg": "1"}})
        assert _run(work, "package.json", pkg, ['"huge-pkg": "1"']) == []


class TestBridge:
    def test_registered_and_scoped(self) -> None:
        from ai_pr_review.analyzers.bridge import _ANALYZERS, ANALYZER_NAMES
        from ai_pr_review.findings.scope import _ANALYZER_PREFIXES

        assert "dep-exists" in ANALYZER_NAMES and "dep-exists" in _ANALYZER_PREFIXES
        spec = next(s for s in _ANALYZERS if s.name == "dep-exists")
        assert spec.required_file_types == ["manifest_lockfile"]
