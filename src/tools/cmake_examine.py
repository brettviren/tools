"""cmake-examine: CLI for inspecting CMake source packages without modifying them."""

import logging
from pathlib import Path

import click

from tools import cmake, cmake_config, dot


@click.group()
def cli():
    """Examine CMake-based source packages without modifying them."""
    # Surface library warnings (dropped globs, missing CMakeLists.txt, cmake
    # configure failures) to the user on stderr.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')


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
@click.argument('paths', nargs=-1, required=False,
                type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('-c', '--config', 'config_files', multiple=True, metavar='FILE',
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help='TOML configuration file defining suites and graphs.  May be '
                   'repeated; a cmake-examine-deps.toml in the current directory '
                   'is always loaded first.')
@click.option('-g', '--graph', 'graph_name', metavar='NAME',
              help='Select the [graph.NAME] section to build (config only).  '
                   'Defaults to [graph.DEFAULT], else the first graph defined.')
@click.option('--depth', 'depth', default=-1, show_default=True, metavar='N',
              help='Limit dependency traversal depth from the directly identified '
                   'packages.  -1 traverses as deep as possible; 0 keeps only the '
                   'directly identified packages; N keeps packages within distance N.')
@click.option('--cmake-prefix-path', 'prefix_path', multiple=True, metavar='PATH',
              help='Prefix(es) under which to locate installed <name>Config.cmake '
                   'files so find_package() can be chased past the source packages.  '
                   'May be repeated or os.pathsep-separated; CMAKE_PREFIX_PATH from '
                   'the environment is also consulted.  Where a config file cannot '
                   'be found the graph may be truncated along that path.')
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
    config_files: tuple[Path, ...],
    graph_name: str | None,
    depth: int,
    prefix_path: tuple[str, ...],
    output: str,
    defines: tuple[str, ...],
    run_cmake: bool,
    reduce: bool,
    show_libs: bool,
) -> None:
    """Produce a package-level dependency graph for the given source PATH(s).

    cmake files in each package are parsed for find_package() declarations.
    When --cmake is active (the default), cmake is also run in a secure
    temporary build directory so that only packages cmake actually locates
    appear in the graph; packages not found are omitted.  Inter-source-package
    edges are always preserved regardless of cmake's find result.  Any -D
    options are forwarded to cmake to reflect optional-build choices.

    With a configuration file (-c, or a cmake-examine-deps.toml in the current
    directory) the packages are identified and styled by suites and the graph
    is chosen with -g/--graph; the command-line PATHs, if any, are added as
    directly identified packages.  Without a configuration file, the PATHs are
    required and behaviour is unchanged.

    \b
    Output rules:
      --output cmake-deps.dot   write GraphViz dot text
      --output deps.png         write cmake-deps.dot AND render it to deps.png
      --output deps.svg         write cmake-deps.dot AND render it to deps.svg
    """
    config = cmake_config.load_config(cmake_config.find_config_files(config_files))
    prefixes = cmake_config.resolve_prefix_paths(prefix_path)

    try:
        if config is None:
            if not paths:
                raise click.UsageError('no source PATHs given and no configuration found')
            nodes = cmake.dependency_graph(
                paths, defines=defines, run_cmake=run_cmake, depth=depth,
                prefix_paths=prefixes)
            written = dot.write(nodes, output, reduce=reduce, show_libs=show_libs)
        else:
            graph = cmake_config.select_graph(config, graph_name)
            nodes, label, legend = cmake_config.build_config_graph(
                config, graph, paths, depth=depth, defines=defines,
                run_cmake=run_cmake, show_libs=show_libs, prefix_paths=prefixes)
            written = dot.write(nodes, output, reduce=reduce, show_libs=show_libs,
                                graph_label=label, legend=legend)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    for path in written:
        click.echo(f'Wrote: {path}')


if __name__ == '__main__':
    cli()
