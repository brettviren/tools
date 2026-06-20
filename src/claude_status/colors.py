"""ANSI color system matching claude-hud's default palette."""
import re

RESET = '\x1b[0m'
DIM   = '\x1b[2m'
RED   = '\x1b[31m'
GREEN = '\x1b[32m'
YELLOW  = '\x1b[33m'
MAGENTA = '\x1b[35m'
CYAN    = '\x1b[36m'
BRIGHT_BLUE    = '\x1b[94m'
BRIGHT_MAGENTA = '\x1b[95m'
CLAUDE_ORANGE  = '\x1b[38;5;208m'

# Regex to strip ANSI escape sequences for width measurement.
ANSI_RE = re.compile(r'\x1b(?:\[[0-9;]*m|\][^\x07\x1b]*(?:\x07|\x1b\\))')

_NAMED: dict[str, str] = {
    'dim': DIM, 'red': RED, 'green': GREEN, 'yellow': YELLOW,
    'magenta': MAGENTA, 'cyan': CYAN,
    'brightBlue': BRIGHT_BLUE, 'brightMagenta': BRIGHT_MAGENTA,
}

# Default role → ANSI escape mapping (matches claude-hud defaults).
DEFAULTS: dict[str, str] = {
    'context':      GREEN,
    'usage':        BRIGHT_BLUE,
    'warning':      YELLOW,
    'usageWarning': BRIGHT_MAGENTA,
    'critical':     RED,
    'model':        CYAN,
    'project':      YELLOW,
    'git':          MAGENTA,
    'gitBranch':    CYAN,
    'label':        DIM,
    'custom':       CLAUDE_ORANGE,
}


def resolve(value: str | int | None, fallback: str) -> str:
    """Convert a color config value to an ANSI escape sequence."""
    if value is None:
        return fallback
    if isinstance(value, int):
        return f'\x1b[38;5;{value}m'
    if isinstance(value, str) and value.startswith('#') and len(value) == 7:
        r, g, b = int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
        return f'\x1b[38;2;{r};{g};{b}m'
    return _NAMED.get(value, fallback)


class Colors:
    """Resolved color palette, merging user overrides over defaults."""

    def __init__(self, overrides: dict | None = None):
        self._esc: dict[str, str] = {}
        for role, default_esc in DEFAULTS.items():
            raw = (overrides or {}).get(role)
            self._esc[role] = resolve(raw, default_esc)

    def __call__(self, role: str, text: str) -> str:
        esc = self._esc.get(role, '')
        return f'{esc}{text}{RESET}' if esc else text

    def get_context_esc(self, pct: int, warn: int = 70, crit: int = 85) -> str:
        if pct >= crit:
            return self._esc.get('critical', RED)
        if pct >= warn:
            return self._esc.get('warning', YELLOW)
        return self._esc.get('context', GREEN)

    def get_quota_esc(self, pct: int) -> str:
        if pct >= 90:
            return self._esc.get('critical', RED)
        if pct >= 75:
            return self._esc.get('usageWarning', BRIGHT_MAGENTA)
        return self._esc.get('usage', BRIGHT_BLUE)


def colored_bar(pct: int, width: int, colors: Colors,
                warn: int = 70, crit: int = 85) -> str:
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    empty = width - filled
    esc = colors.get_context_esc(pct, warn, crit)
    return f'{esc}{"█" * filled}{DIM}{"░" * empty}{RESET}'


def quota_bar(pct: int, width: int, colors: Colors) -> str:
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    empty = width - filled
    esc = colors.get_quota_esc(pct)
    return f'{esc}{"█" * filled}{DIM}{"░" * empty}{RESET}'
