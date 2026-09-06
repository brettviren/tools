from tools.fix.registry import discover_actions, discover_types


def test_discover_types_finds_builtins():
    found = discover_types()
    assert "git" in found
    assert "pyproject" in found
    assert callable(found["git"])


def test_discover_actions_finds_builtins():
    found = discover_actions()
    assert "gitignore" in found
    assert callable(found["gitignore"])
