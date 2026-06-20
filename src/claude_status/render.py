"""Build and emit the status lines."""
from __future__ import annotations
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone

from .colors import (
    Colors, RESET, DIM, GREEN, YELLOW, RED,
    colored_bar, quota_bar, ANSI_RE,
)
from .config import Config
from .env_counts import EnvCounts
from .git_status import GitStatus
from .stdin import StdinData, get_context_percent, get_model_name
from .transcript import TranscriptData


# ---------------------------------------------------------------------------
# Terminal width
# ---------------------------------------------------------------------------

def _terminal_width() -> int:
    col = os.environ.get('COLUMNS')
    if col and col.isdigit():
        return int(col)
    try:
        return shutil.get_terminal_size(fallback=(120, 24)).columns
    except Exception:
        return 120


def _bar_width(cfg: Config, term: int) -> int:
    if cfg.display.bar_width > 0:
        return cfg.display.bar_width
    if term < 80:
        return 5
    if term < 120:
        return 8
    return 10


# ---------------------------------------------------------------------------
# Visual string width (ANSI-aware, handles CJK double-width)
# ---------------------------------------------------------------------------

def _char_width(ch: str) -> int:
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ('W', 'F'):
        return 2
    if unicodedata.category(ch) == 'Cc':
        return 0
    return 1


def _visual_len(s: str) -> int:
    return sum(_char_width(c) for c in ANSI_RE.sub('', s))


def _truncate(s: str, max_w: int) -> str:
    """Truncate *s* to *max_w* visible columns, preserving ANSI codes."""
    if max_w <= 0:
        return ''
    if _visual_len(s) <= max_w:
        return s
    suffix = '...'
    keep = max_w - len(suffix)
    result = ''
    width = 0
    i = 0
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            result += m.group()
            i += len(m.group())
            continue
        ch = s[i]
        cw = _char_width(ch)
        if width + cw > keep:
            break
        result += ch
        width += cw
        i += 1
    return result + suffix + RESET


# ---------------------------------------------------------------------------
# Line-level renderers
# ---------------------------------------------------------------------------

def _project_line(stdin: StdinData, git: GitStatus | None, cfg: Config, colors: Colors) -> str | None:
    parts: list[str] = []

    if stdin.cwd:
        segs = re.split(r'[/\\]', stdin.cwd)
        segs = [s for s in segs if s]
        levels = max(1, cfg.display.path_levels)
        proj_path = '/'.join(segs[-levels:]) if segs else '/'
        parts.append(colors('project', proj_path))

    if git and cfg.git.enabled:
        branch_text = git.branch
        if cfg.git.show_dirty and git.is_dirty:
            branch_text += '*'
        git_str = (
            colors('git', 'git:(')
            + colors('gitBranch', branch_text)
            + colors('git', ')')
        )
        if cfg.git.show_ahead_behind:
            extras: list[str] = []
            if git.ahead:
                extras.append(f'↑{git.ahead}')
            if git.behind:
                extras.append(f'↓{git.behind}')
            if extras:
                git_str += colors('label', ' ' + ' '.join(extras))
        parts.append(git_str)

    return '  '.join(parts) if parts else None


def _context_line(stdin: StdinData, cfg: Config, colors: Colors, bw: int,
                  align_label: bool = False) -> str:
    dp = cfg.display
    pct = get_context_percent(stdin)
    warn, crit = dp.context_warning_threshold, dp.context_critical_threshold
    pct_esc = colors.get_context_esc(pct, warn, crit)
    cv = _fmt_context_value(stdin, pct, dp.context_value)
    pct_display = f'{pct_esc}{cv}{RESET}'
    label = colors('label', 'Context')
    bar   = colored_bar(pct, bw, colors, warn, crit)
    return f'{label} {bar} {pct_display}'


