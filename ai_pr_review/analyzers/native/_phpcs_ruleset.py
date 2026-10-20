"""Ruleset parse, allowlist and build code for the phpcs analyzer.

Reads a repo phpcs ruleset as data, keeps an allowlisted subset, and writes the
trusted ruleset that phpcs receives (#1042). Split out of `phpcs.py` as a pure
move. See the trust boundary note in `phpcs.py`.
"""

from __future__ import annotations

import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ai_pr_review.analyzers.native._phpcs_patterns import _relative_pattern_matches

logger = logging.getLogger(__name__)

# phpcs's own auto-discovery order (Config::CONFIG_FILENAMES, phpcs 4.0.4).
_RULESET_NAMES = (".phpcs.xml", "phpcs.xml", ".phpcs.xml.dist", "phpcs.xml.dist")
_MAX_RULESET_BYTES = 256 * 1024
_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

# "Standard", "Standard.Category", "Standard.Category.Sniff", or
# "Standard.Category.Sniff.Code". A slash or a "../" segment can never match.
# That alone is not enough: phpcs resolves a ref that is not a sniff code with
# `is_file()` and `is_dir()` against its working directory and then loads it as
# a ruleset, so `Generic.evil.xml` or `Generic.evildir` could make phpcs load
# PHP from the workspace. `_ref_ok` rejects those shapes, and phpcs also runs
# from an empty directory (`_trusted_cwd`), so a ref cannot resolve to a
# workspace path even if a shape gets through.
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_]+){0,3}$")
_FILE_LIKE_SUFFIXES = frozenset({"xml", "php", "dist", "inc", "yml", "yaml", "json", "txt"})
_PROP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# phpcs treats exclude patterns as regex fragments (`*` becomes `.*`). Only
# plain path characters, `|`, a leading `^`, a trailing `$`, and escaped `.`,
# `-`, `/`, `,` are accepted. No groups, classes, or quantifiers other than
# `*`, and at most three `*`. The copy handed to phpcs is therefore cheap for
# PCRE. The copy applied here is matched without a backtracking regex (see
# `_relative_pattern_matches`), because a hostile repo controls both the
# pattern and the file names (ReDoS).
_PATTERN_ESCAPE_RE = re.compile(r"\\[.\-/,]")
_PATTERN_BODY_RE = re.compile(r"^\^?[A-Za-z0-9_./*|,\-]*\$?$")
_MAX_PATTERN_STARS = 3
_PATTERN_TYPES = frozenset({"absolute", "relative"})


@dataclass(frozen=True)
class _RepoRuleset:
    """The allowlisted subset of a repo phpcs ruleset (data only)."""

    name: str
    files: tuple[str, ...]
    patterns: tuple[tuple[str, str], ...]
    rules: tuple[ET.Element, ...]
    has_standard_ref: bool
    missing_standards: tuple[str, ...]


def _ref_ok(ref: str) -> bool:
    """Is *ref* a standard name or sniff code, and not a path in the workspace?"""
    if not _REF_RE.match(ref):
        return False
    if ref.rsplit(".", 1)[-1].lower() in _FILE_LIKE_SUFFIXES and "." in ref:
        return False
    # A backstop for any shape the checks above miss. A name that exists as a
    # file, directory or symlink in the workspace is never a sniff code.
    return not (Path(ref).exists() or Path(ref).is_symlink())


def _pattern_ok(pattern: str) -> bool:
    if not pattern or len(pattern) > 200 or pattern.count("*") > _MAX_PATTERN_STARS:
        return False
    # `^` and `$` are accepted only at the very start and end of an alternative.
    if any(part.count("^") > 1 or part.count("$") > 1 for part in pattern.split("|")):
        return False
    return bool(_PATTERN_BODY_RE.match(_PATTERN_ESCAPE_RE.sub("", pattern)))


def _path_ok(path: str) -> bool:
    if not path or len(path) > 300 or "\x00" in path or path.startswith("/") or "\\" in path:
        return False
    return ".." not in path.split("/")


def _normalize_scope_entry(text: str) -> str | None:
    """Return a clean relative `<file>` entry, "." for the whole tree, or None if unusable."""
    if not _path_ok(text):
        return None
    entry = posixpath.normpath(text)
    if entry == ".":
        return "."
    return None if entry.startswith("..") or entry.startswith("/") else entry


