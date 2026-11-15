"""Find files to embed: directory traversal, ignore rules, kind detection."""

from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

TEXT_EXT = {
    ".txt", ".md", ".org", ".rst", ".tex", ".adoc", ".html", ".htm", ".xml", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".log", ".py", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".cxx", ".rs", ".go", ".java", ".js", ".ts", ".jsx", ".tsx",
    ".sh", ".bash", ".fish", ".zsh", ".el", ".lisp", ".scm", ".rb", ".pl", ".lua", ".sql",
    ".cmake", ".jsonnet", ".libsonnet", ".nix", ".css", ".scss", ".swift", ".kt", ".cs",
    ".bib", ".ipynb",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
IGNORE_FILES = (".gitignore", ".ignore")


@dataclass(frozen=True)
class Candidate:
    """A file selected for embedding and its kind (text|image|audio|video)."""
    path: Path
    kind: str


def kind_by_ext(path: Path) -> str | None:
    """Kind implied by the file extension, or None if not a supported type."""
    ext = path.suffix.lower()
    for kind, exts in (("text", TEXT_EXT), ("image", IMAGE_EXT),
                       ("audio", AUDIO_EXT), ("video", VIDEO_EXT)):
        if ext in exts:
            return kind
    return None


def sniff_kind(path: Path) -> str | None:
    """Kind by extension, falling back to treating NUL-free UTF-8 as text."""
    if k := kind_by_ext(path):
        return k
    try:
        head = path.open("rb").read(8192)
    except OSError:
        return None
    if b"\0" in head:
        return None
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as err:
        if err.start < len(head) - 4:       # not merely a split multibyte tail
            return None
    return "text" if head else None


def _spec(lines) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(lines)


def _load_ignores(d: Path) -> list[GitIgnoreSpec]:
    specs = []
    for name in IGNORE_FILES:
        f = d / name
        if f.is_file():
            try:
                specs.append(_spec(f.read_text(errors="replace").splitlines()))
            except OSError:
                pass
    return specs


class Walker:
    """Select files from paths.

    Explicit file arguments are always taken.  Within directories, files
    ignored by .gitignore/.ignore (in the directory or any parent within the
    walk) or matching ``exclude`` are skipped, and only supported types are
    taken unless they match ``include``.  ``include`` overrides file-level
    exclusion; ignored *directories* are still pruned.
    """

    def __init__(self, recursive=False, exclude=(), include=()):
        self.recursive = recursive
        self.exclude = _spec(exclude) if exclude else None
        self.include = _spec(include) if include else None

    def candidates(self, paths):
        """Yield Candidates for the given CLI paths."""
        seen = set()
        for p in map(Path, paths):
            if p.is_dir():
                it = self._walk(p.resolve(), [], p.resolve())
            elif p.is_file():
                kind = sniff_kind(p)
                it = [Candidate(p.resolve(), kind)] if kind else []
            else:
                raise FileNotFoundError(p)
            for c in it:
                if c.path not in seen:
                    seen.add(c.path)
                    yield c

    def _matches(self, spec, rel: str) -> bool:
        return bool(spec and (spec.match_file(rel) or spec.match_file(Path(rel).name)))

    def _walk(self, d: Path, parents, root: Path):
        stack = parents + [(d, _load_ignores(d))]

        def ignored(p: Path, is_dir: bool) -> bool:
            for base, specs in stack:
                rel = p.relative_to(base).as_posix() + ("/" if is_dir else "")
                if any(s.match_file(rel) for s in specs):
                    return True
            return False

        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name == ".git" or e.is_symlink() and not e.exists():
                continue
            rel = e.relative_to(root).as_posix()
            if e.is_dir():
                if self.recursive and not ignored(e, True) \
                        and not self._matches(self.exclude, rel + "/"):
                    yield from self._walk(e, stack, root)
            elif e.is_file():
                if self._matches(self.include, rel):
                    kind = sniff_kind(e)
                elif ignored(e, False) or self._matches(self.exclude, rel):
                    continue
                else:
                    kind = kind_by_ext(e)
                if kind:
                    yield Candidate(e, kind)
