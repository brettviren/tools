# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""tools – manage a collection of CLI tools installed via this package."""

import ast
import functools
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import click
import tomlkit


# ── copyright / license ───────────────────────────────────────────────────────

_COPYRIGHT_HEADER = """\
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""


def _inject_copyright(path: Path, kind: str) -> None:
    """Prepend the copyright/license header if not already present."""
    text = path.read_text()
    if "SPDX-License-Identifier" in text:
        return
    lines = text.splitlines(keepends=True)

    if kind == "bash":
        # Insert after shebang (line 0) and any leading @describe lines.
        idx = 1 if lines and lines[0].startswith("#!") else 0
        while idx < len(lines) and re.match(r"^#\s*@describe", lines[idx]):
            idx += 1
        lines.insert(idx, "\n" + _COPYRIGHT_HEADER)
    else:
        # Python: insert after the PEP 723 block if present, else after shebang.
        m = _PEP723_RE.search(text)
        if m:
            insert_after = text[: m.end()].count("\n")
            lines.insert(insert_after + 1, "\n" + _COPYRIGHT_HEADER)
        else:
            idx = 1 if lines and lines[0].startswith("#!") else 0
            lines.insert(idx, "\n" + _COPYRIGHT_HEADER)

    path.write_text("".join(lines))


# ── package helpers ────────────────────────────────────────────────────────────

def _find_root() -> Path:
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "pyproject.toml").exists():
            return p
    raise click.ClickException("No pyproject.toml found in current directory or any parent")


def _load_pyproject(root: Path) -> tuple[Path, tomlkit.TOMLDocument]:
    path = root / "pyproject.toml"
    return path, tomlkit.loads(path.read_text())


def _detect_type(script: Path) -> str:
    if script.suffix == ".py":
        return "python"
    try:
        first = script.read_bytes().split(b"\n", 1)[0].decode(errors="replace")
        if any(sh in first for sh in ("bash", "/sh", "zsh")):
            return "bash"
        if "python" in first or "uv run" in first:
            return "python"
    except OSError:
        pass
    raise click.ClickException(
        f"Cannot determine script type for '{script.name}' – "
        "add a recognizable shebang or use a .py extension"
    )


def _find_click_entry_points(script: Path) -> list[str]:
    """Return names of top-level functions decorated with @click.command/group.

    Handles both ``import click`` (looks for @click.command / @click.group)
    and ``from click import command, group`` (including aliased imports).
    Only inspects the AST — the script is never imported.
    """
    try:
        tree = ast.parse(script.read_text())
    except SyntaxError:
        return []

    # Build the set of local names that resolve to click.command or click.group.
    # Covers: from click import command, group, command as cmd, etc.
    entry_local_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "click":
            for alias in node.names:
                if alias.name in ("command", "group"):
                    entry_local_names.add(alias.asname or alias.name)

    candidates: list[str] = []
    for node in tree.body:                          # top-level only
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if _is_click_entry_dec(dec, entry_local_names):
                candidates.append(node.name)
                break
    return candidates


def _is_click_entry_dec(dec: ast.expr, entry_local_names: set[str]) -> bool:
    """True if *dec* is @click.command/group or a locally-imported equivalent."""
    node = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "click"
            and node.attr in ("command", "group")
        )
    if isinstance(node, ast.Name):
        return node.id in entry_local_names
    return False


def _resolve_entry_point(script: Path, override: str | None) -> str:
    """Return the Click entry-point function name for *script*.

    Uses *override* if given; otherwise inspects the AST.  Prefers a
    candidate named 'main' or 'cli' when multiple are found, and always
    emits a note when the result is non-obvious.
    """
    if override:
        return override

    found = _find_click_entry_points(script)

    if len(found) == 1:
        if found[0] != "main":
            click.echo(f"  Detected entry point: {found[0]}()")
        return found[0]

    if len(found) == 0:
        click.echo("  Note: no @click.command/group detected; defaulting to 'main'")
        return "main"

    # Multiple candidates – prefer conventional names, then first found.
    for preferred in ("main", "cli"):
        if preferred in found:
            click.echo(
                f"  Multiple Click commands found {found}; "
                f"using '{preferred}' – pass -e to override"
            )
            return preferred
    click.echo(
        f"  Multiple Click commands found {found}; "
        f"using '{found[0]}' – pass -e to override"
    )
    return found[0]


def _detect_shell() -> str:
    shell = os.environ.get("SHELL", "")
    for name in ("fish", "zsh", "bash"):
        if name in shell:
            return name
    return "bash"


def _completion_dir(shell: str) -> Path:
    home = Path.home()
    return {
        "bash": home / ".local/share/bash-completion/completions",
        "zsh":  home / ".local/share/zsh/site-functions",
        "fish": home / ".config/fish/completions",
    }.get(shell, home / ".local/share/bash-completion/completions")


def _completion_filename(shell: str, name: str, outdir: Path) -> Path:
    if shell == "fish":
        return outdir / f"{name}.fish"
    if shell == "zsh":
        return outdir / f"_{name}"
    return outdir / name


def _man_dir() -> Path:
    return Path.home() / ".local/share/man/man1"


def _iter_scripts(root: Path):
    """Yield (name, kind, path, fn) for each registered script, excluding 'tools'.

    Python scripts come from [project.scripts]; fn is the entry-point function
    name parsed from the ep string (e.g. 'cli' from 'tools.clones:cli').
    Bash scripts come from [tool.setuptools] script-files; fn is None.
    """
    _, doc = _load_pyproject(root)
    for name, ep in doc.get("project", {}).get("scripts", {}).items():
        if name == "tools":
            continue
        module, fn = ep.rsplit(":", 1)
        mod_leaf = module.split(".")[-1]
        yield name, "python", root / "src" / "tools" / f"{mod_leaf}.py", fn
    for sf in doc.get("tool", {}).get("setuptools", {}).get("script-files", []):
        path = root / sf
        yield path.name, "bash", path, None


# ── PEP 723 helpers ────────────────────────────────────────────────────────────

_PEP723_RE = re.compile(
    r"^# /// script\s*\n((?:#[^\n]*\n)*?)# ///",
    re.MULTILINE,
)


def _extract_pep723(script: Path) -> dict:
    """Return parsed PEP 723 inline-script metadata, or {} if none found."""
    text = script.read_text(errors="replace")
    m = _PEP723_RE.search(text)
    if not m:
        return {}
    lines = []
    for line in m.group(1).splitlines():
        if line.startswith("# "):
            lines.append(line[2:])
        elif line == "#":
            lines.append("")
    return tomllib.loads("\n".join(lines))


def _pkg_name(dep: str) -> str:
    """Normalised package name from a dependency specifier (PEP 503)."""
    m = re.match(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)", dep.strip())
    return re.sub(r"[-_.]+", "_", m.group(1).lower()) if m else dep.lower()


def _merge_pep723(doc: tomlkit.TOMLDocument, meta: dict) -> list[str]:
    """Merge PEP 723 dependencies into pyproject.toml; return list of added specs."""
    new_specs = meta.get("dependencies", [])
    if not new_specs:
        return []
    proj = doc["project"]
    if "dependencies" not in proj:
        proj.add("dependencies", tomlkit.array())
    existing_names = {_pkg_name(d) for d in proj["dependencies"]}
    added = []
    for spec in new_specs:
        if _pkg_name(spec) not in existing_names:
            proj["dependencies"].append(spec)
            existing_names.add(_pkg_name(spec))
            added.append(spec)
    return added


# ── pyproject.toml mutators ────────────────────────────────────────────────────

def _add_script_file(doc: tomlkit.TOMLDocument, entry: str) -> None:
    """Append entry to [tool.setuptools] script-files if not already present."""
    if "tool" not in doc:
        doc.add("tool", tomlkit.table(is_super_table=True))
    if "setuptools" not in doc["tool"]:
        doc["tool"].add("setuptools", tomlkit.table())
    if "script-files" not in doc["tool"]["setuptools"]:
        doc["tool"]["setuptools"].add("script-files", tomlkit.array())
    sf = doc["tool"]["setuptools"]["script-files"]
    if entry not in sf:
        sf.append(entry)


# ── description extraction ────────────────────────────────────────────────────

def _bash_description(path: Path) -> tuple[str | None, bool]:
    """Return (description, needs_llm).  Reads @describe from the script."""
    m = re.search(r'^#\s*@describe\s+(.+)$', path.read_text(), re.MULTILINE)
    return (m.group(1).strip(), False) if m else (None, True)


def _python_description(module_path: str, fn: str) -> tuple[str | None, bool]:
    """Return (description, needs_llm).  Uses the Click command's help text."""
    try:
        mod = importlib.import_module(module_path)
        cmd = getattr(mod, fn)
        help_text = (cmd.help or "").strip()
    except Exception:
        return None, True
    first = next((l.strip() for l in help_text.splitlines() if l.strip()), "")
    return (first, False) if first else (None, True)


