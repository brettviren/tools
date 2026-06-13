#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click>=8",
# ]
# ///

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import click

CONFIG_DEFAULT = Path("~/.config/manycron/config.toml")
CACHE_REPO = Path("~/.cache/manycron/tabs")
MARKER_BEGIN = "# BEGIN MANYCRON"
MARKER_END = "# END MANYCRON"


def load_config(path: Path) -> dict:
    path = path.expanduser()
    if not path.exists():
        return {"crontabs": {}, "fragments": {}}
    with open(path, "rb") as f:
        return tomllib.load(f)


def normalize_lines(val: str | list[str] | None) -> list[str]:
    """Accept either a TOML array of strings or a single multi-line string."""
    if val is None:
        return []
    if isinstance(val, str):
        return val.strip("\n").splitlines()
    return list(val)


def collect_lines(table: dict, all_fragments: dict, _path: frozenset[str] = frozenset()) -> list[str]:
    """Depth-first collect: a table's own lines come first, then its fragments are expanded.

    _path tracks the ancestor fragment names on the current call stack so that
    circular references are detected without blocking a fragment from being
    reused under different parents.
    """
    result = normalize_lines(table.get("lines"))
    for frag_name in table.get("fragments", []):
        if frag_name in _path:
            click.echo(f"warning: circular fragment reference ignored: {frag_name!r}", err=True)
            continue
        if frag_name not in all_fragments:
            raise click.ClickException(f"Unknown fragment: {frag_name!r}")
        result.extend(
            collect_lines(all_fragments[frag_name], all_fragments, _path | {frag_name})
        )
    return result


