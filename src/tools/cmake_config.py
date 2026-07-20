"""Configuration handling for ``cmake-examine deps``.

A configuration selects and styles packages via *suites* (sets of package
source directories matched by filesystem globs) and defines *graphs* (named
views built from chosen suites).  All functions raise no click dependency and
emit diagnostics via the standard logging module.
"""

import glob as globmod
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tools import cmake

log = logging.getLogger(__name__)

# When present in the current working directory this file is always taken as a
# configuration file, before any given with -c/--config.
DEFAULT_CONFIG_NAME = 'cmake-examine-deps.toml'

# Fallback styling for the DEFAULT suite when the configuration does not define
# a ``[suite.DEFAULT]`` table (or omits some of its keys).
_DEFAULT_SUITE = {'color': 'gray', 'legend': 'External', 'shape': 'ellipse'}


@dataclass
class Suite:
    """A named set of packages selected by filesystem globs and given a style."""
    name: str
    globs: list[str] = field(default_factory=list)
    color: str | None = None
    legend: str | None = None
    shape: str | None = None

    def style(self) -> dict:
        """Return the node style dict for packages belonging to this suite."""
        return {
            'fillcolor': self.color,
            'shape': self.shape,
            'suite': self.name,
            'legend': self.legend,
        }


@dataclass
class Graph:
    """A named view built from the packages of one or more suites."""
    name: str
    label: str | None = None
    suites: list[str] = field(default_factory=list)


@dataclass
class Config:
    """A merged configuration: suites, graphs and graph insertion order."""
    suites: dict[str, Suite] = field(default_factory=dict)
    graphs: dict[str, Graph] = field(default_factory=dict)

    def default_style(self) -> dict:
        """Return the node style for packages not matched by any named suite."""
        suite = self.suites.get('DEFAULT')
        if suite is None:
            suite = Suite('DEFAULT', color=_DEFAULT_SUITE['color'],
                          legend=_DEFAULT_SUITE['legend'], shape=_DEFAULT_SUITE['shape'])
        return {
            'fillcolor': suite.color or _DEFAULT_SUITE['color'],
            'shape': suite.shape or _DEFAULT_SUITE['shape'],
            'suite': 'DEFAULT',
            'legend': suite.legend or _DEFAULT_SUITE['legend'],
        }


def resolve_prefix_paths(explicit: tuple[str, ...] | list[str]) -> list[str]:
    """Return the ordered install prefixes to search for config packages.

    Each *explicit* value (from --cmake-prefix-path) may itself be an
    os.pathsep-separated list; the ``CMAKE_PREFIX_PATH`` environment variable is
    appended.  Blanks are dropped and order is preserved without duplicates.
    """
    raw: list[str] = []
    for value in explicit:
        raw.extend(value.split(os.pathsep))
    raw.extend(os.environ.get('CMAKE_PREFIX_PATH', '').split(os.pathsep))

    seen: set[str] = set()
    result: list[str] = []
    for entry in raw:
        entry = entry.strip()
        if entry and entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


def find_config_files(explicit: tuple[Path, ...] | list[Path],
                      cwd: Path | None = None) -> list[Path]:
    """Return the ordered list of configuration files to load.

    A ``cmake-examine-deps.toml`` in *cwd* (default: the current working
    directory) comes first when present, followed by the *explicit* files given
    with -c/--config, in order.
    """
    cwd = cwd or Path.cwd()
    files: list[Path] = []
    default = cwd / DEFAULT_CONFIG_NAME
    if default.is_file():
        files.append(default)
    files.extend(Path(p) for p in explicit)
    return files


def _merge(dst: dict, table: str, data: dict) -> None:
    """Merge the sub-tables of ``data[table]`` into ``dst[table]`` in place."""
    for subname, subtable in data.get(table, {}).items():
        dst.setdefault(subname, {}).update(subtable)


def load_config(files: list[Path]) -> Config | None:
    """Load and concatenate configuration *files*, returning a Config.

    Files are merged in order: for a repeated ``[suite.X]`` or ``[graph.X]``
    table the later file's keys override the earlier ones.  Returns None when
    *files* is empty so callers can keep the configuration-free behaviour.
    """
    if not files:
        return None

    suites_raw: dict[str, dict] = {}
    graphs_raw: dict[str, dict] = {}
    for path in files:
        with Path(path).open('rb') as fp:
            data = tomllib.load(fp)
        _merge(suites_raw, 'suite', data)
        _merge(graphs_raw, 'graph', data)

    suites = {
        name: Suite(
            name=name,
            globs=_as_list(tbl.get('glob')),
            color=tbl.get('color'),
            legend=tbl.get('legend'),
            shape=tbl.get('shape'),
        )
        for name, tbl in suites_raw.items()
    }
    graphs = {
        name: Graph(
            name=name,
            label=tbl.get('label'),
            suites=_as_list(tbl.get('suites')),
        )
        for name, tbl in graphs_raw.items()
    }
    return Config(suites=suites, graphs=graphs)


