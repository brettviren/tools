"""cmake introspection utilities.

All public functions raise RuntimeError on failure and emit diagnostics via
the standard logging module; no click dependency.
"""

import glob as globmod
import logging
import re
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Character class for a CMake name (project, target or package).  CMake allows
# letters, digits and ``_ . + -`` in these names, so ``\w`` alone is wrong: it
# truncates e.g. ``edep-simphony-plugin`` to ``edep`` at the first dash.
_NAME = r'[\w.+-]+'


@dataclass
class Option:
    """A single cmake cache variable exposed as a user-facing option."""
    name: str
    type: str
    default: str
    help: str = ''


def run(args: list[str]) -> subprocess.CompletedProcess:
    """Run cmake with *args* and capture output."""
    return subprocess.run(['cmake'] + args, capture_output=True, text=True)


def define_flags(defines: tuple[str, ...] | list[str]) -> list[str]:
    """Convert ('VAR=VAL', ...) to ['-DVAR=VAL', ...], leaving '-D…' entries as-is."""
    return [d if d.startswith('-D') else f'-D{d}' for d in defines]


def get_options(source_path: Path, defines: tuple[str, ...] = ()) -> list[Option]:
    """Configure *source_path* in a temp build dir and return its cmake options.

    The temp directory is created securely and removed on return.
    Raises RuntimeError if cmake fails.
    """
    with tempfile.TemporaryDirectory(prefix='cmake-examine-') as tmpdir:
        res = run(['-S', str(source_path), '-B', tmpdir] + define_flags(defines))
        if res.returncode != 0:
            raise RuntimeError(
                f'cmake configure failed for {source_path}:\n{res.stderr.rstrip()}'
            )
        # Positional-arg form reads source location from CMakeCache.txt,
        # avoiding a working-directory dependency.
        res = run(['-LH', tmpdir])
        if res.returncode != 0:
            raise RuntimeError(f'cmake -LH failed:\n{res.stderr.rstrip()}')

    return _parse_lh_output(res.stdout)


def _parse_lh_output(lh_output: str) -> list[Option]:
    options: list[Option] = []
    help_lines: list[str] = []
    for raw in lh_output.splitlines():
        line = raw.rstrip()
        if line.startswith('//'):
            help_lines.append(line[2:].strip())
        elif '=' in line and ':' in line.split('=', 1)[0]:
            lhs, value = line.split('=', 1)
            name, vtype = lhs.rsplit(':', 1)
            options.append(Option(
                name=name,
                type=vtype,
                default=value,
                help=' '.join(filter(None, help_lines)),
            ))
            help_lines = []
        else:
            help_lines = []
    return options


def has_cmake_build(source_path: Path) -> bool:
    """Return True if *source_path* is a directory holding CMake build management.

    A top-level ``CMakeLists.txt`` is the marker; a directory lacking one is not
    a CMake source package.
    """
    return source_path.is_dir() and (source_path / 'CMakeLists.txt').exists()


def project_name(source_path: Path) -> str:
    """Return the cmake project() name found in *source_path*, or the dir name."""
    candidates = [source_path / 'CMakeLists.txt'] + sorted(
        source_path.rglob('CMakeLists.txt')
    )
    for f in candidates:
        if not f.exists():
            continue
        try:
            text = _strip_comments(f.read_text(errors='replace'))
        except OSError:
            continue
        m = re.search(rf'(?i)\bproject\s*\(\s*({_NAME})', text)
        if m:
            return m.group(1)
    return source_path.name


def _strip_comments(text: str) -> str:
    """Strip cmake line comments (everything from # to end of line)."""
    return '\n'.join(line.split('#', 1)[0] for line in text.splitlines())


def parse_find_packages(source_path: Path) -> set[str]:
    """Return all find_package() names declared anywhere in *source_path*.

    cmake comments are stripped before parsing so that documentation lines
    such as ``# downstream find_package(foo CONFIG)`` are not treated as
    actual dependency declarations.
    """
    found: set[str] = set()
    for f in (
        list(source_path.rglob('CMakeLists.txt'))
        + list(source_path.rglob('*.cmake'))
    ):
        try:
            text = _strip_comments(f.read_text(errors='replace'))
        except OSError:
            continue
        found.update(re.findall(rf'(?i)\bfind_package\s*\(\s*({_NAME})', text))
    return found


