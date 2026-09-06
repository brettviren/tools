"""Orchestration: turn a Context into a type set, then apply every action to it."""

import logging

from tools.fix.config import func_kwargs
from tools.fix.context import Context
from tools.fix.registry import discover_actions, discover_types

log = logging.getLogger(__name__)


def determine_types(
    context: Context,
    config: dict,
    override: list[str] | None = None,
    add: list[str] | None = None,
) -> list[str]:
    """Run every categorizer (unless `override` replaces detection), then union in `add`."""
    if override:
        detected = set(override)
        log.debug("type detection overridden: %s", sorted(detected))
    else:
        detected = set()
        for name, categorize in discover_types().items():
            kwargs = func_kwargs(config, "types", name, categorize)
            found = categorize(context, **kwargs)
            if found:
                log.debug("%s asserted: %s", name, found)
            detected.update(found)
    if add:
        detected.update(add)
    return sorted(detected)


def apply_actions(context: Context, types: list[str], config: dict, dryrun: bool = False) -> None:
    """Call every discovered action with context, types, and its config-supplied kwargs."""
    for name, action in discover_actions().items():
        kwargs = func_kwargs(config, "actions", name, action)
        action(context, types, dryrun=dryrun, **kwargs)
