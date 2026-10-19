"""Verdict owners for Bitbucket: who may apply `/ai-pr-review` verdict commands
without an admin-only permission lookup.

Bitbucket allows only repository admins to read another user's role
(`GET /workspaces/{ws}/permissions/repositories/{slug}` returns 403 for any
other account). A review bot that runs with write access can never pass that
check, so `_bitbucket_verdicts.check_authority` could never authorize anyone.
This module reads the owners from `.bitbucket/CODEOWNERS` instead, the file
that Bitbucket itself uses for code owners.

Only a line whose pattern is exactly `*` counts. That is a global list: it
names people who may dismiss findings anywhere in the pull request. Path
scoped lines stay valid for Bitbucket, and this module ignores them. Rejected
alternative: let an owner of any path dismiss any finding. That would let the
owner of `docs/` silence a security finding in code.

Owner tokens that this module can resolve from a pull request comment:

- An Atlassian `account_id` (`557058:9aa59bdf-...` or 24 hex digits) or a
  `{uuid}`. These do not change.

Every other token is ignored and logged. That covers an email address,
`workspace/group` and `@teams/name`, because a comment carries no email and a
group lookup needs admin as well. It also covers `@nickname`. A nickname is not
a unique identifier: Atlassian names the `account_id` as the stable key for a
user, and a nickname is not guaranteed to be unique. Unverified: whether a user
can change a nickname. Authorization on a nickname could let a commenter pass as
an owner who has the same nickname. v2.23.1
matched on `@nickname`, and v2.23.2 removed that. The loader logs a warning
that names each ignored `@nickname`, so an operator can replace it with an
`account_id` or a `{uuid}`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from ai_pr_review.policy import read_repo_file

_log = logging.getLogger(__name__)

OWNERS_PATH: Final[str] = ".bitbucket/CODEOWNERS"

# A real CODEOWNERS file is a few lines. Refuse to parse anything large.
_MAX_BYTES: Final[int] = 64 * 1024

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^\{?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}?$"
)
# `557058:9aa59bdf-2b71-431a-af18-286802c44d56` or a bare 24-digit hex id, the
# two shapes seen on live accounts.
_ACCOUNT_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:\d+:[0-9a-fA-F-]{36}|[0-9a-fA-F]{24})$"
)

MatchKind = Literal["id"]


@dataclass(frozen=True)
class VerdictOwners:
    """Owners parsed from one CODEOWNERS file."""

    ids: frozenset[str] = frozenset()
    ignored_nicknames: tuple[str, ...] = ()
    ignored_owners: tuple[str, ...] = ()
    ignored_lines: int = 0

    def matches(self, *, account_id: str | None, uuid: str | None) -> MatchKind | None:
        """Return how the commenter matches, or None. Only an ID matches."""
        if account_id and account_id.lower() in self.ids:
            return "id"
        if uuid:
            normalized = _normalize_uuid(uuid)
            if normalized and normalized in self.ids:
                return "id"
        return None


@dataclass(frozen=True)
class OwnersLoad:
    """The owners to apply, plus what the destination branch says.

    ``owners`` is what the review honors, read from the source that
    ``AI_POLICY_SOURCE`` selects. ``base_owners`` is the destination branch's
    version. They differ only when the source is ``workspace`` and the pull
    request changes the file. ``None`` means no file was found.
    """

    owners: VerdictOwners | None
    base_owners: VerdictOwners | None
    source: str

    def granted_only_by_this_pr(self, *, account_id: str | None, uuid: str | None) -> bool:
        """True when the pull request's own file lists the commenter and the
        destination branch's file does not."""
        if self.source != "workspace" or self.owners is None:
            return False
        if self.owners.matches(account_id=account_id, uuid=uuid) is None:
            return False
        if self.base_owners is None:
            return True
        return self.base_owners.matches(account_id=account_id, uuid=uuid) is None


def _normalize_uuid(value: str) -> str | None:
    match = _UUID_RE.match(value.strip())
    return match.group(1).lower() if match else None


