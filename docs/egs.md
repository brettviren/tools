# egs

Semantic search over local files with EmbeddingGemma 2 (text, code, image, audio, video).

    egs model survey|get <id>|list|use <id>
    egs scan [-r] [-e PAT] [-i PAT] [-f] PATH...
    egs search [-l LEVEL] [-s] [-n N] WORDS...

State (models, files, vectors) is in SQLite (`$EGS_DB`, default `$XDG_DATA_HOME/egs/egs.db`).
Vectors are stored at full length; `-l` truncates (Matryoshka: 768, 512, 256, 128) and renormalizes at query time.

## Backends

The default API is `transformers` (torch); `google/embeddinggemma-2` heads `egs model survey`.  A model's backend is chosen by the model entry:

- `llama`: private `llama-server` (found via `$EGS_LLAMA_SERVER`, `PATH`, `~/opt/llama-cpp/bin`).
  GGUF models; the mmproj file (downloaded by `model get`) enables image/audio/video.
  Extra server args: `$EGS_LLAMA_ARGS` (eg `-ngl 0`).
- `transformers`: sentence-transformers, float32.  `uv tool install -e '.[transformers]'`.
  `$EGS_DEVICE` (cpu|cuda).

`scan` collects all files, skips unchanged ones, then handles one kind at a time in the order
text, image, audio, video: it loads only that kind's encoder, embeds every file of the kind and
frees the encoder before the next.  `search` loads only the text encoder.

Audio and video need `ffmpeg`.  They are cut into windows sized for the 8192-token context
(300 s of audio at 25 tokens/s, 50 s of video at ~140 tokens/frame, 1 frame/s); each window is a
vector, plus a mean vector.  A window that runs out of context or GPU memory is retried at half size.

Vectors from different models are never mixed: switching the default model re-scans files.

## Measurements (GTX 1650 4GB, 12 cores; 16 text windows of ~3 kB, 3 images, 2 audio of 34 s, one 20 s video)

Seconds per item.  llama = ggml-org Q8_0; torch = google fp32.

| backend      | load | text | query | image | audio | video |
|--------------|------|------|-------|-------|-------|-------|
| llama GPU    | 8    | 0.4-0.7 | 0.014-0.05 | 0.4-0.5 | 1.3-1.7 | 41 |
| llama CPU    | 3    | 0.73 | 0.06 | 3.5   | 5.2   | 179 |
| torch GPU    | 8-9  | 0.30 | 0.04-0.05 | 1.4 | 1.1  | 14 |
| torch CPU    | 9    | 1.16 | 0.08 | 5.9   | 3.5   | 47 |

Cosine to the torch-fp32 vectors: llama text 0.9998, audio 0.997, video 0.93, image ~0.63.
Loading one encoder at a time is what lets fp32 torch fit on a 4 GB GPU (video windows of 45 s still needed halving there).
