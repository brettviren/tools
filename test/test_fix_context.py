from pathlib import Path

from tools.fix.context import detect_context


def test_detect_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("socket.getfqdn", lambda: "myhost.example.org")
    monkeypatch.setattr("getpass.getuser", lambda: "alice")

    context = detect_context()

    assert context.path == tmp_path.resolve()
    assert context.user == "alice"
    assert context.fqdn == "myhost.example.org"
    assert context.host == "myhost"


def test_detect_overrides(tmp_path):
    context = detect_context(path=tmp_path, user="bob", host="short", fqdn="short.example.org")

    assert context.path == tmp_path.resolve()
    assert context.user == "bob"
    assert context.host == "short"
    assert context.fqdn == "short.example.org"


def test_detect_path_expands_and_resolves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)

    context = detect_context(path="a/b")

    assert context.path == sub.resolve()
    assert isinstance(context.path, Path)
