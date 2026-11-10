# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>

"""Resolve which account name and local directory a backup uses.

The Artifact tool only sees the organization the Claude Code session is signed
in to, so the account name is derived from that active org (read from
``.claude.json``) through an ``[orgs]`` table in the per-install config file.
An explicit account (argument or environment) is accepted only when no org can
be detected, or when it agrees with the mapped one; otherwise artifacts of one
org could be filed under another's name.

The directory comes from an argument, ``$CLAUDE_ARTIFACTS_DIR``, the config
file's ``dir``, or a built-in default, in that order.  The config file lives in
the Claude config dir (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``)::

    dir = "~/somewhere"
    [orgs]
    WireCell = "wirecell"
    "Jane's Organization" = "personal"
"""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomlkit

DEFAULT_DIR = Path("~/.cache/claude-artifacts")
ENV_DIR = "CLAUDE_ARTIFACTS_DIR"
ENV_ACCOUNT = "CLAUDE_ARTIFACTS_ACCOUNT"
ORIGINS = ("yours", "shared")


class ConfigError(ValueError):
    """A setting is missing or invalid."""


@dataclass(frozen=True)
class Org:
    """The organization the Claude Code session is signed in to."""

    name: str
    uuid: str


@dataclass(frozen=True)
class Settings:
    """Resolved settings: tree root and account short name."""

    root: Path
    account: str

    @property
    def account_dir(self) -> Path:
        """Directory holding this account's ``yours``/``shared`` subtrees."""
        return self.root / self.account

    def origin_dir(self, origin: str) -> Path:
        """Directory holding this account's artifacts of one origin."""
        if origin not in ORIGINS:
            raise ConfigError(f"origin must be one of {ORIGINS}, got {origin!r}")
        return self.account_dir / origin


def config_path() -> Path:
    """Per-install config file location."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(base).expanduser() / "claude-artifacts.toml"


def load_file(path: Path | None = None) -> dict:
    """Read the config file, returning {} if it does not exist."""
    path = path or config_path()
    if not path.exists():
        return {}
    with path.open("rb") as fp:
        return tomllib.load(fp)


def check_account(name: str) -> str:
    """Validate an account short name, usable as a directory name."""
    if not name or name in (".", "..") or "/" in name or name != name.strip():
        raise ConfigError(f"invalid account name {name!r}: use a short name without '/'")
    return name


def resolve_root(directory: str | Path | None = None, file_data: dict | None = None) -> Path:
    """Tree root from argument, environment, config file or default."""
    data = load_file() if file_data is None else file_data
    root = directory or os.environ.get(ENV_DIR) or data.get("dir") or DEFAULT_DIR
    return Path(root).expanduser().resolve()


def claude_json_path() -> Path:
    """Claude Code's state file, which records the signed-in account."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() / ".claude.json") if base else Path("~/.claude.json").expanduser()


def active_org(path: Path | None = None) -> Org | None:
    """The active organization, or None if it cannot be determined."""
    path = path or claude_json_path()
    try:
        acct = json.loads(path.read_text()).get("oauthAccount") or {}
    except (OSError, ValueError):
        return None
    name, uuid = acct.get("organizationName"), acct.get("organizationUuid")
    return Org(name or "", uuid or "") if (name or uuid) else None


def mapped_account(org: Org | None, orgs: dict) -> str | None:
    """Short account name configured for *org*, matched by name or uuid."""
    if org is None:
        return None
    return orgs.get(org.name) or orgs.get(org.uuid)


def resolve(account: str | None = None, directory: str | Path | None = None,
            file_data: dict | None = None, org: Org | None = None) -> Settings:
    """Combine arguments, environment, active org and config file into Settings."""
    data = load_file() if file_data is None else file_data
    org = org if org is not None else active_org()
    given = account or os.environ.get(ENV_ACCOUNT)
    mapped = mapped_account(org, data.get("orgs", {}))
    if org and not mapped:
        raise ConfigError(
            f"organization {org.name or org.uuid!r} has no account name yet: run "
            f"'claude-artifacts setup --account NAME' while signed in to it")
    if given and mapped and given != mapped:
        raise ConfigError(
            f"account {given!r} contradicts the active organization "
            f"{org.name!r}, which is {mapped!r}; this session cannot see other orgs. "
            "Switch organization with /login instead")
    account = mapped or given
    if not account:
        raise ConfigError(
            f"cannot detect the active organization: pass --account or set ${ENV_ACCOUNT}")
    return Settings(resolve_root(directory, data), check_account(account))


def save(account: str, directory: str | None = None, org: Org | None = None,
         path: Path | None = None) -> Path:
    """Map the active org (or *org*) to *account* in the per-install config file."""
    path = path or config_path()
    org = org if org is not None else active_org()
    if org is None:
        raise ConfigError("cannot detect the active organization to map")
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    doc.pop("account", None)                      # legacy, replaced by [orgs]
    if directory:
        doc["dir"] = directory
    if "orgs" not in doc:
        doc["orgs"] = tomlkit.table()
    doc["orgs"][org.name or org.uuid] = check_account(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))
    return path