def parse_library_targets(source_path: Path) -> list[str]:
    """Return library target names produced by *source_path*.

    Captures targets declared with an explicit type (SHARED, STATIC,
    INTERFACE, OBJECT, MODULE).  ALIAS and IMPORTED pseudo-targets are
    excluded as they are not build products.
    """
    targets: list[str] = []
    seen: set[str] = set()
    for f in source_path.rglob('CMakeLists.txt'):
        try:
            text = _strip_comments(f.read_text(errors='replace'))
        except OSError:
            continue
        for m in re.finditer(
            rf'(?i)\badd_library\s*\(\s*({_NAME})\s+'
            r'(SHARED|STATIC|INTERFACE|OBJECT|MODULE)\b',
            text,
        ):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                targets.append(name)
    return targets


# Library directory stems searched for installed CMake config packages.  The
# ``lib/*`` entry covers architecture subdirs such as ``lib/x86_64-linux-gnu``.
_CONFIG_LIBDIRS = ('lib', 'lib64', 'lib/*', 'share')


def _config_file_globs(name: str) -> list[str]:
    """Return prefix-relative glob patterns locating *name*'s CMake config file.

    Mirrors the common subset of CMake's CONFIG-mode search: a
    ``<name>Config.cmake`` or ``<name>-config.cmake`` file under the prefix
    root, a ``cmake`` dir, a versioned ``<name>*`` dir, or the usual
    ``lib``/``lib64``/``share`` cmake locations.  Both the given case and the
    lower-cased name are tried, matching CMake's case handling of the file and
    directory names.
    """
    names = [name] if name == name.lower() else [name, name.lower()]
    bases = [f'{n}Config.cmake' for n in names] + [f'{n}-config.cmake' for n in names]

    dirs: set[str] = {'', 'cmake', 'CMake'}
    for n in names:
        dirs |= {f'{n}*', f'{n}*/cmake', f'{n}*/CMake'}
        for lib in _CONFIG_LIBDIRS:
            dirs |= {
                f'{lib}/cmake', f'{lib}/cmake/{n}*',
                f'{lib}/{n}*', f'{lib}/{n}*/cmake',
                f'{n}*/{lib}/cmake/{n}*', f'{n}*/{lib}/{n}*',
            }

    return [f'{d}/{b}' if d else b for d in dirs for b in dict.fromkeys(bases)]


def find_config_file(name: str, prefix_paths: tuple[str, ...] | list[str]) -> Path | None:
    """Return the installed ``<name>Config.cmake`` under *prefix_paths*, or None.

    Each prefix is searched using the patterns from :func:`_config_file_globs`;
    the first existing match wins.
    """
    patterns = _config_file_globs(name)
    for prefix in prefix_paths:
        base = Path(prefix)
        if not base.is_dir():
            continue
        for pattern in patterns:
            for match in globmod.glob(pattern, root_dir=str(base)):
                candidate = base / match
                if candidate.is_file():
                    return candidate
    return None


def parse_config_deps(config_dir: Path) -> set[str]:
    """Return package names required by the CMake config package in *config_dir*.

    Both ``find_package()`` and ``find_dependency()`` calls (the latter from
    CMakeFindDependencyMacro, ubiquitous in installed ``*Config.cmake`` files)
    across every ``*.cmake`` file in the directory are collected.
    """
    found: set[str] = set()
    for f in config_dir.rglob('*.cmake'):
        try:
            text = _strip_comments(f.read_text(errors='replace'))
        except OSError:
            continue
        found.update(re.findall(rf'(?i)\bfind_package\s*\(\s*({_NAME})', text))
        found.update(re.findall(rf'(?i)\bfind_dependency\s*\(\s*({_NAME})', text))
    return found


