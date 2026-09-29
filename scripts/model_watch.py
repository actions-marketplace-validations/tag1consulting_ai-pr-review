#!/usr/bin/env python3
"""Flag models that are newer than this repo's pinned default models.

Used by .github/workflows/model-watch.yml. It only reads: for each provider that has an
API key set (Anthropic, OpenAI, Google), it lists the models that provider's models API
offers, compares the newest model in each watched family with the pinned `standard` and
`premium` defaults, and opens ONE issue per newer model. It never changes a default.
Bumping a default needs a pricing row, the per-model effort, thinking and temperature
handling, and a live canary run, so a person does it.

Watched families, by provider (standard, premium):
  Anthropic  claude-sonnet-*          claude-opus-*
  OpenAI     the pinned model's tier  the pinned model's tier, plus any tier in a newer
             version than both pinned defaults (OpenAI renames tiers between versions:
             GPT-5.6 had luna/terra/sol, GPT-6 has luna/sol/astra)
  Google     gemini-X.Y-flash-lite    gemini-X.Y-flash, plus a stable gemini-X.Y-pro at
                                      or above the pinned flash version

Exit codes: 0 finished (an issue may have been opened), 1 a real failure for at least
one configured provider (API error, unparseable default), 2 not configured (no API key
for any provider).
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
OPENAI_API_URL = "https://api.openai.com/v1/models"
GOOGLE_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
PAGE_LIMIT = 1000
MAX_PAGES = 20
HTTP_ATTEMPTS = 3

# A model id must match its provider's safe pattern before it is ever put in an issue
# title or body: the ids come from an external API response.
_SAFE_ID = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,63}")
# claude-<family>-<major>[-<minor>][-<yyyymmdd>], with an optional provider prefix such
# as "us.anthropic." in front.
_MODEL = re.compile(r"(?:^|\.)claude-(sonnet|opus)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?\Z")

_OPENAI_SAFE_ID = re.compile(r"gpt-[a-z0-9][a-z0-9.-]{0,63}")
# gpt-<major>[.<minor>]-<tier>[-<yyyy-mm-dd>], where the tier is one word (luna, terra,
# sol, astra). Tier names are not stable across versions, so any one-word tier matches.
_OPENAI_MODEL = re.compile(r"\Agpt-(\d+)(?:\.(\d{1,2}))?-([a-z]+)(?:-\d{4}-\d{2}-\d{2})?\Z")
# One-word suffixes that name a size variant or a different product, not a tier.
_OPENAI_NOT_A_TIER = frozenset({
    "mini", "nano", "pro", "codex", "chat", "search", "audio", "realtime", "transcribe",
    "tts", "image", "embedding", "preview", "latest", "cyber",
})

_GOOGLE_SAFE_ID = re.compile(r"gemini-[a-z0-9][a-z0-9.-]{0,63}")
# gemini-<major>[.<minor>]-flash-lite or -flash, exactly. Previews, dated previews,
# -tts, -live, -image and similar variants do not match, so only a stable model is
# ever reported.
_GOOGLE_MODEL = re.compile(r"\Agemini-(\d+)(?:\.(\d{1,2}))?-(flash-lite|flash|pro)\Z")

# (kind of default, family it belongs to)
_WATCHED = (("standard", "sonnet"), ("premium", "opus"))

Fetch = Callable[[str, str], Mapping[str, Any]]
Run = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Headers = Callable[[str], dict[str, str]]


class WatchError(Exception):
    """A failure the workflow should surface as a red run."""


@dataclass(frozen=True)
class Parsed:
    family: str
    version: tuple[int, int]


@dataclass(frozen=True)
class Provider:
    """How to list, read and report one provider's models."""

    name: str  # the ReviewConfig provider value
    label: str  # used in issue titles, so it must never change for an existing provider
    env_var: str
    watched: tuple[tuple[str, str], ...]
    parse: Callable[[str], Parsed | None]
    safe_id: re.Pattern[str]
    list_ids: Callable[[str, Fetch], list[str]]
    headers: Headers
    api_notes: str  # what to read in the provider's release notes
    quirk_functions: str  # the _config.py functions that match this provider's models
    canary_step: str
    # (kind, family): a stable model of another family that also counts as a candidate for
    # that kind when its version is at or above the pinned one (Google: a stable Pro for
    # the premium slot, which is a Flash model only because no stable Pro existed).
    alternates: tuple[tuple[str, str], ...] = ()
    # Report models in a version newer than every pinned default whose tier is not a
    # pinned tier (OpenAI renames tiers between versions).
    other_tiers: bool = False


