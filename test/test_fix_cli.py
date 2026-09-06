from click.testing import CliRunner

from tools.fix.__main__ import main


def test_cli_applies_actions_for_detected_types(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    result = CliRunner().invoke(main, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".gitignore").exists()


def test_cli_dryrun_leaves_no_file(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")

    result = CliRunner().invoke(main, ["-n", "-L", "info", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".gitignore").exists()


def test_cli_no_types_detected_warns_and_exits_cleanly(tmp_path):
    result = CliRunner().invoke(main, ["-L", "warning", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".gitignore").exists()


def test_cli_types_override(tmp_path):
    result = CliRunner().invoke(main, ["-t", "pyproject", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".gitignore").exists()


def test_cli_add_types(tmp_path):
    result = CliRunner().invoke(main, ["-a", "pyproject", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".gitignore").exists()


def test_cli_invalid_log_level(tmp_path):
    result = CliRunner().invoke(main, ["-L", "bogus", str(tmp_path)])

    assert result.exit_code != 0
