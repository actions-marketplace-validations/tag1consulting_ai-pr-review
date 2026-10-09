"""dep-exists: flag a newly added dependency that its registry does not know.

A language model can invent a package name that looks real. An attacker can then
register that name and ship malicious code under it. This analyzer asks the
public registry whether each newly added dependency exists. It reports:

- High: the registry has no package with that name.
- Medium: the package exists but was first published fewer than 30 days ago
  (npm, PyPI, and crates.io only, the registries that report a creation date).

Scope, on purpose:

- It checks only dependencies added in this diff, found on added lines of a
  direct-dependency manifest. A lockfile is not read.
- It supports npm (``package.json``), PyPI (``requirements*.txt``), crates.io
  (``Cargo.toml``), Packagist (``composer.json``), and RubyGems (``Gemfile``).
- It does not check Go modules. A private Go module returns "not found" from the
  public proxy, so a check would raise false High findings.
- It fails open. A timeout, a rate limit, a redirect, an oversize body, or any
  status other than 200 or 404 gives no finding.
- A repository that uses a private registry is skipped for that ecosystem.

Trust boundary: a package name comes from the diff, so it is untrusted. Each name
must match a strict per-ecosystem pattern before it is placed in a URL. A name
that does not match is skipped. Redirects are never followed, so a registry
cannot send a request to another host.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ai_pr_review.diff.parse import FileDiff, Hunk, parse_diff, split_lines
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_SOURCE = "dep-exists"
_AGENT = "dep-exists"

_HTTP_TIMEOUT = 8.0
_MAX_LOOKUPS = 25
_MAX_BODY_BYTES = 5_000_000
_NEW_PACKAGE_DAYS = 30
_USER_AGENT = "ai-pr-review dep-exists (+https://github.com/tag1consulting/ai-pr-review)"

_NPM = "npm"
_PYPI = "PyPI"
_CRATES = "crates.io"
_PACKAGIST = "Packagist"
_RUBYGEMS = "RubyGems"

# Strict name patterns, checked before a name goes into a URL.
_NAME_PATTERNS: dict[str, re.Pattern[str]] = {
    _NPM: re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$"),
    _PYPI: re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"),
    _CRATES: re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$"),
    # Each separator is required between runs of characters, so there is exactly one
    # way to match and no nested quantifier that can backtrack without bound.
    _PACKAGIST: re.compile(r"^[a-z0-9]+(?:[_.-][a-z0-9]+)*/[a-z0-9]+(?:[_.-][a-z0-9]+)*$"),
    _RUBYGEMS: re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
}
_MAX_NAME_LEN = 214

_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;].*)?$")
_GEM_LINE = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"](.*)$""")
_REQUIREMENTS_FILE = re.compile(r"^requirements.*\.txt$")
_PRIVATE_INDEX_FLAGS = ("--index-url", "--extra-index-url", "-i ", "--find-links", "-f ")
_NON_REGISTRY_NPM_PREFIXES = ("file:", "link:", "workspace:", "git", "http", "github:", "npm:", "portal:", "patch:")


class Lookup(Enum):
    EXISTS = "exists"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Dep:
    ecosystem: str
    name: str
    file: str
    line: int


@dataclass(frozen=True)
class LookupResult:
    status: Lookup
    created: datetime | None = None


# --- reading the diff ----------------------------------------------------


def added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Return ``{path: [(new_line_number, text), ...]}`` for every added line."""
    return _added_from(parse_diff(diff_text))


def _added_from(files: list[FileDiff]) -> dict[str, list[tuple[int, str]]]:
    result: dict[str, list[tuple[int, str]]] = {}
    for file in files:
        if file.new_path is None:
            continue
        added = result.setdefault(file.new_path, [])
        for hunk in file.hunks:
            added.extend((row.new_no, row.text) for row in hunk.rows if row.marker == "+" and row.new_no is not None)
    return result


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _hunks_by_file(diff_text: str) -> dict[str, list[Hunk]]:
    """Each changed file's hunks, keyed by the path the file has after the change."""
    return {f.new_path: f.hunks for f in parse_diff(diff_text) if f.new_path is not None}


