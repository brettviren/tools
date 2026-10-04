"""Scan GitHub and Gitea into state, plan reconciliation, and apply it."""

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from tools.gitea_mirror import rules
from tools.gitea_mirror.config import ACTIONS, UNMIRROR, Config
from tools.gitea_mirror.gitea import Inventory, is_rate_limit, is_stub, normalize_url
from tools.gitea_mirror.github import GithubRepo
from tools.gitea_mirror.rest import ApiError
from tools.gitea_mirror.state import MANUAL_SOURCE, Record, State, now

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    """What changed during a scan."""

    new: list[str] = field(default_factory=list)
    flips: list[dict] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    stubs: list[str] = field(default_factory=list)


@dataclass
class Step:
    """One reconciliation action."""

    kind: str  # create | replace | prune | leave
    repo: str
    gitea_name: str
    ok: bool | None = None
    error: str | None = None


# ── scan ─────────────────────────────────────────────────────────────────────

def discover(cfg: Config, github) -> list[tuple[str, GithubRepo]]:
    """List (source name, repo) over all sources; the first source wins a repo."""
    seen: dict[str, tuple[str, GithubRepo]] = {}
    for src in cfg.sources:
        for repo in github.source_repos(src.kind, src.owner, src.include_private):
            seen.setdefault(repo.full_name, (src.name, repo))
    return list(seen.values())


def merge_discovered(state: State, found: list[tuple[str, GithubRepo]],
                     report: ScanReport) -> None:
    """Create/refresh records for FOUND repos and mark vanished ones gone."""
    stamp = now()
    names = set()
    for src, repo in found:
        names.add(repo.full_name)
        rec = state.records.get(repo.full_name)
        if rec is None:
            rec = state.records[repo.full_name] = Record(repo.full_name, src, first_seen=stamp)
            report.new.append(repo.full_name)
            state.event("new", repo.full_name, source=src)
        elif rec.gone:
            rec.gone = False
            report.returned.append(repo.full_name)
            state.event("returned", repo.full_name)
        rec.source, rec.meta, rec.last_seen = src, repo.to_meta(), stamp
    for rec in state.records.values():
        if rec.source != MANUAL_SOURCE and rec.full_name not in names and not rec.gone:
            rec.gone = True
            report.gone.append(rec.full_name)
            state.event("gone", rec.full_name)


def evaluate_rules(cfg: Config, state: State, report: ScanReport) -> None:
    """Re-evaluate rules on every record; record decision flips."""
    for rec in state.records.values():
        src = cfg.source(rec.source)
        new = rules.evaluate(cfg.rules, rec.repo, rec.source, src.default if src else "review")
        if new == rec.rule:
            continue
        before, had_rule = rec.decision, rec.rule is not None
        old_rule = rec.rule
        rec.rule = new
        state.event("rule", rec.full_name, old=old_rule, new=new)
        if had_rule and rec.decision != before:
            report.flips.append({"repo": rec.full_name, "from": before, "to": rec.decision,
                                 "rule": new["name"]})


def github_url(full_name: str) -> str:
    """Canonical GitHub URL of FULL_NAME."""
    return f"https://github.com/{full_name}"


def reconcile_inventory(state: State, inv: Inventory, report: ScanReport) -> None:
    """Match Gitea mirrors to records by origin; adopt, notice losses, list untracked."""
    by_url = {normalize_url(github_url(k)): r for k, r in state.records.items()}
    matched, untracked = set(), []
    for url, gname in sorted(inv.mirrors.items()):
        rec = by_url.get(url)
        if rec is None:
            untracked.append(gname)
            continue
        matched.add(rec.full_name)
        if rec.gitea is None:
            adopt(state, rec, gname)
            report.adopted.append(rec.full_name)
        rec.gitea["name"] = gname
    originless = set(inv.originless)
    for rec in state.records.values():
        if rec.gitea and rec.gitea["name"] in originless and rec.full_name not in matched:
            matched.add(rec.full_name)  # adopted by hand with 'set --adopt-existing'
            originless.discard(rec.gitea["name"])
    untracked = sorted(untracked + list(originless))
    for rec in state.records.values():
        if rec.gitea and rec.full_name not in matched:
            state.event("lost", rec.full_name, gitea=rec.gitea["name"])
            report.lost.append(rec.full_name)
            rec.gitea = None
    state.untracked = untracked
    report.untracked = untracked
    report.stubs = sorted(inv.stubs.values())


