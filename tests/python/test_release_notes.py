"""Tests for scripts/release_notes.py, which builds GitHub release notes from a
docs/version-history/vX.Y.Z.md page (see .github/workflows/draft-release.yml).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "release_notes.py"

_PAGE = """---
layout: default
title: v9.9.9
parent: Version History
nav_order: 1
render_with_liquid: false
---

# v9.9.9

## Changed

- **Something changed.** Details, with a `code span`.

## Fixed

- **Something fixed.** See [CHANGELOG.md](https://example.com/x) inside a bullet is kept.

There are no engine changes in this release.

See [CHANGELOG.md](https://github.com/tag1consulting/ai-pr-review/blob/main/CHANGELOG.md) for full technical detail.
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = _load()


def test_strips_front_matter_title_and_trailing_changelog_line() -> None:
    notes = release_notes.build_notes(_PAGE)
    assert notes.startswith("## Changed\n")
    assert "layout: default" not in notes
    assert "# v9.9.9" not in notes
    assert notes.rstrip().endswith("There are no engine changes in this release.")


def test_keeps_a_changelog_mention_that_is_not_the_closing_line() -> None:
    assert "See [CHANGELOG.md](https://example.com/x) inside a bullet is kept." in (
        release_notes.build_notes(_PAGE)
    )


def test_output_ends_with_exactly_one_newline() -> None:
    notes = release_notes.build_notes(_PAGE)
    assert notes.endswith("\n") and not notes.endswith("\n\n")


def test_page_without_front_matter_or_changelog_line() -> None:
    assert release_notes.build_notes("# v1.0.0\n\n## Added\n\n- One thing.\n") == (
        "## Added\n\n- One thing.\n"
    )


def test_page_with_only_boilerplate_yields_no_notes() -> None:
    page = "---\ntitle: v1\n---\n\n# v1\n\nSee [CHANGELOG.md](https://x.example/c) for more.\n"
    assert release_notes.build_notes(page) == ""


def test_only_the_first_heading_is_removed() -> None:
    notes = release_notes.build_notes("# Title\n\n# Second top level\n\ntext\n")
    assert notes == "# Second top level\n\ntext\n"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_cli_prints_notes_and_exits_zero(tmp_path: Path) -> None:
    page = tmp_path / "v9.9.9.md"
    page.write_text(_PAGE, encoding="utf-8")
    result = _run(str(page))
    assert result.returncode == 0
    assert result.stdout == release_notes.build_notes(_PAGE)


def test_cli_missing_page_exits_2(tmp_path: Path) -> None:
    result = _run(str(tmp_path / "does-not-exist.md"))
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def test_cli_empty_result_exits_1(tmp_path: Path) -> None:
    page = tmp_path / "empty.md"
    page.write_text("---\ntitle: x\n---\n\n# x\n", encoding="utf-8")
    result = _run(str(page))
    assert result.returncode == 1
    assert result.stdout == ""


def test_cli_wrong_argument_count_exits_2() -> None:
    assert _run().returncode == 2


@pytest.mark.parametrize("page", sorted((_REPO / "docs" / "version-history").glob("v*.md")))
def test_every_real_version_page_produces_notes(page: Path) -> None:
    notes = release_notes.build_notes(page.read_text(encoding="utf-8"))
    assert notes.strip(), f"{page.name} would produce empty release notes"
    assert not notes.startswith("---")
    assert "\n# " not in "\n" + notes.split("\n", 1)[0]
