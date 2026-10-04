"""Minimal GitHub REST client: discover repos of users and orgs."""

import logging
from dataclasses import asdict, dataclass, field

from tools.gitea_mirror.rest import Rest

log = logging.getLogger(__name__)

API = "https://api.github.com"


@dataclass
class GithubRepo:
    """The GitHub facts gitea-mirror cares about."""

    full_name: str
    fork: bool = False
    private: bool = False
    archived: bool = False
    description: str = ""
    topics: list[str] = field(default_factory=list)
    language: str = ""
    html_url: str = ""
    clone_url: str = ""
    created_at: str = ""
    pushed_at: str = ""

    @property
    def owner(self) -> str:
        """Owner part of full_name."""
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        """Repo part of full_name."""
        return self.full_name.split("/", 1)[1]

    @classmethod
    def from_api(cls, d: dict) -> "GithubRepo":
        """Build from a GitHub API repo object."""
        return cls(
            full_name=d["full_name"],
            fork=bool(d.get("fork")),
            private=bool(d.get("private")),
            archived=bool(d.get("archived")),
            description=d.get("description") or "",
            topics=list(d.get("topics") or []),
            language=d.get("language") or "",
            html_url=d.get("html_url") or "",
            clone_url=d.get("clone_url") or "",
            created_at=d.get("created_at") or "",
            pushed_at=d.get("pushed_at") or "",
        )

    def to_meta(self) -> dict:
        """Serialize everything but full_name."""
        meta = asdict(self)
        meta.pop("full_name")
        return meta

    @classmethod
    def from_meta(cls, full_name: str, meta: dict) -> "GithubRepo":
        """Inverse of to_meta."""
        known = {k: v for k, v in meta.items() if k in cls.__dataclass_fields__}
        return cls(full_name=full_name, **known)


class GitHub:
    """GitHub REST API access."""

    def __init__(self, headers: dict, api: str = API):
        self.rest = Rest(api, {"Accept": "application/vnd.github+json", **headers})
        self._login = None

    def login(self) -> str:
        """Return the authenticated user's login."""
        if self._login is None:
            self._login = self.rest.get("/user")["login"]
        return self._login

    def _repo_path(self, kind: str, owner: str, include_private: bool) -> tuple[str, dict]:
        if kind == "org":
            return f"/orgs/{owner}/repos", {"type": "all"}
        if include_private and owner.lower() == self.login().lower():
            return "/user/repos", {"affiliation": "owner", "visibility": "all"}
        return f"/users/{owner}/repos", {"type": "owner"}

    def source_repos(self, kind: str, owner: str, include_private: bool = True) -> list[GithubRepo]:
        """List repos of a user or org."""
        path, params = self._repo_path(kind, owner, include_private)
        repos = [GithubRepo.from_api(d) for d in self.rest.paginate(path, params, page_size=100)]
        if not include_private:
            repos = [r for r in repos if not r.private]
        log.debug("github %s %s: %d repos", kind, owner, len(repos))
        return repos

    def repo(self, full_name: str) -> GithubRepo:
        """Fetch one repo."""
        return GithubRepo.from_api(self.rest.get(f"/repos/{full_name}"))