def _usage_line(stdin: StdinData, cfg: Config, colors: Colors, bw: int,
                align_label: bool = False) -> str | None:
    if not cfg.display.show_usage:
        return None
    dp = cfg.display

    parts: list[str] = []

    five_h = stdin.five_hour
    seven_d = stdin.seven_day

    if five_h is not None and five_h.used_percentage is not None:
        pct = max(0, min(100, round(five_h.used_percentage)))
        reset_str = _fmt_reset(five_h.resets_at)
        body = _fmt_usage_window('5h', pct, reset_str, dp.usage_bar_enabled, bw, colors)
        parts.append(body)

    if seven_d is not None and seven_d.used_percentage is not None:
        pct = max(0, min(100, round(seven_d.used_percentage)))
        if pct >= dp.seven_day_threshold or five_h is None:
            reset_str = _fmt_reset(seven_d.resets_at)
            body = _fmt_usage_window('7d', pct, reset_str, dp.usage_bar_enabled, bw, colors)
            parts.append(body)

    if not parts:
        return None

    label = colors('label', 'Usage')
    return f'{label} ' + ' | '.join(parts)


def _environment_line(counts: EnvCounts, cfg: Config, colors: Colors) -> str | None:
    dp = cfg.display
    parts: list[str] = []

    if dp.show_config_counts:
        total = counts.claude_md + counts.mcps + counts.hooks
        if total >= dp.environment_threshold and total > 0:
            if counts.claude_md:
                parts.append(f'{counts.claude_md} CLAUDE.md')
            if counts.mcps:
                parts.append(f'{counts.mcps} MCPs')
            if counts.hooks:
                parts.append(f'{counts.hooks} hooks')

    if dp.show_output_style and counts.output_style:
        parts.append(counts.output_style)

    if not parts:
        return None

    return colors('label', ' | '.join(parts))


def _tools_line(transcript: TranscriptData, colors: Colors) -> str | None:
    tools = transcript.tools
    if not tools:
        return None

    parts: list[str] = []

    running = [t for t in tools if t.status == 'running']
    for t in running[-2:]:
        tgt = _trunc_path(t.target) if t.target else ''
        entry = f'{YELLOW}◐{RESET} {colors("gitBranch", t.name)}'
        if tgt:
            entry += colors('label', f': {tgt}')
        parts.append(entry)

    done = [t for t in tools if t.status in ('completed', 'error')]
    counts: dict[str, int] = {}
    for t in done:
        counts[t.name] = counts.get(t.name, 0) + 1
    for name, n in sorted(counts.items(), key=lambda x: -x[1])[:4]:
        parts.append(f'{GREEN}✓{RESET} {name} {colors("label", f"×{n}")}')

    return ' | '.join(parts) if parts else None


def _agents_line(transcript: TranscriptData, colors: Colors) -> str | None:
    agents = transcript.agents
    running   = [a for a in agents if a.status == 'running']
    completed = [a for a in agents if a.status == 'completed'][-2:]

    seen: set[str] = set()
    to_show = []
    for a in running + completed:
        if a.id not in seen:
            seen.add(a.id)
            to_show.append(a)
    to_show = to_show[-3:]

    if not to_show:
        return None

    lines: list[str] = []
    for a in to_show:
        icon = f'{YELLOW}◐{RESET}' if a.status == 'running' else f'{GREEN}✓{RESET}'
        atype = colors('git', a.agent_type)
        desc  = colors('label', f': {a.description[:40]}') if a.description else ''
        elapsed = _fmt_elapsed(a.start_time, a.end_time)
        lines.append(f'{icon} {atype}{desc} {colors("label", f"({elapsed})")}')

    return '\n'.join(lines)


def _todos_line(transcript: TranscriptData, colors: Colors) -> str | None:
    todos = transcript.todos
    if not todos:
        return None

    in_progress = next((t for t in todos if t.status == 'in_progress'), None)
    completed   = sum(1 for t in todos if t.status == 'completed')
    total       = len(todos)

    if in_progress is None:
        if completed == total and total > 0:
            return f'{GREEN}✓{RESET} All tasks complete {colors("label", f"({completed}/{total})")}'
        return None

    content = in_progress.content
    if len(content) > 50:
        content = content[:47] + '...'
    return f'{YELLOW}▸{RESET} {content} {colors("label", f"({completed}/{total})")}'


# ---------------------------------------------------------------------------
# Session duration
# ---------------------------------------------------------------------------

