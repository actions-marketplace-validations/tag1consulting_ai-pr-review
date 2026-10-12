"""Native Python implementation of the phpcs analyzer.

Replaces analyzers/run-phpcs.sh. Invokes phpcs directly via subprocess and
converts its JSON output to Finding instances.

Trust boundary (#1042): the workspace is untrusted PR content, and a phpcs
ruleset is not inert data. It can set ``installed_paths``, ``<arg
name="bootstrap">``, ``<autoload>`` and path-style ``<rule ref>`` entries,
all of which make phpcs load PHP from the workspace. phpcs therefore never
reads the repo ruleset. This module reads the ruleset itself as data, keeps
an allowlisted subset (which files to lint, which exclude patterns, which
installed standards and sniffs, and per-rule tweaks), and gives phpcs a
freshly written ruleset built from that subset. This is the same approach as
the empty trusted ``--configuration`` file in ``phpstan.py`` (#739).
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ai_pr_review.analyzers.native._paths import strip_workspace_prefix
from ai_pr_review.findings.models import Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 90
_SOURCE = "phpcs"
_TIMEOUT_SECS = 120
_PHP_EXTENSIONS = {".php", ".module", ".inc", ".theme", ".install", ".profile"}

# phpcs's --standard flag only accepts full ruleset names (or a path to a
# ruleset.xml); it cannot mix a named standard with a bare sniff code
# (`--standard=PSR12,Squiz.Commenting.FunctionComment` errors: "the
# Squiz.Commenting.FunctionComment coding standard is not installed").
# Verified empirically against phpcs 4.0.4 this session. A small ruleset.xml
# combining PSR12 with the one Squiz docblock sniff is the documented way to
# add a single sniff on top of a named standard.
_PSR12_PLUS_DOCBLOCK_RULESET = """<?xml version="1.0"?>
<ruleset name="PSR12PlusDocblock">
    <description>PSR12 with Squiz function-comment sniffs added.</description>
    <rule ref="PSR12"/>
    <rule ref="Squiz.Commenting.FunctionComment"/>
