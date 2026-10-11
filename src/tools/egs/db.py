"""SQLite state for egs: settings, models, files and embedding vectors."""

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS models(
    id TEXT PRIMARY KEY, repo TEXT, backend TEXT, path TEXT, mmproj TEXT,
    dims TEXT, added REAL);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, model_id TEXT NOT NULL,
    kind TEXT, mtime_ns INTEGER, size INTEGER, sha256 TEXT, summary TEXT,
    scanned REAL, UNIQUE(path, model_id));
CREATE TABLE IF NOT EXISTS vectors(
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq INTEGER, role TEXT, unit TEXT, start INTEGER, length INTEGER,
    dim INTEGER, vec BLOB);
CREATE INDEX IF NOT EXISTS vectors_file ON vectors(file_id);
"""


def default_db_path() -> Path:
    """Database location: $EGS_DB or $XDG_DATA_HOME/egs/egs.db."""
    if "EGS_DB" in os.environ:
        return Path(os.environ["EGS_DB"])
    return data_home() / "egs.db"


def data_home() -> Path:
    """Directory holding egs data (database, models)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "egs"


@dataclass
class ModelRec:
    """A locally available model instance."""
    id: str
    repo: str
    backend: str
    path: str
    mmproj: str | None
    dims: tuple[int, ...]


@dataclass
class FileRec:
    """Scan state of one file for one model."""
    id: int
    path: str
    kind: str
    mtime_ns: int
    size: int
    sha256: str
    summary: str


@dataclass
class VectorRow:
    """One stored vector and the span of its file that produced it."""
    file_id: int
    seq: int
    role: str          # "chunk" (one window) or "mean" (average over chunks)
    unit: str          # "bytes" for text; "seconds" for time-based media
    start: int
    length: int
    vec: np.ndarray    # full-length, L2 normalized


class Database:
    """Thin wrapper over the sqlite3 connection."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path))
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript(SCHEMA)

    def close(self):
        """Close the connection."""
        self.con.close()

    # -- settings
    def get_meta(self, key: str) -> str | None:
        """Return a setting or None."""
        row = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        """Store a setting."""
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))

    # -- models
    def put_model(self, m: ModelRec):
        """Register (or update) a local model."""
        with self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO models VALUES(?,?,?,?,?,?,?)",
                (m.id, m.repo, m.backend, m.path, m.mmproj,
                 ",".join(map(str, m.dims)), time.time()))

    def models(self) -> list[ModelRec]:
        """All local models."""
        rows = self.con.execute(
            "SELECT id,repo,backend,path,mmproj,dims FROM models ORDER BY id").fetchall()
        return [ModelRec(i, r, b, p, mm, tuple(int(d) for d in ds.split(",")))
                for i, r, b, p, mm, ds in rows]

    def model(self, model_id: str) -> ModelRec | None:
        """A local model by id."""
        return next((m for m in self.models() if m.id == model_id), None)

    # -- files
    def file(self, path: str, model_id: str) -> FileRec | None:
        """Scan state of a file under a model."""
        row = self.con.execute(
            "SELECT id,path,kind,mtime_ns,size,sha256,summary FROM files "
            "WHERE path=? AND model_id=?", (path, model_id)).fetchone()
        return FileRec(*row) if row else None

    def touch_file(self, rec: FileRec, mtime_ns: int):
        """Record a new mtime for unchanged content."""
        with self.con:
            self.con.execute("UPDATE files SET mtime_ns=?, scanned=? WHERE id=?",
                             (mtime_ns, time.time(), rec.id))

    def replace_file(self, path: str, model_id: str, kind: str, mtime_ns: int,
                     size: int, sha256: str, summary: str,
                     rows: list[VectorRow]) -> int:
        """Atomically (re)write a file's record and all its vectors."""
        with self.con:
            self.con.execute("DELETE FROM files WHERE path=? AND model_id=?",
                             (path, model_id))
            cur = self.con.execute(
                "INSERT INTO files(path,model_id,kind,mtime_ns,size,sha256,summary,scanned)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (path, model_id, kind, mtime_ns, size, sha256, summary, time.time()))
            fid = cur.lastrowid
            self.con.executemany(
                "INSERT INTO vectors(file_id,seq,role,unit,start,length,dim,vec)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(fid, r.seq, r.role, r.unit, r.start, r.length, len(r.vec),
                  r.vec.astype(np.float32).tobytes()) for r in rows])
        return fid

    def chunk_count(self, file_id: int) -> int:
        """Number of window vectors stored for a file."""
        return self.con.execute(
            "SELECT COUNT(*) FROM vectors WHERE file_id=? AND role='chunk'",
            (file_id,)).fetchone()[0]
