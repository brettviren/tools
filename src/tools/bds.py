"""bds: convenience wrapper adding functionality to the Beads command `bd`."""

import os
import shlex
import shutil
import subprocess
import sys

import click

# Ordered fallback pagers tried when no pager is configured.  Each entry is a
# full argv; the first whose executable is found on PATH is used.
PAGER_FALLBACKS = [
    ["batcat", "--wrap", "auto", "-l", "md"],
    ["glow", "-p"],
    ["less"],
    ["cat"],
]


def resolve_pager(pager: str | None) -> list[str]:
    """Return the pager argv to use.

    A ``pager`` string (from ``--pager`` or ``$BDS_PAGER``) is split with shell
    quoting rules and used verbatim.  Otherwise the first available fallback in
    PAGER_FALLBACKS is chosen; ``cat`` is always the last resort.
    """
    pager = pager or os.environ.get("BDS_PAGER")
    if pager:
        return shlex.split(pager)
    for argv in PAGER_FALLBACKS:
        if shutil.which(argv[0]):
            return argv
    return ["cat"]


def run_bd(args: list[str]) -> int:
    """Run ``bd`` with ``args`` inheriting the terminal; return its exit code."""
    try:
        return subprocess.call(["bd", *args])
    except FileNotFoundError as exc:
        raise click.ClickException("the 'bd' command was not found on PATH") from exc


def page_bd(args: list[str], pager_argv: list[str]) -> int:
    """Run ``bd`` with ``args`` and pipe its output through ``pager_argv``.

    Returns the pager's exit code.  Mirrors a shell ``bd ... | pager`` pipeline
    so the source receives SIGPIPE if the pager quits early.
    """
    try:
        source = subprocess.Popen(["bd", *args], stdout=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise click.ClickException("the 'bd' command was not found on PATH") from exc
    try:
        pager = subprocess.Popen(pager_argv, stdin=source.stdout)
    except FileNotFoundError as exc:
        source.kill()
        raise click.ClickException(f"pager not found: {pager_argv[0]!r}") from exc
    source.stdout.close()  # let source get SIGPIPE if pager exits first
    pager.wait()
    source.wait()
    return pager.returncode


class DefaultGroup(click.Group):
    """A group that routes unknown or absent commands to a default command.

    With no arguments the default command is invoked with no arguments.  A
    first argument that matches no known subcommand is passed through as the
    default command's first argument.
    """

    def __init__(self, *args, **kwargs):
        self.default_cmd_name = kwargs.pop("default", None)
        super().__init__(*args, **kwargs)

    def parse_args(self, ctx, args):
        if not args:
            args = [self.default_cmd_name]
        return super().parse_args(ctx, args)

    def get_command(self, ctx, cmd_name):
        if cmd_name not in self.commands:
            ctx.__bds_passthrough = cmd_name
            cmd_name = self.default_cmd_name
        return super().get_command(ctx, cmd_name)

    def resolve_command(self, ctx, args):
        cmd_name, cmd, args = super().resolve_command(ctx, args)
        passthrough = getattr(ctx, "_DefaultGroup__bds_passthrough", None)
        if passthrough is not None:
            args.insert(0, passthrough)
        return cmd_name, cmd, args


@click.group(cls=DefaultGroup, default="dwim",
             context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--pager", default=None, metavar="COMMAND",
              help="Pager for rendered output (may be multi-word).  "
                   "Overrides $BDS_PAGER.")
@click.pass_context
def cli(ctx, pager: str | None) -> None:
    """Convenience wrapper around the Beads command `bd`.

    With no matching subcommand, `dwim` handles the arguments.
    """
    ctx.obj = {"pager": pager}


@cli.command()
@click.argument("issue", required=False)
@click.pass_context
def dwim(ctx, issue: str | None) -> None:
    """Do What I Mean: show ISSUE, or list ready issues.

    With no ISSUE, run `bd ready`.  Given an ISSUE name, run `bd show ISSUE`
    and page the output through a markdown-aware pager.
    """
    if issue is None:
        sys.exit(run_bd(["ready"]))
    pager_argv = resolve_pager(ctx.obj.get("pager"))
    sys.exit(page_bd(["show", issue], pager_argv))


if __name__ == "__main__":
    cli()