def _rebuild_base(new_text: str, hunks: list[Hunk]) -> str | None:
    """Undo a file's hunks to get its text before the change, or None when they do not fit.

    Each hunk's new-side text must match the file. If it does not (a changed line
    ending, a file that is not the one in the diff), the caller treats every
    dependency as new, so an odd diff gets more checking and not less.
    """
    lines = split_lines(new_text)
    out: list[str] = []
    pos = 0
    for hunk in sorted(hunks, key=lambda h: h.new_start):
        idx = hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1
        if idx < pos or lines[idx : idx + hunk.new_count] != hunk.new_side:
            return None
        out += lines[pos:idx] + hunk.old_side
        pos = idx + hunk.new_count
    out += lines[pos:]
    return "\n".join(out) + "\n"


def _declared_names(base_name: str, text: str) -> set[str] | None:
    """Registry dependency names a manifest declares, or None when it cannot be parsed.

    A name counts only when it came from the registry. A dependency that was a local
    path, a workspace member, or a git source and now names a registry version is a
    change of source (dependency confusion), so it must not look already declared.
    """
    names: set[str] = set()
    try:
        if base_name == "package.json":
            data = json.loads(text)
            for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                table = data.get(section) if isinstance(data, dict) else None
                if isinstance(table, dict):
                    names.update(
                        n
                        for n, spec in table.items()
                        if isinstance(spec, str) and not spec.startswith(_NON_REGISTRY_NPM_PREFIXES) and "/" not in spec
                    )
        elif base_name == "composer.json":
            data = json.loads(text)
            if isinstance(data, dict) and not data.get("repositories"):
                for section in ("require", "require-dev"):
                    table = data.get(section)
                    if isinstance(table, dict):
                        names.update(n for n in table if "/" in n)
        elif base_name == "Cargo.toml":
            data = tomllib.loads(text)
            toml_sections = ("dependencies", "dev-dependencies", "build-dependencies")
            tables = [data.get(s) for s in toml_sections]
            target = data.get("target")
            if isinstance(target, dict):
                tables += [p.get(s) for p in target.values() if isinstance(p, dict) for s in toml_sections]
            for table in tables:
                if isinstance(table, dict):
                    names.update(
                        n
                        for n, spec in table.items()
                        if not (isinstance(spec, dict) and any(k in spec for k in ("path", "git", "registry", "package", "workspace")))
                    )
        elif _REQUIREMENTS_FILE.match(base_name):
            if any(flag in text for flag in _PRIVATE_INDEX_FLAGS):
                return names  # the old file used a private index, so its names were not public
            for line in split_lines(text):
                stripped = line.split("#", 1)[0].strip()
                if not stripped or stripped.startswith("-") or "://" in stripped or " @ " in stripped:
                    continue
                match = _REQUIREMENT_NAME.match(stripped)
                if match:
                    names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
        elif base_name == "Gemfile":
            if any("rubygems.org" not in src for src in re.findall(r"""^\s*source\s+['"]([^'"]+)['"]""", text, re.MULTILINE)):
                return names  # the old Gemfile used a private source
            for line in split_lines(text):
                gem = _GEM_LINE.match(line)
                if gem and not re.search(r"\b(?:git|github|path|source|gist|bitbucket):", gem.group(2)):
                    names.add(gem.group(1))
    except (ValueError, tomllib.TOMLDecodeError, AttributeError):
        return None
    return names


_REGISTRY_CONFIG_NAMES = frozenset({".npmrc", ".yarnrc", ".yarnrc.yml"})


def _registry_config_changed(files: list[FileDiff]) -> bool:
    """True when the diff touches a file that can point npm or cargo at a private registry."""
    for file in files:
        for name in (file.old_path, file.new_path):
            if name is None:
                continue
            path = Path(name)
            if path.name in _REGISTRY_CONFIG_NAMES or (path.parent.name == ".cargo" and path.name in ("config", "config.toml")):
                return True
    return False


def _renamed_paths(files: list[FileDiff]) -> set[str]:
    """New paths of files the diff renames or moves."""
    return {f.new_path for f in files if f.renamed and f.new_path is not None}


def _already_declared(path: str, diff_hunks: dict[str, list[Hunk]]) -> set[str]:
    """Names the manifest declared before this diff. Empty when that cannot be worked out."""
    hunks = diff_hunks.get(path)
    if not hunks:
        return set()
    base_text = _rebuild_base(_read(path), hunks)
    if base_text is None:
        return set()
    return _declared_names(Path(path).name, base_text) or set()


