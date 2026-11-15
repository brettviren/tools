"""Windowing of text into context-sized pieces, tracked by byte offsets."""


def windows(data: bytes, size: int, overlap: int) -> list[tuple[int, int]]:
    """Split ``data`` into (start, end) byte spans of at most ``size`` bytes.

    Spans end at a newline when one is reasonably near and never split a UTF-8
    sequence.  Consecutive spans share about ``overlap`` bytes.
    """
    n = len(data)
    if n <= size:
        return [(0, n)] if n else []
    spans, start = [], 0
    while start < n:
        end = min(start + size, n)
        if end < n:
            nl = data.rfind(b"\n", start + size // 2, end)
            if nl >= 0:
                end = nl + 1
            else:
                while end > start and (data[end] & 0xC0) == 0x80:
                    end -= 1
        spans.append((start, end))
        if end >= n:
            break
        nxt = end - overlap
        nl = data.find(b"\n", nxt, end)
        nxt = nl + 1 if nl >= 0 else nxt
        while nxt < n and (data[nxt] & 0xC0) == 0x80:
            nxt += 1
        start = nxt if nxt > start else end
    return spans


def head_summary(text: str, lines: int = 3, width: int = 240) -> str:
    """Brief summary: the first few non-blank lines, whitespace collapsed."""
    got = [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()][:lines]
    out = " | ".join(got)
    return out if len(out) <= width else out[: width - 1] + "…"


def snippet(text: str, width: int = 200) -> str:
    """Whitespace-collapsed excerpt for showing a matching window."""
    out = " ".join(text.split())
    return out if len(out) <= width else out[: width - 1] + "…"
