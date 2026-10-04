"""Minimal Gitea REST client: inventory, create and delete pull mirrors."""

import logging
import re
from dataclasses import dataclass, field

from tools.gitea_mirror.rest import ApiError, Rest

log = logging.getLogger(__name__)

_SCP = re.compile(r"^[\w.-]+@([\w.-]+):(.+)$")


def normalize_url(url: str) -> str:
    """Canonical form of a git remote URL for comparing origins."""
    url = url.strip()
    m = _SCP.match(url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    url = re.sub(r"^[a-z+]+://", "https://", url, flags=re.IGNORECASE)
    url = re.sub(r"^https://[^/@]*@", "https://", url)
    url = url.rstrip("/")
    url = url.removesuffix(".git")
    return url.lower()


@dataclass
class Inventory:
    """Repos currently held by the Gitea owner."""

    names: set[str] = field(default_factory=set)
    mirrors: dict[str, str] = field(default_factory=dict)
    """Normalized origin URL -> Gitea repo name, for pull mirrors."""
    stubs: dict[str, str] = field(default_factory=dict)
    """Normalized origin URL -> name, for empty non-mirrors left by failed migrations."""
    originless: set[str] = field(default_factory=set)
    """Names of pull mirrors whose origin the API does not report."""


def is_stub(repo: dict) -> bool:
    """True for the empty, non-mirror repo a failed migration leaves behind."""
    return bool(not repo.get("mirror") and repo.get("empty") and repo.get("original_url"))


def is_rate_limit(err: Exception) -> bool:
    """True if ERR is Gitea relaying a remote (GitHub) rate limit."""
    return isinstance(err, ApiError) and err.status == 422 and "rate limit" in err.message.lower()


class Gitea:
    """Gitea REST API access."""

    def __init__(self, url: str, headers: dict):
        self.url = url.rstrip("/")
        self.rest = Rest(self.url + "/api/v1", {"Accept": "application/json", **headers})
        self._login = None

    def login(self) -> str:
        """Return the authenticated user's login."""
        if self._login is None:
            self._login = self.rest.get("/user")["login"]
        return self._login

    def owner_kind(self, owner: str) -> str:
        """Return "org" or "user" for OWNER."""
        try:
            self.rest.get(f"/orgs/{owner}")
        except ApiError as err:
            if err.status == 404:
                self.rest.get(f"/users/{owner}")
                return "user"
            raise
        return "org"

    def owner_repos(self, owner: str) -> list[dict]:
        """List all repos owned by OWNER visible to the authenticated user."""
        if owner.lower() == self.login().lower():
            repos = self.rest.paginate("/user/repos", size_key="limit")
            return [r for r in repos if r["owner"]["login"].lower() == owner.lower()]
        path = f"/orgs/{owner}/repos" if self.owner_kind(owner) == "org" else f"/users/{owner}/repos"
        return list(self.rest.paginate(path, size_key="limit"))

    def inventory(self, owner: str) -> Inventory:
        """Return names and mirror origins of OWNER's repos."""
        inv = Inventory()
        for r in self.owner_repos(owner):
            inv.names.add(r["name"])
            if r.get("mirror") and r.get("original_url"):
                inv.mirrors[normalize_url(r["original_url"])] = r["name"]
            elif r.get("mirror"):
                inv.originless.add(r["name"])
            elif is_stub(r):
                inv.stubs[normalize_url(r["original_url"])] = r["name"]
        log.debug("gitea %s: %d repos, %d mirrors", owner, len(inv.names), len(inv.mirrors))
        return inv

    def create_mirror(self, owner: str, name: str, clone_url: str, *, private: bool,
                      description: str = "", interval: str = "8h0m0s",
                      wiki: bool = False, lfs: bool = False, service: str = "git",
                      auth_token: str | None = None) -> dict:
        """Create a pull mirror OWNER/NAME of CLONE_URL via Gitea's SERVICE migrator."""
        body = {
            "clone_addr": clone_url,
            "repo_owner": owner,
            "repo_name": name,
            "mirror": True,
            "service": service,
            "private": private,
            "description": description[:2048],
            "mirror_interval": interval,
            "wiki": wiki,
            "lfs": lfs,
        }
        if auth_token:
            body["auth_token"] = auth_token
        return self.rest.post("/repos/migrate", body)

    def repo(self, owner: str, name: str) -> dict:
        """Fetch OWNER/NAME."""
        return self.rest.get(f"/repos/{owner}/{name}")

    def delete(self, owner: str, name: str) -> None:
        """Delete OWNER/NAME."""
        self.rest.delete(f"/repos/{owner}/{name}")
