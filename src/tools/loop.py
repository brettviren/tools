"""loop: run a command once per item of a file glob, line list, numeric range, or literal list.

    loop <options> <iter> <args> -- <body>

<iter> selects how <args> are interpreted (-f/-l/-r/-L, see --help).  <body>
is every token after the first "--" and may contain "{}", which is replaced
with the current item just before that item's command runs.
"""

import os
import shlex
import shutil
import subprocess
import sys
from glob import glob

import click

# Names recognized by --delim, mapped to a printf(1) format-string escape.
CONTROL_DELIMS = {
    "NUL": "\\000", "NULL": "\\000",
    "SOH": "\\001",
    "STX": "\\002",
    "ETX": "\\003",
    "EOT": "\\004",
    "ENQ": "\\005",
    "ACK": "\\006",
    "BEL": "\\007",
    "BS": "\\010",
    "TAB": "\\t", "HT": "\\t",
    "LF": "\\n", "NL": "\\n",
    "VT": "\\013",
    "FF": "\\014",
    "CR": "\\r",
    "SO": "\\016",
    "SI": "\\017",
    "FS": "\\034",
    "GS": "\\035",
    "RS": "\\036",
    "US": "\\037",
    "SP": " ", "SPACE": " ",
}


# ---------------------------------------------------------------------------
# Library functions
# ---------------------------------------------------------------------------

def iter_files(patterns: list[str]) -> list[str]:
    """Return files matched by glob *patterns*; each pattern supports '**'."""
    items = []
    for pat in patterns:
        items.extend(sorted(glob(pat, recursive=True)))
    return items


def iter_lines(files: list[str]) -> list[str]:
    """Return every line (newline stripped) from each file in *files*, in order."""
    items = []
    for fname in files:
        with open(fname) as fh:
            items.extend(line.rstrip("\n") for line in fh)
    return items


