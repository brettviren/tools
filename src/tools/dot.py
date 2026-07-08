"""GraphViz dot generation utilities.

All public functions raise RuntimeError on failure and emit diagnostics via
the standard logging module; no click dependency.
"""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Node fill colours distinguishing packages named on the command line from
# those discovered only through find_package() dependencies.
SOURCE_FILLCOLOR = 'lightblue'
DEP_FILLCOLOR = 'lightgrey'


def escape_string(s: str) -> str:
    """Escape a string for safe use inside a GraphViz double-quoted attribute."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def transitive_reduction(adj: dict[str, set[str]]) -> dict[str, set[str]]:
    """Return a copy of *adj* with transitively redundant edges removed.

    For each node u, the direct edge u→v is kept only when v is NOT reachable
    from any other direct neighbour of u.  Correct for DAGs; cmake package
    graphs are always acyclic.

    Self-loops (u→u) are discarded up front.  A self-loop is meaningless for a
    dependency graph and, worse, would make every other neighbour of u appear
    reachable "via u itself" and thus be wrongly pruned as transitively
    redundant -- silently dropping u's real dependencies.
    """
    adj = {u: (targets - {u}) for u, targets in adj.items()}

    def descendants(start: str) -> set[str]:
        # Initialise seen with start so it is never added to the result set;
        # this makes any cycle back through start harmless.
        seen: set[str] = {start}
        stack = list(adj.get(start, set()))
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(adj.get(n, set()))
        seen.discard(start)
        return seen

    reduced: dict[str, set[str]] = {}
    for u, direct in adj.items():
        reachable_via_others: set[str] = set()
        for v in direct:
            reachable_via_others |= descendants(v)
        reduced[u] = direct - reachable_via_others
    return reduced


def to_dot(nodes: list[dict], reduce: bool = True, show_libs: bool = False) -> str:
    """Return GraphViz dot text for *nodes*.

    Each node dict must have:
      name  – unique identifier string used as the dot node id
      deps  – set of name strings this node depends on
    A truthy ``source`` key marks a package named on the command line; such
    nodes are filled with ``SOURCE_FILLCOLOR`` while packages discovered only
    through dependencies (present solely as edge targets, with no node dict)
    inherit the default ``DEP_FILLCOLOR`` node style.

    When *reduce* is True (the default), transitive reduction is applied
    before emitting edges so that edges implied by longer paths are omitted.
    """
    adj: dict[str, set[str]] = {n['name']: set(n['deps']) for n in nodes}
    edges = transitive_reduction(adj) if reduce else adj

    lines = [
        'digraph cmake_deps {',
        '    rankdir=LR;',
        f'    node [shape=box, style=filled, fillcolor={DEP_FILLCOLOR}];',
        '',
    ]
    for n in nodes:
        # Build the label from individually-escaped parts joined with the
        # GraphViz \n escape.  The separator is placed after escaping so it
        # is never itself escaped.
        node_id = escape_string(n['name'])
        parts = [node_id]
        if show_libs:
            for lib in n.get('libs', []):
                parts.append(escape_string(lib))
        label = r'\n'.join(parts)
        # Command-line packages get a distinct fill; dependency-only packages
        # are never emitted here and so keep the default node fillcolor.
        fill = f', fillcolor={SOURCE_FILLCOLOR}' if n.get('source') else ''
        lines.append(f'    "{node_id}" [label="{label}"{fill}];')
    lines.append('')
    for n in nodes:
        src = n['name']
        for dep in sorted(edges.get(src, set())):
            lines.append(
                f'    "{escape_string(src)}" -> "{escape_string(dep)}";'
            )
    lines.append('}')
    return '\n'.join(lines) + '\n'


def write(
    nodes: list[dict],
    output_filename: str | Path,
    reduce: bool = True,
    show_libs: bool = False,
) -> list[Path]:
    """Write *nodes* to *output_filename*, rendering via GraphViz when needed.

    If the file extension is ``dot``, the dot text is written directly.
    Any other extension causes the dot file to be written alongside the
    output and then rendered by invoking the system ``dot`` command.

    *reduce* is forwarded to :func:`to_dot`; set to False to suppress
    transitive reduction and show all declared edges.

    Returns the list of paths actually written (dot file, and graphics file
    when applicable).  Raises RuntimeError on any failure.
    """
    output_path = Path(output_filename)
    ext = output_path.suffix.lstrip('.').lower()
    dot_path = output_path if ext == 'dot' else output_path.with_suffix('.dot')

    dot_path.write_text(to_dot(nodes, reduce=reduce, show_libs=show_libs))
    log.debug('wrote dot file %s', dot_path)
    written = [dot_path]

    if ext != 'dot':
        if not shutil.which('dot'):
            raise RuntimeError(
                'GraphViz dot(1) not found in PATH; install the graphviz package.'
            )
        res = subprocess.run(
            ['dot', f'-T{ext}', str(dot_path), '-o', str(output_path)],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f'GraphViz failed to render {dot_path} → {output_path}:\n'
                f'{res.stderr.rstrip()}'
            )
        log.debug('rendered %s', output_path)
        written.append(output_path)

    return written
