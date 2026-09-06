"""Action modules.

Each module here exposes a module-level `action(context, types, dryrun=False, **kwargs)`
function.  The module's file name (minus .py) is the config-file section key
(`[actions.<name>]`).  An action must check `types` itself and return
immediately if none of the types it cares about are present.
"""
