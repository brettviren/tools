"""Model catalog, discovery of available instances, and download."""

import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tools.egs.db import Database, ModelRec, data_home

HF = "https://huggingface.co"
MRL_DIMS = (768, 512, 256, 128)

# repo -> (backend, description, fallback gguf files used when offline)
CATALOG = {
    "google/embeddinggemma-2": (
        "transformers", "official Google weights (safetensors); default API", []),
    "unsloth/embeddinggemma-2": (
        "transformers", "unsloth mirror of the official weights", []),
    "ggml-org/embeddinggemma-2-GGUF": (
        "llama", "ggml-org GGUF conversion (llama.cpp)",
        ["embeddinggemma-2-BF16.gguf", "embeddinggemma-2-Q8_0.gguf"]),
    "unsloth/embeddinggemma-2-GGUF": (
        "llama", "unsloth GGUF quantizations (llama.cpp)",
        ["embeddinggemma-2-BF16.gguf", "embeddinggemma-2-F16.gguf",
         "embeddinggemma-2-Q8_0.gguf", "embeddinggemma-2-UD-Q4_K_XL.gguf",
         "embeddinggemma-2-UD-Q5_K_XL.gguf", "embeddinggemma-2-UD-Q6_K_XL.gguf"]),
}


@dataclass
class Available:
    """A model instance one could download."""
    id: str
    repo: str
    backend: str
    file: str | None
    mmproj: str | None
    description: str


def _hf_files(repo: str) -> list[str] | None:
    """File names in a HF repo, or None if offline."""
    try:
        with urllib.request.urlopen(f"{HF}/api/models/{repo}", timeout=10) as r:
            return [s["rfilename"] for s in json.load(r)["siblings"]]
    except (OSError, KeyError, ValueError):
        return None


def _quant(fname: str) -> str:
    stem = fname.removesuffix(".gguf")
    return stem.removeprefix("embeddinggemma-2-")


def _pick_mmproj(files: list[str], quant: str) -> str | None:
    """Prefer an mmproj matching the quant, else Q8_0, F16, BF16."""
    projs = [f for f in files if "mmproj" in f]
    for want in (quant, "Q8_0", "F16", "BF16"):
        for f in projs:
            if f.endswith(f"-{want}.gguf"):
                return f
    return projs[0] if projs else None


def survey(online: bool = True) -> list[Available]:
    """List known downloadable instances, refreshed from HF when reachable."""
    out = []
    for repo, (backend, desc, fallback) in CATALOG.items():
        if backend != "llama":
            out.append(Available(repo, repo, backend, None, None, desc))
            continue
        live = _hf_files(repo) if online else None
        files = live or fallback
        ggufs = sorted(f for f in files if f.endswith(".gguf") and "mmproj" not in f)
        for f in ggufs:
            q = _quant(f)
            mm = _pick_mmproj(files, q) if live else None
            out.append(Available(f"{repo}:{q}", repo, backend, f, mm, desc))
    return out


def find_available(model_id: str, online: bool = True) -> Available:
    """Resolve a (possibly partial) model id against the survey."""
    avail = survey(online)
    exact = [a for a in avail if a.id == model_id]
    if exact:
        return exact[0]
    partial = [a for a in avail if a.id.endswith(model_id) or a.id.split("/")[-1] == model_id]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise ValueError(f"ambiguous model {model_id!r}: " + ", ".join(a.id for a in partial))
    raise ValueError(f"unknown model {model_id!r}; see 'egs model survey'")


def models_dir() -> Path:
    """Where models are downloaded."""
    return data_home() / "models"


def _download(url: str, dest: Path):
    """Stream a URL to ``dest`` via a .part file."""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url)
    if tok := os.environ.get("HF_TOKEN"):
        req.add_header("Authorization", f"Bearer {tok}")
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as fp:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while block := r.read(1 << 20):
            fp.write(block)
            done += len(block)
            if total and sys.stderr.isatty():
                print(f"\r{dest.name}: {done >> 20}/{total >> 20} MiB", end="", file=sys.stderr)
    if sys.stderr.isatty():
        print(file=sys.stderr)
    part.rename(dest)


def get_model(db: Database, model_id: str, mmproj: bool = True) -> ModelRec:
    """Download a model instance, register it in the database and return it."""
    a = find_available(model_id)
    d = models_dir() / a.repo.replace("/", "__")
    if a.backend == "llama":
        _download(f"{HF}/{a.repo}/resolve/main/{a.file}", d / a.file)
        mm = None
        if mmproj and a.mmproj:
            _download(f"{HF}/{a.repo}/resolve/main/{a.mmproj}", d / a.mmproj)
            mm = str(d / a.mmproj)
        rec = ModelRec(a.id, a.repo, "llama", str(d / a.file), mm, MRL_DIMS)
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as err:
            raise ValueError("backend 'transformers' needs huggingface_hub "
                             "(uv add --optional transformers sentence-transformers)") from err
        path = snapshot_download(a.repo, local_dir=d)
        rec = ModelRec(a.id, a.repo, "transformers", str(path), None, MRL_DIMS)
    db.put_model(rec)
    if db.get_meta("default_model") is None:
        db.set_meta("default_model", rec.id)
    return rec


def default_model(db: Database) -> ModelRec:
    """The model marked for use, or an error explaining how to get one."""
    mid = db.get_meta("default_model")
    rec = db.model(mid) if mid else None
    if rec is None:
        raise ValueError("no default model; try 'egs model survey' then 'egs model get <id>'")
    return rec


def use_model(db: Database, model_id: str) -> ModelRec:
    """Mark a local model as the default."""
    rec = db.model(model_id)
    if rec is None:
        cands = [m for m in db.models() if m.id.endswith(model_id)]
        if len(cands) != 1:
            raise ValueError(f"no unique local model {model_id!r}; see 'egs model list'")
        rec = cands[0]
    db.set_meta("default_model", rec.id)
    return rec
