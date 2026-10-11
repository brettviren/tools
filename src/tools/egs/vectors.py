"""Vector similarity search with Matryoshka (MRL) truncation.

``VectorIndex`` is the swap point for another vector store (eg sqlite-vec).
``BruteForceIndex`` is an exact numpy implementation; truncation is trivial
there because full vectors are stored and sliced at query time.
"""

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from tools.egs.db import Database


def truncate(vecs: np.ndarray, dim: int) -> np.ndarray:
    """Keep the first ``dim`` components (last axis) and L2 renormalize."""
    out = np.asarray(vecs, dtype=np.float32)[..., :dim]
    norm = np.linalg.norm(out, axis=-1, keepdims=True)
    return out / np.maximum(norm, 1e-12)


def level_dim(dims: tuple[int, ...], level: int) -> int:
    """Dimension for a truncation level: 0 is the largest; beyond range the smallest."""
    ordered = sorted(dims, reverse=True)
    return ordered[min(max(level, 0), len(ordered) - 1)]


@dataclass
class Hit:
    """A matching vector."""
    file_id: int
    path: str
    score: float
    role: str
    unit: str
    start: int
    length: int


class VectorIndex(Protocol):
    """Similarity search over stored vectors."""

    def search(self, model_id: str, query: np.ndarray, dim: int, limit: int) -> list[Hit]:
        """Best hit per file for ``query`` compared at ``dim`` components."""
        ...


class BruteForceIndex:
    """Exact cosine search by loading vectors into numpy."""

    def __init__(self, db: Database):
        self.db = db

    def search(self, model_id, query, dim, limit):
        rows = self.db.con.execute(
            "SELECT v.file_id,f.path,v.role,v.unit,v.start,v.length,v.vec "
            "FROM vectors v JOIN files f ON f.id=v.file_id WHERE f.model_id=?",
            (model_id,)).fetchall()
        if not rows:
            return []
        mat = truncate(np.stack([np.frombuffer(r[6], dtype=np.float32) for r in rows]), dim)
        scores = mat @ truncate(query, dim)
        best: dict[int, tuple[float, int]] = {}
        for i, s in enumerate(scores):
            fid = rows[i][0]
            if fid not in best or s > best[fid][0]:
                best[fid] = (float(s), i)
        top = sorted(best.values(), reverse=True)[:limit]
        return [Hit(rows[i][0], rows[i][1], s, *rows[i][2:6]) for s, i in top]
