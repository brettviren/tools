"""gitea-mirror: keep a Gitea account populated with selected GitHub mirrors."""

import logging
import sys
from pathlib import Path

import click

from tools.gitea_mirror import app as appmod
from tools.gitea_mirror import core, creds, report
from tools.gitea_mirror.config import (
    ConfigError,
    default_config_path,
    load_config,
    starter_config,
)
from tools.gitea_mirror.rest import ApiError
from tools.gitea_mirror.review import review as review_loop
from tools.gitea_mirror.state import CATEGORIES, LockTimeout

CTX = {"help_option_names": ["-h", "--help"]}
EXPECTED = (ConfigError, creds.CredentialError, ApiError, LockTimeout, OSError, ValueError)


class Group(click.Group):
    """Turn expected library errors into clean CLI errors."""

    def invoke(self, ctx):
        """Invoke, mapping EXPECTED exceptions to ClickException."""
        try:
            return super().invoke(ctx)
        except EXPECTED as err:
            raise click.ClickException(str(err)) from err


def _app(ctx: click.Context) -> appmod.App:
    obj = ctx.find_root().obj
    if "app" not in obj:
        obj["app"] = appmod.App(load_config(obj["config"]))
    return obj["app"]


def _echo_if(text: str) -> None:
    if text:
        click.echo(text)


@click.group(cls=Group, context_settings=CTX)
@click.option("-c", "--config", type=click.Path(path_type=Path), default=None,
              help="Config file (default: $XDG_CONFIG_HOME/gitea-mirror/config.toml).")
@click.option("-L", "--log-level", default="warning", show_default=True,
              type=click.Choice(["debug", "info", "warning", "error"]), help="Log level.")
