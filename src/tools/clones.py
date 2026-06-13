#!/usr/bin/env -S uv run --script
# -*- python -*-
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "click",
# ]
# ///



import dataclasses
import os
import shlex
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import click


CONFIG_PATH = Path("~/.config/clones/config.toml").expanduser()


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _infer_group(config):
    """Return a group name inferred from the cwd's git worktree root, or None."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    name = Path(result.stdout.strip()).name
    return name if name in config else None


def parse_path(path):
    """Return (user, host, path) from an SCP-style URI or plain path."""
    if ":" in path:
        hostpart, remotepath = path.split(":", 1)
        if "@" in hostpart:
            user, host = hostpart.split("@", 1)
        else:
            user = None
            host = hostpart
        return user, host, remotepath
    return None, None, path


_SSH_BASE = [
    "ssh",
    "-o", "ConnectTimeout=2",
    "-o", "BatchMode=yes",
]


def run_in_repo(user, host, path, script, capture=False):
    """Run script under 'cd path && ...' on host (or locally if host is None).

    The script is fed to bash via stdin so the remote login shell (e.g. fish)
    is never asked to parse it.
    """
    full = f"cd {shlex.quote(path)} && {{\n{script}\n}}"
    if host:
        target = f"{user}@{host}" if user else host
        argv = _SSH_BASE + [target, "bash"]
    else:
        argv = ["bash"]
    if capture:
        return subprocess.run(argv, input=full, capture_output=True, text=True)
    return subprocess.run(argv, input=full, text=True)


def run_command(user, host, script, capture=False):
    """Run script on host without cd'ing anywhere (starts in home dir)."""
    if host:
        target = f"{user}@{host}" if user else host
        argv = _SSH_BASE + [target, "bash"]
    else:
        argv = ["bash"]
    if capture:
        return subprocess.run(argv, input=script, capture_output=True, text=True)
    return subprocess.run(argv, input=script, text=True)


# ── preflight ──────────────────────────────────────────────────────────────

@dataclasses.dataclass
class RepoState:
    group: str
    label: str
    user: str | None
    host: str | None
    path: str
    ahead: int = 0
    behind: int = 0
    uncommitted: int = 0
    error: str | None = None
    missing: bool = False
    git_dir_is_cwd: bool = False  # bare or vcsh-style: the git dir IS the path

    @property
    def diverged(self):
        return self.ahead > 0 and self.behind > 0

    def summary(self):
        if self.error:
            return f"error: {self.error}"
        parts = []
        if self.uncommitted:
            parts.append(f"uncommitted ({self.uncommitted})")
        if self.ahead and self.behind:
            parts.append(f"diverged (ahead {self.ahead}, behind {self.behind})")
        elif self.ahead:
            parts.append(f"ahead {self.ahead}")
        elif self.behind:
            parts.append(f"behind {self.behind}")
        return ", ".join(parts) if parts else "clean"


# Runs on the remote (or local) shell; outputs "ahead behind|dirty_count".
# Handles three repo layouts automatically:
#   normal  – git dir inside worktree, cwd == worktree root
#   vcsh    – git dir IS cwd (core.bare=false, core.worktree=elsewhere)
#   bare    – git dir IS cwd, no worktree (core.bare=true)
# Detection order: check whether the git dir IS the cwd first (covers bare and
# vcsh), then fall back to the normal worktree-root check.  Using
# --absolute-git-dir avoids the ambiguity of --git-dir sometimes returning '.'
# and sometimes an absolute path depending on git version/config.
# For vcsh, git honours core.worktree so `git status` returns a real dirty count.
# For bare, git status exits non-zero with stderr suppressed → wc -l gives 0.
_PREFLIGHT_SCRIPT = """\
RCWD=$(pwd -P)
AGD=$(git rev-parse --absolute-git-dir 2>/dev/null)
if [ "$AGD" = "$RCWD" ]; then
  GDC=1
else
  GDC=0
  TOP=$(git rev-parse --show-toplevel 2>/dev/null)
  [ -n "$TOP" ] || { echo "NOGIT|0|0"; exit 0; }
  RTOP=$(cd "$TOP" && pwd -P)
  [ "$RTOP" = "$RCWD" ] || { echo "NOTROOT|0|0"; exit 0; }
fi
git fetch -q 2>/dev/null
R=$(git rev-list --left-right --count HEAD...@{u} 2>/dev/null || echo ERR)
D=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
echo "$R|$D|$GDC"
"""