_MAX_DROP_LOGS = 5


class _Drops:
    """Count and log the ruleset entries dropped as outside the allowed subset.

    A dropped exclude means more files get linted than the repo intended, so
    every drop is visible. Only the first few are logged one by one, then
    `summary()` reports the total.
    """

    def __init__(self, ruleset_name: str) -> None:
        self.name = ruleset_name
        self.count = 0

    def note(self, kind: str, value: str) -> None:
        self.count += 1
        if self.count <= _MAX_DROP_LOGS:
            logger.warning(
                "[ai-pr-review] WARNING: %s: dropping %s %r (not in the allowed subset).",
                self.name, kind, value[:80],
            )

    def summary(self) -> None:
        if self.count > _MAX_DROP_LOGS:
            logger.warning(
                "[ai-pr-review] WARNING: %s: dropped %d entries in total, showed %d.",
                self.name, self.count, _MAX_DROP_LOGS,
            )


def _find_repo_ruleset() -> Path | None:
    """Return the first repo ruleset in the working directory, in phpcs's own order."""
    for name in _RULESET_NAMES:
        candidate = Path(name)
        # A symlink could point at any file on the runner. Never follow one.
        if candidate.is_symlink():
            logger.warning("[ai-pr-review] WARNING: ignoring symlinked phpcs ruleset %s.", name)
            continue
        if candidate.is_file():
            return candidate
    return None


def _keep_properties(src: ET.Element, kept_rule: ET.Element, ref: str, drops: _Drops) -> None:
    props = ET.SubElement(kept_rule, "properties")
    for prop in src:
        name = prop.get("name", "")
        value = prop.get("value", "")
        if prop.tag != "property" or not _PROP_NAME_RE.match(name) or len(value) > 200:
            drops.note(f"{ref} property", name or prop.tag)
            continue
        attrs = {"name": name}
        if "value" in prop.attrib:
            attrs["value"] = value
        if prop.get("type") == "array":
            attrs["type"] = "array"
        if prop.get("extend") in ("true", "false"):
            attrs["extend"] = prop.get("extend", "")
        new_prop = ET.SubElement(props, "property", attrs)
        for el in prop:
            el_value = el.get("value", "")
            el_key = el.get("key", "")
            if el.tag == "element" and len(el_value) <= 200 and len(el_key) <= 200:
                el_attrs = {"value": el_value}
                if "key" in el.attrib:
                    el_attrs["key"] = el_key
                ET.SubElement(new_prop, "element", el_attrs)
            else:
                drops.note(f"{ref} property {name} element", el_value)


def _keep_rule(
    rule: ET.Element, installed: set[str], drops: _Drops
) -> tuple[ET.Element | None, str | None]:
    """Rebuild one `<rule>` from its allowlisted parts.

    Returns (rule, None) when kept, (None, standard) when it names a standard
    that is not installed, and (None, None) when it is dropped as unsafe.
    """
    ref = rule.get("ref", "")
    if not _ref_ok(ref):
        drops.note("rule ref", ref)
        return None, None
    standard = ref.split(".", 1)[0]
    if standard not in installed:
        return None, standard

    kept = ET.Element("rule", ref=ref)
    for child in rule:
        text = (child.text or "").strip()
        if child.tag == "exclude":
            if _ref_ok(child.get("name", "")):
                ET.SubElement(kept, "exclude", name=child.get("name", ""))
            else:
                drops.note(f"{ref} exclude", child.get("name", ""))
        elif child.tag in ("exclude-pattern", "include-pattern"):
            kind = child.get("type", "absolute")
            if _pattern_ok(text) and kind in _PATTERN_TYPES:
                ET.SubElement(kept, child.tag, {"type": kind}).text = text
            else:
                drops.note(f"{ref} {child.tag}", text)
        elif child.tag == "severity":
            # `str.isdigit()` is true for characters such as "²" that `int()` rejects.
            if re.fullmatch(r"[0-9]{1,2}", text) and int(text) <= 10:
                ET.SubElement(kept, "severity").text = text
            else:
                drops.note(f"{ref} severity", text)
        elif child.tag == "type":
            if text in ("error", "warning"):
                ET.SubElement(kept, "type").text = text
            else:
                drops.note(f"{ref} type", text)
        elif child.tag == "properties":
            _keep_properties(child, kept, ref, drops)
        else:
            drops.note(f"{ref} <{child.tag}>", "")
    return kept, None


