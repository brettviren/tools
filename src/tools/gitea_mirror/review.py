"""Interactive one-key-per-repo review of records."""

from collections.abc import Callable

from tools.gitea_mirror.core import set_manual
from tools.gitea_mirror.report import record_detail
from tools.gitea_mirror.state import Store

KEYS = {
    "m": ("mirror", "decide: mirror"),
    "s": ("skip", "decide: don't mirror"),
    "v": ("review", "decide: keep pending"),
    "r": (None, "forget manual decision; follow rules"),
    "o": ("open", "open on GitHub in browser"),
    "n": ("next", "next, no change"),
    "q": ("quit", "stop reviewing"),
}
HELP = "  ".join(f"[{k}]{v[1].split(':')[-1].strip().split()[0]}" for k, v in KEYS.items())


def _decide(store: Store, name: str, action: str | None) -> None:
    with store.transaction() as state:
        set_manual(state, [state.records[name]], action, "review")


def review(store: Store, names: list[str], ask: Callable[[], str],
           show: Callable[[str], None], open_url: Callable[[str], None]) -> list[str]:
    """Walk NAMES asking for a key each; return names whose decision was set."""
    decided = []
    for i, name in enumerate(names, 1):
        rec = store.load().records[name]
        show(f"\n[{i}/{len(names)}] " + record_detail(rec, []))
        while True:
            show(HELP)
            action = KEYS.get(ask(), ("help", ""))[0]
            if action == "open":
                open_url(rec.meta.get("html_url") or f"https://github.com/{name}")
                continue
            if action == "help":
                show("\n".join(f"  {k}  {v[1]}" for k, v in KEYS.items()))
                continue
            break
        if action == "quit":
            break
        if action != "next":
            _decide(store, name, action)
            decided.append(name)
            show(f"  -> {action or 'rules'}")
    return decided
