"""Embedding backends.

``Embedder`` is the interface the rest of egs uses.  A backend only needs to
implement ``_embed(texts)`` (raw strings in, vectors out); prefixing for
query vs. document use, normalization and media hooks live in the base class.
Add a backend by subclassing and registering it in ``BACKENDS``.
"""

import atexit
import base64
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from tools.egs import mediautil
from tools.egs.db import ModelRec

QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: {title} | text: "


class EmbedError(RuntimeError):
    """Embedding failed (backend not found, input too long, ...)."""


class TooLong(EmbedError):
    """The input exceeded the model context; the caller may retry with less."""


class Unsupported(EmbedError):
    """The backend cannot embed this kind of input."""


_PLACEHOLDERS = ("<|image|>", "<|video|>", "<|audio|>")


def _clean(text: str) -> str:
    """Defang the model's media placeholders so file text cannot be read as media markers."""
    for ph in _PLACEHOLDERS:
        text = text.replace(ph, ph.replace("<|", "< |"))
    return text


def _normalize(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)


class Embedder:
    """Base embedder.  Subclasses set ``kinds`` and implement ``_embed``."""

    kinds: frozenset[str] = frozenset({"text"})
    _marker: str | None = None
    max_bytes: int = 6000          # text window size appropriate for the context

    def _embed(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def query(self, text: str) -> np.ndarray:
        """Embed a search query."""
        return _normalize(self._embed([QUERY_PREFIX + _clean(text)]))[0]

    def document(self, text: str, title: str = "none") -> np.ndarray:
        """Embed a document (or document window)."""
        return _normalize(self._embed([DOC_PREFIX.format(title=_clean(title)) + _clean(text)]))[0]

    image_suffixes: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".bmp"})
    window_seconds = {"audio": 300, "video": 50}   # 25 audio tokens/s, ~140 per video frame at 1 fps: 8192-token context

    def _embed_media(self, kind: str, path: Path) -> np.ndarray:
        """Embed one prepared piece of media (backends with media support override)."""
        raise Unsupported(f"{type(self).__name__} cannot embed {kind}")

    def _embed_windows(self, kind, path, windows):
        out = []
        for start, length in windows:
            with mediautil.cut(kind, path, start, length) as piece:
                out.append((start, length, _normalize(self._embed_media(kind, piece))))
        return out

    def media(self, kind: str, path: Path) -> list[tuple[int, int, np.ndarray]]:
        """Embed image/audio/video as (start_s, length_s, vector) windows.

        An image is one window with start and length 0.
        """
        if kind not in self.kinds:
            raise Unsupported(f"{type(self).__name__} cannot embed {kind}")
        try:
            if kind == "image":
                with mediautil.image_as(path, self.image_suffixes) as img:
                    return [(0, 0, _normalize(self._embed_media(kind, img)))]
            window = self.window_seconds[kind]
            total = mediautil.duration(path)
            while True:
                try:
                    return self._embed_windows(kind, path, mediautil.spans(total, window))
                except TooLong:             # token-dense media: retry with shorter windows
                    if window < 4:
                        raise
                    window /= 2
        except mediautil.MediaError as err:
            raise EmbedError(str(err)) from err

    def close(self):
        """Release resources."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _usable(kinds) -> frozenset[str]:
    """Requested kinds (plus text) that can work here: audio/video need ffmpeg."""
    ok = {"text", "image"} | ({"audio", "video"} if mediautil.have_ffmpeg() else set())
    return frozenset((set(kinds) | {"text"}) & ok)


def find_llama_server() -> str:
    """Locate llama-server via $EGS_LLAMA_SERVER, PATH, then ~/opt/llama-cpp/bin."""
    for cand in (os.environ.get("EGS_LLAMA_SERVER"),
                 shutil.which("llama-server"),
                 str(Path.home() / "opt/llama-cpp/bin/llama-server")):
        if cand and Path(cand).exists():
            return cand
    raise EmbedError("llama-server not found; set EGS_LLAMA_SERVER")


class LlamaServerEmbedder(Embedder):
    """Runs a private llama-server and calls its /v1/embeddings endpoint."""

    def __init__(self, model: ModelRec, kinds=frozenset({"text"}), timeout: float = 300):
        self.timeout = timeout
        media = bool(kinds - {"text"}) and bool(model.mmproj)
        ctx = 8192 if media else 4096       # a large batch slows text embedding
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self._log = tempfile.NamedTemporaryFile("w+", prefix="egs-llama-", suffix=".log")
        cmd = [find_llama_server(), "-m", model.path, "--embeddings", "--pooling", "mean",
               "-c", str(ctx), "-b", str(ctx), "-ub", str(ctx), "--parallel", "1",
               "--host", "127.0.0.1", "--port", str(port)]
        cmd += shlex.split(os.environ.get("EGS_LLAMA_ARGS", ""))     # eg "-ngl 0" for CPU only
        if media:
            cmd += ["--mmproj", model.mmproj]
            self.kinds = _usable(kinds)
        self.proc = subprocess.Popen(cmd, stdout=self._log, stderr=subprocess.STDOUT)
        atexit.register(self.close)
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.time() + 300
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise EmbedError(f"llama-server exited:\n{self._tail()}")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2):
                    self._marker = self._get("/props").get("media_marker")
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(0.3)
        raise EmbedError(f"llama-server not ready:\n{self._tail()}")

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.url + path, timeout=10) as r:
            return json.load(r)

    def _tail(self) -> str:
        self._log.flush()
        self._log.seek(0)
        return "".join(self._log.readlines()[-15:])

    def _embed(self, texts):
        return self._post(texts)

    def _embed_media(self, kind, path):
        if not self._marker:
            raise Unsupported("llama-server did not report a media marker")
        data = base64.b64encode(path.read_bytes()).decode()
        return self._post([{"prompt_string": self._marker, "multimodal_data": [data]}])[0]

    def _post(self, inputs) -> np.ndarray:
        body = json.dumps({"input": inputs, "encoding_format": "float"}).encode()
        req = urllib.request.Request(self.url + "/v1/embeddings", body,
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)["data"]
        except urllib.error.HTTPError as err:
            msg = err.read().decode(errors="replace")[:300]
            raise (TooLong if "too large" in msg else EmbedError)(f"llama-server: {msg}") from err
        vecs = np.array([d["embedding"] for d in sorted(data, key=lambda d: d["index"])],
                        dtype=np.float32)
        if not np.isfinite(vecs).all():
            raise EmbedError("non-finite embedding (try a BF16/Q8_0 model)")
        return vecs

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class TransformersEmbedder(Embedder):
    """sentence-transformers backend: text, image, audio and video.

    Device is chosen by $EGS_DEVICE (default: cuda if available, else cpu) and
    weights are float32 (the model card warns against float16).
    """

    PLACEHOLDER = {"image": "<|image|>", "audio": "<|audio|>", "video": "<|video|>"}
    image_suffixes = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"})

    def __init__(self, model: ModelRec, kinds=frozenset({"text"})):
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as err:
            raise EmbedError("backend 'transformers' needs the 'transformers' extra: "
                             "uv tool install -e '.[transformers]'") from err
        self._torch = torch
        device = os.environ.get("EGS_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        self.kinds = _usable(kinds)
        # Load only the encoders needed for the requested kinds.
        cfg = {}
        if not self.kinds & {"image", "video"}:
            cfg["vision_config"] = None
        if "audio" not in self.kinds:
            cfg["audio_config"] = None
        self.model = SentenceTransformer(model.path, device=device, config_kwargs=cfg,
                                         model_kwargs={"torch_dtype": torch.float32})
        self.model.max_seq_length = 8192

    def close(self):
        if getattr(self, "model", None) is not None:
            self.model = None
            import gc
            gc.collect()
            if self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()

    def _embed(self, texts):
        return np.asarray(self.model.encode(texts, prompt="", convert_to_numpy=True),
                          dtype=np.float32)

    def _embed_media(self, kind, path):
        inp = {"text": self.PLACEHOLDER[kind], kind: [str(path)] if kind != "video" else str(path)}
        try:
            return np.asarray(self.model.encode(inp, convert_to_numpy=True), dtype=np.float32)
        except self._torch.OutOfMemoryError as err:
            self._torch.cuda.empty_cache()
            raise TooLong("out of GPU memory") from err


BACKENDS = {"llama": LlamaServerEmbedder, "transformers": TransformersEmbedder}


def open_embedder(model: ModelRec, kinds=frozenset({"text"})) -> Embedder:
    """Instantiate the backend a model record calls for, loading what ``kinds`` needs."""
    try:
        return BACKENDS[model.backend](model, frozenset(kinds))
    except KeyError as err:
        raise EmbedError(f"unknown backend {model.backend!r}") from err
