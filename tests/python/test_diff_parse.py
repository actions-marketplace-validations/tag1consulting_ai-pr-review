"""Tests for the shared unified-diff parser in ai_pr_review/diff/parse.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_pr_review.diff.parse import decode_git_path, parse_diff


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


class TestDecodeGitPath:
    def test_a_plain_path_is_unchanged(self) -> None:
        assert decode_git_path("src/app.py") == "src/app.py"

    def test_octal_escapes_are_utf8_bytes(self) -> None:
        assert decode_git_path(r'"pkg-\303\251/package.json"') == "pkg-é/package.json"

    def test_standard_escapes(self) -> None:
        assert decode_git_path(r'"a\tb\\c\"d"') == 'a\tb\\c"d'


class TestFiles:
    def test_a_simple_change_has_rows_with_both_line_numbers(self) -> None:
        diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
        (file,) = parse_diff(diff)
        assert (file.old_path, file.new_path, file.renamed) == ("x.py", "x.py", False)
        (hunk,) = file.hunks
        assert [(r.old_no, r.new_no, r.marker, r.text) for r in hunk.rows] == [
            (1, 1, " ", "a"),
            (2, None, "-", "b"),
            (None, 2, "+", "B"),
            (3, 3, " ", "c"),
        ]
        assert hunk.old_side == ["a", "b", "c"] and hunk.new_side == ["a", "B", "c"]

    def test_a_rename_has_both_paths(self) -> None:
        diff = (
            "diff --git a/old/p.json b/new/p.json\nsimilarity index 80%\nrename from old/p.json\nrename to new/p.json\n"
            "--- a/old/p.json\n+++ b/new/p.json\n@@ -1 +1 @@\n-a\n+b\n"
        )
        (file,) = parse_diff(diff)
        assert (file.old_path, file.new_path, file.renamed) == ("old/p.json", "new/p.json", True)

    def test_a_rename_without_changes_has_no_hunks(self) -> None:
        diff = "diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n"
        (file,) = parse_diff(diff)
        assert (file.old_path, file.new_path, file.hunks) == ("a.txt", "b.txt", [])

    def test_a_quoted_path_is_decoded(self) -> None:
        diff = (
            'diff --git "a/pkg-\\303\\251/package.json" "b/pkg-\\303\\251/package.json"\n'
            '--- "a/pkg-\\303\\251/package.json"\n+++ "b/pkg-\\303\\251/package.json"\n@@ -1 +1 @@\n-a\n+b\n'
        )
        (file,) = parse_diff(diff)
        assert file.path == "pkg-é/package.json"

    def test_a_path_with_spaces_and_a_trailing_tab(self) -> None:
        diff = "diff --git a/my dir/p.json b/my dir/p.json\n--- a/my dir/p.json\t\n+++ b/my dir/p.json\t\n@@ -1 +1 @@\n-a\n+b\n"
        (file,) = parse_diff(diff)
        assert file.path == "my dir/p.json"

    def test_a_path_that_contains_b_slash(self) -> None:
        diff = "diff --git a/x b/y/f b/x b/y/f\n--- a/x b/y/f\n+++ b/x b/y/f\n@@ -1 +1 @@\n-a\n+b\n"
        (file,) = parse_diff(diff)
        assert file.path == "x b/y/f"

    def test_new_and_deleted_files(self) -> None:
        diff = (
            "diff --git a/n.txt b/n.txt\nnew file mode 100644\n--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n"
            "diff --git a/d.txt b/d.txt\ndeleted file mode 100644\n--- a/d.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
        )
        new, deleted = parse_diff(diff)
        assert (new.old_path, new.new_path, new.path) == (None, "n.txt", "n.txt")
        assert (deleted.old_path, deleted.new_path, deleted.path) == ("d.txt", None, "d.txt")
        assert [r.new_no for r in new.hunks[0].rows] == [1, 2]

    def test_a_removed_line_that_starts_with_dashes_is_a_row_not_a_header(self) -> None:
        diff = "diff --git a/s.sql b/s.sql\n--- a/s.sql\n+++ b/s.sql\n@@ -1,2 +1,1 @@\n--- a comment\n keep\n"
        (file,) = parse_diff(diff)
        assert [(r.marker, r.text) for r in file.hunks[0].rows] == [("-", "-- a comment"), (" ", "keep")]
        assert file.path == "s.sql"

    def test_a_missing_newline_marker_is_skipped(self) -> None:
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
        (file,) = parse_diff(diff)
        assert [r.marker for r in file.hunks[0].rows] == ["-", "+"]

    def test_a_count_of_one_may_be_left_out(self) -> None:
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -5 +5 @@\n-a\n+b\n"
        (file,) = parse_diff(diff)
        h = file.hunks[0]
        assert (h.old_start, h.old_count, h.new_start, h.new_count) == (5, 1, 5, 1)

    def test_several_hunks_and_files(self) -> None:
        diff = (
            "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n@@ -10 +10 @@\n-c\n+d\n"
            "diff --git a/g b/g\n--- a/g\n+++ b/g\n@@ -1 +1 @@\n-e\n+f\n"
        )
        f, g = parse_diff(diff)
        assert [h.new_start for h in f.hunks] == [1, 10] and len(g.hunks) == 1

    def test_text_before_the_first_header_is_ignored(self) -> None:
        assert parse_diff("noise\n@@ -1 +1 @@\n-a\n+b\n") == []

    def test_an_empty_diff(self) -> None:
        assert parse_diff("") == []


@pytest.mark.parametrize("mnemonic", [False, True])
def test_a_real_git_diff_with_a_non_ascii_directory(tmp_path: Path, mnemonic: bool) -> None:
    """Parse what git writes. The prefixes are pinned the way the engine pins them."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "pkg-é").mkdir()
    (tmp_path / "pkg-é/package.json").write_text('{"a": 1}\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "pkg-é/package.json").write_text('{"a": 2}\n')
    flags = ["-c", f"diff.mnemonicPrefix={'true' if mnemonic else 'false'}", "diff", "--src-prefix=a/", "--dst-prefix=b/"]
    diff = _git(tmp_path, *flags)
    assert '"a/pkg-\\303\\251/package.json"' in diff  # git quoted the path
    (file,) = parse_diff(diff)
    assert file.path == "pkg-é/package.json"


class TestConsumersAgree:
    """dep-exists and the judge window read one diff through one parser (follow-up to F3)."""

    DIFF = (
        "diff --git a/old/package.json b/pkgs/package.json\nsimilarity index 70%\nrename from old/package.json\n"
        "rename to pkgs/package.json\n--- a/old/package.json\n+++ b/pkgs/package.json\n@@ -1,2 +1,3 @@\n a\n+one\n b\n"
        'diff --git "a/pkg-\\303\\251/package.json" "b/pkg-\\303\\251/package.json"\n'
        '--- "a/pkg-\\303\\251/package.json"\n+++ "b/pkg-\\303\\251/package.json"\n@@ -4,2 +4,3 @@\n c\n+two\n d\n'
        "diff --git a/x b/y/package.json b/x b/y/package.json\n--- a/x b/y/package.json\n+++ b/x b/y/package.json\n"
        "@@ -7 +7,2 @@\n e\n+three\n"
    )

    @pytest.mark.parametrize(
        ("path", "line", "text"),
        [("pkgs/package.json", 2, "one"), ("pkg-é/package.json", 5, "two"), ("x b/y/package.json", 8, "three")],
    )
    def test_the_same_file_and_line_in_both(self, path: str, line: int, text: str) -> None:
        import importlib.util
        import sys

        from ai_pr_review.analyzers.native.dep_exists import added_lines

        canary = Path(__file__).resolve().parent.parent / "canary" / "judge_code.py"
        spec = importlib.util.spec_from_file_location("judge_code", canary)
        assert spec is not None and spec.loader is not None
        module = sys.modules.get("judge_code") or importlib.util.module_from_spec(spec)
        sys.modules["judge_code"] = module
        if not hasattr(module, "extract_hunk"):
            spec.loader.exec_module(module)
        extract_hunk = module.extract_hunk

        assert (line, text) in added_lines(self.DIFF)[path]
        window = extract_hunk(self.DIFF, path, line)
        assert f"{line:>5} + {text}" in window


def test_compute_diff_ignores_the_users_git_diff_config_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1032: mnemonic prefixes and quoted non-ASCII paths in the user's git config must not
    make dep-exists skip a manifest. The test sets both, then runs compute_diff and dep-exists."""
    import json

    import httpx

    from ai_pr_review.analyzers.native import dep_exists as de
    from ai_pr_review.diff.compute import compute_diff
    from ai_pr_review.manifest import ChangedFiles

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "diff.mnemonicPrefix", "true")
    _git(repo, "config", "core.quotePath", "true")
    (repo / "pkg-é").mkdir()
    (repo / "pkg-é/package.json").write_text(json.dumps({"dependencies": {"left-pad": "1"}}, indent=2) + "\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "pkg-é/package.json").write_text(
        json.dumps({"dependencies": {"left-pad": "1", "invented-pkg": "1"}}, indent=2) + "\n"
    )
    _git(repo, "commit", "-qam", "add a dependency")
    head = _git(repo, "rev-parse", "HEAD").strip()

    result = compute_diff("main", head, workspace=str(repo), ignore_merge_commits=False)
    assert result.changed_files == ["pkg-é/package.json"]
    assert "+++ b/pkg-é/package.json" in result.diff_text

    monkeypatch.chdir(repo)
    seen: list[str] = []
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(404)

        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(de.httpx, "Client", factory)
    diff_file = tmp_path / "d.diff"
    diff_file.write_text(result.diff_text)
    found = de._run_dep_exists(ChangedFiles(all_files=result.changed_files, manifest_lockfile=result.changed_files), diff_file)
    assert [f.file for f in found] == ["pkg-é/package.json"]
    assert seen == ["/invented-pkg"]


def test_a_manifest_missing_from_the_diff_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from ai_pr_review.analyzers.native import dep_exists as de
    from ai_pr_review.manifest import ChangedFiles

    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}")
    diff_file = tmp_path / "d.diff"
    diff_file.write_text("diff --git c/other.txt i/other.txt\n--- c/other.txt\n+++ i/other.txt\n@@ -1 +1 @@\n-a\n+b\n")
    with caplog.at_level("WARNING", logger=de.logger.name):
        assert de._run_dep_exists(ChangedFiles(all_files=["package.json"], manifest_lockfile=["package.json"]), diff_file) == []
    assert "package.json is in the changed-file list but not in the diff" in caplog.text