def adopt(state: State, rec: Record, gname: str) -> None:
    """Record a pre-existing Gitea mirror; pin it as a manual mirror if rules disagree."""
    rec.gitea, rec.error = {"name": gname, "at": now(), "by": "adopted"}, None
    state.event("adopted", rec.full_name, gitea=gname)
    if rec.decision != "mirror":
        set_manual(state, [rec], "mirror", "adopted existing mirror")


def scan(cfg: Config, state: State, github, gitea) -> ScanReport:
    """Discover GitHub repos, evaluate rules, and match existing Gitea mirrors."""
    report = ScanReport()
    merge_discovered(state, discover(cfg, github), report)
    evaluate_rules(cfg, state, report)
    reconcile_inventory(state, gitea.inventory(cfg.gitea_owner), report)
    state.last_scan = now()
    return report


# ── plan / apply ─────────────────────────────────────────────────────────────

def gitea_name(cfg: Config, rec: Record) -> str:
    """Target Gitea repo name for REC."""
    if rec.gitea:
        return rec.gitea["name"]
    if rec.gitea_name:
        return rec.gitea_name
    src = cfg.source(rec.source)
    template = (src.name_template if src else None) or cfg.name_template
    owner, name = rec.full_name.split("/", 1)
    return template.format(name=name, owner=owner, source=rec.source)


def plan(cfg: Config, state: State, records: list[Record] | None = None) -> list[Step]:
    """Steps that bring Gitea in line with decisions (gone repos are left alone)."""
    steps = []
    for rec in records if records is not None else state.records.values():
        if rec.gone:
            continue
        if rec.decision == "mirror" and rec.gitea is None:
            steps.append(Step("create", rec.full_name, gitea_name(cfg, rec)))
        elif rec.decision == "skip" and rec.gitea:
            policy = rec.on_unmirror or cfg.on_unmirror
            steps.append(Step("prune" if policy == "prune" else "leave",
                              rec.full_name, rec.gitea["name"]))
    return sorted(steps, key=lambda s: s.repo.lower())


def _private(cfg: Config, repo: GithubRepo) -> bool:
    if cfg.visibility == "same":
        return repo.private
    return cfg.visibility == "private"


def _service(cfg: Config, repo: GithubRepo) -> str:
    return "github" if repo.private else cfg.service


def _migrate(cfg: Config, rec: Record, step: Step, gitea, mirror_token: Callable) -> None:
    repo = rec.repo
    service = _service(cfg, repo)
    gitea.create_mirror(
        cfg.gitea_owner, step.gitea_name, repo.clone_url or github_url(rec.full_name) + ".git",
        private=_private(cfg, repo), description=repo.description,
        interval=cfg.mirror_interval, wiki=cfg.wiki, lfs=cfg.lfs, service=service,
        auth_token=mirror_token() if service == "github" else None,
    )


def conflict_reason(existing: dict, full_name: str) -> str:
    """Say why EXISTING Gitea repo is not taken as the mirror of FULL_NAME."""
    origin = existing.get("original_url") or ""
    if origin and normalize_url(origin) != normalize_url(github_url(full_name)):
        return f"its origin is {origin}"
    if not existing.get("mirror"):
        kind = "an empty" if existing.get("empty") else "a non-empty"
        return f"is {kind} regular repo, not a pull mirror (origin: {origin or 'none'})"
    return "is a pull mirror with no recorded origin"


