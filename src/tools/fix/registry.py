"""Discover categorizing and action functions from the types/ and actions/ subpackages.

Adding a new context type or action is a matter of dropping a module into
tools.fix.types (exposing `categorize(context, **kwargs) -> list[str]`) or
tools.fix.actions (exposing `action(context, types, dryrun=False, **kwargs)`);
no registration elsewhere is needed.
"""

import importlib
import logging
import pkgutil
from collections.abc import Callable

log = logging.getLogger(__name__)


def _discover(package_name: str, attr: str) -> dict[str, Callable]:
    package = importlib.import_module(package_name)
    found = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package_name}.{info.name}")
        func = getattr(module, attr, None)
        if func is None:
            log.warning("module %s has no %s() function; skipping", module.__name__, attr)
            continue
        found[info.name] = func
    return found


def discover_types() -> dict[str, Callable]:
    """Return {name: categorize} for every module under tools.fix.types."""
    return _discover("tools.fix.types", "categorize")


def discover_actions() -> dict[str, Callable]:
    """Return {name: action} for every module under tools.fix.actions."""
    return _discover("tools.fix.actions", "action")
