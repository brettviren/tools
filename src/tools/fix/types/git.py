"""Assert the "git" type when context.path (or a parent) is a git working tree."""

import logging

from tools.fix.context import Context

log = logging.getLogger(__name__)


def categorize(context: Context, marker: str = ".git") -> list[str]:
    """Return ["git"] if `marker` is found in context.path or any of its parents."""
    for directory in (context.path, *context.path.parents):
        if (directory / marker).exists():
            log.debug("found %s in %s", marker, directory)
            return ["git"]
    return []