def _session_duration(ts: datetime | None) -> str:
    if ts is None:
        return ''
    now_ms = datetime.now(timezone.utc)
    # make ts timezone-aware if it isn't
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    mins = int((now_ms - ts).total_seconds() // 60)
    if mins < 1:
        return '<1m'
    if mins < 60:
        return f'{mins}m'
    h, m = divmod(mins, 60)
    return f'{h}h {m}m'


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

def render(
    stdin: StdinData,
    transcript: TranscriptData,
    git: GitStatus | None,
    counts: EnvCounts,
    cfg: Config,
) -> None:
    colors = Colors(cfg.colors)
    term   = _terminal_width()
    bw     = _bar_width(cfg, term)
    dp     = cfg.display

    if cfg.layout == 'compact':
        _render_compact(stdin, transcript, git, counts, cfg, colors, bw, term)
        return

    lines: list[str] = []

    proj = _project_line(stdin, git, cfg, colors)
    if proj:
        lines.append(proj)

    ctx_line   = _context_line(stdin, cfg, colors, bw)
    usage_line = _usage_line(stdin, cfg, colors, bw)

    if ctx_line and usage_line:
        merged = f'{ctx_line} {DIM}│{RESET} {usage_line}'
        if term and _visual_len(merged) <= term:
            lines.append(merged)
        else:
            lines.append(ctx_line)
            lines.append(usage_line)
    elif ctx_line:
        lines.append(ctx_line)
    elif usage_line:
        lines.append(usage_line)

    env = _environment_line(counts, cfg, colors)
    if env:
        lines.append(env)

    if dp.show_tools:
        tl = _tools_line(transcript, colors)
        if tl:
            lines.append(tl)

    if dp.show_agents:
        al = _agents_line(transcript, colors)
        if al:
            lines.extend(al.split('\n'))

    if dp.show_todos:
        todo = _todos_line(transcript, colors)
        if todo:
            lines.append(todo)

    for line in lines:
        # Wrap long lines at ' | ' separators when we know the width.
        for wrapped in _wrap(line, term):
            print(f'{RESET}{wrapped}')


def _render_compact(
    stdin: StdinData,
    transcript: TranscriptData,
    git: GitStatus | None,
    counts: EnvCounts,
    cfg: Config,
    colors: Colors,
    bw: int,
    term: int,
) -> None:
    """All data on one line, separated by ' | '."""
    dp = cfg.display
    parts: list[str] = []

    model = get_model_name(stdin)
    pct   = get_context_percent(stdin)
    warn, crit = dp.context_warning_threshold, dp.context_critical_threshold
    bar   = colored_bar(pct, bw, colors, warn, crit)
    pct_e = colors.get_context_esc(pct, warn, crit)
    cv    = _fmt_context_value(stdin, pct, dp.context_value)
    parts.append(f'{colors("model", f"[{model}]")} {bar} {pct_e}{cv}{RESET}')

    if stdin.cwd:
        segs   = [s for s in re.split(r'[/\\]', stdin.cwd) if s]
        proj   = '/'.join(segs[-max(1, dp.path_levels):]) if segs else '/'
        proj_s = colors('project', proj)
        if git and cfg.git.enabled:
            bname = git.branch + ('*' if cfg.git.show_dirty and git.is_dirty else '')
            git_s = (colors('git', 'git:(')
                     + colors('gitBranch', bname)
                     + colors('git', ')'))
            parts.append(f'{proj_s} {git_s}')
        else:
            parts.append(proj_s)

    env_parts: list[str] = []
    if dp.show_config_counts:
        total = counts.claude_md + counts.mcps + counts.hooks
        if total >= dp.environment_threshold and total > 0:
            if counts.claude_md:
                env_parts.append(f'{counts.claude_md} CLAUDE.md')
            if counts.mcps:
                env_parts.append(f'{counts.mcps} MCPs')
            if counts.hooks:
                env_parts.append(f'{counts.hooks} hooks')
    if dp.show_output_style and counts.output_style:
        env_parts.append(counts.output_style)
    if env_parts:
        parts.append(colors('label', ' | '.join(env_parts)))

    if dp.show_usage:
        five_h = stdin.five_hour
        if five_h and five_h.used_percentage is not None:
            p = max(0, min(100, round(five_h.used_percentage)))
            r = _fmt_reset(five_h.resets_at)
            parts.append(_fmt_usage_window('5h', p, r, dp.usage_bar_enabled, bw, colors))

    if dp.show_tools:
        tl = _tools_line(transcript, colors)
        if tl:
            parts.append(tl)

    if dp.show_todos:
        todo = _todos_line(transcript, colors)
        if todo:
            parts.append(todo)

    if dp.show_duration and transcript.session_start:
        dur = _session_duration(transcript.session_start)
        if dur:
            parts.append(colors('label', f'⏱ {dur}'))

    line = ' | '.join(parts)
    for wrapped in _wrap(line, term):
        print(f'{RESET}{wrapped}')


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_context_value(stdin: StdinData, pct: int, mode: str) -> str:
    u = stdin.context.current_usage or {}
    total = (int(u.get('input_tokens') or 0)
             + int(u.get('cache_creation_input_tokens') or 0)
             + int(u.get('cache_read_input_tokens') or 0))
    size = stdin.context.size

    if mode == 'tokens':
        return f'{_fmt_tok(total)}/{_fmt_tok(size)}' if size else _fmt_tok(total)
    if mode == 'both':
        return f'{pct}% ({_fmt_tok(total)}/{_fmt_tok(size)})' if size else f'{pct}%'
    if mode == 'remaining':
        return f'{max(0, 100 - pct)}%'
    return f'{pct}%'


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.0f}k'
    return str(n)