def found_packages(
    source_path: Path,
    build_dir: str | Path,
    defines: tuple[str, ...] = (),
) -> set[str] | None:
    """Configure *source_path* in *build_dir* and return the packages cmake found.

    Returns None when cmake configure fails so callers can fall back to the
    full parsed dependency set.

    cmake's "-- Found X:" status lines are the primary signal; _FOUND cache
    variables are cmake-scope only and rarely appear in CMakeCache.txt, but
    are checked as a secondary source for completeness.
    """
    res = run(
        ['-S', str(source_path), '-B', str(build_dir)] + define_flags(defines)
    )
    if res.returncode != 0:
        log.warning('cmake configure failed for %s:\n%s', source_path, res.stderr.rstrip())
        return None

    pkgs: set[str] = set()
    for line in res.stdout.splitlines():
        m = re.match(rf'--\s+Found\s+({_NAME})\s*[:.]', line)
        if m:
            pkgs.add(m.group(1))

    cache = Path(build_dir) / 'CMakeCache.txt'
    if cache.exists():
        for line in cache.read_text(errors='replace').splitlines():
            m = re.match(rf'({_NAME})_FOUND(?::\w+)?=(.+)', line.strip())
            if m and m.group(2).strip().upper() in ('TRUE', '1', 'YES', 'ON'):
                pkgs.add(m.group(1))

    return pkgs


def _package_deps(
    source_path: Path,
    name: str,
    source_names: set[str],
    defines: tuple[str, ...] = (),
    run_cmake: bool = True,
    build_dir: str | Path | None = None,
) -> set[str]:
    """Return the package dependencies declared by *source_path*.

    Deps are taken from parsing ``find_package()`` calls across the source
    tree.  cmake's stdout cannot be used to filter external deps reliably:
    many projects suppress output with ``QUIET``, use custom status messages,
    or report failures in non-standard formats.

    When *run_cmake* is True, *defines* are provided and *build_dir* is given,
    cmake IS run with those defines so that conditional ``find_package`` blocks
    gated behind cmake options are only executed when the option is active.
    The parsed dep set is then intersected with packages cmake actually
    executed ``find_package`` for (detected via ``_DIR`` or ``_FOUND`` cache
    entries), giving a more accurate picture of which optional deps apply.

    Inter-source-package edges (deps naming a package in *source_names*) are
    always preserved regardless of cmake's find result, because sibling source
    packages are not installed and cmake will never report them as found.

    A package never depends on itself; any self-reference is dropped.  A
    self-referential ``find_package()`` can arise from bundled stand-alone
    examples (e.g. edep-sim's examples/ExternalKinematics does
    ``find_package(EDepSim)``) which are scanned but are not part of this
    package's own build.
    """
    deps = parse_find_packages(source_path)

    if run_cmake and defines and build_dir is not None:
        fp = found_packages(source_path, build_dir, defines)
        if fp is not None:
            # Packages cmake actually looked up (found or cached).  Also accept
            # packages whose _DIR cache entry was set, which covers CONFIG-mode
            # finds that succeed silently.
            cache = Path(build_dir) / 'CMakeCache.txt'
            if cache.exists():
                for line in cache.read_text(errors='replace').splitlines():
                    m = re.match(rf'({_NAME})_DIR(?::\w+)?=(.+)', line.strip())
                    if m and m.group(2).strip() not in ('', 'NOTFOUND',
                                                         f'{m.group(1)}_DIR-NOTFOUND'):
                        fp.add(m.group(1))
            deps = (deps & fp) | (deps & source_names)

    return deps - {name}