</ruleset>
"""

# Correctness-only sniffs with no code-style opinion (no indentation, tabs,
# line length, naming, or brace placement). Used when a repo ruleset names a
# standard that the image does not have (for example WordPress). PSR12 is the
# wrong fallback there: it requires spaces, so tab-indented code gets a
# finding on almost every line. All seven names verified against phpcs 4.0.4
# with `phpcs -e` (#1042).
_STYLE_NEUTRAL_REFS = (
    "Generic.PHP.Syntax",
    "Generic.CodeAnalysis.EmptyStatement",
    "Generic.CodeAnalysis.UnconditionalIfStatement",
    "Generic.CodeAnalysis.JumbledIncrementer",
    "Generic.PHP.DeprecatedFunctions",
    "Squiz.PHP.Eval",
    "Squiz.PHP.NonExecutableCode",
)

# phpcs 4.x exit codes are a bitmask (PHPCSStandards/PHP_CodeSniffer#184,
# src/Util/ExitCode.php): 0=clean, 1=fixable violations, 2=non-fixable
# violations, 3=both. All four mean "phpcs completed a normal run; the JSON
# output is trustworthy." Fatal/config errors use 16 (PROCESS_ERROR) or 64
# (REQUIREMENTS_NOT_MET) instead, which never overlap this range. This
# differs from phpcs 3.x, where 2 meant "some fixable issue exists" and a
# genuine config error was not representable via this simple 0/1/2 scheme.
_SUCCESS_RETURNCODES = frozenset({0, 1, 2, 3})

# phpcs's own auto-discovery order (Config::CONFIG_FILENAMES, phpcs 4.0.4).
_RULESET_NAMES = (".phpcs.xml", "phpcs.xml", ".phpcs.xml.dist", "phpcs.xml.dist")
_MAX_RULESET_BYTES = 256 * 1024
_MAX_COMPOSER_BYTES = 1024 * 1024
_DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

# "Standard", "Standard.Category", "Standard.Category.Sniff", or
# "Standard.Category.Sniff.Code". A path, a ".xml" suffix, or a "../" segment
# can never match, which is what keeps path-style refs out of the trusted
# ruleset (a path ref makes phpcs load the referenced file).
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_]+){0,3}$")
_PROP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# phpcs treats exclude patterns as regex fragments (`*` becomes `.*`). Only
# plain path characters, `|`, anchors, and escaped `.`, `-`, `/`, `,` are
# accepted. No groups, classes, or quantifiers other than `*`, so a pattern
# from the workspace cannot cause catastrophic backtracking.
_PATTERN_ESCAPE_RE = re.compile(r"\\[.\-/,]")
_PATTERN_BODY_RE = re.compile(r"^[A-Za-z0-9_./*|,^$\-]{1,200}$")
_MAX_PATTERN_STARS = 8
_PATTERN_TYPES = frozenset({"absolute", "relative"})
_DRUPAL_EXTENSIONS = frozenset({".module", ".install", ".theme", ".profile"})
# Drupal 7 modules describe themselves with a plain `<name>.info` file (no
# `.info.yml`, often no composer.json). `.info` is also used by unrelated tools,
# so the file counts only when it has both keys every Drupal 7 info file needs.
_MAX_INFO_BYTES = 64 * 1024
_D7_NAME_RE = re.compile(rb"^\s*name\s*=", re.MULTILINE)
_D7_CORE_RE = re.compile(rb"^\s*core\s*=", re.MULTILINE)
# Ships inside phpcs itself but is not listed by `phpcs -i`.
_ALWAYS_INSTALLED = frozenset({"Generic"})


@dataclass(frozen=True)
class _RepoRuleset:
    """The allowlisted subset of a repo phpcs ruleset (data only)."""

    name: str
    files: tuple[str, ...]
    patterns: tuple[tuple[str, str], ...]
    rules: tuple[ET.Element, ...]
    has_standard_ref: bool
    missing_standards: tuple[str, ...]


@dataclass
class _Plan:
    """What to run: a named standard or a generated ruleset, plus scope."""

    label: str
    remediation_standard: str
    named_standard: str | None = None
    ruleset_xml: str | None = None
    files: list[str] = field(default_factory=list)
    drupal: bool = False
    # True when the generated ruleset carries rules or patterns from the repo.
    # Those can name a sniff that this phpcs version lacks, so a process error
    # is retried once without them.
    repo_rules: bool = False
    # The repo's top-level exclude patterns. They survive a retry, because only
    # the rules can name a sniff that this phpcs version lacks.
    patterns: tuple[tuple[str, str], ...] = ()


def _installed_standards() -> set[str]:
    """Return the standard names phpcs lists, or an empty set when it cannot be asked."""
    try:
        result = subprocess.run(
            ["phpcs", "-i"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[ai-pr-review] WARNING: phpcs -i timed out detecting standard; falling back to PSR12.")
        return set()
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs -i failed: %s; falling back to PSR12.", exc)
        return set()
    if result.returncode != 0 or not result.stdout.strip():
        logger.warning(
            "[ai-pr-review] WARNING: phpcs -i exited %d with no usable output; "
            "the installed standards are unknown, so no repo standard can be honored.",
            result.returncode,
        )
        return set()
    text = result.stdout
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    elif " are " in text:
        text = text.split(" are ", 1)[-1]
    names = {t for t in re.split(r"[,\s]+", text) if t and t != "and"}
    return names | _ALWAYS_INSTALLED


def _pattern_ok(pattern: str) -> bool:
    if pattern.count("*") > _MAX_PATTERN_STARS:
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
    if not _REF_RE.match(ref):
        drops.note("rule ref", ref)
        return None, None
    standard = ref.split(".", 1)[0]
    if standard not in installed:
        return None, standard

    kept = ET.Element("rule", ref=ref)
    for child in rule:
        text = (child.text or "").strip()
        if child.tag == "exclude":
            if _REF_RE.match(child.get("name", "")):
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
            if text.isdigit() and int(text) <= 10:
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


def _relative_pattern_matches(pattern: str, relative_path: str) -> bool:
    """Apply one `type="relative"` exclude pattern the way phpcs does for a directory scan.

    phpcs turns `*` into `.*`, reads `\\,` as `,`, and matches the result as a
    case-insensitive regex. Only patterns that passed `_pattern_ok` get here.
    """
    try:
        return re.search(pattern.replace("\\,", ",").replace("*", ".*"), relative_path, re.IGNORECASE) is not None
    except re.error:
        return False


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


def _is_drupal7_info(path: Path) -> bool:
    """Is *path* a Drupal 7 `.info` file? Reads at most 64 KiB, as data, never a symlink."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as fh:
            data = fh.read(_MAX_INFO_BYTES)
    except OSError:
        return False
    return bool(_D7_NAME_RE.search(data) and _D7_CORE_RE.search(data))


def _dir_has_drupal_info(directory: Path) -> bool:
    try:
        if any(directory.glob("*.info.yml")):
            return True
        return any(_is_drupal7_info(p) for p in directory.glob("*.info"))
    except OSError:
        return False


