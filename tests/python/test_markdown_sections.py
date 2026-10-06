"""markdown_sections must stay importable without the rest of the package, so
agents/ and vcs/ can both use it (#999 review finding F2)."""

from __future__ import annotations

import subprocess
import sys

from ai_pr_review.markdown_sections import find_section_span, strip_walkthrough_section


def test_importing_vcs_does_not_load_the_agents_package() -> None:
    code = (
        "import sys; import ai_pr_review.vcs.bitbucket; "
        "print('ai_pr_review.agents.summarizer' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_find_section_span_and_strip_work_from_the_neutral_module() -> None:
    text = "## Summary\n\nS.\n\n## Walkthrough\n\n| a |\n"
    span = find_section_span(text, "walkthrough")
    assert span is not None
    assert text[span[0]:span[1]].startswith("## Walkthrough")
    assert strip_walkthrough_section(text) == "## Summary\n\nS.\n"
