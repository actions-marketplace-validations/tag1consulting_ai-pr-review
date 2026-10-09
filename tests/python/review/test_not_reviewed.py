"""Tests for the "Not reviewed" note."""

from __future__ import annotations

from ai_pr_review.review.not_reviewed import build_not_reviewed_notice as build


def test_nothing_left_out_gives_an_empty_note() -> None:
    assert build(excluded_patterns=[], skipped_agents=[], is_incremental=False) == ""


def test_excluded_patterns_lose_the_pathspec_prefix() -> None:
    note = build(excluded_patterns=[":!*lock.json", ":!vendor/*"], skipped_agents=[], is_incremental=False)
    assert note == "> **Not reviewed:** Files matching `*lock.json`, `vendor/*`."


def test_skipped_agents_are_named_once_and_sorted() -> None:
    note = build(excluded_patterns=[], skipped_agents=["b-agent", "a-agent", "a-agent"], is_incremental=False)
    assert "`a-agent`, `b-agent`" in note and "these agents" in note
    single = build(excluded_patterns=[], skipped_agents=["a-agent"], is_incremental=False)
    assert "this agent" in single


def test_incremental_scope_is_stated() -> None:
    assert "incremental" in build(excluded_patterns=[], skipped_agents=[], is_incremental=True)


def test_a_long_pattern_list_is_cut_with_a_count() -> None:
    patterns = [f":!p{i}/*" for i in range(12)]
    note = build(excluded_patterns=patterns, skipped_agents=[], is_incremental=False)
    assert "`p7/*`" in note and "`p8/*`" not in note and "and 4 more" in note


def test_note_is_one_line_with_no_semicolon_or_em_dash() -> None:
    note = build(excluded_patterns=[":!a"], skipped_agents=["x"], is_incremental=True)
    assert "\n" not in note and ";" not in note and chr(0x2014) not in note