def _fmt_reset(resets_at: float | None) -> str:
    """Return a human-readable time-until string, e.g. '2h 30m'."""
    if resets_at is None:
        return ''
    now = datetime.now(timezone.utc).timestamp()
    secs = resets_at - now
    if secs <= 0:
        return ''
    mins = int(secs // 60)
    if mins < 60:
        return f'{mins}m'
    h, m = divmod(mins, 60)
    return f'{h}h {m}m'


def _fmt_usage_window(label: str, pct: int, reset: str, bar_enabled: bool,
                      bw: int, colors: Colors) -> str:
    pct_esc = colors.get_quota_esc(pct)
    pct_str = f'{pct_esc}{pct}%{RESET}'
    lbl     = colors('label', f'{label}:')
    if bar_enabled:
        bar = quota_bar(pct, bw, colors)
        body = f'{bar} {pct_str}'
        if reset:
            body += f' {colors("label", f"({reset} / {label})")}'
    else:
        body = pct_str
        if reset:
            body += f' {colors("label", f"({reset})")}'
    return f'{lbl} {body}'


def _fmt_elapsed(start: datetime, end: datetime | None) -> str:
    now = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    finish = end if end else now
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    ms = max(0, (finish - start).total_seconds() * 1000)
    if ms < 1000:
        return '<1s'
    s = int(ms / 1000)
    if s < 60:
        return f'{s}s'
    m, s = divmod(s, 60)
    if m < 60:
        return f'{m}m {s}s'
    h, m = divmod(m, 60)
    return f'{h}h {m}m'


def _trunc_path(path: str, max_len: int = 20) -> str:
    path = path.replace('\\', '/')
    if len(path) <= max_len:
        return path
    parts = path.split('/')
    fname = parts[-1] if parts else path
    if len(fname) >= max_len:
        return fname[:max_len - 3] + '...'
    return '.../' + fname


# ---------------------------------------------------------------------------
# Line wrapping
# ---------------------------------------------------------------------------

_SEP_RE = re.compile(r'( [│|] )')


def _wrap(line: str, max_w: int) -> list[str]:
    """Split a line at ' | ' or ' │ ' separators if it exceeds max_w columns."""
    if max_w <= 0 or _visual_len(line) <= max_w:
        return [line]

    # Split on separators, keeping them with the next segment.
    tokens = _SEP_RE.split(line)
    # tokens alternates: segment, sep, segment, sep, ...
    segments: list[tuple[str, str]] = []  # (separator, segment)
    it = iter(tokens)
    first = next(it, '')
    segments.append(('', first))
    while True:
        sep  = next(it, None)
        seg  = next(it, None)
        if sep is None:
            break
        segments.append((sep, seg or ''))

    result: list[str] = []
    current = ''
    for sep, seg in segments:
        candidate = current + sep + seg if current else seg
        if current and _visual_len(candidate) > max_w:
            result.append(current)
            current = seg
        else:
            current = candidate
    if current:
        result.append(current)
    return result or [line]
