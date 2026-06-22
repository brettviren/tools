Read README.org for overview of this package.

# Developing 

Shell script tools are under `scripts/`.

Python tools are under `src/` and comprised of a Python module and CLI entry point with similar names.  Module names use underscores, CLI names uses dashes.

The `uv` program is used package installation and development.  Do not use `pip`.

Some basic `uv` commands:

```
uv sync                         # update package venv
uv run <tool> [tool options]    # run a <tool> in-source
uv run pytest [pytest options]  # run the test suite
uv tool install -e .            # install to user's area in editable form
```

# CLI Guidelines

Generally, but with some exceptions:

A tool CLI is composed of sub commands with top-level and command-level options, called like:

```
<tool> [top-level options] <command> [command-level options]
```

Top and command level options include `-h/--help` to print a help message for the given context and exit.  A command lacking options, or a tool lacking options and a command, should assume `-h/--help`.  These cases produce help (but again, there can be exceptions):

```
<tool>
<tool> <command>
<tool> -h
<tool> <command> -h
```

# Design Guidelines

Factor the implementation separate CLI command functions from core library
functions.

Library functions translate between user data (option string, filenames) and object representations and provide core "business logic" in terms of the object representation.

Command functions are brief compositions of library functions.  Command
functions should be brief and not contain extensive loop and branch code blocks.

Depending on the requested tool language read the corresponding file for details:

- For Bash, read docs/bash.md 
- For Python, read docs/python.md