def build_graph_nodes(
    seeds: dict[str, Path],
    universe: dict[str, Path],
    styling: dict[str, dict],
    default_style: dict,
    depth: int = -1,
    defines: tuple[str, ...] = (),
    run_cmake: bool = True,
    show_libs: bool = False,
    prefix_paths: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    """Return styled node dicts by traversing ``find_package()`` dependencies.

    *seeds* maps depth-0 package names to their source directories; these are
    the explicitly identified packages.  *universe* maps every package name we
    have source for (a superset of *seeds*) to its source directory; traversal
    descends into these by parsing their source.

    A package discovered via ``find_package()`` that is absent from *universe*
    is an installed dependency: when *prefix_paths* are given its
    ``<name>Config.cmake`` is located and its own ``find_package()`` /
    ``find_dependency()`` calls are chased so the graph can grow past it.  If
    that config file cannot be located, a message is logged noting the graph
    may be truncated along that path, and the package becomes a leaf.  Without
    *prefix_paths* every such package is simply a leaf.

    *styling* maps package names to a style dict (``fillcolor``, ``shape``,
    ``suite``, ``legend``); *default_style* is used for any package not present
    in *styling* (typically installed/external dependencies).

    *depth* bounds the breadth-first traversal.  The depth of a package is the
    length of the shortest ``find_package()`` path to it from any seed (seeds
    are depth 0).  ``depth == -1`` traverses as deep as possible; ``depth == 0``
    keeps only the seeds; ``depth == N`` keeps packages up to distance N.

    Each returned node dict has keys ``name``, ``deps`` (edge targets, already
    restricted to included nodes), ``libs``, ``source`` (True for seeds) and the
    style keys from *styling* or *default_style*.
    """
    dist: dict[str, int] = {name: 0 for name in seeds}
    queue: deque[str] = deque(seeds)
    adjacency: dict[str, set[str]] = {}
    universe_names = set(universe)
    config_cache: dict[str, Path | None] = {}
    truncated: set[str] = set()

    with tempfile.TemporaryDirectory(prefix='cmake-examine-') as tmpdir:
        build_index = 0
        while queue:
            name = queue.popleft()
            d = dist[name]
            if depth != -1 and d > depth:
                continue  # beyond the requested depth; its edges are pruned too

            source_path = universe.get(name)
            if source_path is not None:
                build_dir: Path | None = None
                if run_cmake and defines:
                    build_dir = Path(tmpdir) / f'build{build_index}'
                    build_dir.mkdir()
                    build_index += 1
                deps = _package_deps(source_path, name, universe_names,
                                     defines, run_cmake, build_dir)
            elif prefix_paths:
                if name not in config_cache:
                    config_cache[name] = find_config_file(name, prefix_paths)
                config = config_cache[name]
                if config is not None:
                    deps = parse_config_deps(config.parent) - {name}
                else:
                    deps = set()
                    # Only note truncation when we would actually have descended
                    # further (i.e. this package's children would be included).
                    want_children = depth == -1 or d < depth
                    if want_children and name not in truncated:
                        truncated.add(name)
                        log.warning(
                            'find_package(%s): no *Config.cmake found under the '
                            'given prefixes; graph may be truncated here', name)
            else:
                deps = set()  # installed dependency, not chased: a leaf

            adjacency[name] = deps
            for dep in deps:
                if dep not in dist:
                    dist[dep] = d + 1
                    queue.append(dep)

    included = {n for n, d in dist.items() if depth == -1 or d <= depth}

    nodes: list[dict] = []
    for name in sorted(included):
        source_path = universe.get(name)
        style = styling.get(name, default_style)
        nodes.append({
            'name': name,
            'deps': {dep for dep in adjacency.get(name, set()) if dep in included},
            'libs': parse_library_targets(source_path) if (show_libs and source_path) else [],
            'source': name in seeds,
            'fillcolor': style.get('fillcolor'),
            'shape': style.get('shape'),
            'suite': style.get('suite'),
            'legend': style.get('legend'),
        })
    return nodes


def dependency_graph(
    paths: list[Path] | tuple[Path, ...],
    defines: tuple[str, ...] = (),
    run_cmake: bool = True,
    depth: int = -1,
    prefix_paths: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    """Return a node list describing the package-level dependency graph.

    Each node is a dict with keys ``name``, ``libs``, ``deps`` and ``source``
    (True for the command-line packages).  This is the configuration-free path:
    the packages named on the command line are the depth-0 packages and their
    ``find_package()`` dependencies are installed packages at depth 1.

    *depth* of 0 keeps only inter-command-line edges; the default -1 (or any
    positive value) keeps the dependencies.  When *prefix_paths* are given the
    dependencies' own ``<name>Config.cmake`` files are chased so the graph can
    grow past them (see :func:`build_graph_nodes`).
    """
    seeds = {project_name(p): p for p in paths}
    default_style = {'fillcolor': None, 'shape': None, 'suite': None, 'legend': None}
    return build_graph_nodes(
        seeds, dict(seeds), {}, default_style,
        depth=depth, defines=defines, run_cmake=run_cmake,
        show_libs=True, prefix_paths=prefix_paths,
    )
