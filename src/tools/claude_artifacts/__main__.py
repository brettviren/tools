# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>

"""claude-artifacts: local, indexed backup tree of claude.ai artifacts.

The downloading itself is done by the ``/backup-claude-artifacts`` Claude Code
skill, which is the only thing able to reach claude.ai as you.  This tool does
the deterministic parts: settings, bookkeeping and the ``index.html`` pages.
"""

import json
from pathlib import Path

import click

from tools.claude_artifacts import config, index, store

CTX = {"help_option_names": ["-h", "--help"]}


def _where_options(fn):
    """Options shared by commands that locate the tree."""
    fn = click.option("-d", "--dir", "directory", type=click.Path(path_type=Path),
                      default=None, help="Tree root (overrides $CLAUDE_ARTIFACTS_DIR and config).")(fn)
    return click.option("-a", "--account", default=None,
                        help="Account short name; must agree with the active organization if known.")(fn)


class Group(click.Group):
    """Show help when no command is given; map ConfigError to CLI errors."""

    def invoke(self, ctx):
        """Invoke, turning ConfigError into a clean CLI error."""
        try:
            return super().invoke(ctx)
        except config.ConfigError as err:
            raise click.ClickException(str(err)) from err


@click.group(cls=Group, context_settings=CTX, invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Local, indexed backup tree of claude.ai artifacts: <root>/<account>/<origin>/<artifact>."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command(context_settings=CTX)
@click.option("-a", "--account", required=True,
              help="Short name for the organization this session is signed in to.")
@click.option("-d", "--dir", "directory", default=None,
              help=f"Tree root to remember (default: {config.DEFAULT_DIR}).")
def setup(account, directory):
    """Map the active organization to an account short name, and set the tree root."""
    path = config.save(account, directory)
    click.echo(f"wrote {path}")


@main.command("where", context_settings=CTX)
@_where_options
def where_cmd(account, directory):
    """Print the resolved settings as JSON."""
    s = config.resolve(account, directory)
    org = config.active_org()
    others = sorted(set(config.load_file().get("orgs", {}).values()) - {s.account})
    click.echo(json.dumps({"account": s.account, "root": str(s.root),
                           "org": org.name if org else None,
                           "other_accounts_not_backed_up":
                               [a for a in others if not (s.root / a).is_dir()],
                           "config": str(config.config_path())}, indent=2))


@main.command(context_settings=CTX)
@_where_options
def known(account, directory):
    """List already-downloaded artifacts as '<origin> <id>' lines."""
    ids = store.known_ids(config.resolve(account, directory))
    for origin in config.ORIGINS:
        for aid in sorted(ids[origin]):
            click.echo(f"{origin} {aid}")


@main.command(context_settings=CTX)
@_where_options
@click.option("-o", "--origin", required=True, type=click.Choice(config.ORIGINS))
@click.option("-i", "--id", "aid", required=True, help="Artifact id or claude.ai URL.")
@click.option("-t", "--title", required=True)
@click.option("-u", "--url", required=True, help="claude.ai artifact URL.")
@click.option("--updated", default="", help="Last-updated date, YYYY-MM-DD.")
@click.option("-s", "--summary", default="", help="One-sentence summary.")
@click.option("--src", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Directory of downloaded files to copy in.")
def add(account, directory, origin, aid, title, url, updated, summary, src):
    """Store a downloaded artifact and refresh the indexes."""
    s = config.resolve(account, directory)
    entry = store.Entry(id=aid, title=title, url=url, origin=origin,
                        updated=updated, summary=summary)
    dest = store.add(s, entry, src)
    index.write_all(s.root)
    click.echo(str(dest))


@main.command(name="index", context_settings=CTX)
@click.option("-d", "--dir", "directory", type=click.Path(path_type=Path),
              default=None, help="Tree root (overrides $CLAUDE_ARTIFACTS_DIR and config).")
def index_cmd(directory):
    """Rewrite every index.html in the tree."""
    root = config.resolve_root(directory)
    if not root.is_dir():
        raise click.ClickException(f"no such directory: {root}")
    for path in index.write_all(root):
        click.echo(str(path))


if __name__ == "__main__":
    main()