def _added_name_lines(added: list[tuple[int, str]], name: str) -> int | None:
    """First added line that declares *name* as a key, or None."""
    needle = re.compile(r'^\s*"?' + re.escape(name) + r'"?\s*[:=]|^\s*\[[\w.-]*dependencies\.' + re.escape(name) + r"\]")
    for line_no, text in added:
        if needle.search(text):
            return line_no
    return None


# --- extracting new dependencies ----------------------------------------


def _deps_from_package_json(path: str, added: list[tuple[int, str]]) -> list[Dep]:
    if _has_private_npm_registry(Path(path).parent):
        return []
    try:
        data = json.loads(_read(path))
    except ValueError as exc:
        logger.warning("[ai-pr-review] dep-exists: cannot parse %r, its new dependencies are not checked: %s", path, exc)
        return []
    if not isinstance(data, dict):
        return []
    deps: list[Dep] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for name, spec in table.items():
            if not isinstance(spec, str) or spec.startswith(_NON_REGISTRY_NPM_PREFIXES) or "/" in spec:
                continue
            line = _added_name_lines(added, name)
            if line is not None:
                deps.append(Dep(_NPM, name, path, line))
    return deps


def _has_private_npm_registry(directory: Path) -> bool:
    for candidate in (directory / ".npmrc", Path(".npmrc")):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"^\s*(?:@[\w.-]+:)?registry\s*=", text, re.MULTILINE):
            return True
    return False


def _deps_from_requirements(path: str, added: list[tuple[int, str]]) -> list[Dep]:
    full = _read(path)
    if any(flag in full for flag in _PRIVATE_INDEX_FLAGS):
        return []
    deps: list[Dep] = []
    for line_no, text in added:
        stripped = text.split("#", 1)[0].strip()
        if not stripped or stripped.startswith("-") or "://" in stripped or " @ " in stripped:
            continue
        match = _REQUIREMENT_NAME.match(stripped)
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            deps.append(Dep(_PYPI, name, path, line_no))
    return deps


def _deps_from_cargo_toml(path: str, added: list[tuple[int, str]]) -> list[Dep]:
    if _has_private_cargo_registry(Path(path).parent):
        return []
    try:
        data = tomllib.loads(_read(path))
    except tomllib.TOMLDecodeError as exc:
        logger.warning("[ai-pr-review] dep-exists: cannot parse %r, its new dependencies are not checked: %s", path, exc)
        return []
    deps: list[Dep] = []
    sections = ("dependencies", "dev-dependencies", "build-dependencies")
    tables: list[dict[str, Any]] = [data[s] for s in sections if isinstance(data.get(s), dict)]
    target = data.get("target")
    if isinstance(target, dict):
        for platform in target.values():
            if isinstance(platform, dict):
                tables += [platform[s] for s in sections if isinstance(platform.get(s), dict)]
    for table in tables:
        for name, spec in table.items():
            if isinstance(spec, dict) and any(k in spec for k in ("path", "git", "registry", "package", "workspace")):
                continue
            line = _added_name_lines(added, name)
            if line is not None:
                deps.append(Dep(_CRATES, name, path, line))
    return deps


def _has_private_cargo_registry(directory: Path) -> bool:
    for candidate in (directory / ".cargo" / "config.toml", Path(".cargo/config.toml"), directory / ".cargo" / "config"):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"^\s*\[(?:registries|source)\b", text, re.MULTILINE):
            return True
    return False


def _deps_from_composer_json(path: str, added: list[tuple[int, str]]) -> list[Dep]:
    try:
        data = json.loads(_read(path))
    except ValueError as exc:
        logger.warning("[ai-pr-review] dep-exists: cannot parse %r, its new dependencies are not checked: %s", path, exc)
        return []
    if not isinstance(data, dict) or data.get("repositories"):
        return []
    deps: list[Dep] = []
    for section in ("require", "require-dev"):
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for name in table:
            if "/" not in name:
                continue  # php, ext-*, lib-*
            line = _added_name_lines(added, name)
            if line is not None:
                deps.append(Dep(_PACKAGIST, name, path, line))
    return deps


