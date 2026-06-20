"""Parse the JSON blob Claude Code sends via stdin."""
from __future__ import annotations
import json
import select
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextWindow:
    size: int = 0
    used_percentage: float | None = None
    current_usage: dict = field(default_factory=dict)


@dataclass
class RateLimitWindow:
    used_percentage: float | None = None
    resets_at: float | None = None   # unix timestamp


@dataclass
class StdinData:
    transcript_path: str = ''
    cwd: str = ''
    model_id: str = ''
    model_display_name: str = ''
    context: ContextWindow = field(default_factory=ContextWindow)
    cost_usd: float | None = None
    five_hour: RateLimitWindow | None = None
    seven_day: RateLimitWindow | None = None
    effort: str | None = None        # "low" | "medium" | "high" | "max"


def read_stdin(timeout: float = 0.25) -> dict[str, Any] | None:
    """Read JSON from stdin; return None if nothing arrives within *timeout* seconds."""
    if sys.stdin.isatty():
        return None
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (ValueError, OSError):
        return None
    if not ready:
        return None
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def parse(raw: dict[str, Any]) -> StdinData:
    data = StdinData()

    data.transcript_path = raw.get('transcript_path') or ''
    data.cwd             = raw.get('cwd') or ''

    model = raw.get('model') or {}
    data.model_id           = (model.get('id') or '').strip()
    data.model_display_name = (model.get('display_name') or '').strip()

    cw = raw.get('context_window') or {}
    data.context.size = int(cw.get('context_window_size') or 0)
    data.context.used_percentage = _float_or_none(cw.get('used_percentage'))
    data.context.current_usage   = cw.get('current_usage') or {}

    cost = raw.get('cost') or {}
    data.cost_usd = _float_or_none(cost.get('total_cost_usd'))

    rl = raw.get('rate_limits') or {}
    if rl.get('five_hour'):
        w = rl['five_hour']
        data.five_hour = RateLimitWindow(
            used_percentage=_float_or_none(w.get('used_percentage')),
            resets_at=_float_or_none(w.get('resets_at')),
        )
    if rl.get('seven_day'):
        w = rl['seven_day']
        data.seven_day = RateLimitWindow(
            used_percentage=_float_or_none(w.get('used_percentage')),
            resets_at=_float_or_none(w.get('resets_at')),
        )

    effort = raw.get('effort')
    if isinstance(effort, dict):
        data.effort = effort.get('level') or None
    elif isinstance(effort, str):
        data.effort = effort or None

    return data


def get_context_percent(data: StdinData) -> int:
    """Return context usage as 0-100 integer."""
    native = data.context.used_percentage
    if native is not None and native > 0:
        return max(0, min(100, round(native)))
    size = data.context.size
    if not size:
        return 0
    u = data.context.current_usage or {}
    total = (
        int(u.get('input_tokens') or 0)
        + int(u.get('cache_creation_input_tokens') or 0)
        + int(u.get('cache_read_input_tokens') or 0)
    )
    return max(0, min(100, round(total / size * 100)))


def get_model_name(data: StdinData) -> str:
    if data.model_display_name:
        # Strip redundant "(Xk context)" suffix
        import re
        name = re.sub(r'\s*\([^)]*\bcontext\b[^)]*\)', '', data.model_display_name, flags=re.I).strip()
        return name
    if data.model_id:
        return data.model_id
    return 'Unknown'


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None   # NaN check
    except (TypeError, ValueError):
        return None
