"""fix: categorize a filesystem context and apply matching fix-up actions."""

import logging
import sys
from pathlib import Path

import click

from tools.fix.config import load_config
from tools.fix.context import detect_context
from tools.fix.core import apply_actions, determine_types

log = logging.getLogger(__name__)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _setup_logging(sink: str, level: str) -> None:
    level_value = logging.getLevelName(level.upper())
    if not isinstance(level_value, int):
        raise click.BadParameter(f"invalid log level: {level!r}")

    if sink == "stderr":
        handler = logging.StreamHandler(sys.stderr)
    elif sink == "stdout":
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.FileHandler(sink)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    pkg_log = logging.getLogger("tools.fix")
    pkg_log.setLevel(level_value)
    pkg_log.handlers.clear()
    pkg_log.addHandler(handler)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("path", required=False, type=click.Path(path_type=Path))
@click.option("-u", "--user", default=None, help="Override the detected username.")
@click.option("--host", default=None, help="Override the detected short hostname.")
@click.option("--fqdn", default=None, help="Override the detected fully-qualified hostname.")
@click.option("-t", "--types", "types_override", default=None, metavar="CSV",
              help="Comma-separated type list overriding detection entirely.")
@click.option("-a", "--add-types", "types_add", default=None, metavar="CSV",
              help="Comma-separated type list added to the detected/overridden set.")
@click.option("-c", "--config", "config_path", default=None,
              type=click.Path(path_type=Path),
              help="Config file path (default: XDG fix/config.toml).")
@click.option("-n", "--dryrun", is_flag=True, default=False,
              help="Log intended actions at INFO level instead of applying them.")
@click.option("-l", "--log-sink", default="stderr", show_default=True, metavar="SINK",
              help="Log destination: stderr, stdout, or a file path.")
@click.option("-L", "--log-level", default="info", show_default=True, metavar="LEVEL",
              help="Log level: debug, info, warning, error, critical.")
def main(
    path: Path | None,
    user: str | None,
    host: str | None,
    fqdn: str | None,
    types_override: str | None,
    types_add: str | None,
    config_path: Path | None,
    dryrun: bool,
    log_sink: str,
    log_level: str,
) -> None:
    """Categorize the context at PATH (default: current directory) and apply matching actions."""
    _setup_logging(log_sink, log_level)

    context = detect_context(path=path, user=user, host=host, fqdn=fqdn)
    config = load_config(config_path)

    types = determine_types(
        context, config,
        override=_split_csv(types_override),
        add=_split_csv(types_add),
    )
    if not types:
        log.warning("no context types detected for %s; nothing to do", context.path)
        return

    log.debug("context types: %s", ", ".join(types))
    apply_actions(context, types, config, dryrun=dryrun)


if __name__ == "__main__":
    main()
