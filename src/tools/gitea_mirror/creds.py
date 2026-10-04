"""Resolve service credentials without storing secrets in the config.

A credential spec is a table with one of:

- token_command: a command printing the token on stdout (e.g. "pass show x")
- token_env: an environment variable holding the token
- token_file: a file holding the token (default per-service file if present)
- user + password_command (Gitea only): HTTP basic auth

and optionally store_command (receives a new token on stdin) used by the
"auth login" workflow.
"""

import base64
import logging
import os
import shlex
import stat
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

GITHUB_SCOPES = "repo,read:org"
GITEA_SCOPES = "read:user, read:organization, write:repository"


class CredentialError(Exception):
    """A credential could not be resolved or stored."""


def run_secret_command(cmd: str, stdin: str | None = None) -> str:
    """Run CMD (no shell) and return the first line of its stdout."""
    try:
        proc = subprocess.run(shlex.split(cmd), input=stdin, capture_output=True,
                              text=True, check=False)
    except OSError as err:
        raise CredentialError(f"cannot run {cmd!r}: {err}") from err
    if proc.returncode != 0:
        raise CredentialError(f"{cmd!r} failed ({proc.returncode}): {proc.stderr.strip()}")
    lines = proc.stdout.splitlines()
    return lines[0].strip() if lines else ""


def _read_token_file(path: Path) -> str:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("token file %s is readable by others; chmod 600 it", path)
    return path.read_text().strip()


def resolve_token(spec: dict, default_file: Path) -> str | None:
    """Return the token described by SPEC, or None if SPEC gives no token."""
    if "token_command" in spec:
        token = run_secret_command(spec["token_command"])
    elif "token_env" in spec:
        token = os.environ.get(spec["token_env"], "")
    else:
        path = Path(spec.get("token_file", default_file)).expanduser()
        if not path.exists():
            return None
        token = _read_token_file(path)
    if not token:
        raise CredentialError(f"empty token from {spec or default_file}")
    return token


def auth_headers(spec: dict, default_file: Path, scheme: str = "token") -> dict:
    """HTTP headers authenticating per SPEC (token or user+password_command)."""
    if "password_command" in spec:
        if "user" not in spec:
            raise CredentialError("password_command requires user")
        password = run_secret_command(spec["password_command"])
        basic = base64.b64encode(f"{spec['user']}:{password}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}
    token = resolve_token(spec, default_file)
    if token is None:
        raise CredentialError(
            f"no credential configured and no {default_file}; run 'gitea-mirror auth login'")
    return {"Authorization": f"{scheme} {token}"}


def store_token(spec: dict, token: str, default_file: Path) -> str:
    """Persist TOKEN via store_command or a 0600 file; return where it went."""
    if "store_command" in spec:
        run_secret_command(spec["store_command"], stdin=token + "\n")
        return spec["store_command"]
    path = Path(spec.get("token_file", default_file)).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)
    return str(path)


def token_page(service: str, gitea_url: str, spec: dict) -> tuple[str, str]:
    """Return (URL, instructions) of the web page that issues a token."""
    if service == "github":
        scopes = spec.get("scopes", GITHUB_SCOPES)
        url = ("https://github.com/settings/tokens/new"
               f"?description=gitea-mirror&scopes={scopes}")
        return url, f"Scopes {scopes} are pre-selected.  Click 'Generate token' and copy it."
    url = f"{gitea_url}/user/settings/applications"
    scopes = spec.get("scopes", GITEA_SCOPES)
    return url, (f"Under 'Generate New Token' name it 'gitea-mirror', select {scopes} "
                 "(add write:organization if the owner is an org and creation fails), "
                 "click 'Generate Token' and copy it.")


def store_warning(spec: dict) -> str:
    """Advice when the stored token is not where the config will look for it."""
    if "store_command" in spec:
        return ""
    if "token_command" in spec:
        return (f"note: config reads the token with {spec['token_command']!r}; make sure it "
                "returns the new token, or add store_command to the config")
    if "token_env" in spec:
        return f"note: config reads the token from ${spec['token_env']}; update it there"
    return ""
