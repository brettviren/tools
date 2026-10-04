"""Render records, plans and reports for humans (or as JSON)."""

import json
from collections import Counter
from dataclasses import asdict

from tools.gitea_mirror.core import Step
from tools.gitea_mirror.state import CATEGORIES, Record, State


def flags(rec: Record) -> str:
    """Compact F(ork) P(rivate) A(rchived) G(one) E(rror) flags."""
    m = rec.meta
    marks = [("F", m.get("fork")), ("P", m.get("private")), ("A", m.get("archived")),
             ("G", rec.gone), ("E", rec.error)]
    return "".join(c if on else "." for c, on in marks)


def record_line(rec: Record) -> str:
    """One-line summary: decision (* = manual), on-gitea marker, flags, name, rule."""
    decision = rec.decision + ("*" if rec.manual else "")
    where = "G" if rec.gitea else "-"
    rule = rec.rule["name"] if rec.rule else ""
    return f"{decision:8} {where} {flags(rec)} {rec.full_name:45} {rule}"


def records_text(recs: list[Record]) -> str:
    """Table of records."""
    return "\n".join(record_line(r) for r in recs)


def records_json(recs: list[Record]) -> str:
    """Records as a JSON list."""
    return json.dumps([asdict(r) | {"decision": r.decision} for r in recs], indent=1)


def record_detail(rec: Record, history: list[dict]) -> str:
    """Multi-line description of one record and its history."""
    m = rec.meta
    lines = [
        f"{rec.full_name}  ({m.get('html_url', '')})",
        f"  description: {m.get('description', '')}",
        f"  source:      {rec.source}   flags: {flags(rec)}   language: {m.get('language', '')}",
        f"  topics:      {', '.join(m.get('topics', []))}",
        f"  seen:        {rec.first_seen} .. {rec.last_seen}",
        f"  rule:        {rec.rule}",
        f"  manual:      {rec.manual}",
        f"  decision:    {rec.decision}",
        f"  on_unmirror: {rec.on_unmirror or '(default)'}   gitea_name: {rec.gitea_name or '-'}",
        f"  gitea:       {rec.gitea}",
    ]
    if rec.error:
        lines.append(f"  error:       {rec.error}")
    lines += ["  history:"] + [f"    {event_line(e)}" for e in history]
    return "\n".join(lines)


def event_line(ev: dict) -> str:
    """One history event."""
    rest = {k: v for k, v in ev.items() if k not in ("at", "event", "repo")}
    extra = " ".join(f"{k}={v}" for k, v in rest.items())
    return f"{ev['at']} {ev['event']:11} {ev['repo']} {extra}".rstrip()


def status_text(state: State) -> str:
    """Counts per category plus untracked mirrors."""
    counts = Counter()
    for rec in state.records.values():
        for cat in CATEGORIES:
            counts[cat] += rec.in_category(cat)
    lines = [f"last scan: {state.last_scan or 'never'}"]
    lines += [f"{cat:9} {counts[cat]:5}" for cat in CATEGORIES]
    lines.append(f"{'untracked':9} {len(state.untracked):5}  (Gitea mirrors of unknown origin)")
    return "\n".join(lines)


def steps_text(steps: list[Step]) -> str:
    """Plan / apply results."""
    out = []
    for s in steps:
        if s.ok is None:
            result = s.error or ""
        else:
            result = "ok" if s.ok else f"FAILED: {s.error}"
        out.append(f"{s.kind:6} {s.repo:45} -> {s.gitea_name} {result}".rstrip())
    return "\n".join(out)


def sync_text(report: dict) -> str:
    """Human summary of a sync report, listing only non-empty parts."""
    out = []
    scan = report.get("scan") or {}
    for key in ("new", "returned", "gone", "adopted", "lost"):
        if scan.get(key):
            out.append(f"{key}: {', '.join(scan[key])}")
    if scan.get("stubs"):
        out.append(f"failed-migration stubs (replaced on next create): {', '.join(scan['stubs'])}")
    for f in scan.get("flips", []):
        out.append(f"flip: {f['repo']} {f['from']} -> {f['to']} (rule {f['rule']})")
    steps = [Step(**s) for s in report["steps"] if s["kind"] != "leave"]
    if steps:
        out.append(steps_text(steps))
    if report.get("pending"):
        out.append(f"pending review: {len(report['pending'])} (gitea-mirror review)")
    return "\n".join(out)
