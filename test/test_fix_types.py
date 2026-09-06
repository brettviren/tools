from tools.fix.context import Context
from tools.fix.types import git, pyproject


def _ctx(path):
    return Context(path=path, user="u", host="h", fqdn="h.example.org")


def test_git_found_in_place(tmp_path):
    (tmp_path / ".git").mkdir()
    assert git.categorize(_ctx(tmp_path)) == ["git"]


def test_git_found_in_parent(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    assert git.categorize(_ctx(sub)) == ["git"]


def test_git_not_found(tmp_path):
    assert git.categorize(_ctx(tmp_path)) == []


def test_git_custom_marker(tmp_path):
    (tmp_path / ".jj").mkdir()
    assert git.categorize(_ctx(tmp_path)) == []
    assert git.categorize(_ctx(tmp_path), marker=".jj") == ["git"]


def test_pyproject_found_in_place(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert pyproject.categorize(_ctx(tmp_path)) == ["pyproject"]


def test_pyproject_found_in_parent(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    assert pyproject.categorize(_ctx(sub)) == ["pyproject"]


def test_pyproject_not_found(tmp_path):
    assert pyproject.categorize(_ctx(tmp_path)) == []


def test_pyproject_requires_file_not_dir(tmp_path):
    (tmp_path / "pyproject.toml").mkdir()
    assert pyproject.categorize(_ctx(tmp_path)) == []
