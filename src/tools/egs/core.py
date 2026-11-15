"""Scanning (embedding files into the database) and searching."""

import hashlib
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tools.egs import chunk
from tools.egs.db import Database, FileRec, ModelRec, VectorRow
from tools.egs.embed import Embedder, EmbedError, TooLong, Unsupported
from tools.egs.vectors import BruteForceIndex, Hit, level_dim
from tools.egs.walk import Candidate

log = logging.getLogger(__name__)
OVERLAP_FRACTION = 10


@dataclass
class ScanResult:
    """Outcome for one file: new|updated|unchanged|skipped|error."""
    path: Path
    status: str
    nvec: int = 0
    detail: str = ""


def file_sha256(path: Path) -> str:
    """Hex SHA-256 of a file's content."""
    h = hashlib.sha256()
    with path.open("rb") as fp:
        while block := fp.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def check_changed(db: Database, model: ModelRec, path: Path) -> tuple[str, FileRec | None, str]:
    """Decide whether a file needs (re)embedding.

    Returns (status, previous record, sha256) with status one of
    "new", "updated" or "unchanged".  An mtime change alone does not force
    re-embedding if the content hash is the same.
    """
    st = path.stat()
    rec = db.file(str(path), model.id)
    if rec is None:
        return "new", None, file_sha256(path)
    if rec.mtime_ns == st.st_mtime_ns and rec.size == st.st_size:
        return "unchanged", rec, rec.sha256
    sha = file_sha256(path)
    if sha == rec.sha256:
        db.touch_file(rec, st.st_mtime_ns)
        return "unchanged", rec, sha
    return "updated", rec, sha


def _embed_windows(emb: Embedder, path: Path, data: bytes, size: int) -> list[VectorRow]:
    rows = []
    for i, (a, b) in enumerate(chunk.windows(data, size, size // OVERLAP_FRACTION)):
        vec = emb.document(data[a:b].decode("utf-8", "replace"), title=path.name)
        rows.append(VectorRow(0, i, "chunk", "bytes", a, b - a, vec))
    return rows


def embed_text_file(emb: Embedder, path: Path) -> tuple[str, list[VectorRow]]:
    """Embed a text file in windows; returns (summary, vectors).

    Files that need more than one window also get a "mean" vector.
    """
    data = path.read_bytes()
    if not data.strip():
        raise EmbedError("empty file")
    size = emb.max_bytes
    while True:
        try:
            rows = _embed_windows(emb, path, data, size)
            break
        except TooLong:                 # token-dense text: retry with smaller windows
            if size < 200:
                raise
            size //= 2
    if len(rows) > 1:
        mean = np.mean([r.vec for r in rows], axis=0)
        mean /= max(np.linalg.norm(mean), 1e-12)
        rows.append(VectorRow(0, len(rows), "mean", "bytes", 0, len(data), mean))
    return chunk.head_summary(data[:4096].decode("utf-8", "replace")), rows


def embed_media_file(emb: Embedder, kind: str, path: Path) -> tuple[str, list[VectorRow]]:
    """Embed image/audio/video; audio and video windows are measured in seconds."""
    wins = emb.media(kind, path)
    if kind == "image":
        size = path.stat().st_size
        return f"image: {path.name}", [VectorRow(0, 0, "chunk", "bytes", 0, size, wins[0][2])]
    rows = [VectorRow(0, i, "chunk", "seconds", s, d, v) for i, (s, d, v) in enumerate(wins)]
    total = rows[-1].start + rows[-1].length
    if len(rows) > 1:
        mean = np.mean([r.vec for r in rows], axis=0)
        mean /= max(np.linalg.norm(mean), 1e-12)
        rows.append(VectorRow(0, len(rows), "mean", "seconds", 0, total, mean))
    return f"{kind}: {path.name} ({total}s)", rows


KIND_ORDER = ("text", "image", "audio", "video")


def _scan_one(db: Database, model: ModelRec, emb: Embedder, c: Candidate,
              status: str, sha: str) -> ScanResult:
    try:
        if c.kind == "text":
            summary, rows = embed_text_file(emb, c.path)
        else:
            summary, rows = embed_media_file(emb, c.kind, c.path)
        st = c.path.stat()
        db.replace_file(str(c.path), model.id, c.kind, st.st_mtime_ns, st.st_size,
                        sha, summary, rows)
        return ScanResult(c.path, status, len(rows))
    except Unsupported as err:
        return ScanResult(c.path, "skipped", detail=str(err))
    except (EmbedError, OSError) as err:
        return ScanResult(c.path, "error", detail=str(err))


def scan(db: Database, model: ModelRec, open_embedder: Callable[[frozenset], Embedder],
         cands: Iterable[Candidate], force: bool = False) -> Iterator[ScanResult]:
    """Embed each candidate that is new or changed, one file kind at a time.

    Unchanged files are reported first.  The rest are grouped by kind and, in
    ``KIND_ORDER``, an embedder loading only that kind's encoder is opened,
    used for every file of the kind, and closed before the next kind.
    """
    pending: dict[str, list] = {k: [] for k in KIND_ORDER}
    for c in cands:
        try:
            status, _, sha = check_changed(db, model, c.path)
        except OSError as err:
            yield ScanResult(c.path, "error", detail=str(err))
            continue
        if status == "unchanged" and not force:
            yield ScanResult(c.path, "unchanged")
        else:
            pending.setdefault(c.kind, []).append((c, "updated" if status == "unchanged" else status, sha))
    for kind, items in pending.items():
        if not items:
            continue
        try:
            emb = open_embedder(frozenset({kind}))
        except EmbedError as err:
            for c, _, _ in items:
                yield ScanResult(c.path, "error", detail=str(err))
            continue
        with emb:
            for c, status, sha in items:
                if kind not in emb.kinds:
                    yield ScanResult(c.path, "skipped", detail=f"{kind} unsupported by backend")
                else:
                    yield _scan_one(db, model, emb, c, status, sha)


def search(db: Database, model: ModelRec, emb: Embedder, text: str,
           level: int = 0, limit: int = 10) -> list[Hit]:
    """Embed ``text`` and return the best-matching files at a truncation level."""
    dim = level_dim(model.dims, level)
    return BruteForceIndex(db).search(model.id, emb.query(text), dim, limit)


def describe(db: Database, hit: Hit) -> tuple[str, str, str]:
    """(file summary, location in file, context excerpt) for a hit.

    Location and excerpt are empty unless the hit is one window of several.
    """
    row = db.con.execute("SELECT summary FROM files WHERE id=?", (hit.file_id,)).fetchone()
    summary = row[0] if row else ""
    if hit.role != "chunk" or db.chunk_count(hit.file_id) < 2:
        return summary, "", ""
    if hit.unit == "seconds":
        return summary, f"{hit.start}s-{hit.start + hit.length}s", ""
    context = ""
    try:
        with open(hit.path, "rb") as fp:
            fp.seek(hit.start)
            context = chunk.snippet(fp.read(min(hit.length, 2048)).decode("utf-8", "replace"))
    except OSError:
        pass
    return summary, f"bytes {hit.start}+{hit.length}", context
