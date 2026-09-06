import logging

from tools.fix.actions import gitignore
from tools.fix.context import Context


def _ctx(path):
    return Context(path=path, user="u", host="h", fqdn="h.example.org")


def test_no_applicable_types_does_nothing(tmp_path):
    gitignore.action(_ctx(tmp_path), ["git"])
    assert not (tmp_path / ".gitignore").exists()


def test_creates_gitignore_with_block(tmp_path):
    gitignore.action(_ctx(tmp_path), ["pyproject"])

    text = (tmp_path / ".gitignore").read_text()
    assert "# BEGIN fix:pyproject" in text
    assert "# END fix:pyproject" in text
    assert "__pycache__/" in text


def test_idempotent(tmp_path):
    ctx = _ctx(tmp_path)
    gitignore.action(ctx, ["pyproject"])
    first = (tmp_path / ".gitignore").read_text()

    gitignore.action(ctx, ["pyproject"])
    second = (tmp_path / ".gitignore").read_text()

    assert first == second
    assert first.count("# BEGIN fix:pyproject") == 1


def test_preserves_unrelated_content(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text("# my own notes\ncustom-ignore/\n")

    gitignore.action(_ctx(tmp_path), ["pyproject"])

    text = gitignore_path.read_text()
    assert "# my own notes" in text
    assert "custom-ignore/" in text
    assert "# BEGIN fix:pyproject" in text


def test_does_not_duplicate_preexisting_entry(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text("__pycache__/\n")

    gitignore.action(_ctx(tmp_path), ["pyproject"])

    text = gitignore_path.read_text()
    assert text.count("__pycache__/") == 1
    # It stays as the user's own line, outside the managed block.
    block_start = text.index("# BEGIN fix:pyproject")
    assert "__pycache__/" not in text[block_start:]


def test_dryrun_does_not_write(tmp_path, caplog):
    gitignore_path = tmp_path / ".gitignore"
    with caplog.at_level(logging.INFO, logger="tools.fix.actions.gitignore"):
        gitignore.action(_ctx(tmp_path), ["pyproject"], dryrun=True)

    assert not gitignore_path.exists()
    assert "would update" in caplog.text


def test_custom_sets_kwarg(tmp_path):
    gitignore.action(_ctx(tmp_path), ["custom"], sets={"custom": ["foo/"]})

    text = (tmp_path / ".gitignore").read_text()
    assert "foo/" in text
    assert "# BEGIN fix:custom" in text
