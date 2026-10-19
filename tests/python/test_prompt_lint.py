"""Prompt lint: keeps tool-runtime instructions and the NONE/[] contradiction
from re-entering prompts/*.md.

Background (ADR 0004, F4/F5): this engine's agents are each one tool-less
HTTP call whose output must survive `findings/extract.py`'s fenced-JSON
regex. Every agent prompt was, at various points, imported from
claude-comprehensive-review (Claude Code subagents with real Read/Bash/MCP
tool access, output read as free-form prose by an orchestrating skill).
That mismatch has already shipped a defect to a live PR comment (#446:
issue-linker instructed to run `gh issue view`/`git diff`, had no tools,
emitted literal `<tool_call>` XML) and produced a self-contradictory
empty-state instruction across the seven finding-agent prompts (#372 added
`NONE` to five of them while `_trailer-findings.md` — appended to all seven
— says emit `[]`, and `findings/extract.py` has no `NONE` handling at all:
an agent that obeys it fails the fence match and its entire review is
silently discarded).

This is a regression lint, not a general prompt-quality check: it enforces
the specific invariants those two incidents established, so the next prompt
edit that borrows one instruction from a tool-having plugin gets caught at
review time instead of in a posted PR comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai_pr_review.findings.models import CATEGORIES

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Every prompt in this engine backs a tool-less HTTP call. None of these
# markers can ever be actionable, in any prompt file.
_TOOL_AFFORDANCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "Read tool": re.compile(r"\bRead tool\b"),
    "executable git diff invocation": re.compile(r"git diff\s*(--|<|@\{)"),
    "gh CLI invocation": re.compile(r"\bgh (issue|pr|api)\b"),
    "MCP tool reference": re.compile(r"\bmcp__"),
    "EXTENDED_THINKING flag": re.compile(r"\bEXTENDED_THINKING\b"),
}

# The seven agents whose output ai_pr_review/findings/extract.py parses as a
# fenced json-findings block. issue-linker.md and pr-summarizer.md are
# preflight prose agents parsed separately by review/preflight.py, which
# does understand a bare NONE — they are deliberately excluded below.
_FINDING_AGENT_PROMPTS = (
    "adversarial-general.md",
    "architecture-reviewer.md",
    "blind-hunter.md",
    "code-reviewer.md",
    "edge-case-hunter.md",
    "security-reviewer.md",
    "silent-failure-hunter.md",
)

# The exact phrasing #372's backport used to instruct emitting the bare word
# NONE. Every finding-agent prompt now instead says "Do NOT output the bare
# word `NONE`" (matching _trailer-findings.md's `[]` empty state), so this
# phrase should never reappear in any of them.
_POSITIVE_NONE_INSTRUCTION = re.compile(r"output exactly", re.IGNORECASE)

# #798: code-reviewer.md's "Severity Classification" section keyed each
# level directly to a confidence range ("**High** (confidence 80-90)"),
# while _governance.md (appended to the same composed prompt) says severity
# reflects harm and review/outcome.py turns any High into REQUEST_CHANGES --
# a confidently-identified but harmless finding could reach High and block
# a merge purely because it was confident. CONTEXT.md now defines Severity
# and Confidence as independent axes (Severity: the blocking contract,
# harm-based; Confidence: an existence floor only, findings/merge.py's
# threshold). This catches the pattern reappearing in any finding-agent
# prompt, not just the one it originally shipped in.
_SEVERITY_FROM_CONFIDENCE = re.compile(
    r"\*\*(Critical|High|Medium|Low)\*\*\s*\(confidence", re.IGNORECASE
)


def _all_prompt_files() -> list[Path]:
    files = sorted(PROMPTS_DIR.glob("*.md"))
    assert files, f"expected prompt files under {PROMPTS_DIR}"
    return files


@pytest.mark.parametrize("prompt_path", _all_prompt_files(), ids=lambda p: p.name)
def test_no_tool_affordance_markers(prompt_path: Path) -> None:
    content = prompt_path.read_text()
    for label, pattern in _TOOL_AFFORDANCE_PATTERNS.items():
        match = pattern.search(content)
        assert match is None, (
            f"{prompt_path.name} contains a {label} ({match.group(0)!r}), but no "
            "agent in this engine has tool access — see ADR 0004"
        )


@pytest.mark.parametrize("filename", _FINDING_AGENT_PROMPTS)
def test_no_bare_none_instruction(filename: str) -> None:
    content = (PROMPTS_DIR / filename).read_text()
    match = _POSITIVE_NONE_INSTRUCTION.search(content)
    assert match is None, (
        f"{filename} instructs outputting a bare sentinel word (matched "
        f"{match.group(0)!r}), but findings/extract.py has no handling for "
        "anything but a fenced json-findings block — an agent that obeys "
        "this has its entire review silently discarded (#372, F5)"
    )


@pytest.mark.parametrize("filename", _FINDING_AGENT_PROMPTS)
def test_no_severity_derived_from_confidence(filename: str) -> None:
    content = (PROMPTS_DIR / filename).read_text()
    match = _SEVERITY_FROM_CONFIDENCE.search(content)
    assert match is None, (
        f"{filename} keys a severity level directly to a confidence range "
        f"(matched {match.group(0)!r}), contradicting _governance.md's "
        "harm-based severity definition and letting a confidently-identified "
        "but harmless finding reach a level that blocks the merge (#798)"
    )


# The prompts that carry their own copy of the `category` list. code-reviewer.md
# and silent-failure-hunter.md rely on _trailer-findings.md alone.
_CATEGORY_LIST_PROMPTS = (
    "_trailer-findings.md",
    "adversarial-general.md",
    "architecture-reviewer.md",
    "blind-hunter.md",
    "edge-case-hunter.md",
    "security-reviewer.md",
)


@pytest.mark.parametrize("filename", _CATEGORY_LIST_PROMPTS)
def test_category_list_names_every_category(filename: str) -> None:
    """A category the model is not told about is never chosen. Adding one to
    findings/models.py must reach every prompt that lists the categories (#1076)."""
    content = (PROMPTS_DIR / filename).read_text()
    missing = [c for c in CATEGORIES if f"`{c}`" not in content]
    assert not missing, f"{filename} does not list the categories {missing}"


def test_security_reviewer_routes_crypto_findings_to_crypto_category() -> None:
    content = (PROMPTS_DIR / "security-reviewer.md").read_text()
    section = content.split("### Cryptographic Issues", 1)[1].split("###", 1)[0]
    assert "`crypto`" in section


@pytest.mark.parametrize("filename", _CATEGORY_LIST_PROMPTS)
def test_embedded_schema_names_impact_and_trigger(filename: str) -> None:
    """The example `json-findings` line is the schema the model copies (#1078)."""
    lines = [
        line
        for line in (PROMPTS_DIR / filename).read_text().splitlines()
        if line.startswith("[{") and '"remediation"' in line
    ]
    assert lines, f"{filename} has no json-findings example line"
    for line in lines:
        assert '"impact"' in line and '"trigger"' in line, f"{filename}: {line[:80]}"


def test_trailer_asks_for_one_kind_of_defect_per_finding() -> None:
    content = (PROMPTS_DIR / "_trailer-findings.md").read_text()
    assert "Put one kind of defect in each finding" in content
    assert "Do not move text from `finding` into `impact`" in content


def test_code_reviewer_checks_cross_function_consistency() -> None:
    """#1079: a value that one function writes and another reads must agree on
    transport, name, prefix and format. The prompt names the cookie and header case."""
    content = (PROMPTS_DIR / "code-reviewer.md").read_text()
    section = content.split("**Cross-Function Consistency**", 1)[1].split("**Code Quality**", 1)[0]
    for needle in ("transport", "prefix", "cookie", "`Authorization` header", "Bearer"):
        assert needle in section, needle
    assert "Do not guess" in section


def test_edge_case_hunter_rates_destructive_shell_gaps_critical() -> None:
    """#1080: the scope line excludes security implications, so the destructive
    operation rule is written as an explicit exception there and again as a
    Critical clause."""
    content = (PROMPTS_DIR / "edge-case-hunter.md").read_text()
    scope = content.split("## Scope Boundaries", 1)[1].split("## ", 1)[0]
    assert "Exception, destructive operations" in scope
    for needle in ("`chmod`", "`/*`", "`$DIR/*`"):
        assert needle in scope, needle
    severity = content.split("## Severity Classification", 1)[1].split("## ", 1)[0]
    critical = next(line for line in severity.splitlines() if line.startswith("- **Critical**"))
    for needle in ("`chmod`", "`/*`", "root"):
        assert needle in critical, needle


def test_shell_profile_flags_empty_variable_glob_on_destructive_commands() -> None:
    content = (Path(PROMPTS_DIR).parent / "language-profiles" / "shell.md").read_text()
    line = next(line for line in content.splitlines() if "empty value expands to `/*`" in line)
    assert "`chmod`" in line and "`${VAR:?message}`" in line and "root" in line