def _looks_like_drupal(php_files: list[str]) -> bool:
    """Does the repo show Drupal signals? Reads file names, `*.info` files and composer.json as data."""
    for f in php_files:
        if Path(f).suffix in _DRUPAL_EXTENSIONS:
            return True
    composer = Path("composer.json")
    if composer.is_file() and not composer.is_symlink():
        try:
            with composer.open("rb") as fh:
                data = json.loads(fh.read(_MAX_COMPOSER_BYTES))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            if str(data.get("type", "")).startswith("drupal-"):
                return True
            for key in ("require", "require-dev"):
                deps = data.get(key)
                if isinstance(deps, dict) and any(str(k).startswith("drupal/core") for k in deps):
                    return True
    cwd = Path.cwd()
    checked: set[Path] = set()
    for f in php_files:
        directory = Path(f).parent
        if directory.is_absolute():
            try:
                directory = directory.relative_to(cwd)
            except ValueError:
                checked_dirs = [directory]
            else:
                checked_dirs = [directory, *directory.parents]
        else:
            checked_dirs = [directory, *directory.parents]
        for d in checked_dirs:
            if d in checked:
                continue
            checked.add(d)
            if _dir_has_drupal_info(d):
                return True
    return False


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


def _plan_standard(php_files: list[str]) -> _Plan | None:
    """Choose what to run. None when the repo ruleset puts every changed file out of scope."""
    installed = _installed_standards()
    ruleset_path = _find_repo_ruleset()
    if ruleset_path is not None:
        ruleset = _parse_repo_ruleset(ruleset_path, installed)
        if ruleset is None:
            logger.warning(
                "[ai-pr-review] WARNING: %s could not be used; linting with the style-neutral fallback.",
                ruleset_path.name,
            )
            return _Plan(
                label="style-neutral fallback",
                remediation_standard=ruleset_path.name,
                ruleset_xml=_build_ruleset_xml(_STYLE_NEUTRAL_REFS, None),
                files=php_files,
            )
        scoped = [f for f in php_files if _in_ruleset_scope(f, ruleset)]
        if not scoped:
            logger.info("[ai-pr-review] phpcs: no changed file is inside the <file> scope of %s; skipping.", ruleset.name)
            return None
        if ruleset.missing_standards:
            logger.warning(
                "[ai-pr-review] WARNING: %s names standard(s) not installed in this image: %s. "
                "Linting with the style-neutral fallback plus the installed rules it names.",
                ruleset.name, ", ".join(ruleset.missing_standards),
            )
            return _Plan(
                label="style-neutral fallback",
                remediation_standard=ruleset.name,
                ruleset_xml=_build_ruleset_xml(_STYLE_NEUTRAL_REFS, ruleset),
                files=scoped,
                repo_rules=True,
                patterns=ruleset.patterns,
            )
        if ruleset.has_standard_ref:
            return _Plan(
                label="repo ruleset",
                remediation_standard=ruleset.name,
                ruleset_xml=_build_ruleset_xml((), ruleset),
                files=scoped,
                drupal=any(r.get("ref", "").startswith("Drupal") for r in ruleset.rules),
                repo_rules=True,
                patterns=ruleset.patterns,
            )
        # A ruleset with no standard of its own: keep its scope and excludes
        # and pick the default standard below.
        php_files = scoped
        default_extra: _RepoRuleset | None = ruleset
    else:
        default_extra = None

    if "Drupal" in installed and _looks_like_drupal(php_files):
        if default_extra is None:
            return _Plan(
                label="Drupal,DrupalPractice",
                remediation_standard="Drupal,DrupalPractice",
                named_standard="Drupal,DrupalPractice",
                files=php_files,
                drupal=True,
            )
        return _Plan(
            label="Drupal,DrupalPractice",
            remediation_standard=default_extra.name,
            ruleset_xml=_build_ruleset_xml(("Drupal", "DrupalPractice"), default_extra),
            files=php_files,
            drupal=True,
            repo_rules=True,
            patterns=default_extra.patterns,
        )
    if default_extra is not None:
        return _Plan(
            label="PSR12",
            remediation_standard=default_extra.name,
            ruleset_xml=_build_ruleset_xml(("PSR12", "Squiz.Commenting.FunctionComment"), default_extra),
            files=php_files,
            repo_rules=True,
            patterns=default_extra.patterns,
        )
    return _Plan(label="PSR12", remediation_standard="PSR12", ruleset_xml=_PSR12_PLUS_DOCBLOCK_RULESET, files=php_files)