def _deps_from_gemfile(path: str, added: list[tuple[int, str]]) -> list[Dep]:
    full = _read(path)
    for source in re.findall(r"""^\s*source\s+['"]([^'"]+)['"]""", full, re.MULTILINE):
        if "rubygems.org" not in source:
            return []
    deps: list[Dep] = []
    for line_no, text in added:
        match = _GEM_LINE.match(text)
        if not match:
            continue
        if re.search(r"\b(?:git|github|path|source|gist|bitbucket):", match.group(2)):
            continue
        deps.append(Dep(_RUBYGEMS, match.group(1), path, line_no))
    return deps


def _is_checked_manifest(base_name: str) -> bool:
    return base_name in ("package.json", "composer.json", "Cargo.toml", "Gemfile") or bool(_REQUIREMENTS_FILE.match(base_name))


def collect_new_deps(changed_files: ChangedFiles, diff_text: str) -> list[Dep]:
    """New direct dependencies on added lines of the changed manifests."""
    files = parse_diff(diff_text)
    by_file = _added_from(files)
    diff_hunks = {f.new_path: f.hunks for f in files if f.new_path is not None}
    deps: list[Dep] = []
    in_diff = {f.path for f in files}
    unreadable = sum(1 for f in files if f.path is None)
    if unreadable:
        # A path the parser could not read (a control character in the name, or an unexpected
        # prefix) cannot be matched to a file. Say so, with a count only: the path is attacker text.
        logger.warning(
            "[ai-pr-review] dep-exists: %d file(s) in the diff have a path that could not be read, so they are not checked",
            unreadable,
        )
    for path in changed_files.manifest_lockfile:
        added = by_file.get(path)
        if path not in in_diff and _is_checked_manifest(Path(path).name):
            # A manifest in the changed-file list with no entry in the diff is a skipped check.
            # Say so, so a path the diff parser could not match cannot turn the check off quietly.
            logger.warning("[ai-pr-review] dep-exists: %r is in the changed-file list but not in the diff, so its dependencies are not checked", path)
        if not added or not Path(path).is_file():
            continue
        base = Path(path).name
        if base == "package.json":
            deps += _deps_from_package_json(path, added)
        elif _REQUIREMENTS_FILE.match(base):
            deps += _deps_from_requirements(path, added)
        elif base == "Cargo.toml":
            deps += _deps_from_cargo_toml(path, added)
        elif base == "composer.json":
            deps += _deps_from_composer_json(path, added)
        elif base == "Gemfile":
            deps += _deps_from_gemfile(path, added)
    # A name the base manifest already declared is a version bump or a move, not a new dependency.
    # If the same change adds, removes or edits registry config, the old manifest's names may
    # have come from a private registry. Trust none of them: check every added dependency.
    # A moved manifest may have left the directory whose registry config its old names relied on.
    trust_base = not _registry_config_changed(files)
    renamed = _renamed_paths(files)
    already = {
        path: _already_declared(path, diff_hunks) if trust_base and path not in renamed else set()
        for path in {d.file for d in deps}
    }
    return [d for d in deps if d.name not in already[d.file]]


# --- registry lookups ----------------------------------------------------


def valid_name(ecosystem: str, name: str) -> bool:
    pattern = _NAME_PATTERNS.get(ecosystem)
    return bool(pattern and len(name) <= _MAX_NAME_LEN and pattern.match(name))


def _url(ecosystem: str, name: str) -> str:
    if ecosystem == _NPM:
        return "https://registry.npmjs.org/" + quote(name, safe="@").replace("/", "%2F")
    if ecosystem == _PYPI:
        return f"https://pypi.org/pypi/{quote(name, safe='')}/json"
    if ecosystem == _CRATES:
        return f"https://crates.io/api/v1/crates/{quote(name, safe='')}"
    if ecosystem == _PACKAGIST:
        vendor, package = name.split("/", 1)
        return f"https://repo.packagist.org/p2/{quote(vendor, safe='')}/{quote(package, safe='')}.json"
    return f"https://rubygems.org/api/v1/gems/{quote(name, safe='')}.json"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _created(ecosystem: str, payload: object) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    if ecosystem == _NPM:
        times = payload.get("time")
        return _parse_time(times.get("created")) if isinstance(times, dict) else None
    if ecosystem == _CRATES:
        crate = payload.get("crate")
        return _parse_time(crate.get("created_at")) if isinstance(crate, dict) else None
    if ecosystem == _PYPI:
        releases = payload.get("releases")
        if not isinstance(releases, dict):
            return None
        stamps = [
            t
            for files in releases.values() if isinstance(files, list)
            for f in files if isinstance(f, dict)
            if (t := _parse_time(f.get("upload_time_iso_8601"))) is not None
        ]
        return min(stamps) if stamps else None
    return None


