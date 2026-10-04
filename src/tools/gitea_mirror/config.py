"""Load and validate the gitea-mirror TOML configuration."""

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP = "gitea-mirror"
ACTIONS = ("mirror", "skip", "review")
UNMIRROR = ("leave", "prune")
VISIBILITY = ("same", "private", "public")
SERVICES = ("git", "github")
SOURCE_KINDS = ("user", "org")
RULE_CONDITIONS = frozenset({
    "source", "owner", "repo", "regex", "fork", "private", "archived",
    "topic", "language", "description",
})


class ConfigError(Exception):
    """The configuration is missing or invalid."""


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value).expanduser() if value else Path.home() / fallback


def default_config_path() -> Path:
    """Return $XDG_CONFIG_HOME/gitea-mirror/config.toml."""
    return _xdg("XDG_CONFIG_HOME", ".config") / APP / "config.toml"


def default_state_dir() -> Path:
    """Return $XDG_STATE_HOME/gitea-mirror."""
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


@dataclass
class Source:
    """A GitHub user or org whose repos are discovered."""

    name: str
    kind: str
    owner: str
    default: str = "review"
    include_private: bool = True
    name_template: str | None = None


@dataclass
class Rule:
    """An ordered matching rule mapping repo properties to an action."""

    name: str
    action: str
    conditions: dict = field(default_factory=dict)


@dataclass
class Config:
    """Resolved gitea-mirror configuration."""

    gitea_url: str
    gitea_owner: str
    path: Path
    state_dir: Path
    mirror_interval: str = "8h0m0s"
    visibility: str = "same"
    wiki: bool = False
    lfs: bool = False
    service: str = "git"
    create_delay: float = 2.0
    max_creates: int = 0
    name_template: str = "{name}"
    on_unmirror: str = "leave"
    gitea_auth: dict = field(default_factory=dict)
    github_auth: dict = field(default_factory=dict)
    mirror_auth: dict | None = None
    sources: list[Source] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)

    def source(self, name: str) -> Source | None:
        """Return the source named NAME, if configured."""
        return next((s for s in self.sources if s.name == name), None)


def _choice(value, allowed, what):
    if value not in allowed:
        raise ConfigError(f"{what}: {value!r} not one of {', '.join(allowed)}")
    return value


def _parse_source(d: dict) -> Source:
    try:
        src = Source(**d)
    except TypeError as err:
        raise ConfigError(f"[[sources]] {d.get('name', '?')}: {err}") from err
    _choice(src.kind, SOURCE_KINDS, f"source {src.name} kind")
    _choice(src.default, ACTIONS, f"source {src.name} default")
    return src


def _parse_rule(d: dict, index: int, source_names: set[str]) -> Rule:
    d = dict(d)
    name = d.pop("name", f"rule{index}")
    action = _choice(d.pop("action", None), ACTIONS, f"rule {name} action")
    unknown = set(d) - RULE_CONDITIONS
    if unknown:
        raise ConfigError(f"rule {name}: unknown conditions {', '.join(sorted(unknown))}")
    if "source" in d and d["source"] not in source_names:
        raise ConfigError(f"rule {name}: unknown source {d['source']!r}")
    return Rule(name=name, action=action, conditions=d)