_PLACEHOLDER_SUMMARY = "add your description here"


@functools.lru_cache(maxsize=1)
def _bundled_summaries() -> dict[str, str]:
    """Curated one-line descriptions for externally-sourced scripts whose own
    package metadata is empty or a placeholder; see the 'summary' command."""
    path = Path(__file__).parent / "bundled_summaries.json"
    return json.loads(path.read_text())


def _dist_summary(name: str) -> str | None:
    """Return the 'Summary' metadata of the distribution owning console script NAME.

    Looked up via the installed console_scripts entry point rather than by
    importing anything, so heavy external packages (torch, scipy, ...) never
    get imported just to produce a one-line description.

    The "tools" distribution re-declares every bundled command's entry point
    (that's how 'uv tool install' is made to expose them), so it shows up as
    a second, spurious match here; it must be excluded to find the real
    owning package.
    """
    eps = importlib.metadata.entry_points(group="console_scripts", name=name)
    ep = next((e for e in eps if e.dist and e.dist.name != "tools"), None)
    if ep is None:
        return None
    return (ep.dist.metadata["Summary"] or "").strip() or None


def _external_description(name: str) -> tuple[str | None, bool]:
    """Return (description, needs_llm) for an externally-sourced console script.

    Prefers the owning package's own Summary metadata.  When that's empty or
    the well-known "Add your description here" placeholder, falls back to
    bundled_summaries.json and appends U+F49B (a reminder to fix the
    upstream package's metadata).  When metadata is fine but a now-redundant
    bundled_summaries.json entry still exists for it, appends U+F127 instead
    (a reminder to delete that stale entry).
    """
    summary = _dist_summary(name)
    is_placeholder = not summary or summary.lower() == _PLACEHOLDER_SUMMARY
    cached = _bundled_summaries().get(name)

    if is_placeholder:
        return (f"{cached} ", False) if cached else (None, True)
    if cached:
        return f"{summary} ", False
    return summary, False


