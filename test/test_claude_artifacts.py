"""Tests for the claude-artifacts backup tree and the skills installer."""

import json

import pytest
from click.testing import CliRunner

from tools import skills
from tools.claude_artifacts import config, index, store
from tools.claude_artifacts.__main__ import main


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate config from the real environment."""
    monkeypatch.delenv(config.ENV_DIR, raising=False)
    monkeypatch.delenv(config.ENV_ACCOUNT, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


WC = config.Org("WireCell", "uuid-wc")
FILE = {"orgs": {"WireCell": "wirecell", "Pro Org": "personal"}}


def test_resolve_without_org_uses_argument_env_dir(monkeypatch, tmp_path):
    with pytest.raises(config.ConfigError):
        config.resolve(file_data={})
    assert config.resolve("x", file_data={}).root.name == "claude-artifacts"
    monkeypatch.setenv(config.ENV_DIR, str(tmp_path / "e"))
    monkeypatch.setenv(config.ENV_ACCOUNT, "env")
    s = config.resolve(file_data={"dir": str(tmp_path / "f")})
    assert (s.account, s.root) == ("env", tmp_path / "e")
    assert config.resolve("arg", tmp_path / "a", {}).root == tmp_path / "a"
    with pytest.raises(config.ConfigError):
        config.resolve("a/b", file_data={})


def test_resolve_from_active_org():
    assert config.resolve(file_data=FILE, org=WC).account == "wirecell"
    by_uuid = {"orgs": {"uuid-wc": "wc2"}}
    assert config.resolve(file_data=by_uuid, org=WC).account == "wc2"
    assert config.resolve("wirecell", file_data=FILE, org=WC).account == "wirecell"


def test_resolve_refuses_mismatch_and_unmapped_org():
    with pytest.raises(config.ConfigError, match="contradicts"):
        config.resolve("personal", file_data=FILE, org=WC)
    with pytest.raises(config.ConfigError, match="no account name yet"):
        config.resolve(file_data={}, org=WC)
    with pytest.raises(config.ConfigError, match="no account name yet"):
        config.resolve("anything", file_data={}, org=WC)


def test_active_org_reads_claude_json(tmp_path):
    f = tmp_path / ".claude.json"
    assert config.active_org(f) is None
    f.write_text(json.dumps({"oauthAccount": {"organizationName": "WireCell",
                                              "organizationUuid": "u"}}))
    assert config.active_org(f) == config.Org("WireCell", "u")
    f.write_text("{}")
    assert config.active_org(f) is None


def test_save_maps_org_keeps_dir_and_drops_legacy_account():
    path = config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('account = "old"\ndir = "/somewhere"\n')
    config.save("wirecell", org=WC)
    config.save("personal", org=config.Org("Jane's Organization", "u2"))
    assert config.load_file() == {
        "dir": "/somewhere",
        "orgs": {"WireCell": "wirecell", "Jane's Organization": "personal"}}
    with pytest.raises(config.ConfigError):
        config.save("x", org=None)       # no .claude.json in the isolated env


def test_artifact_id_and_dirname():
    assert store.artifact_id("https://claude.ai/artifact/AbC_-1") == "AbC_-1"
    assert store.artifact_id("AbC") == "AbC"
    assert store.dirname("SNB Stream: Sim!", "X1") == "snb-stream-sim-X1"


def _add(settings, origin, aid, title, updated, src):
    e = store.Entry(id=aid, title=title, url=f"https://claude.ai/artifact/{aid}",
                    origin=origin, updated=updated, summary=f"about {title}")
    return store.add(settings, e, src)


def test_add_known_and_index(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text("<p>art</p>")
    s = config.Settings(tmp_path / "tree", "acct")
    d1 = _add(s, "yours", "A1", "Same", "2026-01-01", src)
    _add(s, "yours", "A2", "Same", "2026-02-01", src)
    _add(s, "shared", "S1", "Other </script>", "2026-03-01", src)
    assert d1.name == "same-A1" and (d1 / "index.html").read_text() == "<p>art</p>"
    assert store.known_ids(s) == {"yours": {"A1", "A2"}, "shared": {"S1"}}

    written = {p.relative_to(s.root).as_posix() for p in index.write_all(s.root)}
    assert written == {"index.html", "acct/index.html",
                       "acct/yours/index.html", "acct/shared/index.html"}
    # An artifact's own index.html is never overwritten by a listing.
    assert (d1 / "index.html").read_text() == "<p>art</p>"

    root_page = (s.root / "index.html").read_text()
    assert "acct/yours/same-A1/index.html" in root_page
    assert "</script>" not in root_page.split("const ROWS")[1].split("</script>")[0]
    origin_page = (s.origin_dir("shared") / "index.html").read_text()
    assert '"href": "other-script-S1/index.html"' in origin_page
    assert '<a href="../../index.html">all</a>' in origin_page
    assert 'href="../index.html">acct</a>' in origin_page


def test_cli_roundtrip(tmp_path):
    run = CliRunner().invoke
    root = str(tmp_path / "t")
    claude_json = config.claude_json_path()
    claude_json.parent.mkdir(parents=True)
    claude_json.write_text(json.dumps({"oauthAccount": {"organizationName": "WireCell"}}))
    assert run(main, ["where"]).exit_code != 0                    # org not mapped yet
    assert run(main, ["setup", "-a", "me"]).exit_code == 0
    out = json.loads(run(main, ["where", "-d", root]).output)
    assert (out["account"], out["org"], out["root"]) == ("me", "WireCell", root)
    assert run(main, ["where", "-a", "other"]).exit_code != 0     # contradicts org
    r = run(main, ["add", "-d", root, "-o", "yours", "-i", "Z9", "-t", "T",
                   "-u", "https://claude.ai/artifact/Z9"])
    assert r.exit_code == 0, r.output
    assert run(main, ["known", "-d", root]).output == "yours Z9\n"
    assert run(main, ["index", "-d", root]).exit_code == 0


def test_install_skills(tmp_path):
    src = tmp_path / "pkg" / "skills"
    (src / "demo").mkdir(parents=True)
    (src / "demo" / skills.SKILL_FILE).write_text("---\nname: demo\n---\n")
    (tmp_path / "pkg" / "pyproject.toml").write_text("")
    target = tmp_path / "out"
    assert skills.install_all(src, target)[0].action == "linked"
    assert (target / "demo").is_symlink()
    assert skills.install_all(src, target)[0].action == "linked"      # idempotent
    (target / "demo").unlink()
    (target / "demo").mkdir()
    assert skills.install_all(src, target)[0].action == "skipped"     # foreign: keep
    assert skills.install_all(src, target, force=True)[0].action == "linked"
    assert skills.install_all(src, target, copy=True, force=True)[0].action == "copied"
    assert not (target / "demo").is_symlink()