def _parse_repo_ruleset(path: Path, installed: set[str]) -> _RepoRuleset | None:
    """Read *path* as data and keep only the allowlisted parts. None when unusable."""
    try:
        with path.open("rb") as fh:
            data = fh.read(_MAX_RULESET_BYTES + 1)
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: could not read %s: %s", path, exc)
        return None
    if len(data) > _MAX_RULESET_BYTES:
        logger.warning("[ai-pr-review] WARNING: %s is larger than %d bytes; not using it.", path, _MAX_RULESET_BYTES)
        return None
    # A NUL byte means UTF-16/32, which would hide a DOCTYPE from the byte check.
    if b"\x00" in data or _DOCTYPE_RE.search(data):
        logger.warning("[ai-pr-review] WARNING: %s has a DOCTYPE, an ENTITY declaration, or a non-UTF-8 encoding; not using it.", path)
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        logger.warning("[ai-pr-review] WARNING: %s is not valid XML: %s", path, exc)
        return None
    if root.tag != "ruleset":
        logger.warning("[ai-pr-review] WARNING: %s has no <ruleset> root; not using it.", path)
        return None

    files: list[str] = []
    scope_all = False
    patterns: list[tuple[str, str]] = []
    rules: list[ET.Element] = []
    missing: list[str] = []
    has_standard_ref = False
    drops = _Drops(path.name)
    for child in root:
        text = (child.text or "").strip()
        if child.tag == "file":
            entry = _normalize_scope_entry(text)
            if entry is None:
                drops.note("file", text)
            elif entry == ".":
                # `<file>.</file>` means the whole tree: no scope limit.
                files = []
                scope_all = True
            elif not scope_all:
                files.append(entry)
        elif child.tag == "exclude-pattern":
            kind = child.get("type", "absolute")
            if _pattern_ok(text) and kind in _PATTERN_TYPES:
                patterns.append((text, kind))
            else:
                drops.note("exclude-pattern", text)
        elif child.tag == "rule":
            has_standard_ref = True
            kept, absent = _keep_rule(child, installed, drops)
            if kept is not None:
                rules.append(kept)
            elif absent is not None and absent not in missing:
                missing.append(absent)
    drops.summary()
    return _RepoRuleset(
        name=path.name,
        files=tuple(files),
        patterns=tuple(patterns),
        rules=tuple(rules),
        has_standard_ref=has_standard_ref,
        missing_standards=tuple(missing),
    )


def _in_ruleset_scope(path: str, ruleset: _RepoRuleset) -> bool:
    """Is *path* inside the ruleset's `<file>` scope and outside its relative excludes?

    phpcs ignores `<file>` when files are given on the command line, and it
    does not apply `type="relative"` exclude patterns to such files either
    (both verified against phpcs 4.0.4), so both are applied here. A ruleset
    with no `<file>` entry has no scope limit. Absolute exclude patterns are
    not applied here, because phpcs applies them itself from the generated
    ruleset.
    """
    norm = posixpath.normpath(path)
    if norm.startswith("/"):
        try:
            norm = str(Path(norm).relative_to(Path.cwd()))
        except ValueError:
            return not ruleset.files
    if any(kind == "relative" and _relative_pattern_matches(pattern, norm) for pattern, kind in ruleset.patterns):
        return False
    if not ruleset.files:
        return True
    return any(norm == entry or norm.startswith(entry + "/") for entry in ruleset.files)


def _build_ruleset_xml(base_refs: tuple[str, ...], ruleset: _RepoRuleset | None) -> str:
    """Write the trusted ruleset. ElementTree escapes every string from the repo."""
    root = ET.Element("ruleset", name="AiPrReviewTrusted")
    ET.SubElement(root, "description").text = "Generated by ai-pr-review. Not read from the repo."
    for ref in base_refs:
        ET.SubElement(root, "rule", ref=ref)
    if ruleset is not None:
        for pattern, kind in ruleset.patterns:
            ET.SubElement(root, "exclude-pattern", type=kind).text = pattern
        for rule in ruleset.rules:
            root.append(rule)
    return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