# ── description updaters ───────────────────────────────────────────────────────

def _update_bash_description(path: Path, desc: str) -> None:
    """Add or replace the @describe line in a Bash script."""
    text = path.read_text()
    new_line = f"# @describe {desc}"
    pat = re.compile(r"^#\s*@describe\s+.*$", re.MULTILINE)
    if pat.search(text):
        text = pat.sub(new_line, text, count=1)
    else:
        lines = text.splitlines(keepends=True)
        idx = 1 if lines and lines[0].startswith("#!") else 0
        lines.insert(idx, new_line + "\n")
        text = "".join(lines)
    path.write_text(text)


def _update_python_description(path: Path, fn_name: str, desc: str) -> None:
    """Update or insert a one-line docstring for fn_name in a Python source file."""
    text = path.read_text()
    tree = ast.parse(text)
    src = text.splitlines(keepends=True)

    target = next(
        (n for n in tree.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name),
        None,
    )
    if target is None:
        return

    # Indentation from first body line
    first_body = src[target.body[0].lineno - 1]
    indent = " " * (len(first_body) - len(first_body.lstrip()))

    body0 = target.body[0]
    has_docstring = (
        isinstance(body0, ast.Expr)
        and isinstance(body0.value, ast.Constant)
        and isinstance(body0.value.value, str)
    )

    if has_docstring:
        s = body0.lineno - 1       # 0-indexed inclusive start
        e = body0.end_lineno       # 0-indexed exclusive end (end_lineno is 1-indexed)
        first_src = src[s].lstrip()
        quote = '"""' if first_src.startswith('"""') else "'''"
        current = ast.get_docstring(target) or ""
        parts = current.split("\n", 1)
        rest = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        if rest:
            rest_indented = "\n".join(
                f"{indent}{l}" if l.strip() else "" for l in rest.splitlines()
            )
            new_doc = f"{indent}{quote}{desc}\n\n{rest_indented}\n{indent}{quote}\n"
        else:
            new_doc = f"{indent}{quote}{desc}{quote}\n"
        result = src[:s] + [new_doc] + src[e:]
    else:
        ins = target.body[0].lineno - 1
        new_doc = f'{indent}"""{desc}"""\n'
        result = src[:ins] + [new_doc] + src[ins:]

    path.write_text("".join(result))


# ── completion / manpage generators ───────────────────────────────────────────