@contextmanager
def _standard_arg(plan: _Plan) -> Iterator[str]:
    """Return the value to pass as --standard for *plan*.

    A named standard passes through unchanged. A generated ruleset is written
    to a temp file that phpcs reads. The temp file never lives in the
    workspace.
    """
    if plan.ruleset_xml is None:
        yield plan.named_standard or ""
        return
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", prefix="phpcs-ruleset-", delete=True
    ) as f:
        f.write(plan.ruleset_xml)
        f.flush()
        yield f.name


def _invoke_phpcs(plan: _Plan) -> subprocess.CompletedProcess[str] | None:
    """Run phpcs for *plan*. None on timeout or when phpcs cannot start."""
    try:
        with _standard_arg(plan) as standard_arg:
            return subprocess.run(
                [
                    "phpcs",
                    "--report=json",
                    f"--standard={standard_arg}",
                    "--extensions=php,module,inc,theme,install,profile",
                    "-q",
                    "--",
                    *plan.files,
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECS,
            )
    except subprocess.TimeoutExpired as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs timed out after %ss; skipping.", exc.timeout)
        return None
    except OSError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs failed to start: %s", exc)
        return None


def _remediation(plan: _Plan, path: str) -> str:
    if plan.drupal:
        return f"See https://www.drupal.org/docs/develop/standards or fix with: phpcs --standard={plan.remediation_standard} {path}"
    return f"Fix the violation named above. Check locally with: phpcs --standard={plan.remediation_standard} {path}"


def _run_phpcs(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Run phpcs on changed PHP files and return Finding instances."""
    php_files = [f for f in changed_files.php if Path(f).is_file() and Path(f).suffix in _PHP_EXTENSIONS]
    if not php_files:
        return []

    if not shutil.which("phpcs"):
        logger.warning("[ai-pr-review] WARNING: phpcs not found; skipping.")
        return []

    plan = _plan_standard(php_files)
    if plan is None:
        return []

    result = _invoke_phpcs(plan)
    if result is None:
        return []

    if result.returncode not in _SUCCESS_RETURNCODES and plan.repo_rules:
        # A rule or sniff code the repo ruleset names may not exist in this
        # phpcs version (a phpcs 3 sniff that phpcs 4 removed, for example).
        # Retry once with the style-neutral fallback and none of the repo
        # rules, instead of returning no findings at all.
        logger.warning(
            "[ai-pr-review] WARNING: phpcs rejected the generated ruleset (exit %d); "
            "retrying with the style-neutral fallback. The repo rules are dropped and "
            "the repo's top-level exclude patterns are kept. stderr: %s",
            result.returncode, result.stderr[:200],
        )
        plan = _Plan(
            label="style-neutral fallback",
            remediation_standard=plan.remediation_standard,
            ruleset_xml=_build_ruleset_xml(
                _STYLE_NEUTRAL_REFS,
                _RepoRuleset(
                    name=plan.remediation_standard,
                    files=(),
                    patterns=plan.patterns,
                    rules=(),
                    has_standard_ref=False,
                    missing_standards=(),
                ),
            ),
            files=plan.files,
        )
        result = _invoke_phpcs(plan)
        if result is None:
            return []

    if result.returncode not in _SUCCESS_RETURNCODES:
        logger.warning(
            "[ai-pr-review] WARNING: phpcs exited %d (process/config error); standard=%r may not be usable. "
            "stderr: %s",
            result.returncode, plan.label, result.stderr[:200],
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("[ai-pr-review] WARNING: phpcs produced non-JSON output: %s", exc)
        return []

    if not isinstance(data, dict):
        logger.warning("[ai-pr-review] WARNING: phpcs produced unexpected output structure; skipping.")
        return []

    findings: list[Finding] = []

    for file_path, file_data in (data.get("files") or {}).items():
        if not isinstance(file_data, dict):
            continue
        normalized_path = strip_workspace_prefix(file_path)

        for msg in file_data.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type", "")
            severity = "High" if msg_type == "ERROR" else "Medium"
            source_rule = msg.get("source") or ""
            try:
                findings.append(
                    Finding(
                        severity=severity,  # type: ignore[arg-type]
                        confidence=_CONFIDENCE,
                        source=_SOURCE,
                        file=normalized_path,
                        line=msg.get("line") or None,
                        finding=f"{source_rule}: {msg.get('message', '')}",
                        remediation=_remediation(plan, normalized_path),
                        category="lint",
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[ai-pr-review] WARNING: phpcs dropped malformed finding: %s; msg=%r",
                    exc, repr(msg)[:200],
                )

    return findings