def _read_capped(response: httpx.Response) -> bytes | None:
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > _MAX_BODY_BYTES:
            return None
    return bytes(body)


def lookup(client: httpx.Client, ecosystem: str, name: str) -> LookupResult:
    """Ask the registry about one package. Never raises. Unknown means no finding."""
    try:
        with client.stream("GET", _url(ecosystem, name), headers={"Accept": "application/json"}) as response:
            if response.status_code == 404:
                return LookupResult(Lookup.MISSING)
            if response.status_code != 200:
                return LookupResult(Lookup.UNKNOWN)
            body = _read_capped(response)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.debug("dep-exists: lookup of %s %s failed: %s", ecosystem, name, exc)
        return LookupResult(Lookup.UNKNOWN)
    if body is None:
        return LookupResult(Lookup.EXISTS)
    try:
        payload = json.loads(body)
    except ValueError:
        return LookupResult(Lookup.EXISTS)
    return LookupResult(Lookup.EXISTS, _created(ecosystem, payload))


# --- findings --------------------------------------------------------------


def _finding(dep: Dep, result: LookupResult, now: datetime) -> Finding | None:
    if result.status is Lookup.MISSING:
        return Finding(
            severity="High",
            confidence=85,
            source=_SOURCE,
            agent=_AGENT,
            file=dep.file,
            line=dep.line,
            category="other",
            finding=(
                f"`{dep.name}` is added as a dependency, but the {dep.ecosystem} registry has no "
                "package with this name. A model can invent a name that looks real, and an "
                "attacker can register a missing name and publish malicious code under it."
            ),
            remediation=(
                "Check the spelling and where the package comes from. If it is a private package, "
                "set up the private registry. Otherwise remove the dependency."
            ),
        )
    if result.status is Lookup.EXISTS and result.created is not None:
        age_days = (now - result.created).days
        if 0 <= age_days < _NEW_PACKAGE_DAYS:
            return Finding(
                severity="Medium",
                confidence=65,
                source=_SOURCE,
                agent=_AGENT,
                file=dep.file,
                line=dep.line,
                category="other",
                finding=(
                    f"`{dep.name}` was first published on {dep.ecosystem} {age_days} days ago. "
                    "A very new package has little history, and a lookalike of a known name "
                    "is a common way to deliver malicious code."
                ),
                remediation="Confirm that this is the package you mean and that its maintainers are known.",
            )
    return None


def _run_dep_exists(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Check each newly added dependency against its public registry."""
    try:
        diff_text = diff_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("[ai-pr-review] dep-exists: cannot read the diff: %s", exc)
        return []
    deps = collect_new_deps(changed_files, diff_text)
    seen: set[tuple[str, str]] = set()
    unique: list[Dep] = []
    for dep in deps:
        key = (dep.ecosystem, dep.name)
        if key in seen or not valid_name(dep.ecosystem, dep.name):
            continue
        seen.add(key)
        unique.append(dep)
    if not unique:
        return []
    if len(unique) > _MAX_LOOKUPS:
        logger.warning("[ai-pr-review] dep-exists: checking %d of %d new dependencies", _MAX_LOOKUPS, len(unique))
        unique = unique[:_MAX_LOOKUPS]

    now = datetime.now(UTC)
    findings: list[Finding] = []
    unknown = 0
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False, headers={"User-Agent": _USER_AGENT}) as client:
        for dep in unique:
            result = lookup(client, dep.ecosystem, dep.name)
            if result.status is Lookup.UNKNOWN:
                unknown += 1
            finding = _finding(dep, result, now)
            if finding is not None:
                findings.append(finding)
    if unknown:
        # A lookup that fails open adds no finding, so a registry that cannot be
        # reached would look like a clean run. Say so once at warning level.
        logger.warning(
            "[ai-pr-review] dep-exists: %d of %d lookups got no usable answer from the registry "
            "(timeout, rate limit, or error), so those dependencies are not checked",
            unknown, len(unique),
        )
    return findings