def _parse_with(pattern: re.Pattern[str], model_id: str) -> Parsed | None:
    match = pattern.search(model_id.lower())
    if not match:
        return None
    return Parsed(match.group(3), (int(match.group(1)), int(match.group(2) or 0)))


def parse_model(model_id: str) -> Parsed | None:
    """Return the Sonnet or Opus family and (major, minor) version, or None."""
    match = _MODEL.search(model_id.lower())
    if not match:
        return None
    return Parsed(match.group(1), (int(match.group(2)), int(match.group(3) or 0)))


def parse_openai_model(model_id: str) -> Parsed | None:
    """Return the tier (luna, sol, ...) and (major, minor) version, or None."""
    parsed = _parse_with(_OPENAI_MODEL, model_id)
    if parsed is None or parsed.family in _OPENAI_NOT_A_TIER:
        return None
    return parsed


def parse_google_model(model_id: str) -> Parsed | None:
    """Return the flash-lite, flash or pro family and (major, minor) version, or None."""
    return _parse_with(_GOOGLE_MODEL, model_id)


def newer_models(
    pinned_id: str, listed: Sequence[str], provider: Provider | None = None
) -> list[str]:
    """Ids in the pinned model's family that are a newer version, newest first.

    One id per version, preferring the shortest (the undated alias).
    """
    provider = provider or ANTHROPIC
    pinned = provider.parse(pinned_id)
    if pinned is None:
        raise WatchError(
            f"cannot read a watched {provider.label} model version from the pinned model "
            f"{pinned_id!r}"
        )
    best: dict[tuple[int, int], str] = {}
    for model_id in listed:
        if not provider.safe_id.fullmatch(model_id):
            continue
        parsed = provider.parse(model_id)
        if parsed is None or parsed.family != pinned.family or parsed.version <= pinned.version:
            continue
        current = best.get(parsed.version)
        if current is None or len(model_id) < len(current):
            best[parsed.version] = model_id
    return [best[version] for version in sorted(best, reverse=True)]


def _best_per_key(
    listed: Sequence[str], provider: Provider, keep: Callable[[Parsed], bool]
) -> dict[tuple[tuple[int, int], str], str]:
    """One safe id per (version, family) that `keep` accepts, preferring the shortest
    (the undated alias)."""
    best: dict[tuple[tuple[int, int], str], str] = {}
    for model_id in listed:
        if not provider.safe_id.fullmatch(model_id):
            continue
        parsed = provider.parse(model_id)
        if parsed is None or not keep(parsed):
            continue
        key = (parsed.version, parsed.family)
        current = best.get(key)
        if current is None or len(model_id) < len(current):
            best[key] = model_id
    return best


def alternate_models(pinned_id: str, family: str, listed: Sequence[str], provider: Provider) -> list[str]:
    """Stable `family` ids at or above the pinned model's version, newest first."""
    pinned = provider.parse(pinned_id)
    if pinned is None:
        raise WatchError(f"cannot read a watched {provider.label} model version from {pinned_id!r}")
    best = _best_per_key(
        listed, provider, lambda p: p.family == family and p.version >= pinned.version
    )
    return [best[key] for key in sorted(best, reverse=True)]


