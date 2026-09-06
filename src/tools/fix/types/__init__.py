"""Context-categorizing modules.

Each module here exposes a module-level `categorize(context, **kwargs) -> list[str]`
function.  The module's file name (minus .py) is both the config-file section
key (`[types.<name>]`) and, conventionally, the name of the type(s) it asserts.
"""
