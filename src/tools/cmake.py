"""cmake introspection utilities.

All public functions raise RuntimeError on failure and emit diagnostics via
the standard logging module; no click dependency.
"""

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


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
        m = re.search(r'(?i)\bproject\s*\(\s*(\w+)', text)
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
        found.update(re.findall(r'(?i)\bfind_package\s*\(\s*(\w+)', text))
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
            r'(?i)\badd_library\s*\(\s*(\w+)\s+'
            r'(SHARED|STATIC|INTERFACE|OBJECT|MODULE)\b',
            text,
        ):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                targets.append(name)
    return targets


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
        m = re.match(r'--\s+Found\s+(\w+)\s*[:.]', line)
        if m:
            pkgs.add(m.group(1))

    cache = Path(build_dir) / 'CMakeCache.txt'
    if cache.exists():
        for line in cache.read_text(errors='replace').splitlines():
            m = re.match(r'(\w+)_FOUND(?::\w+)?=(.+)', line.strip())
            if m and m.group(2).strip().upper() in ('TRUE', '1', 'YES', 'ON'):
                pkgs.add(m.group(1))

    return pkgs


def dependency_graph(
    paths: list[Path] | tuple[Path, ...],
    defines: tuple[str, ...] = (),
    run_cmake: bool = True,
) -> list[dict]:
    """Return a node list describing the package-level dependency graph.

    Each node is a dict with keys:
      name  – cmake project name (or directory name as fallback)
      libs  – library targets produced by this package
      deps  – set of package-name strings this project depends on

    Deps are always taken from parsing ``find_package()`` calls across the
    source tree.  cmake's stdout cannot be used to filter external deps
    reliably: many projects suppress output with ``QUIET``, use custom
    status messages, or report failures in non-standard formats.

    When *run_cmake* is True and *defines* are provided, cmake IS run with
    those defines so that conditional ``find_package`` blocks that are gated
    behind cmake options are only executed when the option is active.  The
    parsed dep set is then intersected with packages cmake actually executed
    ``find_package`` for (detected via ``_DIR`` or ``_FOUND`` cache entries),
    giving a more accurate picture of which optional deps apply.

    Inter-source-package edges are always preserved regardless of cmake's
    find result because sibling source packages are not installed and cmake
    will never report them as found.
    """
    source_names: set[str] = {project_name(p) for p in paths}

    nodes: list[dict] = []
    with tempfile.TemporaryDirectory(prefix='cmake-examine-') as tmpdir:
        for i, source_path in enumerate(paths):
            name = project_name(source_path)
            deps = parse_find_packages(source_path)

            if run_cmake and defines:
                # Only run cmake (and filter) when the caller supplied -D
                # options; without them the cmake environment is not set up
                # for these source packages and the "found" set is meaningless.
                build_dir = Path(tmpdir) / f'build{i}'
                build_dir.mkdir()
                fp = found_packages(source_path, build_dir, defines)
                if fp is not None:
                    # Packages cmake actually looked up (found or cached).
                    # Also accept packages whose _DIR cache entry was set,
                    # which covers CONFIG-mode finds that succeed silently.
                    cache = Path(build_dir) / 'CMakeCache.txt'
                    if cache.exists():
                        for line in cache.read_text(errors='replace').splitlines():
                            m = re.match(r'(\w+)_DIR(?::\w+)?=(.+)', line.strip())
                            if m and m.group(2).strip() not in ('', 'NOTFOUND',
                                                                 f'{m.group(1)}_DIR-NOTFOUND'):
                                fp.add(m.group(1))
                    deps = (deps & fp) | (deps & source_names)

            # A package never depends on itself.  A self-referential
            # find_package() can arise from bundled stand-alone examples (e.g.
            # edep-sim's examples/ExternalKinematics does find_package(EDepSim))
            # which are scanned but are not part of this package's own build.
            deps = deps - {name}

            nodes.append({
                'name': name,
                'libs': parse_library_targets(source_path),
                'deps': deps,
            })
    return nodes
