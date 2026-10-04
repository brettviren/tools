import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from tools.gitea_mirror import app as appmod
from tools.gitea_mirror import core, creds
from tools.gitea_mirror.__main__ import main
from tools.gitea_mirror.config import ConfigError, parse_config
from tools.gitea_mirror.gitea import Gitea, Inventory, is_rate_limit, normalize_url
from tools.gitea_mirror.github import GithubRepo
from tools.gitea_mirror.rest import ApiError, next_link
from tools.gitea_mirror.review import review
from tools.gitea_mirror.rules import evaluate
from tools.gitea_mirror.state import State, Store

CONFIG = {
    "gitea": {"url": "https://gitea.test/", "owner": "bvgh", "create_delay": 0},
    "defaults": {"on_unmirror": "leave"},
    "sources": [
        {"name": "me", "kind": "user", "owner": "me"},
        {"name": "org", "kind": "org", "owner": "BigOrg", "default": "skip",
         "name_template": "bigorg-{name}"},
    ],
    "rules": [
        {"name": "archived", "archived": True, "action": "skip"},
        {"name": "keep", "source": "org", "repo": ["keep*"], "action": "mirror"},
        {"name": "own", "source": "me", "fork": False, "action": "mirror"},
    ],
}


def make_cfg(tmp_path, data=CONFIG):
    return parse_config(json.loads(json.dumps(data)), tmp_path / "config.toml",
                        tmp_path / "state")


class FakeGitHub:
    def __init__(self, repos):
        self.repos = repos  # owner -> [GithubRepo]

    def login(self):
        return "me"

    def source_repos(self, kind, owner, include_private=True):
        return list(self.repos.get(owner, []))

    def repo(self, full_name):
        return GithubRepo(full_name, clone_url=f"https://github.com/{full_name}.git")


class FakeGitea:
    def __init__(self, mirrors=None, fail=(), rate_limit_after=None, others=None):
        self.mirrors = dict(mirrors or {})  # gitea name -> origin url
        self.others = dict(others or {})  # gitea name -> repo dict of non-mirrors
        self.fail = set(fail)
        self.rate_limit_after = rate_limit_after
        self.created = []

    def login(self):
        return "bv"

    def inventory(self, owner):
        inv = Inventory(names=set(self.mirrors))
        inv.mirrors = {normalize_url(u): n for n, u in self.mirrors.items()}
        return inv

    def create_mirror(self, owner, name, clone_url, **kw):
        if name in self.fail:
            raise ApiError(500, "POST", "/repos/migrate", "boom")
        if name in self.mirrors or name in self.others:
            raise ApiError(409, "POST", "/repos/migrate", "exists")
        if self.rate_limit_after is not None and len(self.created) >= self.rate_limit_after:
            raise ApiError(422, "POST", "/repos/migrate",
                           "Remote visit addressed rate limitation.")
        self.mirrors[name] = clone_url
        self.created.append((name, kw))

    def repo(self, owner, name):
        if name in self.others:
            return self.others[name]
        return {"name": name, "mirror": True, "empty": False, "original_url": self.mirrors[name]}

    def delete(self, owner, name):
        self.mirrors.pop(name, None) or self.others.pop(name)


def gh(*names, **kw):
    return [GithubRepo(n, **kw) for n in names]


def basic_github():
    return FakeGitHub({
        "me": gh("me/tool", "me/old") + gh("me/prfork", fork=True),
        "BigOrg": gh("BigOrg/keepme", "BigOrg/junk"),
    })


# ── config / rules / urls ────────────────────────────────────────────────────

def test_config_parses(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.gitea_url == "https://gitea.test"
    assert [r.name for r in cfg.rules] == ["archived", "keep", "own"]
    assert cfg.source("org").default == "skip"


@pytest.mark.parametrize("mutate", [
    lambda d: d["rules"].append({"name": "x", "action": "maybe"}),
    lambda d: d["rules"].append({"name": "x", "colour": "red", "action": "skip"}),
    lambda d: d["rules"].append({"name": "x", "source": "nope", "action": "skip"}),
    lambda d: d["gitea"].pop("owner"),
    lambda d: d["sources"].append({"name": "me", "kind": "user", "owner": "x"}),
])
def test_config_rejects(tmp_path, mutate):
    data = json.loads(json.dumps(CONFIG))
    mutate(data)
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, data)