def other_tier_models(pinned_ids: Sequence[str], listed: Sequence[str], provider: Provider) -> list[str]:
    """Ids in a version newer than every pinned default, in a tier no default uses.
    Newest first."""
    pinned = [provider.parse(model_id) for model_id in pinned_ids]
    if any(p is None for p in pinned):
        raise WatchError(f"cannot read a watched {provider.label} model version from {pinned_ids!r}")
    newest = max(p.version for p in pinned if p is not None)
    tiers = {p.family for p in pinned if p is not None}
    best = _best_per_key(listed, provider, lambda p: p.version > newest and p.family not in tiers)
    return [best[key] for key in sorted(best, reverse=True)]


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key, "anthropic-version": API_VERSION}


def _openai_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _google_headers(api_key: str) -> dict[str, str]:
    return {"x-goog-api-key": api_key}


def _http_get(url: str, api_key: str, headers: Headers = _anthropic_headers) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers=headers(api_key))
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
    """Every Anthropic model id, following pagination. Never returns a partial list."""
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


def list_openai_model_ids(api_key: str, fetch: Fetch) -> list[str]:
    """Every OpenAI model id. The endpoint is not paginated, so a response that says
    there is more is an error rather than a silently partial list."""
    page = fetch(OPENAI_API_URL, api_key)
    data = page.get("data")
    if not isinstance(data, list):
        raise WatchError("OpenAI models API response has no 'data' list")
    if page.get("has_more"):
        raise WatchError("OpenAI models API reported more pages, which this watcher cannot follow")
    return [
        item["id"] for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def list_google_model_ids(api_key: str, fetch: Fetch) -> list[str]:
    """Every Gemini API model id, without the "models/" prefix, following pagination.
    Never returns a partial list."""
    ids: list[str] = []
    token: str | None = None
    for _ in range(MAX_PAGES):
        url = f"{GOOGLE_API_URL}?pageSize={PAGE_LIMIT}"
        if token:
            url += f"&pageToken={urllib.parse.quote(token)}"
        page = fetch(url, api_key)
        models = page.get("models")
        if not isinstance(models, list):
            raise WatchError("Gemini models API response has no 'models' list")
        for item in models:
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str):
                ids.append(name.removeprefix("models/"))
        next_token = page.get("nextPageToken")
        if not next_token:
            return ids
        if not isinstance(next_token, str):
            raise WatchError("Gemini models API returned a non-string nextPageToken")
        token = next_token
    raise WatchError(f"Gemini models API pagination did not finish within {MAX_PAGES} pages")


ANTHROPIC = Provider(
    name="anthropic",
    label="Anthropic",
    env_var="ANTHROPIC_API_KEY",
    watched=_WATCHED,
    parse=parse_model,
    safe_id=_SAFE_ID,
    list_ids=list_model_ids,
    headers=_anthropic_headers,
    api_notes="thinking, tool_choice, sampling parameters, effort levels",
    quirk_functions="`resolve_temperature` and `resolve_effort`",
    canary_step=(
        "- [ ] Run the `Live Model Canary` workflow with the new model as the default and "
        "record exactly what it did and did not show. It makes billed API calls, so it needs "
        "explicit approval."
    ),
)

_LOCAL_CANARY_STEP = (
    "- [ ] Run `tests/canary/live_model_canary.py` locally with `{env_var}` set and the new "
    "model in `PROVIDER_MODELS` (the scheduled `Live Model Canary` workflow has only an "
    "Anthropic key). Set `CANARY_OUTPUT_DIR` and read the saved reviews, not only the stop "
    "reasons, and record exactly what it did and did not show. It makes billed API calls, so "
    "it needs explicit approval."
)

OPENAI = Provider(
    name="openai",
    label="OpenAI",
    env_var="OPENAI_API_KEY",
    watched=(("standard", "luna"), ("premium", "sol")),
    parse=parse_openai_model,
    safe_id=_OPENAI_SAFE_ID,
    list_ids=list_openai_model_ids,
    headers=_openai_headers,
    api_notes="reasoning effort defaults, accepted sampling parameters, max_completion_tokens",
    quirk_functions="`resolve_temperature`",
    canary_step=_LOCAL_CANARY_STEP.format(env_var="OPENAI_API_KEY"),
    other_tiers=True,
)

