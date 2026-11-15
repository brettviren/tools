# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>

"""The on-disk artifact tree: ``<root>/<account>/<origin>/<slug>-<id>/``.

Each artifact directory holds the artifact's published files (its page is
``index.html``) plus a ``meta.json`` written here.
"""

import json
import re
import shutil
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

from tools.claude_artifacts.config import ORIGINS, ConfigError, Settings

META = "meta.json"
_URL_ID = re.compile(r"/artifact/([A-Za-z0-9_-]+)")


@dataclass
class Entry:
    """Metadata for one downloaded artifact."""

    id: str
    title: str
    url: str
    origin: str
    updated: str = ""
    summary: str = ""
    downloaded: str = ""


def artifact_id(url_or_id: str) -> str:
    """Extract the artifact id from a claude.ai URL (or pass an id through)."""
    m = _URL_ID.search(url_or_id)
    return m.group(1) if m else url_or_id


def slug(title: str, limit: int = 48) -> str:
    """Filesystem-safe lowercase slug of a title."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:limit].strip("-") or "artifact"


def dirname(title: str, aid: str) -> str:
    """Artifact directory name; the id keeps same-titled artifacts apart."""
    return f"{slug(title)}-{aid}"


def read_entry(path: Path) -> Entry:
    """Load an Entry from an artifact directory."""
    raw = json.loads((path / META).read_text())
    return Entry(**{f.name: raw.get(f.name, "") for f in fields(Entry)})


def known_ids(settings: Settings) -> dict[str, set[str]]:
    """Ids already downloaded for this account, by origin."""
    out: dict[str, set[str]] = {o: set() for o in ORIGINS}
    for origin in ORIGINS:
        base = settings.origin_dir(origin)
        if base.is_dir():
            out[origin] = {read_entry(m.parent).id for m in base.glob(f"*/{META}")}
    return out


def add(settings: Settings, entry: Entry, src: Path | None = None) -> Path:
    """Create the artifact directory, copy files from *src*, write meta.json."""
    if entry.origin not in ORIGINS:
        raise ConfigError(f"origin must be one of {ORIGINS}, got {entry.origin!r}")
    if src is not None and not src.is_dir():
        raise ConfigError(f"no such directory: {src}")
    entry.id = artifact_id(entry.id)
    dest = settings.origin_dir(entry.origin) / dirname(entry.title, entry.id)
    dest.mkdir(parents=True, exist_ok=True)
    if src is not None:
        shutil.copytree(src, dest, dirs_exist_ok=True)
    entry.downloaded = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (dest / META).write_text(json.dumps(asdict(entry), indent=2) + "\n")
    return dest


def entries_under(base: Path) -> list[tuple[Path, Entry]]:
    """All (artifact dir, Entry) at any depth below *base*, newest first."""
    found = [(m.parent, read_entry(m.parent)) for m in base.rglob(META)]
    return sorted(found, key=lambda pe: (pe[1].updated, pe[1].title), reverse=True)