def test_rules_first_match_and_default(tmp_path):
    cfg = make_cfg(tmp_path)
    assert evaluate(cfg.rules, GithubRepo("BigOrg/keepX"), "org", "skip")["name"] == "keep"
    assert evaluate(cfg.rules, GithubRepo("BigOrg/keepX", archived=True), "org", "skip") == \
        {"name": "archived", "action": "skip"}
    assert evaluate(cfg.rules, GithubRepo("me/f", fork=True), "me", "review") == \
        {"name": "(default)", "action": "review"}


def test_rules_topic_language_description(tmp_path):
    data = json.loads(json.dumps(CONFIG))
    data["rules"] = [{"name": "t", "topic": "mirror-me", "language": ["python", "c++"],
                      "description": "^tool", "action": "mirror"}]
    cfg = make_cfg(tmp_path, data)
    hit = GithubRepo("a/b", topics=["Mirror-Me"], language="Python", description="Tool for x")
    assert evaluate(cfg.rules, hit, "me", "review")["action"] == "mirror"
    hit.language = "Go"
    assert evaluate(cfg.rules, hit, "me", "review")["action"] == "review"


@pytest.mark.parametrize("url", [
    "https://github.com/Me/Tool.git", "https://user:tok@github.com/me/tool/",
    "git@github.com:me/tool.git", "http://github.com/me/tool",
])
def test_normalize_url(url):
    assert normalize_url(url) == "https://github.com/me/tool"


def test_next_link():
    h = '<https://x/2>; rel="next", <https://x/9>; rel="last"'
    assert next_link(h) == "https://x/2"
    assert next_link('<https://x/1>; rel="prev"') is None


# ── scan / plan / apply ──────────────────────────────────────────────────────

