"""Bundle config, state store and lazily-authenticated API clients."""

import json
from functools import cached_property

from tools.gitea_mirror import core, creds, report
from tools.gitea_mirror.config import Config, default_config_path
from tools.gitea_mirror.gitea import Gitea
from tools.gitea_mirror.github import GitHub
from tools.gitea_mirror.rest import ApiError
from tools.gitea_mirror.state import Record, State, Store


def token_file(service: str):
    """Default fallback token file for SERVICE."""
    return default_config_path().parent / f"{service}.token"


class App:
    """What a command needs: config, store, and clients built on first use."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.state_dir)

    @cached_property
    def github(self) -> GitHub:
        """Authenticated GitHub client."""
        return GitHub(creds.auth_headers(self.cfg.github_auth, token_file("github"), "Bearer"))

    @cached_property
    def gitea(self) -> Gitea:
        """Authenticated Gitea client."""
        return Gitea(self.cfg.gitea_url,
                     creds.auth_headers(self.cfg.gitea_auth, token_file("gitea")))

    def mirror_token(self) -> str:
        """GitHub token handed to Gitea for cloning private repos."""
        spec = self.cfg.mirror_auth if self.cfg.mirror_auth is not None else self.cfg.github_auth
        token = creds.resolve_token(spec, token_file("github"))
        if token is None:
            raise creds.CredentialError("no GitHub token available for private mirrors")
        return token


def auth_spec(cfg: Config, service: str) -> dict:
    """The credential spec for SERVICE ("github" or "gitea")."""
    return cfg.github_auth if service == "github" else cfg.gitea_auth


def client_for_token(cfg: Config, service: str, token: str):
    """A client for SERVICE authenticated with a raw TOKEN."""
    if service == "github":
        return GitHub({"Authorization": f"Bearer {token}"})
    return Gitea(cfg.gitea_url, {"Authorization": f"token {token}"})


def verify_token(cfg: Config, service: str, token: str) -> str:
    """Return the login TOKEN authenticates as on SERVICE (raises if invalid)."""
    return client_for_token(cfg, service, token).login()


def _owner_access(app: App) -> str:
    gitea, owner = app.gitea, app.cfg.gitea_owner
    login = gitea.login()
    if owner.lower() == login.lower():
        return f"gitea owner {owner}: is you"
    kind = gitea.owner_kind(owner)
    if gitea.rest.get("/user").get("is_admin"):
        return f"gitea owner {owner} ({kind}): you ({login}) are a site admin"
    if kind == "org":
        orgs = {o["username"].lower() for o in gitea.rest.paginate("/user/orgs", size_key="limit")}
        if owner.lower() in orgs:
            return f"gitea owner {owner} (org): {login} is a member"
    return f"gitea owner {owner} ({kind}): WARNING {login} may not be able to create repos there"


def check_auth(app: App) -> list[str]:
    """Human-readable credential and access checks."""
    lines = []
    for service in ("github", "gitea"):
        try:
            lines.append(f"{service}: ok, authenticated as {getattr(app, service).login()}")
        except (creds.CredentialError, ApiError) as err:
            lines.append(f"{service}: FAILED: {err}")
            if service == "gitea":
                return lines
    try:
        lines.append(_owner_access(app))
    except ApiError as err:
        lines.append(f"gitea owner {app.cfg.gitea_owner}: FAILED: {err}")
    return lines


def require(state: State, patterns) -> list[Record]:
    """Records matching PATTERNS; error if none do."""
    recs = state.select(list(patterns))
    if not recs:
        raise ValueError(f"no records match {' '.join(patterns)}")
    return recs


def edit_records(state: State, recs: list[Record], decision: str | None, policy: str | None,
                 gitea_name: str | None, note: str) -> None:
    """Apply the manual edits requested by the 'set' command."""
    if decision:
        core.set_manual(state, recs, None if decision == "rules" else decision, note)
    if policy:
        core.set_on_unmirror(state, recs, None if policy == "default" else policy)
    if gitea_name:
        if len(recs) != 1:
            raise ValueError("--gitea-name needs exactly one repo")
        core.set_gitea_name(state, recs[0], gitea_name)


def describe_targets(app: App, recs: list[Record]) -> str:
    """What exists on Gitea where each of RECS would be mirrored."""
    lines = []
    for rec in recs:
        existing = core.existing_repo(app.cfg, rec, app.gitea)
        lines.append(f"{app.cfg.gitea_owner}/{core.gitea_name(app.cfg, rec)} "
                     f"({core.describe_existing(existing)}) <- {rec.full_name}")
    return "\n".join(lines)


def handle_existing(app: App, state: State, recs: list[Record], decision: str | None,
                    adopt: bool, replace: bool) -> str:
    """Adopt or replace the existing Gitea repos of RECS, as the 'set' command asks."""
    if not (adopt or replace):
        return ""
    if adopt and replace:
        raise ValueError("--adopt-existing and --replace-existing exclude each other")
    if decision in ("skip", "review"):
        raise ValueError(f"--{decision} contradicts adopting/replacing a mirror")
    if adopt:
        return "\n".join(core.adopt_existing(app.cfg, state, recs, app.gitea))
    steps = core.replace_steps(app.cfg, state, recs)
    core.apply(app.cfg, state, steps, app.gitea, app.mirror_token)
    return report.steps_text(steps)


def dumps(data) -> str:
    """Pretty JSON."""
    return json.dumps(data, indent=1)