def _gen_argc_completion(script: Path, shell: str, name: str, outdir: Path) -> None:
    try:
        result = subprocess.run(
            ["argc", "--argc-completions", shell, str(script)],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        click.echo(f"  {name}: 'argc' not found in PATH", err=True)
        return
    except subprocess.CalledProcessError as e:
        click.echo(f"  {name}: argc failed – {e}", err=True)
        return
    out = _completion_filename(shell, name, outdir)
    out.write_text(result.stdout)
    click.echo(f"  {name} ({shell}) → {out}")


def _click_fish_source(name: str, complete_var: str) -> str:
    """Return a correct fish completion script for a Click program.

    Click's built-in fish template (_SOURCE_FISH) has two compounding bugs:
    1. Python \\n/\\t escapes in the template string produce a literal newline
       (breaking the `string split` call) and a literal tab (which fish treats
       as whitespace, collapsing it to a space in `echo` output).
    2. More fundamentally, Click outputs each completion as three *separate*
       lines (type / value / description).  Fish command substitution already
       splits on newlines, so `$response` is a flat list of individual fields.
       The template's `string split \\n` on a single field like "plain" yields a
       one-element list, so `$metadata[2]` and `$metadata[3]` never exist,
       producing the "Missing argument at index 3" errors at completion time.

    The correct approach iterates $response in steps of three and uses printf
    to produce the tab-separated value+description line that fish expects.
    """
    func = f"_{name.replace('-', '_')}_completion"
    return (
        f"function {func};\n"
        f"    set -l response (env {complete_var}=fish_complete"
        f" COMP_WORDS=(commandline -cp) COMP_CWORD=(commandline -t) {name});\n"
        f"    set -l n (count $response);\n"
        f"    set -l i 1;\n"
        f"    while test $i -le $n;\n"
        f"        set -l type_ $response[$i];\n"
        f"        set -l value $response[(math $i + 1)];\n"
        f"        set -l help_ $response[(math $i + 2)];\n"
        f"        set i (math $i + 3);\n"
        f"        if test $type_ = \"dir\";\n"
        f"            __fish_complete_directories $value;\n"
        f"        else if test $type_ = \"file\";\n"
        f"            __fish_complete_path $value;\n"
        f"        else if test $type_ = \"plain\";\n"
        f"            if test $help_ != \"_\";\n"
        f'                printf "%s\\t%s\\n" $value $help_;\n'
        f"            else;\n"
        f"                echo $value;\n"
        f"            end;\n"
        f"        end;\n"
        f"    end;\n"
        f"end;\n"
        f"\n"
        f"complete --no-files --command {name} --arguments \"({func})\";\n"
    )


def _gen_click_completion(name: str, shell: str, outdir: Path) -> None:
    env_var = f"_{name.upper().replace('-', '_')}_COMPLETE"
    env = {**os.environ, env_var: f"{shell}_source"}
    try:
        result = subprocess.run([name], env=env, capture_output=True, text=True)
    except FileNotFoundError:
        click.echo(f"  {name}: not in PATH – install the package first", err=True)
        return
    if not result.stdout.strip():
        click.echo(f"  {name}: no completion output", err=True)
        return
    source = _click_fish_source(name, env_var) if shell == "fish" else result.stdout
    out = _completion_filename(shell, name, outdir)
    out.write_text(source)
    click.echo(f"  {name} ({shell}) → {out}")


def _gen_argc_manpage(script: Path, name: str, outdir: Path) -> None:
    try:
        subprocess.run(
            ["argc", "--argc-mangen", str(script), str(outdir)],
            check=True,
        )
        click.echo(f"  {name} → {outdir}/{name}.1")
    except FileNotFoundError:
        click.echo(f"  {name}: 'argc' not found in PATH", err=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"  {name}: argc failed – {e}", err=True)


def _gen_click_manpage(name: str, module_path: str, fn: str, outdir: Path) -> None:
    try:
        from click_man.core import write_man_pages  # type: ignore[import]
    except ImportError:
        click.echo("  click-man not installed; run: uv add click-man", err=True)
        return
    try:
        mod = importlib.import_module(module_path)
        cmd = getattr(mod, fn)
        write_man_pages(name=name, cli=cmd, target_dir=str(outdir))
        click.echo(f"  {name} → {outdir}/{name}.1")
    except Exception as e:
        click.echo(f"  {name}: {e}", err=True)


# ── CLI ────────────────────────────────────────────────────────────────────────

@click.group()
def main() -> None:
    """Manage the tools package: import scripts, generate completions and man pages."""


@main.command("import")
@click.argument("script", type=click.Path(exists=True, path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option("-e", "--entry-point", default=None, metavar="FUNC",
              help="Python entry-point function name (auto-detected via AST if omitted).")
def import_cmd(script: Path, force: bool, entry_point: str | None) -> None:
    """Import SCRIPT into the tools package.

    Bash scripts are copied to scripts/ and registered in
    [tool.setuptools] script-files so they install into bin/ directly.
    Python scripts are copied to src/tools/ and registered as entry points
    in [project.scripts].  The Click entry-point function is detected
    automatically; use -e FUNC to override.
    """
    root = _find_root()
    kind = _detect_type(script)
    name = script.stem
    mod_name = name.replace("-", "_")
    toml_path, doc = _load_pyproject(root)

    if kind == "bash":
        scripts_dir = root / "scripts"
        scripts_dir.mkdir(exist_ok=True)

        dest = scripts_dir / name
        if dest.exists() and not force:
            raise click.ClickException(f"{dest} already exists; use --force to overwrite")
        shutil.copy2(script, dest)
        dest.chmod(dest.stat().st_mode | 0o111)

        _inject_copyright(dest, "bash")
        _add_script_file(doc, f"scripts/{name}")
        toml_path.write_text(tomlkit.dumps(doc))
        click.echo(f"Imported '{script.name}' as '{name}' (bash) → {dest}")
        return

    # Python
    dest = root / "src" / "tools" / f"{mod_name}.py"
    if dest.exists() and not force:
        raise click.ClickException(f"{dest} already exists; use --force to overwrite")
    shutil.copy2(script, dest)

    _inject_copyright(dest, "python")
    fn = _resolve_entry_point(script, entry_point)

    meta = _extract_pep723(script)
    added = _merge_pep723(doc, meta)
    if added:
        click.echo(f"  PEP 723 deps added: {', '.join(added)}")
    req_py = meta.get("requires-python")
    if req_py:
        current = doc["project"].get("requires-python", "")
        if req_py != current:
            click.echo(
                f"  Note: script requires-python '{req_py}' "
                f"(package has '{current}') – update pyproject.toml if needed"
            )

    if "scripts" not in doc["project"]:
        doc["project"].add("scripts", tomlkit.table())
    doc["project"]["scripts"][name] = f"tools.{mod_name}:{fn}"
    toml_path.write_text(tomlkit.dumps(doc))
    click.echo(f"Imported '{script.name}' as '{mod_name}.py' (python) → {dest}")


@main.command()
@click.option("-s", "--shell", default=None,
              help="Target shell (bash/zsh/fish); defaults to current shell.")
def completions(shell: str | None) -> None:
    """Generate shell completion files for all tools in the package.

    Output is written to the conventional per-shell user completion directory:
    bash  → ~/.local/share/bash-completion/completions/
    zsh   → ~/.local/share/zsh/site-functions/
    fish  → ~/.config/fish/completions/
    """
    if shell is None:
        shell = _detect_shell()

    root = _find_root()
    outdir = _completion_dir(shell)
    outdir.mkdir(parents=True, exist_ok=True)
    click.echo(f"Shell: {shell}  →  {outdir}")

    _gen_click_completion("tools", shell, outdir)

    for name, kind, path, _fn in _iter_scripts(root):
        if kind == "bash":
            _gen_argc_completion(path, shell, name, outdir)
        else:
            _gen_click_completion(name, shell, outdir)


@main.command()
def manpages() -> None:
    """Generate man pages for all tools in the package.

    Output is written to ~/.local/share/man/man1/.
    Bash scripts use 'argc --argc-mangen'; Python scripts use click-man.
    """
    root = _find_root()
    outdir = _man_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    click.echo(f"Man pages → {outdir}")

    _gen_click_manpage("tools", "tools", "main", outdir)

    for name, kind, path, fn in _iter_scripts(root):
        if kind == "bash":
            _gen_argc_manpage(path, name, outdir)
        else:
            mod_leaf = path.stem
            _gen_click_manpage(name, f"tools.{mod_leaf}", fn, outdir)


@main.command()
def summary() -> None:
    """Print a one-line NAME description for every tool in the package.

    Bash tools use their @describe line; Python tools implemented in this
    package use the first line of their Click docstring.  Externally-sourced
    Python tools (see [tool.uv.sources]) use their own package's Summary
    metadata instead, so nothing gets imported just to describe it; a
    placeholder Summary falls back to bundled_summaries.json (marked with
    U+F49B, meaning: go fix the upstream package's metadata) and a redundant
    bundled_summaries.json entry is flagged too (marked with U+F127, meaning:
    go delete that entry).  Tools with no description are marked [missing].
    """
    root = _find_root()
    col = 22

    tools_desc = (main.help or "").splitlines()[0].strip()
    click.echo(f"{'tools':<{col}}  {tools_desc}")

    for name, kind, path, fn in _iter_scripts(root):
        if kind == "bash":
            desc, missing = _bash_description(path)
        elif path.exists():
            desc, missing = _python_description(f"tools.{path.stem}", fn)
        else:
            desc, missing = _external_description(name)

        click.echo(f"{name:<{col}}  {desc if desc else '[missing]'}")
