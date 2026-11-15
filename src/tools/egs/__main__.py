"""egs: semantic file search with EmbeddingGemma 2 embeddings in SQLite."""

import logging
import sys
from pathlib import Path

import click

from tools.egs import core, models
from tools.egs.db import Database, default_db_path
from tools.egs.embed import EmbedError, open_embedder
from tools.egs.walk import Walker

CTX = {"help_option_names": ["-h", "--help"]}
EXPECTED = (EmbedError, ValueError, OSError)


class Group(click.Group):
    """Turn expected library errors into clean CLI errors."""

    def invoke(self, ctx):
        """Invoke, mapping EXPECTED exceptions to ClickException."""
        try:
            return super().invoke(ctx)
        except EXPECTED as err:
            raise click.ClickException(str(err)) from err


def _db(ctx: click.Context) -> Database:
    obj = ctx.find_root().obj
    if "db" not in obj:
        obj["db"] = Database(obj["db_path"])
        ctx.call_on_close(obj["db"].close)
    return obj["db"]


@click.group(cls=Group, context_settings=CTX, no_args_is_help=True)
@click.option("--db", "db_path", type=click.Path(path_type=Path), default=None,
              help="SQLite database (default: $EGS_DB or $XDG_DATA_HOME/egs/egs.db).")
@click.option("-L", "--log-level", default="warning", show_default=True,
              type=click.Choice(["debug", "info", "warning", "error"]))
@click.pass_context
def main(ctx, db_path, log_level):
    """Semantic search over local files using EmbeddingGemma 2."""
    logging.basicConfig(stream=sys.stderr, level=log_level.upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    ctx.obj = {"db_path": db_path or default_db_path()}


@main.command(context_settings=CTX, no_args_is_help=True)
@click.option("-r", "--recursive", is_flag=True, help="Descend into subdirectories.")
@click.option("-e", "--exclude", multiple=True, metavar="PATTERN",
              help="Extra gitignore-style exclusion pattern (repeatable).")
@click.option("-i", "--include", multiple=True, metavar="PATTERN",
              help="Include matching files even if otherwise excluded (repeatable).")
@click.option("-f", "--force", is_flag=True, help="Re-embed even if unchanged.")
@click.option("-q", "--quiet", is_flag=True, help="Only report errors and the total.")
@click.argument("paths", nargs=-1, required=True, type=click.Path(path_type=Path))
@click.pass_context
def scan(ctx, recursive, exclude, include, force, quiet, paths):
    """Embed files (and files in directories) that are new or changed."""
    db = _db(ctx)
    model = models.default_model(db)
    walker = Walker(recursive, exclude, include)
    counts = {}
    for r in core.scan(db, model, lambda kinds: open_embedder(model, kinds),
                       walker.candidates(paths), force):
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == "error" or (not quiet and r.status != "unchanged"):
            click.echo(f"{r.status:9} {r.path} {r.detail or (str(r.nvec) + ' vec')}",
                       err=r.status == "error")
    click.echo(", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing to scan")


@main.command(context_settings=CTX, no_args_is_help=True)
@click.option("-l", "--level", type=int, default=0, show_default=True,
              help="Matryoshka truncation level: 0 full vector, 1 next smaller, ...")
@click.option("-s", "--summary", is_flag=True, help="Also print file and matching-context summaries.")
@click.option("-n", "--limit", type=int, default=10, show_default=True, help="Max files.")
@click.argument("words", nargs=-1, required=True)
@click.pass_context
def search(ctx, level, summary, limit, words):
    """Find files semantically matching the query WORDS."""
    db = _db(ctx)
    model = models.default_model(db)
    with open_embedder(model) as emb:
        hits = core.search(db, model, emb, " ".join(words), level, limit)
    for h in hits:
        if not summary:
            click.echo(h.path)
            continue
        fsum, where, context = core.describe(db, h)
        click.echo(f"{h.path}  [{h.score:.3f}]")
        if fsum:
            click.echo(f"    file:  {fsum}")
        if where:
            click.echo(f"    match: {where}" + (f": {context}" if context else ""))


@main.group(cls=Group, context_settings=CTX, no_args_is_help=True)
def model():
    """Find, download and select embedding models."""


@model.command(context_settings=CTX)
@click.option("--offline", is_flag=True, help="Use the built-in catalog only.")
def survey(offline):
    """List model instances available for download."""
    for a in models.survey(not offline):
        mm = f"  +{a.mmproj}" if a.mmproj else ""
        click.echo(f"{a.id:55} {a.backend:12} {a.description}{mm}")


@model.command(context_settings=CTX, no_args_is_help=True)
@click.option("--no-mmproj", is_flag=True, help="Skip the multimodal projector file.")
@click.argument("model_id")
@click.pass_context
def get(ctx, no_mmproj, model_id):
    """Download MODEL_ID (see 'survey') and register it."""
    rec = models.get_model(_db(ctx), model_id, not no_mmproj)
    click.echo(f"{rec.id} -> {rec.path}")


@model.command("list", context_settings=CTX)
@click.pass_context
def list_(ctx):
    """List local models; '*' marks the default."""
    db = _db(ctx)
    default = db.get_meta("default_model")
    for m in db.models():
        click.echo(f"{'*' if m.id == default else ' '} {m.id:55} {m.backend:12} {m.path}")


@model.command(context_settings=CTX, no_args_is_help=True)
@click.argument("model_id")
@click.pass_context
def use(ctx, model_id):
    """Make a local model the default for scan and search."""
    click.echo(f"default: {models.use_model(_db(ctx), model_id).id}")


if __name__ == "__main__":
    main()
