"""Match GitHub repos against ordered rules to decide an action."""

import fnmatch
import re

from tools.gitea_mirror.config import Rule
from tools.gitea_mirror.github import GithubRepo


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _glob_any(text: str, patterns) -> bool:
    return any(fnmatch.fnmatch(text.lower(), str(p).lower()) for p in _as_list(patterns))


def _check(key: str, want, repo: GithubRepo, source: str) -> bool:
    if key == "source":
        return source in _as_list(want)
    if key == "owner":
        return _glob_any(repo.owner, want)
    if key == "repo":
        return _glob_any(repo.name, want)
    if key == "regex":
        return re.search(want, repo.full_name, re.IGNORECASE) is not None
    if key == "description":
        return re.search(want, repo.description or "", re.IGNORECASE) is not None
    if key == "topic":
        return bool({t.lower() for t in repo.topics} & {str(t).lower() for t in _as_list(want)})
    if key == "language":
        return (repo.language or "").lower() in {str(x).lower() for x in _as_list(want)}
    return bool(getattr(repo, key)) == bool(want)


def matches(rule: Rule, repo: GithubRepo, source: str) -> bool:
    """True if every condition of RULE holds for REPO found via SOURCE."""
    return all(_check(k, v, repo, source) for k, v in rule.conditions.items())


def evaluate(rules: list[Rule], repo: GithubRepo, source: str, default: str) -> dict:
    """Return {"name", "action"} of the first matching rule, else the DEFAULT."""
    for rule in rules:
        if matches(rule, repo, source):
            return {"name": rule.name, "action": rule.action}
    return {"name": "(default)", "action": default}
