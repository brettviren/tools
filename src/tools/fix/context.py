"""The context fix operates on, and its detection from the environment."""

import getpass
import logging
import socket
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Context:
    """A detected (or overridden) environment fix categorizes and acts on.

    New fields may be added here as future categorizers need more context;
    existing fields must stay stable since config files and tests key on them.
    """

    path: Path
    user: str
    host: str
    fqdn: str


def detect_context(
    path: str | Path | None = None,
    user: str | None = None,
    host: str | None = None,
    fqdn: str | None = None,
) -> Context:
    """Build a Context, auto-detecting any part not explicitly given."""
    resolved_path = Path(path).expanduser().resolve() if path else Path.cwd()
    resolved_fqdn = fqdn or socket.getfqdn()
    resolved_host = host or resolved_fqdn.split(".")[0]
    resolved_user = user or getpass.getuser()
    log.debug(
        "context: path=%s user=%s host=%s fqdn=%s",
        resolved_path, resolved_user, resolved_host, resolved_fqdn,
    )
    return Context(path=resolved_path, user=resolved_user, host=resolved_host, fqdn=resolved_fqdn)
