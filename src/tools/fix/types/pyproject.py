"""Assert the "pyproject" type when a pyproject.toml is found in context.path or a parent."""

import logging

from tools.fix.context import Context

log = logging.getLogger(__name__)


def categorize(context: Context, filename: str = "pyproject.toml") -> list[str]:
    """Return ["pyproject"] if `filename` is found in context.path or any of its parents."""
    for directory in (context.path, *context.path.parents):
        if (directory / filename).is_file():
            log.debug("found %s in %s", filename, directory)
            return ["pyproject"]
    return []