def _resolve_conflict(cfg: Config, state: State, rec: Record, step: Step, gitea,
                      mirror_token: Callable) -> str:
    """Handle "name exists": adopt our own mirror, or clear a failed-migration stub and retry."""
    existing = gitea.repo(cfg.gitea_owner, step.gitea_name)
    ours = normalize_url(existing.get("original_url") or "") == normalize_url(
        github_url(rec.full_name))
    if ours and existing.get("mirror"):
        state.event("adopted", rec.full_name, gitea=step.gitea_name)
        return "adopted"
    if ours and is_stub(existing):
        log.warning("%s: deleting empty stub of a failed migration and retrying", step.gitea_name)
        gitea.delete(cfg.gitea_owner, step.gitea_name)
        state.event("stub_deleted", rec.full_name, gitea=step.gitea_name)
        _migrate(cfg, rec, step, gitea, mirror_token)
        return "manual" if rec.manual else "auto"
    raise ApiError(409, "POST", "/repos/migrate",
                   f"{step.gitea_name} exists but {conflict_reason(existing, rec.full_name)}; "
                   f"see 'gitea-mirror set {rec.full_name} --adopt-existing | "
                   "--replace-existing | --gitea-name NAME'")


def _create(cfg: Config, state: State, rec: Record, step: Step, gitea,
            mirror_token: Callable) -> None:
    try:
        _migrate(cfg, rec, step, gitea, mirror_token)
        how = "manual" if rec.manual else "auto"
    except ApiError as err:
        if err.status != 409:
            raise
        how = _resolve_conflict(cfg, state, rec, step, gitea, mirror_token)
    rec.gitea = {"name": step.gitea_name, "at": now(), "by": how}


def _run_step(cfg: Config, state: State, step: Step, gitea,
              mirror_token: Callable) -> Exception | None:
    """Run one step, recording the outcome; return the error, if any."""
    rec = state.records[step.repo]
    try:
        if step.kind == "replace":
            _delete_if_exists(cfg, gitea, step.gitea_name)
            rec.gitea = None
        if step.kind in ("create", "replace"):
            _create(cfg, state, rec, step, gitea, mirror_token)
        else:
            gitea.delete(cfg.gitea_owner, step.gitea_name)
            rec.gitea = None
    except Exception as err:  # noqa: BLE001 - record any failure and carry on
        step.ok, step.error, rec.error = False, str(err), str(err)
        state.event("failed", step.repo, step=step.kind, error=str(err))
        log.error("%s %s: %s", step.kind, step.repo, err)
        return err
    step.ok, rec.error = True, None
    state.event(step.kind, step.repo, gitea=step.gitea_name)
    return None


def _delete_if_exists(cfg: Config, gitea, name: str) -> None:
    try:
        gitea.delete(cfg.gitea_owner, name)
    except ApiError as err:
        if err.status != 404:
            raise


def _halt(steps: list[Step], reason: str) -> None:
    for step in steps:
        step.error = f"not attempted: {reason}"
    log.warning("stopping with %d steps not attempted: %s", len(steps), reason)


def apply(cfg: Config, state: State, steps: list[Step], gitea, mirror_token: Callable,
          dryrun: bool = False, delay: float | None = None, max_creates: int | None = None,
          sleep: Callable = time.sleep) -> list[Step]:
    """Execute create/prune STEPS politely; leave steps are informational.

    Creations are spaced by DELAY seconds and capped at MAX_CREATES (0: no
    cap), both defaulting to the config.  A remote rate limit or Ctrl-C stops
    the run, keeping the results of steps already done.
    """
    if dryrun:
        return steps
    delay = cfg.create_delay if delay is None else delay
    cap = cfg.max_creates if max_creates is None else max_creates
    todo = [s for s in steps if s.kind != "leave"]
    created = 0
    for i, step in enumerate(todo):
        if step.kind != "prune" and cap and created >= cap:
            _halt(todo[i:], f"reached max {cap} creations per run")
            break
        try:
            if step.kind != "prune":
                if created:
                    sleep(delay)
                created += 1
            err = _run_step(cfg, state, step, gitea, mirror_token)
        except KeyboardInterrupt:
            _halt(todo[i:], "interrupted")
            break
        if err is not None and is_rate_limit(err):
            _halt(todo[i + 1:], "remote rate limit; try again later")
            break
    return steps


# ── manual edits ─────────────────────────────────────────────────────────────

def set_manual(state: State, recs: list[Record], action: str | None, note: str = "") -> None:
    """Set (or with ACTION None, clear) a manual decision on RECS."""
    if action is not None and action not in ACTIONS:
        raise ValueError(f"bad action {action!r}")
    for rec in recs:
        old = rec.manual
        rec.manual = {"action": action, "at": now(), "note": note} if action else None
        rec.error = None  # a past failure no longer applies to a new decision
        state.event("manual", rec.full_name, old=old, new=rec.manual)


