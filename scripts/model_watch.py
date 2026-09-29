#!/usr/bin/env python3
"""Flag Anthropic models that are newer than this repo's pinned default models.

Used by .github/workflows/model-watch.yml. It only reads: it lists the models the
Anthropic Models API offers, compares the newest Sonnet and Opus with the pinned
Anthropic `standard` (Sonnet) and `premium` (Opus) defaults, and opens ONE issue per
newer model. It never changes a default. Bumping a default needs a pricing row, the
per-model effort and temperature handling, and a live canary run, so a person does it.

Exit codes: 0 finished (an issue may have been opened), 1 a real failure (API error,
unparseable default), 2 not configured (no API key).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

API_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"
PAGE_LIMIT = 1000
MAX_PAGES = 20
HTTP_ATTEMPTS = 3

# A model id must match this before it is ever put in an issue title or body: the ids
# come from an external API response.
_SAFE_ID = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,63}")
# claude-<family>-<major>[-<minor>][-<yyyymmdd>], with an optional provider prefix such
# as "us.anthropic." in front.
_MODEL = re.compile(r"(?:^|\.)claude-(sonnet|opus)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?\Z")

# (kind of default, family it belongs to)
_WATCHED = (("standard", "sonnet"), ("premium", "opus"))

Fetch = Callable[[str, str], Mapping[str, Any]]
Run = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class WatchError(Exception):
    """A failure the workflow should surface as a red run."""


@dataclass(frozen=True)
class Parsed:
    family: str
    version: tuple[int, int]


def parse_model(model_id: str) -> Parsed | None:
    """Return the Sonnet or Opus family and (major, minor) version, or None."""
    match = _MODEL.search(model_id.lower())
    if not match:
        return None
    return Parsed(match.group(1), (int(match.group(2)), int(match.group(3) or 0)))


def newer_models(pinned_id: str, listed: Sequence[str]) -> list[str]:
    """Ids in the pinned model's family that are a newer version, newest first.

    One id per version, preferring the shortest (the undated alias).
    """
    pinned = parse_model(pinned_id)
    if pinned is None:
        raise WatchError(f"cannot read a Sonnet or Opus version from the pinned model {pinned_id!r}")
    best: dict[tuple[int, int], str] = {}
    for model_id in listed:
        if not _SAFE_ID.fullmatch(model_id):
            continue
        parsed = parse_model(model_id)
        if parsed is None or parsed.family != pinned.family or parsed.version <= pinned.version:
            continue
        current = best.get(parsed.version)
        if current is None or len(model_id) < len(current):
            best[parsed.version] = model_id
    return [best[version] for version in sorted(best, reverse=True)]


def _http_get(url: str, api_key: str) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url, headers={"x-api-key": api_key, "anthropic-version": API_VERSION}
    )
    last_error = "no attempt made"
    for attempt in range(HTTP_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Never include the request headers: they carry the API key.
            last_error = f"HTTP {exc.code}"
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (OSError, http.client.HTTPException, UnicodeDecodeError,
                json.JSONDecodeError) as exc:
            # OSError covers URLError, timeouts and connection resets.
            last_error = type(exc).__name__
        else:
            if isinstance(body, dict):
                return body
            last_error = "response was not a JSON object"
            break
        time.sleep(2**attempt)
    raise WatchError(f"Models API request failed: {last_error}")


def list_model_ids(api_key: str, fetch: Fetch = _http_get) -> list[str]:
    """Every model id the API lists, following pagination. Never returns a partial list."""
    ids: list[str] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        url = f"{API_URL}?limit={PAGE_LIMIT}"
        if after:
            url += f"&after_id={urllib.parse.quote(after)}"
        page = fetch(url, api_key)
        data = page.get("data")
        if not isinstance(data, list):
            raise WatchError("Models API response has no 'data' list")
        for item in data:
            model_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(model_id, str):
                ids.append(model_id)
        if not page.get("has_more"):
            return ids
        last = page.get("last_id")
        if not isinstance(last, str) or not last:
            raise WatchError("Models API said there are more pages but gave no last_id")
        after = last
    raise WatchError(f"Models API pagination did not finish within {MAX_PAGES} pages")


def pinned_defaults() -> tuple[str, str]:
    """The Anthropic standard and premium defaults, from the real config."""
    from ai_pr_review.config import ReviewConfig

    cfg = ReviewConfig(provider="anthropic").resolve_models()
    if not cfg.model_standard or not cfg.model_premium:
        raise WatchError("could not resolve the pinned Anthropic defaults")
    return cfg.model_standard, cfg.model_premium


def issue_title(kind: str, new_id: str) -> str:
    return f"New Anthropic {kind} model available: {new_id}"


def issue_body(kind: str, pinned_id: str, new_id: str, run_url: str) -> str:
    tier = "AI_MODEL_STANDARD" if kind == "standard" else "AI_MODEL_PREMIUM"
    lines = [
        f"The Anthropic Models API now lists `{new_id}`, which is newer than the pinned "
        f"{kind} default `{pinned_id}`.",
        "",
        "Nothing has changed. This issue was opened by the model watcher, and the defaults "
        "stay as they are until someone bumps them.",
        "",
        "Before bumping the default, work through the model-change verification in CLAUDE.md:",
        "",
        f"- [ ] Read Anthropic's notes for `{new_id}` for breaking API changes (thinking, "
        "tool_choice, sampling parameters, effort levels).",
        "- [ ] Add an explicitly anchored row for it to `config/model-pricing.json`. An unknown "
        "model prices at zero with no warning, and an unanchored older pattern can match it.",
        "- [ ] Check the per-model handling in `ai_pr_review/llm/_config.py` "
        "(`resolve_temperature` and `resolve_effort`), which matches model names by pattern.",
        "- [ ] Verify the Bedrock model ID before touching the `bedrock-proxy` default.",
        "- [ ] Run the `Live Model Canary` workflow with the new model as the default and "
        "record exactly what it did and did not show. It makes billed API calls, so it needs "
        "explicit approval.",
        "- [ ] Update the default tables in README, `docs/configuration.md`, "
        "`docs/architecture-internals.md` and `CLAUDE.md`, and add a CHANGELOG entry that "
        "labels it a behavior change.",
        "",
        f"Anyone who wants to try or pin a model sooner can set `{tier}`.",
    ]
    if run_url:
        lines += ["", f"Opened by the model watcher: {run_url}"]
    return "\n".join(lines) + "\n"


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def _issue_exists(title: str, run: Run) -> bool:
    """True if any issue, open or closed, already has exactly this title.

    Closed counts too, so a model someone decided not to adopt is not reported again.
    """
    result = run(
        ["gh", "issue", "list", "--state", "all", "--search", f'"{title}" in:title',
         "--json", "title", "--limit", "20"]
    )
    if result.returncode != 0:
        raise WatchError("could not search existing issues: " + result.stderr.strip()[:200])
    try:
        found = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise WatchError("could not read the issue search result") from exc
    return any(isinstance(item, dict) and item.get("title") == title for item in found)


def _create_issue(title: str, body: str, run: Run) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(body)
        path = handle.name
    try:
        result = run(["gh", "issue", "create", "--title", title, "--body-file", path])
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise WatchError("could not create the issue: " + result.stderr.strip()[:200])
    print(f"opened: {result.stdout.strip()}")


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetch = _http_get,
    run: Run = _run,
    env: Mapping[str, str] | None = None,
    defaults: tuple[str, str] | None = None,
) -> int:
    args_parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    args_parser.add_argument(
        "--dry-run", action="store_true", help="print what would be opened, open nothing"
    )
    args = args_parser.parse_args(argv)
    environment = os.environ if env is None else env

    api_key = environment.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set, nothing to check.", file=sys.stderr)
        return 2
    try:
        pinned = defaults or pinned_defaults()
        listed = list_model_ids(api_key, fetch)
        server = environment.get("GITHUB_SERVER_URL", "")
        repo = environment.get("GITHUB_REPOSITORY", "")
        run_id = environment.get("GITHUB_RUN_ID", "")
        run_url = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""

        # A watcher that quietly reports "nothing newer" on a broken response is worse than
        # one that fails, so a listing that lacks a pinned default (which includes an empty
        # listing) is an error.
        missing = [model for model in pinned if model not in listed]
        if missing:
            raise WatchError(
                f"pinned default(s) {', '.join(missing)} not in the Models API listing "
                "(retired, no access for this key, or the response shape changed)"
            )
        print(f"Models API lists {len(listed)} models.")
        for (kind, family), pinned_id in zip(_WATCHED, pinned, strict=True):
            newer = newer_models(pinned_id, listed)
            if not newer:
                print(f"{kind}: pinned {pinned_id} is the newest {family} listed.")
                continue
            new_id = newer[0]
            print(f"{kind}: pinned {pinned_id}, newer {family} available: {', '.join(newer)}")
            title = issue_title(kind, new_id)
            if args.dry_run:
                print(f"dry run, would open: {title}")
            elif _issue_exists(title, run):
                print(f"already reported: {title}")
            else:
                _create_issue(title, issue_body(kind, pinned_id, new_id, run_url), run)
    except WatchError as exc:
        print(f"model watch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
