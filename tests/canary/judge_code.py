"""The code-aware judge: an experiment for the eval harness, not part of the production review.

The judge pass can read the code a finding cites. It needs only a few lines, not
the whole diff, so :func:`extract_hunk` returns a short window of the hunk that
holds the cited line. The text is untrusted data from the pull request, so the
payload carries it as a JSON field and the prompt tells the model to treat it as
data (see ``judge_prompts/finding-judge-code.md``).

:func:`judge_findings_with_code` runs the shipped judge (``findings/judge.py``)
with that payload. It adds nothing to the production module. The code-aware
prompt can return a third verdict, ``unsupported`` (the cited code does not show
the problem). The wrapper rewrites it to ``downrank`` in the model's reply, so the
production judge only ever sees ``keep`` and ``downrank``.

The first measurement did not meet the bar for turning this on (see the v2.21.0
notes and ``judge_eval.py``), so nothing in ``ai_pr_review/`` imports this file.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ai_pr_review.agents.dispatch import LLMCall
from ai_pr_review.diff.parse import parse_diff
from ai_pr_review.findings.judge import JudgeResult, judge_findings
from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest, LLMResponse

DEFAULT_CONTEXT_LINES = 6
DEFAULT_MAX_CHARS = 1500

# (new-file line number or None for a removed line, marker, text)
_Entry = tuple[int | None, str, str]


def _hunks_for_file(diff_text: str, file: str) -> list[list[_Entry]]:
    hunks: list[list[_Entry]] = []
    for parsed in parse_diff(diff_text):
        if parsed.path != file:
            continue
        for hunk in parsed.hunks:
            hunks.append([(row.new_no, row.marker, row.text) for row in hunk.rows])
    return hunks


def extract_hunk(
    diff_text: str,
    file: str,
    line: int | None,
    *,
    context: int = DEFAULT_CONTEXT_LINES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Return the lines around *line* of *file*, or an empty string when the line
    is not in the diff. Each row starts with its new-file line number, then ``+``
    (added), ``-`` (removed), or a space (context). The result is cut at a line
    boundary to *max_chars*."""
    if not file or line is None or line < 1:
        return ""
    for entries in _hunks_for_file(diff_text, file):
        target = next((i for i, (no, _m, _t) in enumerate(entries) if no == line), None)
        if target is None:
            continue
        window = entries[max(target - context, 0): target + context + 1]
        out: list[str] = []
        size = 0
        for no, marker, text in window:
            row = f"{'' if no is None else no:>5} {marker} {text}"
            if size + len(row) + 1 > max_chars and out:
                break
            out.append(row)
            size += len(row) + 1
        return "\n".join(out)
    return ""


def _candidate_code(kept: list[Finding], diff_text: str) -> list[str]:
    return [extract_hunk(diff_text, f.file, f.start_line or f.line) for f in kept]


def _unsupported_to_downrank(text: str) -> str:
    """Rewrite ``unsupported`` verdicts to ``downrank`` in a judge reply. Other text is unchanged."""
    try:
        parsed = json.loads(text)
        for verdict in parsed["verdicts"]:
            if verdict.get("verdict") == "unsupported":
                verdict["verdict"] = "downrank"
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return text
    return json.dumps(parsed, ensure_ascii=False)


async def judge_findings_with_code(
    kept: list[Finding],
    *,
    llm_call: LLMCall,
    model: str,
    prompt_path: Path,
    diff_text: str,
) -> JudgeResult:
    """Run the production judge with a ``code`` field added to each candidate.

    The wrapper changes the request on its way to the model (it adds the code window to the
    JSON payload) and the reply on its way back (``unsupported`` becomes ``downrank``).
    """
    codes = _candidate_code(kept, diff_text)

    async def wrapped(request: LLMRequest) -> LLMResponse:
        items = json.loads(request.user_message)
        for item, code in zip(items, codes, strict=True):
            item["code"] = code
        sent = dataclasses.replace(request, user_message=json.dumps(items, ensure_ascii=False))
        response = await llm_call(sent)
        return dataclasses.replace(response, text=_unsupported_to_downrank(response.text))

    return await judge_findings(kept, llm_call=wrapped, model=model, prompt_path=prompt_path)
