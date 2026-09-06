"""Assure .gitignore contains entry sets appropriate to the asserted context types.

Each applicable type gets its own marked, idempotent block (à la manycron's
managed crontab section): re-running never duplicates entries, hand-edits
outside a block are left untouched, and an entry already present anywhere in
the file (e.g. added by hand before fix ever ran) is not duplicated into the
block either.
"""

import logging

from tools.fix.context import Context

log = logging.getLogger(__name__)

FILENAME = ".gitignore"

# Entries assured for each recognized context type.  Extend this table (or
# override it via the `sets` config/kwarg) to cover more types.
GITIGNORE_SETS: dict[str, list[str]] = {
    "pyproject": [
        "__pycache__/",
        "*.py[cod]",
        "*.egg-info/",
        ".eggs/",
        "build/",
        "dist/",
        ".venv/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        ".coverage",
        "htmlcov/",
    ],
}


def _marker(name: str) -> tuple[str, str]:
    return f"# BEGIN fix:{name}", f"# END fix:{name}"


def _inject_block(text: str, begin: str, end: str, lines: list[str]) -> str:
    """Replace (or insert, or remove) a marked block; text outside it is untouched."""
    section = f"{begin}\n" + "\n".join(lines) + f"\n{end}" if lines else ""
    start = text.find(begin)
    if start == -1:
        base = text.rstrip("\n")
        return (base + "\n" + section + "\n") if section else (base + "\n" if base else "")
    end_idx = text.find(end, start)
    end_pos = end_idx + len(end)
    if end_pos < len(text) and text[end_pos] == "\n":
        end_pos += 1
    before, after = text[:start], text[end_pos:]
    return (before + section + "\n" + after) if section else (before + after)


def action(
    context: Context,
    types: list[str],
    dryrun: bool = False,
    sets: dict[str, list[str]] | None = None,
) -> None:
    """Assure a .gitignore under context.path has the entries implied by `types`."""
    sets = sets if sets is not None else GITIGNORE_SETS
    applicable = sorted(t for t in types if t in sets)
    if not applicable:
        log.debug("no gitignore-relevant types in %s; nothing to do", sorted(types))
        return

    gitignore_path = context.path / FILENAME
    original = gitignore_path.read_text() if gitignore_path.exists() else ""
    updated = original

    for type_name in applicable:
        begin, end = _marker(type_name)
        outside = _inject_block(updated, begin, end, [])
        existing = set(outside.splitlines())
        missing = [entry for entry in sets[type_name] if entry not in existing]
        updated = _inject_block(updated, begin, end, missing)

    if updated == original:
        log.debug("%s already up to date", gitignore_path)
        return

    if dryrun:
        log.info("would update %s (types: %s)", gitignore_path, ", ".join(applicable))
        return

    gitignore_path.write_text(updated)
    log.debug("wrote %s (types: %s)", gitignore_path, ", ".join(applicable))