@click.pass_context
def main(ctx, config, log_level):
    """Mirror selected GitHub repos into a Gitea account."""
    logging.basicConfig(stream=sys.stderr, level=log_level.upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    ctx.obj = {"config": config}


@main.command(context_settings=CTX)
@click.option("--url", prompt="Gitea URL", help="Gitea base URL.")
@click.option("--owner", prompt="Gitea owner of mirrors", help="Gitea user/org holding mirrors.")
@click.option("--github-user", prompt="GitHub user", help="Your GitHub login.")
@click.option("-f", "--force", is_flag=True, help="Overwrite an existing config.")
@click.pass_context
def init(ctx, url, owner, github_user, force):
    """Write an annotated starter config."""
    path = ctx.obj["config"] or default_config_path()
    if path.exists() and not force:
        raise click.ClickException(f"{path} exists; use --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(starter_config(url.rstrip("/"), owner, github_user))
    click.echo(f"wrote {path}")


@main.group(cls=Group, context_settings=CTX)
def auth():
    """Acquire and check GitHub/Gitea credentials."""


@auth.command(context_settings=CTX, no_args_is_help=True)
@click.argument("service", type=click.Choice(["github", "gitea"]))
@click.option("--no-browser", is_flag=True, help="Only print the URL.")
@click.pass_context
def login(ctx, service, no_browser):
    """Open the token page, read a pasted token, verify and store it."""
    a = _app(ctx)
    spec = appmod.auth_spec(a.cfg, service)
    url, how = creds.token_page(service, a.cfg.gitea_url, spec)
    click.echo(f"Opening {url}\n{how}")
    if not no_browser:
        click.launch(url)
    token = click.prompt("Paste token", hide_input=True).strip()
    click.echo(f"token works for {service} user {appmod.verify_token(a.cfg, service, token)}")
    click.echo(f"stored via {creds.store_token(spec, token, appmod.token_file(service))}")
    _echo_if(creds.store_warning(spec))


@auth.command(context_settings=CTX)
@click.pass_context
def check(ctx):
    """Verify both credentials and access to the Gitea owner."""
    _echo_if("\n".join(appmod.check_auth(_app(ctx))))


@main.command(context_settings=CTX)
@click.option("-j", "--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.pass_context
def scan(ctx, as_json):
    """Discover repos, evaluate rules, adopt existing mirrors.  No Gitea writes."""
    a = _app(ctx)
    with a.store.transaction() as state:
        rep = core.sync_report(core.scan(a.cfg, state, a.github, a.gitea), [], state)
    _echo_if(appmod.dumps(rep) if as_json else report.sync_text(rep))


@main.command(context_settings=CTX)
@click.option("-n", "--dry-run", is_flag=True, help="Scan, but only show the plan.")
@click.option("-q", "--quiet", is_flag=True, help="Print only when something changed.")
@click.option("-j", "--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.option("--delay", type=float, default=None,
              help="Seconds between mirror creations (default: config create_delay).")
@click.option("--max", "max_creates", type=int, default=None,
              help="Max mirror creations this run, 0 = no cap (default: config max_creates).")
@click.pass_context
def sync(ctx, dry_run, quiet, as_json, delay, max_creates):
    """Scan then apply the plan.  Meant for cron."""
    a = _app(ctx)
    with a.store.transaction() as state:
        scanned = core.scan(a.cfg, state, a.github, a.gitea)
        steps = core.apply(a.cfg, state, core.plan(a.cfg, state), a.gitea, a.mirror_token,
                           dry_run, delay, max_creates)
        rep = core.sync_report(scanned, steps, state)
    a.store.write_report(rep)
    if not quiet or core.report_is_news(rep):
        _echo_if(appmod.dumps(rep) if as_json else report.sync_text(rep))


@main.command(context_settings=CTX)
@click.argument("patterns", nargs=-1)
@click.option("-n", "--dry-run", is_flag=True, help="Only show the plan.")
@click.option("--delay", type=float, default=None,
              help="Seconds between mirror creations (default: config create_delay).")
@click.option("--max", "max_creates", type=int, default=None,
              help="Max mirror creations this run, 0 = no cap (default: config max_creates).")
@click.pass_context
def apply(ctx, patterns, dry_run, delay, max_creates):
    """Apply the plan from current state (optionally only for PATTERNS)."""
    a = _app(ctx)
    with a.store.transaction() as state:
        steps = core.plan(a.cfg, state, state.select(list(patterns)) if patterns else None)
        core.apply(a.cfg, state, steps, a.gitea, a.mirror_token, dry_run, delay, max_creates)
    _echo_if(report.steps_text(steps) or "nothing to do")


@main.command(context_settings=CTX)
@click.pass_context
def status(ctx):
    """Show counts per category."""
    click.echo(report.status_text(_app(ctx).store.load()))


@main.command("list", context_settings=CTX, no_args_is_help=True)
@click.argument("category", type=click.Choice([*CATEGORIES, "untracked"]))
@click.argument("patterns", nargs=-1)
@click.option("-s", "--source", default=None, help="Only records from this source.")
@click.option("-j", "--json", "as_json", is_flag=True, help="Print JSON.")
@click.pass_context
def list_(ctx, category, patterns, source, as_json):
    """List records in CATEGORY, optionally matching glob PATTERNS.

    Columns: decision (* = manual), G if on Gitea, flags F(ork) P(rivate)
    A(rchived) G(one) E(rror), repo, rule.
    """
    state = _app(ctx).store.load()
    if category == "untracked":
        _echo_if("\n".join(state.untracked))
        return
    recs = state.select(list(patterns), category, source)
    _echo_if(report.records_json(recs) if as_json else report.records_text(recs))


@main.command(context_settings=CTX, no_args_is_help=True)
@click.argument("patterns", nargs=-1, required=True)
@click.pass_context
def show(ctx, patterns):
    """Show records matching PATTERNS in detail with their history."""
    a = _app(ctx)
    recs = appmod.require(a.store.load(), patterns)
    click.echo("\n\n".join(report.record_detail(r, a.store.history([r.full_name])) for r in recs))


@main.command(context_settings=CTX)
@click.argument("category", default="pending", type=click.Choice(CATEGORIES))
@click.argument("patterns", nargs=-1)
@click.option("-s", "--source", default=None, help="Only records from this source.")
@click.option("--apply/--no-apply", "do_apply", default=True, show_default=True,
              help="Apply the plan for decided repos afterwards.")
@click.pass_context
def review(ctx, category, patterns, source, do_apply):
    """Interactively decide repos in CATEGORY (default: pending)."""
    a = _app(ctx)
    names = [r.full_name for r in a.store.load().select(list(patterns), category, source)]
    decided = review_loop(a.store, names, click.getchar, click.echo, click.launch)
    if decided and do_apply:
        ctx.invoke(apply, patterns=tuple(decided))


@main.command("set", context_settings=CTX, no_args_is_help=True)
@click.argument("patterns", nargs=-1, required=True)
@click.option("--mirror", "decision", flag_value="mirror", help="Decide: mirror.")
@click.option("--skip", "decision", flag_value="skip", help="Decide: don't mirror.")
@click.option("--review", "decision", flag_value="review", help="Decide: pending review.")
@click.option("--rules", "decision", flag_value="rules", help="Forget manual decision.")
@click.option("--prune", "policy", flag_value="prune", help="Delete Gitea mirror when skipped.")
@click.option("--leave", "policy", flag_value="leave", help="Keep Gitea mirror when skipped.")
@click.option("--default-policy", "policy", flag_value="default",
              help="Use the configured on_unmirror policy.")
@click.option("--gitea-name", default=None, help="Gitea repo name to create (one repo only).")
@click.option("--note", default="", help="Note recorded with a decision.")
@click.option("--adopt-existing", "adopt", is_flag=True,
              help="Take the existing Gitea pull mirror of the same name as the mirror.")
@click.option("--replace-existing", "replace", is_flag=True,
              help="Delete the existing Gitea repo of the same name and create a mirror.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask before --replace-existing deletes.")
@click.option("--apply", "do_apply", is_flag=True, help="Apply the plan for these repos.")
@click.pass_context
def set_(ctx, patterns, decision, policy, gitea_name, note, adopt, replace, yes,
         do_apply):
    """Set manual decision, unmirror policy or Gitea name for repos matching PATTERNS.

    A pattern without "/" matches the repo name under any owner.
    """
    a = _app(ctx)
    if replace and not yes:
        click.echo(appmod.describe_targets(a, appmod.require(a.store.load(), patterns)))
        click.confirm("Delete these Gitea repos and recreate them as mirrors?", abort=True)
    with a.store.transaction() as state:
        recs = appmod.require(state, patterns)
        appmod.edit_records(state, recs, decision, policy, gitea_name, note)
        done = appmod.handle_existing(a, state, recs, decision, adopt, replace)
    _echo_if(done)
    _echo_if(report.records_text(recs))
    if do_apply:
        ctx.invoke(apply, patterns=tuple(r.full_name for r in recs))


@main.command(context_settings=CTX, no_args_is_help=True)
@click.argument("repos", nargs=-1, required=True, metavar="OWNER/NAME...")
@click.option("--note", default="", help="Note recorded with the decision.")
@click.option("--apply/--no-apply", "do_apply", default=True, show_default=True,
              help="Create the mirror now.")
@click.pass_context
def add(ctx, repos, note, do_apply):
    """Mirror GitHub repos that are outside the configured sources."""
    a = _app(ctx)
    found = [a.github.repo(name) for name in repos]
    with a.store.transaction() as state:
        names = tuple(core.add_manual(state, r, note).full_name for r in found)
    if do_apply:
        ctx.invoke(apply, patterns=names)


@main.command(context_settings=CTX)
@click.argument("patterns", nargs=-1)
@click.option("-n", "--last", default=50, show_default=True, help="Show the last N events.")
@click.pass_context
def history(ctx, patterns, last):
    """Show the event log, optionally for repos matching PATTERNS."""
    events = _app(ctx).store.history(list(patterns))[-last:]
    _echo_if("\n".join(report.event_line(e) for e in events))


if __name__ == "__main__":
    main()