GOOGLE = Provider(
    name="google",
    label="Google",
    env_var="GOOGLE_API_KEY",
    watched=(("standard", "flash-lite"), ("premium", "flash")),
    parse=parse_google_model,
    safe_id=_GOOGLE_SAFE_ID,
    list_ids=list_google_model_ids,
    headers=_google_headers,
    api_notes="thinking levels and their defaults, temperature guidance, maxOutputTokens",
    quirk_functions="`resolve_temperature` and `resolve_gemini_thinking_level`",
    canary_step=_LOCAL_CANARY_STEP.format(env_var="GOOGLE_API_KEY"),
    alternates=(("premium", "pro"),),
)

PROVIDERS: tuple[Provider, ...] = (ANTHROPIC, OPENAI, GOOGLE)


def pinned_defaults(provider: Provider | None = None) -> tuple[str, str]:
    """A provider's standard and premium defaults, from the real config."""
    from ai_pr_review.config import ReviewConfig

    provider = provider or ANTHROPIC
    cfg = ReviewConfig(provider=provider.name).resolve_models()
    if not cfg.model_standard or not cfg.model_premium:
        raise WatchError(f"could not resolve the pinned {provider.label} defaults")
    return cfg.model_standard, cfg.model_premium


NEW_TIER = "new-tier"


def issue_title(kind: str, new_id: str, provider: Provider | None = None) -> str:
    label = (provider or ANTHROPIC).label
    if kind == NEW_TIER:
        return f"New {label} model tier available: {new_id}"
    return f"New {label} {kind} model available: {new_id}"