def preflight_one(group, label, user, host, path) -> RepoState:
    state = RepoState(group=group, label=label, user=user, host=host, path=path)
    result = run_in_repo(user, host, path, _PREFLIGHT_SCRIPT, capture=True)
    if result.returncode != 0:
        state.missing = True
        state.error = "missing"
        return state
    output = result.stdout.strip()
    try:
        parts = output.split("|")
        rev_part = parts[0].strip()
        state.uncommitted = int(parts[1]) if len(parts) > 1 else 0
        state.git_dir_is_cwd = len(parts) > 2 and parts[2].strip() == "1"
        if rev_part == "ERR":
            state.error = "no upstream configured"
        elif rev_part == "NOGIT":
            state.missing = True
            state.error = "not a git repository"
        elif rev_part == "NOTROOT":
            state.missing = True
            state.error = "path is inside a git repo but not its root"
        else:
            ahead, behind = rev_part.split()
            state.ahead = int(ahead)
            state.behind = int(behind)
    except (ValueError, AttributeError):
        state.error = f"unexpected output: {output!r}"
    return state


def preflight_groups(ctx) -> list[RepoState]:
    config = ctx.obj["config"]
    groups = ctx.obj["groups"]
    states = []
    for group in groups:
        paths = config[group].get("paths", [])
        if not paths:
            click.echo(f"warning: no paths defined for group '{group}'", err=True)
            continue
        for path in paths:
            user, host, remote_path = parse_path(path)
            label = (f"{user}@{host}" if user else host) + f":{remote_path}" if host else remote_path
            states.append(preflight_one(group, label, user, host, remote_path))
    return states


def _state_marker(state):
    if state.missing:     return "?"
    if state.error:       return "✗"
    if state.diverged:    return "⇅"
    if state.uncommitted: return "●"
    if state.ahead:       return "↑"
    if state.behind:      return "↓"
    return "✓"


def print_states(states):
    current_group = None
    for state in states:
        if state.group != current_group:
            click.echo(f"\n[{state.group}]")
            current_group = state.group
        click.echo(f"  [{_state_marker(state)}] {state.label}: {state.summary()}")


# ── async status ───────────────────────────────────────────────────────────

_M_PENDING     = '·'
_M_CONNECTING  = '⟳'
_M_CHECKING    = '⠿'
_M_CLEAN       = '✓'
_M_UNCOMMITTED = '●'
_M_AHEAD       = '↑'
_M_BEHIND      = '↓'
_M_DIVERGED    = '⇅'
_M_ERROR       = '✗'
_M_MISSING     = '?'
_M_TIMEOUT     = '⏱'


class _LiveLines:
    """Thread-safe terminal display of N fixed lines, updatable in place."""

    def __init__(self):
        self._lock = threading.Lock()
        self._lines = []

    def add(self, text):
        self._lines.append(text)
        return len(self._lines) - 1

    def start(self):
        for line in self._lines:
            sys.stdout.write(line + '\n')
        sys.stdout.flush()

    def update(self, idx, text):
        with self._lock:
            self._lines[idx] = text
            n = len(self._lines)
            up = n - idx
            # Move up to line idx, clear it, write new text, return to bottom.
            sys.stdout.write(f'\033[{up}A\r\033[K{text}\033[{up}B\r')
            sys.stdout.flush()


def _preflight_live(entries) -> list[RepoState]:
    """Run preflight for all entries concurrently with a live terminal display.

    entries: list of (group, label, user, host, path)
    Returns a RepoState per entry in the same order.
    """
    states = [None] * len(entries)
    display = _LiveLines()
    for group, label, *_ in entries:
        display.add(f'  [{_M_PENDING}] [{group}] {label}: pending')
    display.start()

    def _worker(idx, group, label, user, host, path):
        def put(marker, msg):
            display.update(idx, f'  [{marker}] [{group}] {label}: {msg}')
        if host:
            put(_M_CONNECTING, 'connecting...')
            target = f'{user}@{host}' if user else host
            try:
                r = subprocess.run(
                    _SSH_BASE + ['-q', target, 'true'],
                    capture_output=True, timeout=20,
                )
            except subprocess.TimeoutExpired:
                put(_M_TIMEOUT, 'connection timed out')
                states[idx] = RepoState(group=group, label=label, user=user, host=host,
                                        path=path, missing=True, error='connection timed out')
                return
            if r.returncode != 0:
                put(_M_ERROR, 'connection failed')
                states[idx] = RepoState(group=group, label=label, user=user, host=host,
                                        path=path, missing=True, error='connection failed')
                return
        put(_M_CHECKING, 'checking...')
        state = preflight_one(group, label, user, host, path)
        put(_state_marker(state), state.summary())
        states[idx] = state

    with ThreadPoolExecutor(max_workers=min(len(entries), 32)) as pool:
        futures = [pool.submit(_worker, i, *entry) for i, entry in enumerate(entries)]
        for f in futures:
            try:
                f.result()
            except Exception as exc:
                click.echo(f'  error: {exc}', err=True)

    sys.stdout.write('\n')
    sys.stdout.flush()
    return states