def parse_owners(text: str) -> VerdictOwners:
    """Parse the owners of every `*` line. Never raises."""
    ids: set[str] = set()
    nicknames: list[str] = []
    ignored: list[str] = []
    ignored_lines = 0
    # Split on "\n" only. str.splitlines() also splits on \x0b, \x0c, \x1c to
    # \x1e, \x85 and U+2028/2029, which Bitbucket and a diff viewer do not show
    # as a line break. A hidden break could turn a path-scoped line into a
    # global one that a reviewer never sees.
    for raw_line in text.lstrip("\ufeff").split("\n"):
        raw_line = raw_line.removesuffix("\r")
        if any(
            c != "\t" and unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in raw_line
        ):
            ignored_lines += 1
            continue
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        if tokens[0] != "*":
            ignored_lines += 1
            continue
        for token in tokens[1:]:
            uuid = _normalize_uuid(token)
            if uuid:
                ids.add(uuid)
            elif _ACCOUNT_ID_RE.match(token):
                ids.add(token.lower())
            elif (
                token.startswith("@")
                and len(token) > 1
                and "/" not in token
                and token.isascii()
            ):
                nicknames.append(token)
            else:
                ignored.append(token)
    return VerdictOwners(
        ids=frozenset(ids),
        ignored_nicknames=tuple(nicknames),
        ignored_owners=tuple(ignored),
        ignored_lines=ignored_lines,
    )


def _parse_file(text: str | None, *, label: str) -> VerdictOwners | None:
    if text is None:
        return None
    if len(text.encode("utf-8", errors="replace")) > _MAX_BYTES:
        _log.warning(
            "ai-pr-review: %s (%s) is larger than %d bytes; ignoring it for "
            "verdict owners", OWNERS_PATH, label, _MAX_BYTES,
        )
        return None
    owners = parse_owners(text)
    if owners.ignored_nicknames:
        _log.warning(
            "ai-pr-review: %s (%s): %d @nickname entr%s on '*' lines no longer "
            "authorize verdict commands, because a nickname is not a unique "
            "identifier. List the account_id or the {uuid} instead. Ignored: %s",
            OWNERS_PATH, label, len(owners.ignored_nicknames),
            "y" if len(owners.ignored_nicknames) == 1 else "ies",
            ", ".join(owners.ignored_nicknames[:10])
            + (" ..." if len(owners.ignored_nicknames) > 10 else ""),
        )
    if owners.ignored_owners:
        _log.info(
            "ai-pr-review: %s (%s): %d owner entr%s on '*' lines cannot be "
            "resolved from a pull request comment and are ignored "
            "(emails, groups, teams)",
            OWNERS_PATH, label, len(owners.ignored_owners),
            "y" if len(owners.ignored_owners) == 1 else "ies",
        )
    return owners


def load_verdict_owners(*, workspace: str, base_ref: str, source: str) -> OwnersLoad:
    """Read the owners file. Fail-soft: any problem yields no owners, so the
    caller falls back to the permission lookup and never authorizes on a guess.
    """
    if source not in ("base-ref", "workspace"):
        source = "base-ref"
    try:
        head_text = read_repo_file(
            OWNERS_PATH, workspace=workspace, base_ref=base_ref, source=source
        )
        owners = _parse_file(head_text, label=source)
        if source == "workspace":
            base_text = read_repo_file(
                OWNERS_PATH, workspace=workspace, base_ref=base_ref, source="base-ref"
            )
            base_owners = _parse_file(base_text, label="base-ref")
        else:
            base_owners = owners
    except Exception as exc:  # noqa: BLE001 -- fail-soft by design, logged below
        _log.warning(
            "ai-pr-review: could not read %s for verdict owners: %s", OWNERS_PATH, exc
        )
        return OwnersLoad(owners=None, base_owners=None, source=source)
    return OwnersLoad(owners=owners, base_owners=base_owners, source=source)