def issue_body(
    kind: str, pinned_id: str, new_id: str, run_url: str, provider: Provider | None = None
) -> str:
    provider = provider or ANTHROPIC
    if kind == NEW_TIER:
        tier = "AI_MODEL_STANDARD` or `AI_MODEL_PREMIUM"
        intro = (
            f"The {provider.label} Models API now lists `{new_id}`, in a newer version than the "
            f"pinned defaults ({pinned_id}) and in a tier neither default uses. Decide whether "
            "it should become the standard or premium default, or neither."
        )
    else:
        tier = "AI_MODEL_STANDARD" if kind == "standard" else "AI_MODEL_PREMIUM"
        intro = (
            f"The {provider.label} Models API now lists `{new_id}`, a candidate to replace the "
            f"pinned {kind} default `{pinned_id}`."
        )
    lines = [
        intro,
        "",
        "Nothing has changed. This issue was opened by the model watcher, and the defaults "
        "stay as they are until someone bumps them.",
        "",
        "Before bumping the default, work through the model-change verification in CLAUDE.md:",
        "",
        f"- [ ] Read {provider.label}'s notes for `{new_id}` for breaking API changes "
        f"({provider.api_notes}).",
        "- [ ] Add an explicitly anchored row for it to `config/model-pricing.json`. An unknown "
        "model prices at zero with no warning, and an unanchored older pattern can match it.",
        "- [ ] Check the per-model handling in `ai_pr_review/llm/_config.py` "
        f"({provider.quirk_functions}), which matches model names by pattern.",
    ]
    if provider is ANTHROPIC:
        lines.append("- [ ] Verify the Bedrock model ID before touching the `bedrock-proxy` default.")
    lines += [
        provider.canary_step,
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


def _default_fetch(provider: Provider) -> Fetch:
    def fetch(url: str, api_key: str) -> Mapping[str, Any]:
        return _http_get(url, api_key, provider.headers)

    return fetch


def _check_provider(
    provider: Provider,
    api_key: str,
    pinned: tuple[str, str],
    fetch: Fetch,
    run: Run,
    *,
    dry_run: bool,
    run_url: str,
) -> None:
    listed = provider.list_ids(api_key, fetch)
    # A watcher that quietly reports "nothing newer" on a broken response is worse than
    # one that fails, so a listing that lacks a pinned default (which includes an empty
    # listing) is an error.
    missing = [model for model in pinned if model not in listed]
    if missing:
        raise WatchError(
            f"pinned default(s) {', '.join(missing)} not in the {provider.label} Models API "
            "listing (retired, no access for this key, or the response shape changed)"
        )
    print(f"{provider.label} Models API lists {len(listed)} models.")
    for (kind, _), pinned_id in zip(provider.watched, pinned, strict=True):
        parsed = provider.parse(pinned_id)
        family = parsed.family if parsed else "?"
        newer = newer_models(pinned_id, listed, provider)
        if not newer:
            print(f"{provider.label} {kind}: pinned {pinned_id} is the newest {family} listed.")
            continue
        new_id = newer[0]
        print(
            f"{provider.label} {kind}: pinned {pinned_id}, newer {family} available: "
            f"{', '.join(newer)}"
        )
        _report(provider, kind, pinned_id, new_id, run, dry_run=dry_run, run_url=run_url)
    for kind, family in provider.alternates:
        pinned_id = pinned[[k for k, _ in provider.watched].index(kind)]
        for new_id in alternate_models(pinned_id, family, listed, provider)[:1]:
            print(f"{provider.label} {kind}: stable {family} model available: {new_id}")
            _report(provider, kind, pinned_id, new_id, run, dry_run=dry_run, run_url=run_url)
    if provider.other_tiers:
        for new_id in other_tier_models(pinned, listed, provider):
            print(f"{provider.label}: newer version in an unwatched tier: {new_id}")
            _report(
                provider, NEW_TIER, ", ".join(f"`{p}`" for p in pinned), new_id, run,
                dry_run=dry_run, run_url=run_url,
            )


def _report(
    provider: Provider, kind: str, pinned_id: str, new_id: str, run: Run, *,
    dry_run: bool, run_url: str,
) -> None:
    title = issue_title(kind, new_id, provider)
    if dry_run:
        print(f"dry run, would open: {title}")
    elif _issue_exists(title, run):
        print(f"already reported: {title}")
    else:
        _create_issue(title, issue_body(kind, pinned_id, new_id, run_url, provider), run)


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetch | None = None,
    run: Run = _run,
    env: Mapping[str, str] | None = None,
    defaults: tuple[str, str] | Mapping[str, tuple[str, str]] | None = None,
) -> int:
    """`fetch`, when given, is used for every provider (tests). `defaults` is either the
    Anthropic pair or a mapping of provider name to pair."""
    args_parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    args_parser.add_argument(
        "--dry-run", action="store_true", help="print what would be opened, open nothing"
    )
    args = args_parser.parse_args(argv)
    environment = os.environ if env is None else env
    if isinstance(defaults, tuple):
        defaults = {ANTHROPIC.name: defaults}

    server = environment.get("GITHUB_SERVER_URL", "")
    repo = environment.get("GITHUB_REPOSITORY", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else ""

    configured = 0
    failed = 0
    for provider in PROVIDERS:
        api_key = environment.get(provider.env_var, "")
        if not api_key:
            print(f"{provider.env_var} is not set, skipping {provider.label}.", file=sys.stderr)
            continue
        configured += 1
        try:
            pinned = (defaults or {}).get(provider.name) or pinned_defaults(provider)
            _check_provider(
                provider, api_key, pinned, fetch or _default_fetch(provider), run,
                dry_run=args.dry_run, run_url=run_url,
            )
        except WatchError as exc:
            # One provider's failure must not hide the others' results.
            print(f"model watch failed for {provider.label}: {exc}", file=sys.stderr)
            failed += 1
    if configured == 0:
        print("No provider API key is set, nothing to check.", file=sys.stderr)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