# ── CLI ────────────────────────────────────────────────────────────────────

@click.group()
@click.option("--repo", envvar="CLONES_REPO", default=None,
              help="Repo group name (or set CLONES_REPO).")
@click.option("-a", "--all", "all_groups", is_flag=True, default=False,
              help="Operate on all repo groups.")
@click.pass_context
def cli(ctx, repo, all_groups):
    """Manage git repository clones across local and remote hosts."""
    ctx.ensure_object(dict)
    config = load_config()
    ctx.obj["config"] = config

    if ctx.invoked_subcommand in ("edit", "list"):
        return

    if all_groups or repo == "all":
        groups = list(config.keys())
    elif repo:
        if repo not in config:
            raise click.ClickException(f"unknown group '{repo}'")
        groups = [repo]
    else:
        inferred = _infer_group(config)
        if inferred:
            groups = [inferred]
        else:
            raise click.ClickException(
                "specify a repo group with --repo / CLONES_REPO, or use -a/--all"
            )
    ctx.obj["groups"] = groups


def run_in_groups(ctx, cmd):
    config = ctx.obj["config"]
    groups = ctx.obj["groups"]
    shell_cmd = " ".join(shlex.quote(a) for a in cmd)

    for group in groups:
        paths = config[group].get("paths", [])
        if not paths:
            click.echo(f"warning: no paths defined for group '{group}'", err=True)
            continue
        for path in paths:
            user, host, remote_path = parse_path(path)
            label = (f"{user}@{host}" if user else host) + f":{remote_path}" if host else remote_path
            click.echo(f"==> [{group}] {label}")
            run_in_repo(user, host, remote_path, shell_cmd)


@cli.command(name="run", context_settings={"ignore_unknown_options": True})
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def run_cmd(ctx, cmd):
    """Run CMD in each repo path of the selected group(s)."""
    run_in_groups(ctx, cmd)


