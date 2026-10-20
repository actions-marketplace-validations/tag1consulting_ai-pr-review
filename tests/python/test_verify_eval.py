"""Tests for the verifier eval harness in tests/canary/verify_eval.py (#1082).

Nothing here makes a billed call. The harness replays saved reviewer responses, and the
tests use a fake LLM call, a temporary cache directory and a replay-only cache.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest, LLMResponse

_CANARY = Path(__file__).resolve().parent.parent / "canary"


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    sys.path.insert(0, str(_CANARY))
    spec = importlib.util.spec_from_file_location(name, _CANARY / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ve = _load("verify_eval")
pe = _load("precision_eval")
_llm_cache = _load("_llm_cache")

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,30 +1,31 @@\n" + "".join(
    f" ctx{n}\n" if n != 12 else "+query = base + user_id\n" for n in range(1, 32)
)


def _f(line: int = 14, **kw: object) -> Finding:
    base: dict[str, object] = {
        "severity": "High", "confidence": 90, "finding": "SQL injection by string concatenation",
        "source": "code-reviewer", "category": "injection", "file": "a.py", "line": line,
    }
    return Finding.model_validate(base | kw)


def _fixture(diff_path: Path | None = None) -> SimpleNamespace:
    bug = pe.Label(file="a.py", line_start=10, line_end=12, category="injection",
                   summary="sql injection by string concatenation of user id")
    return SimpleNamespace(name="fx", bugs=[bug], accepted_extra=[], diff_path=diff_path)


def _reply(*items: dict[str, object]) -> str:
    return json.dumps({"verdicts": list(items)})


class TestPayload:
    def test_ids_follow_the_list_and_code_is_attached(self) -> None:
        findings = [_f(12), _f(20)]
        items = json.loads(ve.build_payload(findings, DIFF, ve.CONDITIONS[0]))
        assert [i["id"] for i in items] == [0, 1]
        assert "query = base + user_id" in items[0]["code"]
        assert all(set(i) == {"id", "severity", "file", "line", "finding", "code"} for i in items)

    def test_the_wide_window_holds_more_rows_than_the_narrow_one(self) -> None:
        narrow = json.loads(ve.build_payload([_f(12)], DIFF, ve.CONDITIONS[0]))[0]["code"]
        wide = json.loads(ve.build_payload([_f(12)], DIFF, ve.CONDITIONS[1]))[0]["code"]
        assert len(wide.splitlines()) > len(narrow.splitlines())

    def test_a_line_outside_the_diff_has_empty_code(self) -> None:
        assert json.loads(ve.build_payload([_f(999)], DIFF, ve.CONDITIONS[0]))[0]["code"] == ""


class TestParse:
    def test_reads_verdict_and_line(self) -> None:
        got = ve.parse_verdicts(_reply({"id": 0, "verdict": "confirmed", "line": 12, "reason": "ok"}), 1)
        assert got == {0: ve.Verdict("confirmed", 12, "ok")}

    def test_strips_a_markdown_fence(self) -> None:
        text = "```json\n" + _reply({"id": 0, "verdict": "unsupported", "line": None}) + "\n```"
        assert ve.parse_verdicts(text, 1)[0].verdict == "unsupported"

    @pytest.mark.parametrize("line", [None, "12", True, 0, -3, 1.5])
    def test_a_line_that_is_not_a_positive_int_becomes_none(self, line: object) -> None:
        got = ve.parse_verdicts(_reply({"id": 0, "verdict": "confirmed", "line": line}), 1)
        assert got[0].line is None

    def test_out_of_range_ids_and_unknown_verdicts_are_left_out(self) -> None:
        text = _reply(
            {"id": 5, "verdict": "confirmed"},
            {"id": 1, "verdict": "keep"},
            {"id": "x", "verdict": "confirmed"},
            {"id": 0, "verdict": "confirmed"},
        )
        assert list(ve.parse_verdicts(text, 2)) == [0]

    @pytest.mark.parametrize("text", ["not json", "{}", '{"verdicts": "no"}', "[]", ""])
    def test_an_unreadable_reply_stops_the_run(self, text: str) -> None:
        with pytest.raises(ve.VerifyError):
            ve.parse_verdicts(text, 1)


class TestVerify:
    def test_verdicts_line_up_with_findings_and_a_gap_is_none(self) -> None:
        seen: list[LLMRequest] = []

        async def llm_call(req: LLMRequest) -> LLMResponse:
            seen.append(req)
            return LLMResponse(text=_reply({"id": 1, "verdict": "confirmed", "line": 12}),
                               input_tokens=1, output_tokens=1, stop_reason="end_turn")

        out = asyncio.run(ve.verify_findings(
            [_f(12), _f(20)], llm_call=llm_call, cond=ve.CONDITIONS[0], prompt_text="PROMPT", diff_text=DIFF
        ))
        assert out[0] is None and out[1] == ve.Verdict("confirmed", 12, "")
        assert seen[0].system_prompt == "PROMPT" and seen[0].temperature == 0.0
        assert seen[0].model_id == ve.SONNET

    def test_an_empty_code_window_is_recorded_on_the_verdict(self) -> None:
        async def llm_call(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                text=_reply({"id": 0, "verdict": "unsupported"}, {"id": 1, "verdict": "confirmed"}),
                input_tokens=1, output_tokens=1, stop_reason="end_turn",
            )

        out = asyncio.run(ve.verify_findings(
            [_f(999), _f(12)], llm_call=llm_call, cond=ve.CONDITIONS[0], prompt_text="P", diff_text=DIFF
        ))
        assert [v.had_code for v in out if v is not None] == [False, True]

    def test_no_findings_makes_no_call(self) -> None:
        async def llm_call(req: LLMRequest) -> LLMResponse:
            raise AssertionError("no call expected")

        assert asyncio.run(ve.verify_findings(
            [], llm_call=llm_call, cond=ve.CONDITIONS[0], prompt_text="P", diff_text=""
        )) == []


class TestScore:
    def _scored(self):  # type: ignore[no-untyped-def]
        fx = _fixture()
        bug_far = _f(14)                                   # matches the bug, line outside 10-12
        bug_dropped = _f(11, finding="SQL injection by concatenation of user id")
        unlabeled_caught = _f(90, finding="Unused import os", category="lint")
        unlabeled_gap = _f(95, finding="Missing docstring", category="docs")
        findings = [bug_far, bug_dropped, unlabeled_caught, unlabeled_gap]
        verdicts = [
            ve.Verdict("confirmed", 11),
            ve.Verdict("unsupported", None),
            ve.Verdict("unsupported", None),
            None,
        ]
        return fx, findings, verdicts

    def test_counts_for_a_verifier_condition(self) -> None:
        fx, findings, verdicts = self._scored()
        r = ve.score_condition("reread-sonnet", [(fx, findings, verdicts)])
        assert (r.bugs, r.kept_real, r.dropped_real) == (2, 1, 1)
        assert (r.unlabeled, r.caught_unlabeled, r.unverified) == (2, 1, 1)
        # The first bug sits on line 14, outside 10-12. The verifier moves it to 11.
        assert (r.anchor_before, r.anchor_after) == (1, 2)

    def test_a_finding_without_a_verdict_is_not_counted_as_kept(self) -> None:
        fx = _fixture()
        findings = [_f(11), _f(12)]
        r = ve.score_condition("reread-sonnet", [(fx, findings, [None, None])])
        assert (r.bugs, r.kept_real, r.unverified) == (2, 0, 2)

    def test_a_verdict_without_a_line_and_one_without_code_are_counted(self) -> None:
        fx = _fixture()
        verdicts = [ve.Verdict("confirmed", None), ve.Verdict("unsupported", 11, had_code=False)]
        r = ve.score_condition("reread-sonnet", [(fx, [_f(11), _f(12)], verdicts)])
        assert (r.no_line, r.unsupported_without_code) == (1, 1)

    def test_the_none_condition_changes_nothing(self) -> None:
        fx, findings, _ = self._scored()
        r = ve.score_condition("none", [(fx, findings, [None] * len(findings))])
        assert {row.verdict for row in r.rows} == {"-"}
        assert (r.dropped_real, r.caught_unlabeled, r.unverified) == (0, 0, 0)
        assert r.anchor_before == r.anchor_after

    def test_report_shows_every_finding_and_the_moved_line(self) -> None:
        fx, findings, verdicts = self._scored()
        results = [
            ve.score_condition("none", [(fx, findings, [None] * 4)]),
            ve.score_condition("reread-sonnet", [(fx, findings, verdicts)]),
        ]
        report = ve.format_report(results)
        assert "reread-sonnet" in report and "anchor exact" in report
        assert "confirmed ->11" in report
        assert report.count("fx a.py:") == 4


class _FakeGuard:
    def estimate_units(self, model_id: str, *, input_tokens: int, output_tokens: int,
                       worst_case: bool = False) -> int:
        return input_tokens + output_tokens


class TestEstimate:
    def test_counts_one_call_per_condition_and_fixture_with_findings(self) -> None:
        per_fixture = [(_fixture(), [_f(12)]), (_fixture(), [])]
        expected, worst, calls = ve.estimate(per_fixture, ve.CONDITIONS, _FakeGuard())
        assert calls == 2 and worst > expected > 0

    def test_the_wide_window_costs_more_than_the_narrow_one(self) -> None:
        per_fixture = [(_fixture(), [_f(12)])]
        narrow = ve.estimate(per_fixture, ve.CONDITIONS[:1], _FakeGuard())[0]
        wide = ve.estimate(per_fixture, ve.CONDITIONS[1:], _FakeGuard())[0]
        assert wide > narrow

    def test_no_findings_means_no_calls(self) -> None:
        assert ve.estimate([(_fixture(), [])], ve.CONDITIONS, _FakeGuard()) == (0, 0, 0)


class TestMain:
    def test_dry_run_prints_the_estimate_and_makes_no_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AI_EVAL_CACHE_MODE", "auto")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        fx = _fixture(tmp_path / "a.diff")

        async def collect(*_a: object, **_k: object) -> list:
            return [(fx, [_f(12)])]

        monkeypatch.setattr(ve.pe, "load_fixtures", lambda *a, **k: [fx])
        monkeypatch.setattr(ve.je, "collect_findings", collect)

        async def no_calls(*_a: object, **_k: object) -> object:
            raise AssertionError("a dry run must not call the model")

        monkeypatch.setattr(ve, "run_conditions", no_calls)
        assert asyncio.run(ve.main(["--dry-run"])) == 0
        out = capsys.readouterr().out
        assert "pre-flight: up to 2 billed verifier calls" in out
        assert "dry run: no billed calls were made" in out

    def test_a_missing_key_stops_a_real_run_before_any_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("AI_EVAL_CACHE_MODE", "auto")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        fx = _fixture(tmp_path / "a.diff")

        async def collect(*_a: object, **_k: object) -> list:
            return [(fx, [_f(12)])]

        monkeypatch.setattr(ve.pe, "load_fixtures", lambda *a, **k: [fx])
        monkeypatch.setattr(ve.je, "collect_findings", collect)
        assert asyncio.run(ve.main([])) == 1


class TestConditions:
    def test_selection(self) -> None:
        assert [c.name for c in ve._selected("")] == ["hunk-sonnet", "reread-sonnet"]
        assert [c.name for c in ve._selected("reread-sonnet")] == ["reread-sonnet"]
        with pytest.raises(ValueError):
            ve._selected("nope")

    def test_the_none_condition_alone_is_rejected_by_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_EVAL_VERIFY_CONDITIONS", "none")
        assert asyncio.run(ve.main([])) == 1

    def test_the_prompt_file_exists_and_names_both_verdicts(self) -> None:
        text = ve.PROMPT.read_text()
        assert "`confirmed`" in text and "`unsupported`" in text and "`line`" in text
        assert "Trust Boundary" in text

    def test_replay_mode_never_makes_a_billed_call(self, tmp_path: Path) -> None:
        """An empty replay cache must raise a cache miss, not reach the provider."""
        diff_path = tmp_path / "a.diff"
        diff_path.write_text(DIFF)
        fx = _fixture(diff_path)
        cache = _llm_cache.LLMCache(cache_dir=tmp_path / "cache", mode="replay", namespace="anthropic")
        with pytest.raises(_llm_cache.CacheMiss):
            asyncio.run(ve.run_conditions([(fx, [_f(12)])], ve.CONDITIONS[:1], cache, None))