def ssh_crontab(connect: str | None, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    cmd = (["ssh", connect, "crontab"] if connect else ["crontab"]) + args
    result = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def get_live_crontab(connect: str | None) -> str:
    rc, out, _ = ssh_crontab(connect, ["-l"])
    return "" if rc != 0 else out


def inject_managed(live: str, lines: list[str]) -> str:
    """Replace (or insert) the managed section; all text outside markers is untouched."""
    section = f"{MARKER_BEGIN}\n" + "\n".join(lines) + f"\n{MARKER_END}" if lines else ""
    begin = live.find(MARKER_BEGIN)
    if begin == -1:
        base = live.rstrip("\n")
        return (base + "\n" + section + "\n") if section else (base + "\n" if base else "")
    end = live.find(MARKER_END)
    end_pos = end + len(MARKER_END)
    if end_pos < len(live) and live[end_pos] == "\n":
        end_pos += 1
    before, after = live[:begin], live[end_pos:]
    return (before + section + "\n" + after) if section else (before + after)


# ---------------------------------------------------------------------------
# Git cache helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


def ensure_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init")
        _git(repo, "commit", "--allow-empty", "-m", "init manycron cache")


def git_write_commit(repo: Path, filename: str, content: str, message: str) -> bool:
    """Write content, stage, and commit if the file changed. Returns True if committed."""
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    result = _git(repo, "diff", "--cached", "--quiet", check=False)
    if result.returncode == 0:
        return False
    _git(repo, "commit", "-m", message)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_accounts(ctx, account: str | None) -> dict:
    accounts = ctx.obj["config"].get("crontabs", {})
    if account:
        if account not in accounts:
            raise click.ClickException(
                f"Unknown account: {account!r}. Known: {', '.join(accounts) or '(none)'}"
            )
        return {account: accounts[account]}
    return accounts


@click.group()
@click.option("--config", "config_path", default=str(CONFIG_DEFAULT),
              show_default=True, help="Path to config file.")
@click.pass_context
def cli(ctx, config_path):
    """Manage crontab fragments across many accounts."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(Path(config_path))
    ctx.obj["config_path"] = Path(config_path)
    ctx.obj["repo"] = CACHE_REPO.expanduser()


@cli.command("list")
@click.pass_context
def list_cmd(ctx):
    """List all configured accounts."""
    accounts = ctx.obj["config"].get("crontabs", {})
    if not accounts:
        click.echo("No accounts configured.")
        return
    all_frags = ctx.obj["config"].get("fragments", {})
    for name, acct in accounts.items():
        connect = acct.get("connect", "(local)")
        n = len(collect_lines(acct, all_frags))
        click.echo(f"  {name:<20}  {connect:<30}  ({n} lines)")


@cli.command()
@click.argument("account", required=False)
@click.pass_context
def show(ctx, account):
    """Show the assembled crontab lines for ACCOUNT (or all).

    Lines are collected depth-first: a table's own lines come first, then
    each listed fragment is expanded in order (recursively, same rule).
    """
    all_frags = ctx.obj["config"].get("fragments", {})
    for name, acct in resolve_accounts(ctx, account).items():
        lines = collect_lines(acct, all_frags)
        click.echo(f"# --- {name} ({acct.get('connect', 'local')}) ---")
        for line in lines:
            click.echo(line)
        click.echo()


@cli.command()
@click.argument("account", required=False)
@click.pass_context
def get(ctx, account):
    """Fetch the live crontab from ACCOUNT (or all)."""
    for name, acct in resolve_accounts(ctx, account).items():
        connect = acct.get("connect")
        click.echo(f"# --- {name} ({connect or 'local'}) ---")
        content = get_live_crontab(connect)
        click.echo(content.rstrip() if content else "(no crontab)")
        click.echo()


@cli.command()
@click.argument("account", required=False)
@click.pass_context
def diff(ctx, account):
    """Diff proposed vs live crontab for ACCOUNT (or all).

    Only the managed section (between MANYCRON markers) changes;
    content outside those markers is shown as-is in both sides.
    Exits with status 1 if any account has pending changes.
    """
    all_frags = ctx.obj["config"].get("fragments", {})
    any_diff = False
    for name, acct in resolve_accounts(ctx, account).items():
        connect = acct.get("connect")
        lines = collect_lines(acct, all_frags)
        live = get_live_crontab(connect)
        proposed = inject_managed(live, lines)
        if live == proposed:
            click.echo(f"{name}: up to date")
            continue
        any_diff = True
        with (tempfile.NamedTemporaryFile(mode="w", suffix=".live", delete=False) as f1,
              tempfile.NamedTemporaryFile(mode="w", suffix=".proposed", delete=False) as f2):
            f1.write(live)
            f2.write(proposed)
            p1, p2 = Path(f1.name), Path(f2.name)
        result = subprocess.run(
            ["diff", "-u",
             "--label", f"a/{name} (live)",
             "--label", f"b/{name} (proposed)",
             str(p1), str(p2)],
            capture_output=True, text=True,
        )
        click.echo(result.stdout)
        p1.unlink()
        p2.unlink()
    if any_diff:
        sys.exit(1)


@cli.command()
@click.argument("account", required=False)
@click.option("--dry-run", "-n", is_flag=True, help="Show what would be installed without applying.")
@click.option("--force", "-f", is_flag=True, help="Apply even if no changes detected.")
@click.pass_context
def apply(ctx, account, dry_run, force):
    """Apply the managed crontab section to ACCOUNT (or all).

    \b
    For each account:
      1. Fetch the live crontab and commit it to the local git cache.
      2. Assemble managed lines from the fragment tree (depth-first).
      3. Inject the managed section, preserving content outside the markers.
      4. If the result differs, commit the new content and push it to the account.
    """
    all_frags = ctx.obj["config"].get("fragments", {})
    repo = ctx.obj["repo"]

    if not dry_run:
        ensure_git_repo(repo)

    for name, acct in resolve_accounts(ctx, account).items():
        connect = acct.get("connect")
        lines = collect_lines(acct, all_frags)
        live = get_live_crontab(connect)

        if not dry_run:
            git_write_commit(repo, name, live, f"snapshot: {name}")

        proposed = inject_managed(live, lines)

        if live == proposed and not force:
            click.echo(f"{name}: already up to date")
            continue

        if dry_run:
            click.echo(f"# Would install for {name}:\n{proposed}")
            continue

        git_write_commit(repo, name, proposed, f"apply: {name} ({len(lines)} managed lines)")

        rc, _, err = ssh_crontab(connect, ["-"], stdin=proposed)
        if rc != 0:
            click.echo(f"{name}: FAILED — {err.strip()}", err=True)
        else:
            click.echo(f"{name}: applied ({len(lines)} managed lines)")


@cli.command("edit")
@click.argument("target")
@click.pass_context
def edit_cmd(ctx, target):
    """Directly edit a remote crontab in $EDITOR.

    TARGET is either a configured crontabs entry name or a raw SSH connect
    string.  If TARGET matches a crontabs entry its configured connect value
    is used; otherwise TARGET is passed to SSH as-is.

    The live crontab is opened in $EDITOR.  If the content changed when the
    editor exits the new version is installed on the remote immediately.
    This command is independent of configured lines and fragments.
    """
    accounts = ctx.obj["config"].get("crontabs", {})
    if target in accounts:
        connect = accounts[target].get("connect")
    else:
        connect = target or None

    live = get_live_crontab(connect)

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".crontab", delete=False) as f:
        f.write(live)
        tmp = Path(f.name)

    try:
        result = subprocess.run([editor, str(tmp)])
        if result.returncode != 0:
            click.echo(f"Editor exited {result.returncode}, crontab not changed.", err=True)
            sys.exit(result.returncode)

        edited = tmp.read_text()
        if edited == live:
            click.echo("No changes.")
            return

        rc, _, err = ssh_crontab(connect, ["-"], stdin=edited)
        if rc != 0:
            click.echo(f"Failed to install crontab: {err.strip()}", err=True)
            sys.exit(1)
        click.echo("Crontab updated.")
    finally:
        tmp.unlink(missing_ok=True)


_EXAMPLE_CONFIG = """\
# manycron configuration  (~/.config/manycron/config.toml)
#
# Two top-level tables are recognised:
#   [fragments.<name>]  — reusable line groups
#   [crontabs.<name>]   — one entry per managed account
#
# Both accept:
#   lines     — crontab lines, as a TOML array or a multi-line string
#   fragments — ordered list of fragment names to expand (depth-first)
#               after the table's own lines are appended
#
# When `apply` runs it replaces only the region between
#   # BEGIN MANYCRON
#   # END MANYCRON
# markers in the live crontab, leaving everything else untouched.
# The markers (and the file before/after applying) are kept in
#   ~/.cache/manycron/tabs/<name>

# ---------------------------------------------------------------------------
# Fragments — reusable building blocks
# ---------------------------------------------------------------------------

[fragments.env]
# Environment lines inserted at the top of every managed section.
lines = '''
MAILTO=""
SHELL=/bin/bash
'''

[fragments.nightly]
# Pulls in env first (depth-first), then adds its own lines.
fragments = ["env"]
lines = [
    "0 2 * * * /usr/local/bin/backup.sh",
    "0 3 * * 0 /usr/local/bin/weekly-report.sh",
]

# ---------------------------------------------------------------------------
# Crontabs — one table per managed account
# ---------------------------------------------------------------------------

[crontabs.local]
# No 'connect' key → manages the local user's crontab.
lines = '''
*/5  * * * * /home/user/bin/poll.sh
@reboot      /home/user/bin/startup.sh
'''

[crontabs.myserver]
# connect is the SSH target passed to `ssh … crontab`.
connect = "user@myserver.example.com"
# Mix fragments and direct lines; fragments expand before local lines.
fragments = ["nightly"]
lines = [
    "*/15 * * * * /usr/local/bin/healthcheck",
]

[crontabs."user@host-with-special.chars"]
# Keys containing characters outside [A-Za-z0-9_-] are quoted automatically
# by `create`; you can also write them by hand as shown here.
connect = "user@host-with-special.chars"
lines = [
    "0 6 * * 1-5 /usr/local/bin/morning-sync",
]
"""


def _toml_key(s: str) -> str:
    """Bare key if possible, otherwise double-quoted."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_multiline_literal(text: str) -> str:
    """Format text as a TOML multi-line literal string ('''...''').

    Falls back to a basic multi-line string when the content contains '''.
    """
    if "'''" not in text:
        return f"'''\n{text.rstrip()}\n'''"
    escaped = text.rstrip().replace("\\", "\\\\").replace('"', '\\"')
    return f'"""\n{escaped}\n"""'


@cli.command()
@click.argument("connect")
@click.option("--name", default=None, show_default=True,
              help="Config entry name.  Defaults to the CONNECT string.")
@click.pass_context
def create(ctx, connect, name):
    """Create a new crontabs entry by importing the current remote crontab.

    CONNECT is the SSH connect string (e.g. user@host).  The live crontab is
    fetched and stored verbatim as a multi-line `lines` value so that it is
    immediately under manycron control without losing any existing entries.
    """
    if name is None:
        name = connect

    config = ctx.obj["config"]
    if name in config.get("crontabs", {}):
        raise click.ClickException(f"crontabs entry {name!r} already exists — use `show` or `apply`.")

    content = get_live_crontab(connect)

    config_path = ctx.obj["config_path"].expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    key = _toml_key(name)
    lines_toml = _toml_multiline_literal(content) if content else "[]"
    connect_toml = connect.replace("\\", "\\\\").replace('"', '\\"')

    section = f'\n[crontabs.{key}]\nconnect = "{connect_toml}"\nlines = {lines_toml}\n'

    with open(config_path, "a") as f:
        f.write(section)

    n = len(normalize_lines(content))
    click.echo(f"Created [crontabs.{key}] in {config_path} ({n} lines imported).")


@cli.command("config")
@click.option("--edit", is_flag=True, help="Open the config file in $EDITOR.")
@click.pass_context
def config_cmd(ctx, edit):
    """Print an example config, or open the config file in $EDITOR.

    The example (no options) illustrates every supported feature with
    inline comments.  Use it as a starting point or a quick reference.
    """
    config_path = ctx.obj["config_path"].expanduser()

    if edit:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if not config_path.exists():
            config_path.write_text("")
        result = subprocess.run([editor, str(config_path)])
        sys.exit(result.returncode)

    click.echo(_EXAMPLE_CONFIG, nl=False)


if __name__ == "__main__":
    cli()