def set_on_unmirror(state: State, recs: list[Record], policy: str | None) -> None:
    """Set (or with None, clear) the per-repo leave/prune policy."""
    if policy is not None and policy not in UNMIRROR:
        raise ValueError(f"bad policy {policy!r}")
    for rec in recs:
        rec.on_unmirror = policy
        state.event("on_unmirror", rec.full_name, policy=policy)


def set_gitea_name(state: State, rec: Record, name: str | None) -> None:
    """Override the Gitea repo name used when REC is created."""
    rec.gitea_name = name
    state.event("gitea_name", rec.full_name, name=name)


def existing_repo(cfg: Config, rec: Record, gitea) -> dict | None:
    """The Gitea repo REC maps to, or None if there is none."""
    try:
        return gitea.repo(cfg.gitea_owner, gitea_name(cfg, rec))
    except ApiError as err:
        if err.status == 404:
            return None
        raise


def describe_existing(existing: dict | None) -> str:
    """Short description of an existing Gitea repo."""
    if existing is None:
        return "does not exist"
    kind = "pull mirror" if existing.get("mirror") else "regular repo"
    empty = ", empty" if existing.get("empty") else ""
    return f"{kind}{empty}, origin {existing.get('original_url') or 'not recorded'}"


def _ensure_mirror_decision(state: State, rec: Record, note: str) -> None:
    if rec.decision != "mirror":
        set_manual(state, [rec], "mirror", note)
    rec.error = None


def adopt_existing(cfg: Config, state: State, recs: list[Record], gitea) -> list[str]:
    """Take existing Gitea pull mirrors as the mirrors of RECS; return one message each.

    A mirror whose origin is not recorded is accepted on your word; a regular
    repo or a mirror of another origin is refused.
    """
    out = []
    for rec in recs:
        name = gitea_name(cfg, rec)
        existing = existing_repo(cfg, rec, gitea)
        origin = (existing or {}).get("original_url") or ""
        foreign = origin and normalize_url(origin) != normalize_url(github_url(rec.full_name))
        if existing is None or not existing.get("mirror") or foreign:
            out.append(f"{rec.full_name}: not adopted, {name} {describe_existing(existing)}")
            continue
        adopt(state, rec, name)
        rec.gitea["by"] = "adopted-existing"
        _ensure_mirror_decision(state, rec, "adopted existing mirror")
        out.append(f"{rec.full_name}: adopted {name}")
    return out


def replace_steps(cfg: Config, state: State, recs: list[Record]) -> list[Step]:
    """Steps deleting whatever Gitea repo RECS map to and creating fresh mirrors."""
    for rec in recs:
        _ensure_mirror_decision(state, rec, "replace existing")
    return [Step("replace", rec.full_name, gitea_name(cfg, rec)) for rec in recs]


def add_manual(state: State, repo: GithubRepo, note: str = "") -> Record:
    """Track REPO outside of any source and decide to mirror it."""
    rec = state.records.get(repo.full_name)
    if rec is None:
        stamp = now()
        rec = state.records[repo.full_name] = Record(
            repo.full_name, MANUAL_SOURCE, repo.to_meta(), stamp, stamp)
        state.event("new", repo.full_name, source=MANUAL_SOURCE)
    set_manual(state, [rec], "mirror", note)
    return rec


def sync_report(scan_report: ScanReport | None, steps: list[Step], state: State) -> dict:
    """Combine scan and apply results into one serializable report."""
    return {
        "at": now(),
        "scan": asdict(scan_report) if scan_report else None,
        "steps": [asdict(s) for s in steps],
        "pending": [r.full_name for r in state.select(category="pending")],
    }


def report_is_news(report: dict) -> bool:
    """True if a sync report carries anything worth telling a human."""
    scan_part = report["scan"] or {}
    changed = any(scan_part.get(k) for k in ("new", "flips", "gone", "returned", "adopted", "lost"))
    acted = any(s["kind"] != "leave" for s in report["steps"])
    return changed or acted