def _number(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        return float(text)


def iter_range(args: list[str]) -> list[str]:
    """Return the inclusive range described by 2 or 3 numeric strings: start stop [step]."""
    if len(args) not in (2, 3):
        raise click.UsageError("-r/--range takes 2 or 3 numbers: start stop [step]")
    start, stop = _number(args[0]), _number(args[1])
    step = abs(_number(args[2])) if len(args) == 3 else 1
    step = step if start <= stop else -step
    items = []
    v = start
    while (v <= stop) if step > 0 else (v >= stop):
        items.append(str(v))
        v += step
    return items


def iter_list(args: list[str]) -> list[str]:
    """Return the literal *args* unchanged."""
    return list(args)


def resolve_items(mode: str, args: list[str]) -> list[str]:
    """Dispatch to the iterator matching *mode* ('files', 'lines', 'range', 'list')."""
    return {
        "files": iter_files,
        "lines": iter_lines,
        "range": iter_range,
        "list": iter_list,
    }[mode](args)


def resolve_delim(delim: str) -> tuple[str, bool]:
    """Return (text, is_escape) for *delim*: a control-char name resolves to its
    printf format-string escape (is_escape=True); anything else is used as a
    literal printf argument (is_escape=False)."""
    text = CONTROL_DELIMS.get(delim.upper())
    if text is not None:
        return text, True
    return delim, False


def resolve_njobs(jobs: str | None, n_items: int) -> int | None:
    """Return worker count for *jobs* (None disables parallelism; '0' means auto)."""
    if jobs is None:
        return None
    n = int(jobs)
    if n <= 0:
        n = min(n_items, os.cpu_count() or 1)
    return max(1, n)


def _expand_pattern(pattern: str, index: int, item: str) -> str:
    return pattern.replace("%d", str(index)).replace("{}", item)


def build_line(
    item: str,
    index: int,
    body: list[str],
    shell: str,
    out_pattern: str | None,
    err_pattern: str | None,
    delim: str | None,
) -> str:
    """Return one self-contained POSIX shell command line that runs *body* for *item*.

    Redirection (from --output/--error) and delimiter emission are appended
    outside the inner `<shell> -c ...` invocation, so they apply uniformly
    regardless of what --shell interprets the body itself.
    """
    cmd = " ".join(body).replace("{}", item)
    line = shlex.join([shell, "-c", cmd])
    if out_pattern:
        line += " > " + shlex.quote(_expand_pattern(out_pattern, index, item))
    if err_pattern:
        line += " 2> " + shlex.quote(_expand_pattern(err_pattern, index, item))
    if not out_pattern and delim:
        text, is_escape = resolve_delim(delim)
        printf_cmd = "printf " + shlex.quote(text) if is_escape else "printf '%s' " + shlex.quote(text)
        # Preserve the body's own exit status; printf's would otherwise win.
        line = f"{line}; st=$?; {printf_cmd}; exit $st"
    return line


def build_lines(
    items: list[str],
    body: list[str],
    shell: str,
    out_pattern: str | None,
    err_pattern: str | None,
    delim: str | None,
) -> list[str]:
    return [
        build_line(item, i, body, shell, out_pattern, err_pattern, delim)
        for i, item in enumerate(items)
    ]


def run_serial(lines: list[str]) -> bool:
    """Run each of *lines* sequentially via /bin/sh.  Return True if all succeeded."""
    ok = True
    for line in lines:
        res = subprocess.run(["/bin/sh", "-c", line])
        ok = ok and res.returncode == 0
    return ok


def run_parallel(lines: list[str], njobs: int) -> bool:
    """Run *lines* concurrently via GNU parallel with *njobs* workers, preserving order."""
    if not shutil.which("parallel"):
        raise click.ClickException("GNU parallel not found in PATH; required for -j/--jobs")
    res = subprocess.run(
        ["parallel", "--keep-order", "-j", str(njobs)],
        input="\n".join(lines), text=True,
    )
    return res.returncode == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command(context_settings=dict(
    ignore_unknown_options=True,
    help_option_names=["-h", "--help"],
))
@click.option("-f", "--files", "mode", flag_value="files",
              help="<args> are glob patterns (** supported).")
@click.option("-l", "--lines", "mode", flag_value="lines",
              help="<args> are files; iterate their lines.")
@click.option("-r", "--range", "mode", flag_value="range",
              help="<args> are 'start stop [step]'.")
@click.option("-L", "--list", "mode", flag_value="list",
              help="<args> are literal strings.")
@click.option("-j", "--jobs", is_flag=False, flag_value="0", default=None,
              metavar="NJOBS",
              help="Run via GNU parallel with NJOBS workers (0/omitted: auto).")
@click.option("-s", "--shell", default=None, metavar="SHELL",
              help="Shell used to run <body> (default: $SHELL).")
@click.option("-d", "--delim", default="LF", metavar="DELIM",
              help="Delimiter after each item's stdout, ignored with -o. "
                   "A control-char name (NUL, TAB, LF, ...) or a literal string.")
@click.option("-o", "--output", "out_pattern", default=None, metavar="PATTERN",
              help="Redirect each item's stdout to PATTERN (%d, {} substituted).")
@click.option("-e", "--error", "err_pattern", default=None, metavar="PATTERN",
              help="Redirect each item's stderr to PATTERN (%d, {} substituted).")
@click.argument("iter_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def cli(ctx, mode, jobs, shell, delim, out_pattern, err_pattern, iter_args):
    """Run <body> once per iteration item, substituting "{}" with the item.

    \b
    Examples:
      loop --files '**/*.txt' -- "head -5 {} | grep thing"
      loop -j --output {}.definition --error stderr.%d \\
           --lines /usr/share/dict/words -- dict {}
    """
    body = ctx.obj or []
    if mode is None:
        raise click.UsageError("one of -f/-l/-r/-L is required")
    if not body:
        raise click.UsageError("missing '--' <body>")

    items = resolve_items(mode, list(iter_args))
    shell = shell or os.environ.get("SHELL", "/bin/sh")
    lines = build_lines(items, body, shell, out_pattern, err_pattern, delim)

    njobs = resolve_njobs(jobs, len(items))
    ok = run_parallel(lines, njobs) if njobs else run_serial(lines)
    sys.exit(0 if ok else 1)


def main():
    argv = sys.argv[1:]
    if not argv:
        argv = ["--help"]
    if "--" in argv:
        i = argv.index("--")
        head, body = argv[:i], argv[i + 1:]
    else:
        head, body = argv, []
    cli(args=head, obj=body)


if __name__ == "__main__":
    main()
