# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>

"""Generate the browsable ``index.html`` at each level of the artifact tree.

Levels are the root, each account directory and each origin directory: every
directory that is a proper ancestor of an artifact directory.  An artifact
directory is never given an index, because its ``index.html`` is the artifact.
Each index lists every artifact at or below its directory, links up to its
ancestors and down to its sub-levels, and embeds the data for a client-side
fuzzy search so it works from ``file://`` with no server.
"""

import html
import json
import os
from pathlib import Path

from tools.claude_artifacts.store import Entry, entries_under

INDEX = "index.html"


def _json_for_script(obj) -> str:
    """JSON safe to embed inside a <script> element."""
    s = json.dumps(obj, ensure_ascii=False)
    for bad, ok in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                    (" ", "\\u2028"), (" ", "\\u2029")):
        s = s.replace(bad, ok)
    return s


def _posix_rel(target: Path, start: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def level_dirs(root: Path, found: list[tuple[Path, Entry]]) -> list[Path]:
    """Directories needing an index: the root and ancestors of artifacts."""
    levels = {root}
    for art, _ in found:
        for parent in art.parents:
            if parent == root:
                break
            if root in parent.parents:
                levels.add(parent)
    return sorted(levels)


def _rows(level: Path, found: list[tuple[Path, Entry]]) -> list[dict]:
    """Search rows for the artifacts below *level*."""
    rows = []
    for art, e in found:
        if level not in art.parents:
            continue
        where = art.parent.relative_to(level).as_posix()
        rows.append({
            "title": e.title, "date": e.updated, "summary": e.summary,
            "href": _posix_rel(art / INDEX, level), "url": e.url,
            "where": "" if where == "." else where,
        })
    return rows


def _crumbs(root: Path, level: Path) -> list[tuple[str, str | None]]:
    """(label, href) from the root down to *level*; the last has no link."""
    chain = [root, *[p for p in reversed(level.parents) if root in p.parents], level]
    chain = list(dict.fromkeys(chain))
    out: list[tuple[str, str | None]] = []
    for p in chain:
        label = p.name if p != root else "all"
        out.append((label, None if p == level else _posix_rel(p / INDEX, level)))
    return out


def _children(level: Path, levels: list[Path], rows_for) -> list[tuple[str, str, int]]:
    """(name, href, artifact count) for each direct sub-level."""
    kids = [d for d in levels if d.parent == level]
    return [(d.name, _posix_rel(d / INDEX, level), len(rows_for(d))) for d in kids]


def render(root: Path, level: Path, levels: list[Path],
           found: list[tuple[Path, Entry]]) -> str:
    """HTML text of the index for *level*."""
    rows = _rows(level, found)
    crumbs = _crumbs(root, level)
    kids = _children(level, levels, lambda d: _rows(d, found))
    title = " / ".join(c[0] for c in crumbs)
    nav_up = "".join(
        f'<a href="{html.escape(h)}">{html.escape(n)}</a><span>/</span>'
        if h else f"<b>{html.escape(n)}</b>" for n, h in crumbs)
    nav_down = "".join(
        f'<a class="kid" href="{html.escape(h)}">{html.escape(n)}'
        f'<small>{c}</small></a>' for n, h, c in kids)
    page = _TEMPLATE
    for key, val in (("@@TITLE@@", html.escape(title)), ("@@UP@@", nav_up),
                     ("@@DOWN@@", nav_down), ("@@DATA@@", _json_for_script(rows))):
        page = page.replace(key, val)
    return page


def write_all(root: Path) -> list[Path]:
    """(Re)write every index under *root*; returns the files written."""
    found = entries_under(root)
    levels = level_dirs(root, found)
    written = []
    for level in levels:
        path = level / INDEX
        path.write_text(render(root, level, levels, found))
        written.append(path)
    return written


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude artifacts: @@TITLE@@</title>
<style>
:root{--bg:#fafaf7;--fg:#1f2328;--muted:#66707a;--rule:#dcdfe3;--card:#fff;--accent:#2f5fb3;--mark:#fde68a}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e4e6ea;--muted:#9aa3ad;--rule:#2d3238;--card:#1e2126;--accent:#8ab4f8;--mark:#5a4a10}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:56rem;margin:0 auto;padding:1.5rem 16px 4rem}
a{color:var(--accent)}
nav.up{display:flex;flex-wrap:wrap;gap:.4rem;color:var(--muted);font-size:.95rem}
nav.down{display:flex;flex-wrap:wrap;gap:.5rem;margin:1rem 0}
a.kid{border:1px solid var(--rule);background:var(--card);border-radius:6px;padding:.3rem .7rem;text-decoration:none}
a.kid small{color:var(--muted);margin-left:.5rem}
h1{font-size:1.5rem;margin:.6rem 0 0}
input{width:100%;font:inherit;padding:.6rem .8rem;margin:.8rem 0 .3rem;color:var(--fg);background:var(--card);border:1px solid var(--rule);border-radius:6px}
input:focus-visible{outline:2px solid var(--accent)}
#count{color:var(--muted);font-size:.9rem;margin-bottom:.8rem}
ul{list-style:none;margin:0;padding:0;display:grid;gap:.7rem}
li{background:var(--card);border:1px solid var(--rule);border-radius:6px;padding:.7rem .9rem}
li .t{font-weight:600;font-size:1.05rem}
li .m{color:var(--muted);font-size:.85rem;display:flex;flex-wrap:wrap;gap:.2rem 1rem}
li .s{margin-top:.3rem}
@media (max-width:30rem){main{padding-top:1rem}}
</style></head><body><main>
<nav class="up" aria-label="Up the tree">@@UP@@</nav>
<h1>Claude artifacts</h1>
<nav class="down" aria-label="Down the tree">@@DOWN@@</nav>
<input id="q" type="search" placeholder="Fuzzy search title, summary, location  ( / to focus )" autocomplete="off" autofocus>
<div id="count" aria-live="polite"></div>
<ul id="list"></ul>
<script>
const ROWS = @@DATA@@;
const q = document.getElementById("q"), list = document.getElementById("list"),
      count = document.getElementById("count");

// Subsequence match: every query char must appear in order. Consecutive runs
// and word-start hits score higher, gaps lower. Returns -1 if no match.
function fuzzy(needle, hay) {
  let score = 0, pos = 0, run = 0;
  for (const ch of needle) {
    const i = hay.indexOf(ch, pos);
    if (i < 0) return -1;
    run = (i === pos && pos > 0) ? run + 1 : 0;
    const wordStart = i === 0 || /[^a-z0-9]/.test(hay[i - 1]);
    score += 1 + run * 2 + (wordStart ? 3 : 0) - Math.min(i - pos, 10) * 0.1;
    pos = i + 1;
  }
  return score;
}
for (const r of ROWS)
  r.hay = [r.title, r.summary, r.where].join(" \\n ").toLowerCase();

function match(r, terms) {
  let total = 0;
  for (const term of terms) {
    const a = fuzzy(term, r.title.toLowerCase()), b = fuzzy(term, r.hay);
    if (b < 0) return -1;
    total += Math.max(a < 0 ? 0 : a * 2, b);   // title hits count double
  }
  return total;
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}
function render() {
  const terms = q.value.toLowerCase().split(/\\s+/).filter(Boolean);
  let hits = ROWS.map(r => ({r, s: terms.length ? match(r, terms) : 0}))
                 .filter(h => h.s >= 0);
  if (terms.length) hits.sort((a, b) => b.s - a.s);   // else keep newest-first
  list.replaceChildren(...hits.map(({r}) => {
    const li = el("li"), a = el("a", "t", r.title || "(untitled)");
    a.href = r.href;
    const m = el("div", "m");
    if (r.date) m.append(el("span", "", r.date));
    if (r.where) m.append(el("span", "", r.where));
    if (r.url) { const o = el("a", "", "claude.ai"); o.href = r.url; m.append(o); }
    li.append(a, m);
    if (r.summary) li.append(el("div", "s", r.summary));
    return li;
  }));
  count.textContent = hits.length + " of " + ROWS.length + " artifacts";
}
q.addEventListener("input", render);
document.addEventListener("keydown", e => {
  if (e.key === "/" && document.activeElement !== q) { e.preventDefault(); q.focus(); }
});
render();
</script></main></body></html>
"""
