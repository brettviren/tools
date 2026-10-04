"""Per-repo records, decisions, and a locked atomic JSON store with history."""

import fcntl
import fnmatch
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from tools.gitea_mirror.github import GithubRepo

log = logging.getLogger(__name__)

MANUAL_SOURCE = "manual"
CATEGORIES = (
    "pending", "mirror", "skip", "mirrored", "missing", "left",
    "manual", "gone", "failed", "all",
)


def name_matches(full_name: str, patterns: list[str]) -> bool:
    """True if FULL_NAME matches any glob; a glob without "/" matches the repo name."""
    full = full_name.lower()
    name = full.split("/", 1)[-1]
    return any(fnmatch.fnmatch(full if "/" in p else name, p.lower()) for p in patterns)


def now() -> str:
    """Current UTC time as ISO-8601 seconds."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class LockTimeout(Exception):
    """Another gitea-mirror process holds the state lock."""


@dataclass
class Record:
    """Everything known and decided about one GitHub repo."""

    full_name: str
    source: str
    meta: dict = field(default_factory=dict)
    first_seen: str = ""
    last_seen: str = ""
    rule: dict | None = None
    manual: dict | None = None
    on_unmirror: str | None = None
    gitea_name: str | None = None
    gitea: dict | None = None
    gone: bool = False
    error: str | None = None

    @property
    def decision(self) -> str:
        """Manual override if any, else the rule's action, else review."""
        if self.manual:
            return self.manual["action"]
        return self.rule["action"] if self.rule else "review"

    @property
    def repo(self) -> GithubRepo:
        """The GitHub facts as an object."""
        return GithubRepo.from_meta(self.full_name, self.meta)

    def in_category(self, cat: str) -> bool:
        """True if this record belongs to list category CAT."""
        if cat == "all":
            return True
        if cat == "gone":
            return self.gone
        if cat == "mirrored":
            return self.gitea is not None
        if cat == "manual":
            return self.manual is not None
        if cat == "failed":
            return self.error is not None
        if cat == "left":
            return self.gitea is not None and self.decision != "mirror"
        if cat == "missing":
            return self.decision == "mirror" and self.gitea is None and not self.gone
        if cat == "pending":
            return self.decision == "review" and not self.gone
        return self.decision == cat


@dataclass
class State:
    """All records plus scan bookkeeping and unsaved history events."""

    records: dict[str, Record] = field(default_factory=dict)
    untracked: list[str] = field(default_factory=list)
    last_scan: str | None = None
    events: list[dict] = field(default_factory=list)

    def event(self, kind: str, repo: str, **fields) -> None:
        """Queue a history event to be written with the state."""
        self.events.append({"at": now(), "event": kind, "repo": repo, **fields})

    def get(self, full_name: str) -> Record | None:
        """Case-insensitive record lookup."""
        rec = self.records.get(full_name)
        if rec:
            return rec
        low = full_name.lower()
        return next((r for k, r in self.records.items() if k.lower() == low), None)

    def select(self, patterns: list[str] | None = None, category: str = "all",
               source: str | None = None) -> list[Record]:
        """Records matching any glob in PATTERNS, in CATEGORY, from SOURCE."""
        out = []
        for key in sorted(self.records, key=str.lower):
            rec = self.records[key]
            if patterns and not name_matches(key, patterns):
                continue
            if source and rec.source != source:
                continue
            if rec.in_category(category):
                out.append(rec)
        return out

    def to_json(self) -> dict:
        """Serializable form (without pending events)."""
        return {
            "version": 1,
            "last_scan": self.last_scan,
            "untracked": self.untracked,
            "records": {k: asdict(r) for k, r in sorted(self.records.items())},
        }

    @classmethod
    def from_json(cls, data: dict) -> "State":
        """Inverse of to_json."""
        return cls(
            records={k: Record(**v) for k, v in data.get("records", {}).items()},
            untracked=list(data.get("untracked", [])),
            last_scan=data.get("last_scan"),
        )


class Store:
    """The on-disk home of State: state.json, history.jsonl and a lock."""

    def __init__(self, directory: Path, lock_timeout: float = 120):
        self.dir = Path(directory)
        self.state_path = self.dir / "state.json"
        self.history_path = self.dir / "history.jsonl"
        self.lock_path = self.dir / "lock"
        self.report_path = self.dir / "last-report.json"
        self.lock_timeout = lock_timeout

    def load(self) -> State:
        """Read state without locking (a snapshot)."""
        if not self.state_path.exists():
            return State()
        return State.from_json(json.loads(self.state_path.read_text()))

    def _save(self, state: State) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_json(), indent=1) + "\n")
        os.replace(tmp, self.state_path)
        if state.events:
            with open(self.history_path, "a") as f:
                f.writelines(json.dumps(ev) + "\n" for ev in state.events)
            state.events.clear()

    def _acquire(self, fd: int) -> None:
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"state is locked: {self.lock_path}") from None
                time.sleep(0.5)

    @contextmanager
    def transaction(self):
        """Lock, load, yield State, then save it (unless an exception escapes)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lock:
            self._acquire(lock.fileno())
            state = self.load()
            yield state
            self._save(state)

    def history(self, patterns: list[str] | None = None) -> list[dict]:
        """History events, optionally only for repos matching PATTERNS."""
        if not self.history_path.exists():
            return []
        events = [json.loads(line) for line in self.history_path.read_text().splitlines() if line]
        if not patterns:
            return events
        return [e for e in events
                if name_matches(e["repo"], patterns)]

    def write_report(self, report: dict) -> None:
        """Save the latest sync report for notifiers."""
        self.dir.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(json.dumps(report, indent=1) + "\n")