@cli.command(name="git", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def git_cmd(ctx, args):
    """Run git ARGS in each repo path of the selected group(s)."""
    run_in_groups(ctx, ("git",) + args)


@cli.command(name="status")
@click.pass_context
def status_cmd(ctx):
    """Show ahead/behind/diverged state for each repo (runs git fetch)."""
    config = ctx.obj['config']
    groups = ctx.obj['groups']

    entries = []
    for group in groups:
        paths = config[group].get('paths', [])
        if not paths:
            click.echo(f"warning: no paths defined for group '{group}'", err=True)
            continue
        for path in paths:
            user, host, rpath = parse_path(path)
            label = (
                (f'{user}@{host}' if user else host) + f':{rpath}'
                if host else rpath
            )
            entries.append((group, label, user, host, rpath))

    if entries:
        _preflight_live(entries)


def _sync_one_group(config, group, emit, *, interactive=True):
    """Sync one group.  emit(msg) is the output function for labeled messages;
    subprocess output (git, ssh) streams to stdout directly.
    interactive=True uses the live parallel display for preflight (single-group);
    interactive=False runs preflight in parallel but prints results via emit (multi-group)."""
    if config[group].get("bare", False):
        emit("skipping (bare=true — sync not supported).")
        return

    paths = config[group].get("paths", [])
    if not paths:
        emit("warning: no paths defined.")
        return

    entries = []
    for path in paths:
        user, host, rpath = parse_path(path)
        label = (f"{user}@{host}" if user else host) + f":{rpath}" if host else rpath
        entries.append((group, label, user, host, rpath))

    emit("Preflight...")
    if interactive:
        states = _preflight_live(entries)
    else:
        states = [
            RepoState(group=g, label=lbl, user=u, host=h, path=p, error='preflight failed')
            for g, lbl, u, h, p in entries
        ]
        def _pf_worker(idx, entry):
            g, lbl, u, h, p = entry
            states[idx] = preflight_one(g, lbl, u, h, p)
        with ThreadPoolExecutor(max_workers=min(len(entries), 32)) as pool:
            futures = [pool.submit(_pf_worker, i, e) for i, e in enumerate(entries)]
            for f in futures:
                try:
                    f.result()
                except Exception as exc:
                    emit(f"  preflight error: {exc}")
        for state in states:
            emit(f"  [{_state_marker(state)}] {state.label}: {state.summary()}")

    missing          = [s for s in states if s.missing]
    preflight_errors = [s for s in states if s.error and not s.missing]
    diverged         = [s for s in states if s.diverged]
    actionable       = [s for s in states if not s.error]

    clone_failed = []
    if missing:
        emit("Cloning missing paths...")
        for state in missing:
            remote = config[group].get("remote")
            if not remote:
                emit(f"  warning: {state.label}: path missing and no 'remote' configured")
                clone_failed.append(state)
                continue
            parent = str(Path(state.path).parent)
            clone_script = (
                f"mkdir -p {shlex.quote(parent)} && "
                f"git clone {shlex.quote(remote)} {shlex.quote(state.path)}"
            )
            emit(f"  ==> {state.label}")
            result = run_command(state.user, state.host, clone_script)
            if result.returncode != 0:
                clone_failed.append(state)

    if diverged:
        emit(f"Warning: {len(diverged)} repo(s) are diverged — rebase conflicts possible.")
    if preflight_errors:
        emit(f"Skipping {len(preflight_errors)} repo(s) with preflight errors.")
    if not actionable:
        emit("Nothing to sync.")
        all_failed = clone_failed + preflight_errors
        if all_failed:
            emit(f"{len(all_failed)} repo(s) need attention:")
            for s in all_failed:
                emit(f"  {s.label}: {s.error or 'clone or sync failed'}")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    quoted_msg = shlex.quote(f"sync: {timestamp}")
    phase1_script = "; ".join([
        "set -e",
        "git add -A",
        f"git diff --cached --quiet || git commit -m {quoted_msg}",
        "git pull --rebase",
        "git push",
    ])
    # vcsh/bare-worktree: skip auto-commit; worktree is outside the repo path
    # so git add -A would sweep the entire home directory.
    gitdir_script = "; ".join([
        "set -e",
        "git pull --rebase",
        "git push",
    ])

    normal_actionable = [s for s in actionable if not s.git_dir_is_cwd]
    gitdir_actionable = [s for s in actionable if s.git_dir_is_cwd]

    phase1_failed = []

    if normal_actionable:
        emit("Phase 1: commit and push...")
        for state in normal_actionable:
            emit(f"  ==> {state.label}")
            result = run_in_repo(state.user, state.host, state.path, phase1_script)
            if result.returncode != 0:
                emit("      failed — aborting rebase if in progress")
                run_in_repo(state.user, state.host, state.path,
                            "git rebase --abort 2>/dev/null || true", capture=True)
                phase1_failed.append(state)

    if gitdir_actionable:
        emit("Phase 1 (vcsh/bare-worktree): pull and push (no auto-commit)...")
        for state in gitdir_actionable:
            emit(f"  ==> {state.label}")
            result = run_in_repo(state.user, state.host, state.path, gitdir_script)
            if result.returncode != 0:
                emit("      failed — aborting rebase if in progress")
                run_in_repo(state.user, state.host, state.path,
                            "git rebase --abort 2>/dev/null || true", capture=True)
                phase1_failed.append(state)

    phase1_ok = [s for s in actionable if s not in phase1_failed]
    if phase1_ok:
        emit("Phase 2: final pull...")
        for state in phase1_ok:
            emit(f"  ==> {state.label}")
            run_in_repo(state.user, state.host, state.path, "git pull --rebase")

    emit("Done.")
    all_failed = clone_failed + preflight_errors + phase1_failed
    if all_failed:
        emit(f"{len(all_failed)} repo(s) need attention:")
        for s in all_failed:
            emit(f"  {s.label}: {s.error or 'sync failed (possible conflict)'}")


@cli.command(name="sync")
@click.pass_context
def sync_cmd(ctx):
    """Synchronize all repos with their common remote (two-phase commit+push then pull).
    With multiple groups (--repo all / -a) each group syncs concurrently."""
    config = ctx.obj["config"]
    groups = ctx.obj["groups"]

    if len(groups) == 1:
        _sync_one_group(config, groups[0], click.echo)
        return

    lock = threading.Lock()

    def make_emit(group):
        def emit(msg):
            with lock:
                click.echo(f"[{group}] {msg}")
        return emit

    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        futures = {
            pool.submit(_sync_one_group, config, g, make_emit(g), interactive=False): g
            for g in groups
        }
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                click.echo(f"error in [{futures[future]}]: {exc}", err=True)


@cli.command(name="edit")
@click.pass_context
def edit_cmd(ctx):
    """Open the configuration file in $EDITOR."""
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(CONFIG_PATH)])


@cli.command(name="list")
@click.pass_context
def list_cmd(ctx):
    """List configured repo groups and their paths."""
    config = ctx.obj["config"]
    if not config:
        click.echo(f"No configuration found at {CONFIG_PATH}")
        return
    for i, (group, data) in enumerate(config.items()):
        if i:
            click.echo()
        click.secho(f"[{group}]", bold=True)
        for path in data.get("paths", []):
            click.echo(f"  {path}")


if __name__ == "__main__":
    cli()