def test_scan_new_adopt_untracked(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea({"prfork": "https://github.com/me/prfork", "x": "https://github.com/z/x"})
    state = State()
    rep = core.scan(cfg, state, basic_github(), gitea)

    assert sorted(rep.new) == ["BigOrg/junk", "BigOrg/keepme", "me/old", "me/prfork", "me/tool"]
    assert rep.adopted == ["me/prfork"]
    assert rep.untracked == ["x"]
    fork = state.records["me/prfork"]
    # rules say review, but an existing mirror is pinned as a manual mirror
    assert fork.rule["action"] == "review"
    assert fork.manual["action"] == "mirror"
    assert fork.gitea["by"] == "adopted"
    assert state.records["BigOrg/junk"].decision == "skip"


def test_plan_and_apply_create(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    steps = core.plan(cfg, state)
    assert [(s.kind, s.repo, s.gitea_name) for s in steps] == [
        ("create", "BigOrg/keepme", "bigorg-keepme"),
        ("create", "me/old", "old"),
        ("create", "me/tool", "tool"),
    ]
    core.apply(cfg, state, steps, gitea, lambda: "tok")
    assert set(gitea.mirrors) == {"bigorg-keepme", "old", "tool"}
    assert state.records["me/tool"].gitea["by"] == "auto"
    assert core.plan(cfg, state) == []


def test_apply_dryrun_and_failure(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea(fail={"old"})
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok", dryrun=True)
    assert gitea.created == []
    steps = core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    failed = [s for s in steps if s.ok is False]
    assert [s.repo for s in failed] == ["me/old"]
    assert state.records["me/old"].error
    assert state.select(category="failed")[0].full_name == "me/old"


def test_private_mirror_passes_token(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    core.scan(cfg, state, FakeGitHub({"me": gh("me/secret", private=True)}), gitea)
    core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    (_name, kw), = gitea.created
    assert kw["private"] is True
    assert kw["service"] == "github"
    assert kw["auth_token"] == "tok"


def test_public_mirror_uses_plain_git_without_token(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    core.scan(cfg, state, FakeGitHub({"me": gh("me/pub")}), gitea)
    core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    (_name, kw), = gitea.created
    assert kw["service"] == "git"
    assert kw["auth_token"] is None


def test_rate_limit_halts_run(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea(rate_limit_after=1)
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    steps = core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    assert [s.ok for s in steps] == [True, False, None]
    assert steps[2].error.startswith("not attempted: remote rate limit")
    assert state.records["BigOrg/keepme"].gitea  # earlier success is kept


def test_max_creates_and_delay(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    naps = []
    steps = core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok",
                       delay=5, max_creates=2, sleep=naps.append)
    assert [s.ok for s in steps] == [True, True, None]
    assert naps == [5]
    assert len(core.plan(cfg, state)) == 1


def test_interrupt_keeps_done_steps(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    core.scan(cfg, state, basic_github(), gitea)

    def nap(_seconds):
        raise KeyboardInterrupt
    steps = core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok",
                       delay=1, sleep=nap)
    # the interrupt lands in the sleep before step 2; step 1 is done and recorded
    assert steps[0].ok is True
    assert steps[1].ok is None and steps[1].error == "not attempted: interrupted"
    assert state.records[steps[0].repo].gitea


def test_conflict_adopts_own_mirror(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    gitea.mirrors["tool"] = "https://github.com/me/tool"  # appeared after the scan
    steps = core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    assert all(s.ok for s in steps)
    assert state.records["me/tool"].gitea["by"] == "adopted"


def test_conflict_replaces_failed_migration_stub(tmp_path):
    cfg = make_cfg(tmp_path)
    stub = {"name": "tool", "mirror": False, "empty": True,
            "original_url": "https://github.com/me/tool.git"}
    gitea = FakeGitea(others={"tool": stub})
    state = State()
    rep = core.scan(cfg, state, basic_github(), gitea)
    assert rep.stubs == []  # FakeGitea.inventory ignores others; see test_inventory_stubs
    steps = core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    assert all(s.ok for s in steps)
    assert "tool" in gitea.mirrors and "tool" not in gitea.others


def test_conflict_with_foreign_repo_fails(tmp_path):
    cfg = make_cfg(tmp_path)
    other = {"name": "tool", "mirror": False, "empty": False, "original_url": ""}
    gitea = FakeGitea(others={"tool": other})
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    steps = core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    bad = next(s for s in steps if s.repo == "me/tool")
    assert bad.ok is False and "regular repo, not a pull mirror" in bad.error
    assert "tool" in gitea.others


def test_inventory_stubs(monkeypatch):
    g = Gitea("https://gitea.test", {})
    monkeypatch.setattr(g, "owner_repos", lambda owner: [
        {"name": "a", "mirror": True, "empty": False, "original_url": "https://github.com/x/a"},
        {"name": "b", "mirror": False, "empty": True, "original_url": "https://github.com/x/b"},
        {"name": "c", "mirror": False, "empty": True, "original_url": ""},
    ])
    inv = g.inventory("bvgh")
    assert inv.mirrors == {"https://github.com/x/a": "a"}
    assert inv.stubs == {"https://github.com/x/b": "b"}
    assert is_rate_limit(ApiError(422, "POST", "u", "Remote visit addressed rate limitation."))


def test_skip_leaves_then_prunes_and_reverses(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    github = basic_github()
    core.scan(cfg, state, github, gitea)
    core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")

    rec = state.records["me/tool"]
    core.set_manual(state, [rec], "skip")
    assert [s.kind for s in core.plan(cfg, state)] == ["leave"]
    assert rec.in_category("left")

    core.set_on_unmirror(state, [rec], "prune")
    core.apply(cfg, state, core.plan(cfg, state), gitea, lambda: "tok")
    assert "tool" not in gitea.mirrors
    assert rec.gitea is None

    core.set_manual(state, [rec], None)  # back to rules -> mirror again
    assert [s.kind for s in core.plan(cfg, state)] == ["create"]


def test_rule_change_flips_and_gone(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea()
    state = State()
    github = basic_github()
    core.scan(cfg, state, github, gitea)

    data = json.loads(json.dumps(CONFIG))
    data["rules"].insert(0, {"name": "no-old", "repo": "old", "action": "skip"})
    github.repos["BigOrg"] = gh("BigOrg/keepme")
    rep = core.scan(make_cfg(tmp_path, data), state, github, gitea)

    assert rep.flips == [{"repo": "me/old", "from": "mirror", "to": "skip", "rule": "no-old"}]
    assert rep.gone == ["BigOrg/junk"]
    assert state.records["BigOrg/junk"].in_category("gone")


def test_lost_mirror(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea({"tool": "https://github.com/me/tool"})
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    gitea.mirrors.clear()
    rep = core.scan(cfg, state, basic_github(), gitea)
    assert rep.lost == ["me/tool"]
    assert state.records["me/tool"].in_category("missing")


def test_add_manual_outside_sources_not_gone(tmp_path):
    cfg = make_cfg(tmp_path)
    state = State()
    core.add_manual(state, GithubRepo("other/thing"), "want it")
    core.scan(cfg, state, basic_github(), FakeGitea())
    rec = state.records["other/thing"]
    assert not rec.gone
    assert rec.decision == "mirror"


# ── store ────────────────────────────────────────────────────────────────────

def test_store_roundtrip_and_history(tmp_path):
    store = Store(tmp_path / "s")
    with store.transaction() as state:
        core.add_manual(state, GithubRepo("a/b"))
    with pytest.raises(RuntimeError), store.transaction() as state:
        core.set_manual(state, [state.records["a/b"]], "skip")
        raise RuntimeError
    assert store.load().records["a/b"].decision == "mirror"  # aborted txn not saved
    assert [e["event"] for e in store.history(["A/*"])] == ["new", "manual"]


# ── credentials ──────────────────────────────────────────────────────────────

def test_token_command_and_store_file(tmp_path):
    assert creds.resolve_token({"token_command": "echo sekret"}, tmp_path / "x") == "sekret"
    assert creds.resolve_token({}, tmp_path / "none") is None
    where = creds.store_token({}, "abc", tmp_path / "t" / "gh.token")
    path = Path(where)
    assert creds.resolve_token({}, path) == "abc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    hdr = creds.auth_headers({"user": "u", "password_command": "echo pw"}, path)
    assert hdr["Authorization"].startswith("Basic ")


def test_store_command_gets_stdin(tmp_path):
    out = tmp_path / "out"
    spec = {"store_command": f"tee {out}"}
    creds.store_token(spec, "tok", tmp_path / "unused")
    assert out.read_text() == "tok\n"


def test_failing_secret_command():
    with pytest.raises(creds.CredentialError):
        creds.resolve_token({"token_command": "false"}, Path("/nonexistent"))


def test_github_token_page_prefills_scopes():
    url, _ = creds.token_page("github", "https://g", {})
    assert "scopes=repo,read:org" in url


# ── review ───────────────────────────────────────────────────────────────────

def test_review_loop(tmp_path):
    store = Store(tmp_path / "s")
    with store.transaction() as state:
        for n in ("a/one", "a/two", "a/three"):
            core.add_manual(state, GithubRepo(n))
    keys = iter(["?", "o", "s", "n", "q"])
    opened = []
    decided = review(store, ["a/one", "a/two", "a/three"], lambda: next(keys),
                     lambda _text: None, opened.append)
    assert decided == ["a/one"]
    assert opened == ["https://github.com/a/one"]
    assert store.load().records["a/one"].decision == "skip"


# ── CLI ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        '[gitea]\nurl = "https://gitea.test"\nowner = "bvgh"\ncreate_delay = 0\n'
        f'[defaults]\nstate_dir = "{tmp_path / "state"}"\n'
        '[[sources]]\nname = "me"\nkind = "user"\nowner = "me"\n'
        '[[rules]]\nname = "own"\nfork = false\naction = "mirror"\n'
    )
    github, gitea = basic_github(), FakeGitea()
    monkeypatch.setattr(appmod.App, "github", github, raising=False)
    monkeypatch.setattr(appmod.App, "gitea", gitea, raising=False)
    monkeypatch.setattr(appmod.App, "mirror_token", lambda self: "tok")

    def run(*args):
        return CliRunner().invoke(main, ["-c", str(cfgfile), *args])
    run.gitea = gitea
    run.cfgfile = str(cfgfile)
    return run


def test_cli_sync_list_set(cli):
    r = cli("sync")
    assert r.exit_code == 0, r.output
    assert "create me/tool" in r.output.replace("  ", " ").replace("  ", " ")
    assert set(cli.gitea.mirrors) == {"tool", "old"}

    r = cli("sync", "-q")
    assert r.exit_code == 0 and r.output == ""

    r = cli("list", "pending")
    assert "me/prfork" in r.output

    r = cli("set", "me/prfork", "--mirror", "--apply")
    assert r.exit_code == 0, r.output
    assert "prfork" in cli.gitea.mirrors

    r = cli("set", "me/tool", "--skip", "--prune", "--apply")
    assert r.exit_code == 0, r.output
    assert "tool" not in cli.gitea.mirrors

    r = cli("show", "me/tool")
    assert "manual" in r.output and "prune" in r.output

    r = cli("status")
    assert "mirrored" in r.output


def test_cli_no_match_is_clean_error(cli):
    r = cli("set", "nope/*", "--skip")
    assert r.exit_code == 1
    assert "no records match" in r.output


def test_cli_help_when_no_args(cli):
    for args in ([], ["set"], ["list"], ["show"], ["auth", "login"]):
        r = CliRunner().invoke(main, args)
        assert "Usage:" in r.output, args


def test_cli_add(cli):
    r = cli("add", "other/thing")
    assert r.exit_code == 0, r.output
    assert "thing" in cli.gitea.mirrors


@pytest.mark.parametrize(("existing", "says"), [
    ({"mirror": False, "empty": False, "original_url": "https://github.com/me/tool"},
     "non-empty regular repo"),
    ({"mirror": True, "original_url": "https://github.com/else/tool"}, "origin is"),
    ({"mirror": True, "original_url": ""}, "no recorded origin"),
])
def test_conflict_reason(existing, says):
    assert says in core.conflict_reason(existing, "me/tool")


def test_bare_name_pattern_matches_repo_part():
    state = State()
    core.add_manual(state, GithubRepo("me/gegede"))
    core.add_manual(state, GithubRepo("org/gegede-x"))
    assert [r.full_name for r in state.select(["gegede"])] == ["me/gegede"]
    assert len(state.select(["gegede*"])) == 2
    assert state.select(["org/*"])[0].full_name == "org/gegede-x"


def originless_setup(tmp_path):
    cfg = make_cfg(tmp_path)
    gitea = FakeGitea(others={
        "tool": {"name": "tool", "mirror": True, "empty": False, "original_url": ""},
        "old": {"name": "old", "mirror": False, "empty": False, "original_url": ""},
    })
    gitea.inventory = lambda owner: Inventory(names={"tool", "old"}, originless={"tool"})
    state = State()
    core.scan(cfg, state, basic_github(), gitea)
    return cfg, gitea, state


def test_originless_mirror_listed_untracked_then_adopted(tmp_path):
    cfg, gitea, state = originless_setup(tmp_path)
    assert state.untracked == ["tool"]
    rec, old = state.records["me/tool"], state.records["me/old"]
    rec.error = "stale"

    msgs = core.adopt_existing(cfg, state, [rec, old], gitea)
    assert msgs[0] == "me/tool: adopted tool"
    assert "not adopted" in msgs[1] and "regular repo" in msgs[1]
    assert rec.gitea["by"] == "adopted-existing" and rec.error is None

    rep = core.scan(cfg, state, basic_github(), gitea)  # not "lost" on rescan
    assert rep.lost == [] and state.untracked == []
    assert state.records["me/tool"].gitea


def test_replace_existing(tmp_path):
    cfg, gitea, state = originless_setup(tmp_path)
    old = state.records["me/old"]
    core.set_manual(state, [old], "skip")
    steps = core.replace_steps(cfg, state, [old])
    core.apply(cfg, state, steps, gitea, lambda: "tok")
    assert steps[0].ok is True
    assert old.decision == "mirror" and old.gitea["name"] == "old"
    assert "old" not in gitea.others and "old" in gitea.mirrors


def test_set_manual_clears_error():
    state = State()
    rec = core.add_manual(state, GithubRepo("a/b"))
    rec.error = "HTTP 409"
    core.set_manual(state, [rec], "skip")
    assert not rec.in_category("failed")


def test_cli_replace_asks_first(cli):
    cli("sync")
    cli.gitea.others["prfork"] = {"name": "prfork", "mirror": False, "empty": False,
                                  "original_url": ""}
    r = CliRunner().invoke(main, ["-c", cli.cfgfile, "set", "prfork", "--replace-existing"],
                           input="n\n")
    assert r.exit_code == 1 and "regular repo" in r.output
    assert "prfork" in cli.gitea.others
    r = cli("set", "prfork", "--replace-existing", "-y")
    assert r.exit_code == 0, r.output
    assert "prfork" in cli.gitea.mirrors


def test_cli_adopt_with_skip_is_error(cli):
    cli("sync")
    r = cli("set", "tool", "--skip", "--adopt-existing")
    assert r.exit_code == 1 and "contradicts" in r.output
