"""Load ~/.config/fix/config.toml and pull per-function keyword overrides from it."""

import inspect
import logging
import os
import tomllib
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def default_config_path() -> Path:
    """XDG-standard location: $XDG_CONFIG_HOME/fix/config.toml, else ~/.config/fix/config.toml."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "fix" / "config.toml"


def load_config(path: str | Path | None = None) -> dict:
    """Load the TOML config, or return {} if the file doesn't exist."""
    resolved = Path(path).expanduser() if path else default_config_path()
    if not resolved.exists():
        log.debug("no config file at %s", resolved)
        return {}
    with open(resolved, "rb") as f:
        config = tomllib.load(f)
    log.debug("loaded config from %s", resolved)
    return config


def func_kwargs(config: dict, section: str, name: str, func: Callable) -> dict:
    """Return func's keyword-defaulted parameters as supplied by config[section][name].

    Only keys matching a parameter of func that has a default value are
    passed through; unrecognized keys are logged and dropped so a stale or
    mistyped config entry can't silently do nothing.
    """
    allowed = {
        p.name
        for p in inspect.signature(func).parameters.values()
        if p.default is not inspect.Parameter.empty
    }
    supplied = config.get(section, {}).get(name, {})
    if not supplied:
        return {}
    if not isinstance(supplied, dict):
        log.warning("config [%s.%s] is not a table; ignoring", section, name)
        return {}
    unknown = set(supplied) - allowed
    if unknown:
        log.warning(
            "config [%s.%s] has keys unknown to %s(): %s",
            section, name, func.__name__, ", ".join(sorted(unknown)),
        )
    return {k: v for k, v in supplied.items() if k in allowed}