def _as_list(value) -> list[str]:
    """Normalise a scalar-or-list config value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def select_graph(config: Config, name: str | None) -> Graph:
    """Return the graph to build.

    A given *name* selects ``[graph.<name>]`` (a ClickException-friendly
    ValueError is raised if absent).  Otherwise ``[graph.DEFAULT]`` is used,
    falling back to the first graph defined.  When no graphs are defined at all,
    a synthetic graph spanning every non-DEFAULT suite is returned.
    """
    if name is not None:
        if name not in config.graphs:
            raise ValueError(f'no such graph: {name!r}')
        return config.graphs[name]
    if 'DEFAULT' in config.graphs:
        return config.graphs['DEFAULT']
    if config.graphs:
        return next(iter(config.graphs.values()))
    # No graphs defined: span every suite except the styling-only DEFAULT.
    return Graph('DEFAULT', suites=[s for s in config.suites if s != 'DEFAULT'])


def resolve_suite(suite: Suite, base: Path) -> dict[str, Path]:
    """Return a mapping of package name to source directory for *suite*.

    Each glob is expanded relative to *base*.  A match that is a file is
    replaced by its parent directory.  Directories lacking CMake build
    management are warned about and dropped; a glob matching nothing is warned
    about and ignored.
    """
    result: dict[str, Path] = {}
    for pattern in suite.globs:
        matches = globmod.glob(pattern, root_dir=str(base), recursive=True)
        if not matches:
            log.warning('suite %r: glob %r matched nothing', suite.name, pattern)
            continue
        for match in matches:
            path = (base / match)
            if path.is_file():
                path = path.parent
            if not cmake.has_cmake_build(path):
                log.warning('suite %r: %s has no CMakeLists.txt; dropping',
                            suite.name, path)
                continue
            name = cmake.project_name(path)
            result.setdefault(name, path)
    return result


def build_config_graph(
    config: Config,
    graph: Graph,
    paths: tuple[Path, ...] | list[Path],
    base: Path | None = None,
    depth: int = -1,
    defines: tuple[str, ...] = (),
    run_cmake: bool = True,
    show_libs: bool = False,
    prefix_paths: tuple[str, ...] | list[str] = (),
) -> tuple[list[dict], str | None, list[dict]]:
    """Build the styled node list, graph label and legend for *graph*.

    All suites are resolved to form the universe of packages with known source.
    The packages of *graph*'s named suites, plus any packages named on the
    command line (*paths*), are the depth-0 seeds.  Traversal of
    ``find_package()`` dependencies proceeds through the universe up to *depth*;
    packages matching any suite are styled by that suite, the rest by the
    DEFAULT suite.  *prefix_paths* enable chasing installed dependencies'
    ``<name>Config.cmake`` files past the source universe.

    Returns ``(nodes, graph_label, legend)`` for handing to :mod:`tools.dot`.
    """
    base = base or Path.cwd()

    # Resolve suites globally.  Graph-named suites come first so their styling
    # wins for a package that several suites happen to match.
    ordered = graph.suites + [s for s in config.suites
                              if s not in graph.suites and s != 'DEFAULT']
    universe: dict[str, Path] = {}
    styling: dict[str, dict] = {}
    suite_packages: dict[str, set[str]] = {}
    for sname in ordered:
        suite = config.suites.get(sname)
        if suite is None:
            log.warning('graph %r references undefined suite %r', graph.name, sname)
            continue
        resolved = resolve_suite(suite, base)
        suite_packages[sname] = set(resolved)
        for name, path in resolved.items():
            universe.setdefault(name, path)
            styling.setdefault(name, suite.style())

    default_style = config.default_style()

    # Command-line packages are always depth-0 seeds; a matching named suite
    # (already in styling) applies, otherwise they take the DEFAULT style.
    cmdline_names: set[str] = set()
    for path in paths:
        name = cmake.project_name(path)
        cmdline_names.add(name)
        if name not in universe:
            universe[name] = path
            styling[name] = default_style

    seeds: dict[str, Path] = {}
    for sname in graph.suites:
        for name in suite_packages.get(sname, ()):
            seeds[name] = universe[name]
    for name in cmdline_names:
        seeds[name] = universe[name]

    nodes = cmake.build_graph_nodes(
        seeds, universe, styling, default_style,
        depth=depth, defines=defines, run_cmake=run_cmake, show_libs=show_libs,
        prefix_paths=prefix_paths,
    )
    return nodes, graph.label, _legend(nodes)


def _legend(nodes: list[dict]) -> list[dict]:
    """Return one legend entry per suite present among *nodes*, in first-seen order."""
    legend: list[dict] = []
    seen: set[str] = set()
    for node in nodes:
        suite = node.get('suite')
        if suite in seen or not node.get('legend'):
            continue
        seen.add(suite)
        legend.append({
            'label': node['legend'],
            'fillcolor': node.get('fillcolor'),
            'shape': node.get('shape'),
        })
    return legend
