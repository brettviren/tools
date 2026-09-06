# fix

`fix` looks at a directory (a "context"), figures out what kind(s) of thing it
is (its "types"), and applies whatever fix-up actions are relevant to those
types — e.g. making sure a Python project's `.gitignore` covers the usual
Python cruft.

## Usage

```
fix [OPTIONS] [PATH]
```

`PATH` defaults to the current directory. Basic run:

```
$ fix ~/src/myproject
```

Preview without touching anything:

```
$ fix -n ~/src/myproject
```

A successful real run is silent; `-n/--dryrun` logs what it would have done
at INFO level instead of doing it.

### Options

| Option | Meaning |
|---|---|
| `-u, --user TEXT` | Override the detected username |
| `--host TEXT` | Override the detected short hostname |
| `--fqdn TEXT` | Override the detected fully-qualified hostname |
| `-t, --types CSV` | Replace detected types entirely with this comma-separated list |
| `-a, --add-types CSV` | Add this comma-separated list to the detected/overridden types |
| `-c, --config PATH` | Config file (default: `$XDG_CONFIG_HOME/fix/config.toml`, else `~/.config/fix/config.toml`) |
| `-n, --dryrun` | Log intended changes instead of making them |
| `-l, --log-sink SINK` | `stderr` (default), `stdout`, or a file path |
| `-L, --log-level LEVEL` | `debug`, `info` (default), `warning`, `error`, `critical` |

If no type is detected (and none is given via `-t`), `fix` logs a warning
and exits without applying any action.

## Context

A context bundles the parameters categorizers and actions reason about:

- `path` — absolute directory (resolved from `PATH`, default cwd)
- `user` — username (default: `getpass.getuser()`)
- `host` — short hostname (default: derived from the FQDN)
- `fqdn` — fully-qualified hostname (default: `socket.getfqdn()`)

Any of these can be overridden on the command line; more fields may be added
in the future (`tools.fix.context.Context`).

## Types

A *type* is a short string asserted by a categorizer function. The built-ins:

- **git** (`tools/fix/types/git.py`) — asserted when `.git` is found in
  `path` or any parent directory.
- **pyproject** (`tools/fix/types/pyproject.py`) — asserted when
  `pyproject.toml` is found in `path` or any parent directory.

The full type set is the union of every categorizer's result, unless
overridden with `-t/--types`; `-a/--add-types` always adds to whatever set
results.

### Adding a categorizer

Drop a module into `src/tools/fix/types/` exposing:

```python
def categorize(context: Context, **kwargs) -> list[str]:
    ...
```

It's discovered automatically — nothing else needs to be registered. Keyword
parameters with defaults become configurable (see Configuration below); the
module's file name is both its config-section key and, by convention, the
type it asserts.

## Actions

An *action* receives the context and the full type set and decides for
itself whether to do anything:

```python
def action(context: Context, types: list[str], dryrun: bool = False, **kwargs) -> None:
    ...
```

It must return immediately if none of the types it cares about are present.
Modules are discovered the same way as categorizers, from
`src/tools/fix/actions/`.

### gitignore

Built-in action (`tools/fix/actions/gitignore.py`): assures `.gitignore`
under `context.path` contains the entry set for each applicable type (only
**pyproject** out of the box, via `GITIGNORE_SETS`).

Each type gets its own marked block:

```
# BEGIN fix:pyproject
__pycache__/
...
# END fix:pyproject
```

Re-running is idempotent, hand-edited content outside a block is left alone,
and an entry already present anywhere in the file (even one you added by
hand before ever running `fix`) is not duplicated into the block.

## Configuration

`fix` reads `~/.config/fix/config.toml` (or `-c/--config`, or
`$XDG_CONFIG_HOME/fix/config.toml`). Tables are keyed by function name:

```toml
[types.git]
marker = ".jj"          # e.g. detect a Jujutsu repo as "git" too

[actions.gitignore]
# `sets` would override the whole GITIGNORE_SETS table if supplied here
```

Only keys matching one of the target function's keyword-defaulted parameters
are used; unrecognized keys are logged as a warning and dropped.
