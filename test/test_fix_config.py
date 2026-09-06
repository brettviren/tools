import logging
from pathlib import Path

from tools.fix.config import default_config_path, func_kwargs, load_config


def test_default_config_path_uses_xdg(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg/config")
    assert default_config_path() == Path("/xdg/config/fix/config.toml")


def test_default_config_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert default_config_path() == tmp_path / ".config" / "fix" / "config.toml"


def test_load_config_missing_file_returns_empty(tmp_path):
    assert load_config(tmp_path / "nope.toml") == {}


def test_load_config_parses_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[types.git]\nmarker = ".jj"\n')
    assert load_config(path) == {"types": {"git": {"marker": ".jj"}}}


def _func(context, types, dryrun=False, marker=".git", extra=1):
    pass


def test_func_kwargs_selects_known_defaulted_params():
    config = {"types": {"git": {"marker": ".jj", "extra": 2}}}
    assert func_kwargs(config, "types", "git", _func) == {"marker": ".jj", "extra": 2}


def test_func_kwargs_missing_section_returns_empty():
    assert func_kwargs({}, "types", "git", _func) == {}


def test_func_kwargs_warns_and_drops_unknown_keys(caplog):
    config = {"types": {"git": {"marker": ".jj", "bogus": True}}}
    with caplog.at_level(logging.WARNING):
        result = func_kwargs(config, "types", "git", _func)
    assert result == {"marker": ".jj"}
    assert "bogus" in caplog.text


def test_func_kwargs_warns_on_non_table(caplog):
    config = {"types": {"git": "not-a-table"}}
    with caplog.at_level(logging.WARNING):
        result = func_kwargs(config, "types", "git", _func)
    assert result == {}
    assert "not a table" in caplog.text
