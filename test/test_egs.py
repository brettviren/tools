"""Tests for egs that do not need a model (a fake embedder stands in)."""

import hashlib

import numpy as np

from tools.egs import chunk, core
from tools.egs.db import Database, ModelRec
from tools.egs.embed import Embedder
from tools.egs.vectors import BruteForceIndex, level_dim, truncate
from tools.egs.walk import Walker

MODEL = ModelRec("m", "r", "llama", "/x", None, (8, 4, 2))


class FakeEmbedder(Embedder):
    """Hash-based bag-of-words embedder: shared words give similar vectors."""
    max_bytes = 100

    def __init__(self):
        self.calls = 0

    def _embed(self, texts):
        self.calls += 1
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.split():
                out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 8] += 1
        return out


def test_windows_cover_and_bound():
    data = ("line of text é\n" * 40).encode()
    spans = chunk.windows(data, 100, 10)
    assert spans[0][0] == 0 and spans[-1][1] == len(data)
    assert all(b - a <= 100 for a, b in spans)
    for a, b in spans:
        data[a:b].decode("utf-8")           # never splits a character
    assert chunk.windows(b"", 100, 10) == []
    assert chunk.windows(b"abc", 100, 10) == [(0, 3)]


def test_level_dim():
    assert [level_dim((768, 512, 256, 128), n) for n in (0, 1, 3, 9)] == [768, 512, 128, 128]


def test_truncate_renormalizes():
    v = truncate(np.array([3.0, 4.0, 12.0]), 2)
    assert np.allclose(np.linalg.norm(v), 1.0)


def test_walker_ignores_and_includes(tmp_path):
    (tmp_path / ".gitignore").write_text("skip.txt\nbuild/\n")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "skip.txt").write_text("s")
    (tmp_path / "data.xyz").write_text("x")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / "build").mkdir()
    (tmp_path / "build/c.txt").write_text("c")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".ignore").write_text("*.md\n")
    (sub / "d.txt").write_text("d")
    (sub / "e.md").write_text("e")

    def names(**kw):
        paths = kw.pop("paths", [tmp_path])
        return {c.path.name for c in Walker(**kw).candidates(paths)}

    assert names() == {"a.txt", "b.md"}
    assert names(recursive=True) == {"a.txt", "b.md", "d.txt"}
    assert names(recursive=True, exclude=["b.md"]) == {"a.txt", "d.txt"}
    assert "data.xyz" in names(include=["*.xyz"])
    assert names(paths=[tmp_path / "skip.txt"]) == {"skip.txt"}   # explicit files kept


def test_scan_change_detection_and_search(tmp_path):
    db = Database(":memory:")
    emb = FakeEmbedder()
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("apple banana cherry\n" * 20)          # > 100 bytes: windowed
    b.write_text("zebra yak xylophone")
    cands = list(Walker().candidates([tmp_path]))

    res = {r.path.name: r for r in core.scan(db, MODEL, lambda kinds: emb, cands)}
    assert res["a.txt"].status == "new" and res["a.txt"].nvec > 2     # chunks + mean
    assert res["b.txt"].nvec == 1
    roles = [r[0] for r in db.con.execute("SELECT role FROM vectors")]
    assert roles.count("mean") == 1

    calls = emb.calls
    assert {r.status for r in core.scan(db, MODEL, lambda kinds: emb, cands)} == {"unchanged"}
    assert emb.calls == calls

    b.write_text("zebra yak xylophone changed")
    assert [r.status for r in core.scan(db, MODEL, lambda kinds: emb, [cands[1]])] == ["updated"]

    hits = BruteForceIndex(db).search("m", emb.query("zebra yak"), 8, 5)
    assert hits[0].path == str(b)
    hits = BruteForceIndex(db).search("m", emb.query("apple cherry"), 4, 5)
    assert len(hits) == 2                       # one best hit per file, truncated dims


def test_media_spans_cover_duration():
    from tools.egs.mediautil import spans
    assert spans(45, 20) == [(0, 20), (20, 20), (40, 5)]
    assert spans(0.4, 20) == [(0, 1)]


def test_placeholders_defanged():
    from tools.egs.embed import _clean
    assert "<|image|>" not in _clean("see <|image|> here")


def test_too_long_retries_with_smaller_windows(tmp_path):
    from tools.egs.embed import TooLong

    class Picky(FakeEmbedder):
        max_bytes = 400

        def _embed(self, texts):
            if len(texts[0]) > 150:
                raise TooLong("too large")
            return super()._embed(texts)

    f = tmp_path / "t.txt"
    f.write_text("word " * 200)
    _, rows = core.embed_text_file(Picky(), f)
    assert all(r.length <= 150 for r in rows if r.role == "chunk")


def test_scan_groups_by_kind_in_order(tmp_path):
    from tools.egs.walk import Candidate
    opened = []

    class Media(FakeEmbedder):
        kinds = frozenset({"text", "image", "video"})

        def __init__(self, k):
            super().__init__()
            opened.append(k)
            self.closed = False

        def media(self, kind, path):
            return [(0, 0, np.ones(8, dtype=np.float32) / 8 ** 0.5)]

        def close(self):
            self.closed = True

    files = {}
    for name, kind in [("a.mp4", "video"), ("b.txt", "text"), ("c.png", "image"),
                       ("d.txt", "text"), ("e.mp3", "audio")]:
        (tmp_path / name).write_text("data " + name)
        files[name] = Candidate(tmp_path / name, kind)
    db = Database(":memory:")
    res = {r.path.name: r.status for r in core.scan(db, MODEL, Media, files.values())}
    assert opened == [frozenset({"text"}), frozenset({"image"}),
                      frozenset({"audio"}), frozenset({"video"})]
    assert res["e.mp3"] == "skipped" and res["a.mp4"] == "new" and res["d.txt"] == "new"
