A Bash tool should be single-file.  CLI command and library functions are all in a single script file.

A Bash tool uses [argc](https://github.com/sigoden/argc) which may be assumed to be in the user's `$PATH`.  Every argc-based script follows this layout:

1. Shebang + top-level `@describe` comment
2. Configuration variables (static, top of file)
3. Internal helper functions prefixed with `_` (not exposed as subcommands)
4. `argc` subcommand functions with annotation comments
5. Dispatch block at the bottom

## Annotations

Comments immediately above a function body declare its CLI interface.

```bash
# @cmd One-line description of the subcommand.
#   Additional detail on a second line if needed.
# @arg name!           Required positional argument
# @arg name=default    Optional positional argument with default
# @option --long -s <val>  String option (sets $argc_long)
# @flag --verbose -v       Boolean flag (sets $argc_verbose to 1)
cmd_name() { ... }
```

Parsed values are available as `$argc_<name>` inside the function body.
Use `!` for required args and `=value` for optional args with a default.

## Internal helpers vs subcommands

Keep logic in `_`-prefixed helpers that accept positional arguments.
Subcommand functions are thin wrappers that pass `argc_*` variables through.
This allows the main workflow to call helpers directly without going through argc dispatch.

```bash
_upload() {
    local file="$1"
    ...
}

# @cmd Upload a file to the server.
# @arg file!  Path to the file
upload() {
    _upload "${argc_file}"
}
```

If a subcommand function name shadows an external command (e.g. `zip`),
use `command zip` inside the helper to call the external binary.

## Root-level (global) options

Options declared before any `@cmd` function apply to the script as a whole.
They appear under `OPTIONS` in top-level help and their values are available
as `$argc_*` variables inside every subcommand function.

```bash
# @describe My tool talking to @@server_url@@.
# @option --url <url>  Override the default server URL for this invocation
server_url=https://default.example.com
```

Invocation: `tool --url https://other.com subcmd args…`

Because configuration variables are set at load time (before argc runs), root
options cannot simply reassign them at the top level. Two steps are needed:

**1. Replace pre-computed variables that embed overridable values with functions**
that read the variable at call time:

```bash
# Instead of: curl_cmd="curl ... -e $server_url"
_curl() {
    curl ... -e "$server_url" "$@"
}
```

**2. Centralize all global-override assignments in a `_setup` function** and
call it as the first line of every subcommand wrapper. `_setup` applies each
root option override and rebuilds any derived variables:

```bash
_setup() {
    server_url="${argc_url:-$server_url}"
    cache="${argc_cache:-$cache}"
    # rebuild variables derived from the above
    log="$cache/log"
    mkdir -p "$cache"
}

subcmd() {
    _setup
    _subcmd_helper "${argc_arg}"
}
```

Adding a new root option means editing only `_setup` and adding the
`# @option` annotation — subcommand wrappers need no changes.

When a root option overrides a directory that is created at startup, remove
the top-level `mkdir -p` and let `_setup` create whichever directory is
actually in use.

## Dispatch block

Place this block verbatim at the bottom of every argc script, updating the
subcommand list in the `case` pattern to match the script's actual commands:

```bash
# Subcommand with no args shows help.
case "${1:-}" in
    cmd1|cmd2|cmd3) [[ $# -eq 1 ]] && set -- "$1" --help ;;
esac

# Top-level help: pipe through sed to expand shell variables in @describe.
if [[ $# -eq 0 ]] || [[ "${1:-}" == --help ]] || [[ "${1:-}" == -h ]]; then
    eval "$(argc --argc-eval "$0" "$@")" 2>&1 | sed "s|@@VAR@@|$var|g" >&2
    exit 0
fi

eval "$(argc --argc-eval "$0" "$@")"
```

If the script has no variable substitution in `@describe`, drop the
middle block and use a single `eval` at the bottom.

## Embedding variable values in @describe

`argc` parses comments statically, so `$var` in a `@describe` comment
appears literally in help output. Use a placeholder token and expand it
via the `sed` pipe in the dispatch block:

```bash
# @describe Connect to the server at @@server_url@@.
server_url=https://example.com
...
# in dispatch block:
    eval "$(argc --argc-eval "$0" "$@")" 2>&1 | sed "s|@@server_url@@|$server_url|g" >&2
```

## Converting an existing script

1. Add `#!/usr/bin/env bash` shebang if not present.
2. Write a top-level `# @describe` comment.
3. Identify the "main workflow" function and rename it to a meaningful
   subcommand name (e.g. `push`); rename internal helpers to `_name`.
4. Add `# @cmd`, `# @arg`, `# @option`, `# @flag` annotations to each
   subcommand function derived from inspecting the function body.
5. Replace positional `$1`, `$2` references with `${argc_name}` in the
   subcommand wrappers; keep `$1`-style in the `_` helpers.
6. Append the dispatch block.
7. Verify with `script --help` and `script subcmd --help`.
