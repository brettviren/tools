"""cmake-examine: CLI for inspecting CMake source packages without modifying them."""

from pathlib import Path

import click

from tools import cmake, dot


@click.group()
def cli():
    """Examine CMake-based source packages without modifying them."""


@cli.command(name='options')
@click.argument('paths', nargs=-1, required=True,
                type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('-D', 'defines', multiple=True, metavar='VAR=VALUE',
              help='Extra cmake -D options to pass during configuration.')
def options_cmd(paths: tuple[Path, ...], defines: tuple[str, ...]) -> None:
    """Show cmake configure options for each source PATH.

    cmake is run in a secure temporary build directory; no source file is
    modified.  The temporary directory is removed on exit.
    """
    for path in paths:
        if len(paths) > 1:
            click.secho(f'\n==> {path}', bold=True)
        try:
            options = cmake.get_options(path, defines)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if not options:
            click.echo('  (no options found)')
            continue
        for opt in options:
            click.echo(f'  {opt.name}  ({opt.type})  default={opt.default!r}')
            if opt.help:
                click.echo(f'    {opt.help}')


@cli.command(name='deps')
@click.argument('paths', nargs=-1, required=True,
                type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('-o', '--output', 'output', default='cmake-deps.dot', show_default=True,
              metavar='FILE',
              help='Output file.  Extension "dot" writes dot text; any other '
                   'extension also renders via GraphViz dot(1).')
@click.option('-D', 'defines', multiple=True, metavar='VAR=VALUE',
              help='cmake -D options (e.g. BUILD_TESTS=ON) to control optional deps.')
@click.option('--cmake/--no-cmake', 'run_cmake', default=True, show_default=True,
              help='When -D options are given, run cmake so conditional '
                   'find_package blocks are evaluated with those options active. '
                   'Without -D options this flag has no effect on the output.')
@click.option('--reduce/--no-reduce', 'reduce', default=True, show_default=True,
              help='Apply transitive reduction: omit edges implied by a longer path '
                   '(--no-reduce shows every declared dependency).')
@click.option('--libs/--no-libs', 'show_libs', default=False, show_default=True,
              help='List library targets produced by each package inside its node.')
def deps_cmd(
    paths: tuple[Path, ...],
    output: str,
    defines: tuple[str, ...],
    run_cmake: bool,
    reduce: bool,
    show_libs: bool,
) -> None:
    """Produce a package-level dependency graph for the given source PATH(s).

    cmake files in each PATH are parsed for find_package() declarations.
    When --cmake is active (the default), cmake is also run in a secure
    temporary build directory so that only packages cmake actually locates
    appear in the graph; packages not found are omitted.  Inter-source-package
    edges are always preserved regardless of cmake's find result.  Any -D
    options are forwarded to cmake to reflect optional-build choices.

    \b
    Output rules:
      --output cmake-deps.dot   write GraphViz dot text
      --output deps.png         write cmake-deps.dot AND render it to deps.png
      --output deps.svg         write cmake-deps.dot AND render it to deps.svg
    """
    try:
        nodes = cmake.dependency_graph(paths, defines=defines, run_cmake=run_cmake)
        written = dot.write(nodes, output, reduce=reduce, show_libs=show_libs)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    for path in written:
        click.echo(f'Wrote: {path}')


if __name__ == '__main__':
    cli()
