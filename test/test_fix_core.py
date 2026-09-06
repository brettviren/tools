from tools.fix.context import Context
from tools.fix.core import apply_actions, determine_types


def _ctx(path):
    return Context(path=path, user="u", host="h", fqdn="h.example.org")


def test_determine_types_detects_from_filesystem(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    types = determine_types(_ctx(tmp_path), config={})

    assert types == ["git", "pyproject"]


def test_determine_types_override_replaces_detection(tmp_path):
    (tmp_path / ".git").mkdir()

    types = determine_types(_ctx(tmp_path), config={}, override=["custom"])

    assert types == ["custom"]


def test_determine_types_add_merges(tmp_path):
    (tmp_path / ".git").mkdir()

    types = determine_types(_ctx(tmp_path), config={}, add=["extra"])

    assert types == ["extra", "git"]


def test_determine_types_override_and_add_combine(tmp_path):
    types = determine_types(_ctx(tmp_path), config={}, override=["a"], add=["b"])

    assert types == ["a", "b"]


def test_determine_types_config_supplies_categorizer_kwargs(tmp_path):
    (tmp_path / ".jj").mkdir()

    types = determine_types(_ctx(tmp_path), config={"types": {"git": {"marker": ".jj"}}})

    assert "git" in types


def test_apply_actions_runs_gitignore(tmp_path):
    apply_actions(_ctx(tmp_path), ["pyproject"], config={})

    assert (tmp_path / ".gitignore").exists()


def test_apply_actions_respects_dryrun(tmp_path):
    apply_actions(_ctx(tmp_path), ["pyproject"], config={}, dryrun=True)

    assert not (tmp_path / ".gitignore").exists()