def parse_config(data: dict, path: Path, state_dir: Path | None = None) -> Config:
    """Build a Config from parsed TOML DATA."""
    gitea = data.get("gitea", {})
    github = data.get("github", {})
    defaults = data.get("defaults", {})
    for key in ("url", "owner"):
        if key not in gitea:
            raise ConfigError(f"[gitea] requires {key!r}")
    sources = [_parse_source(s) for s in data.get("sources", [])]
    names = {s.name for s in sources}
    if len(names) != len(sources):
        raise ConfigError("[[sources]] names must be unique")
    rules = [_parse_rule(r, i, names) for i, r in enumerate(data.get("rules", []))]
    state = state_dir or Path(defaults.get("state_dir", default_state_dir())).expanduser()
    return Config(
        gitea_url=gitea["url"].rstrip("/"),
        gitea_owner=gitea["owner"],
        path=path,
        state_dir=state,
        mirror_interval=gitea.get("mirror_interval", "8h0m0s"),
        visibility=_choice(gitea.get("visibility", "same"), VISIBILITY, "gitea visibility"),
        wiki=bool(gitea.get("wiki", False)),
        lfs=bool(gitea.get("lfs", False)),
        service=_choice(gitea.get("service", "git"), SERVICES, "gitea service"),
        create_delay=float(gitea.get("create_delay", 2.0)),
        max_creates=int(gitea.get("max_creates", 0)),
        name_template=gitea.get("name_template", "{name}"),
        on_unmirror=_choice(defaults.get("on_unmirror", "leave"), UNMIRROR,
                            "defaults on_unmirror"),
        gitea_auth=gitea.get("auth", {}),
        github_auth=github.get("auth", {}),
        mirror_auth=github.get("mirror_auth"),
        sources=sources,
        rules=rules,
    )


def load_config(path: str | Path | None = None, state_dir: Path | None = None) -> Config:
    """Load and validate the config at PATH (default: XDG location)."""
    resolved = Path(path).expanduser() if path else default_config_path()
    if not resolved.exists():
        raise ConfigError(f"no config at {resolved}; run 'gitea-mirror init'")
    with open(resolved, "rb") as f:
        data = tomllib.load(f)
    log.debug("loaded config from %s", resolved)
    return parse_config(data, resolved, state_dir)


STARTER = """\
# gitea-mirror configuration.  See docs/gitea-mirror.md in the tools package.

[gitea]
url = "{url}"
owner = "{owner}"            # Gitea user or org that holds the mirrors
mirror_interval = "8h0m0s"
visibility = "same"          # same (as GitHub) | private | public
wiki = false
lfs = false
# How Gitea clones public repos.  "git" is a plain clone using no GitHub API
# (no API rate limit, no token stored in Gitea).  "github" uses Gitea's GitHub
# migrator, which needs the GitHub token (stored in every mirror) to avoid
# GitHub's anonymous API limit of 60 requests/hour.  Private repos always use
# "github" with the token.
service = "git"
create_delay = 2.0           # seconds between mirror creations
max_creates = 0              # cap on creations per run; 0 = no cap
name_template = "{{name}}"     # placeholders: {{name}} {{owner}} {{source}}

# Gitea credential.  Use one of token_command, token_env, token_file, or
# user + password_command.  store_command (token on stdin) is used by
# 'gitea-mirror auth login gitea'.
[gitea.auth]
token_command = "pass show gitea/{owner}-token"
store_command = "pass insert -m -f gitea/{owner}-token"

[github.auth]
token_command = "gh auth token"
# scopes = "repo,read:org"     # requested by 'auth login github'

# Optional separate GitHub token handed to Gitea to clone private repos.
# Default: the [github.auth] token.
# [github.mirror_auth]
# token_command = "pass show github/gitea-mirror-token"

[defaults]
on_unmirror = "leave"        # leave | prune: what happens to a Gitea mirror
                             # whose decision becomes "skip"

[[sources]]
name = "me"
kind = "user"                # user | org
owner = "{github_user}"
default = "review"           # action when no rule matches
include_private = true

# [[sources]]
# name = "someorg"
# kind = "org"
# owner = "SomeOrg"
# default = "skip"
# name_template = "someorg-{{name}}"

# Rules: first match wins.  Conditions (all must match): source, owner
# (glob), repo (glob or list of globs on the repo name), regex (on
# owner/name), fork, private, archived, topic, language, description
# (regex).  action: mirror | skip | review.

[[rules]]
name = "archived"
archived = true
action = "review"

[[rules]]
name = "own-repos"
source = "me"
fork = false
action = "mirror"

[[rules]]
name = "own-forks"
source = "me"
fork = true
action = "review"
"""


def starter_config(url: str, owner: str, github_user: str) -> str:
    """Return an annotated starter config."""
    return STARTER.format(url=url, owner=owner, github_user=github_user)
