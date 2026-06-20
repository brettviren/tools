"""Count CLAUDE.md files, MCP servers, and hooks visible from a working directory."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EnvCounts:
    claude_md: int = 0
    mcps: int = 0
    hooks: int = 0
    output_style: str | None = None   # e.g. "accept edits"


def count(cwd: str) -> EnvCounts:
    home = Path.home()
    claude_dir = Path(os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude'))
    cwd_path = Path(cwd) if cwd else Path.cwd()

    result = EnvCounts()
    result.claude_md  = _count_claude_md(cwd_path, home)
    result.mcps, result.hooks, result.output_style = _read_settings(cwd_path, claude_dir)
    return result


# ---------------------------------------------------------------------------
# CLAUDE.md counting
# ---------------------------------------------------------------------------

def _count_claude_md(cwd: Path, home: Path) -> int:
    """Count CLAUDE.md files from cwd up to (and including) home."""
    count = 0
    current = cwd
    for _ in range(20):  # safety limit
        candidate = current / 'CLAUDE.md'
        if candidate.is_file():
            count += 1
        if current == home or current == current.parent:
            break
        current = current.parent
    # Also count ~/.claude/CLAUDE.md
    if (home / '.claude' / 'CLAUDE.md').is_file():
        count += 1
    return count


# ---------------------------------------------------------------------------
# Settings file parsing (MCPs, hooks, permissions mode)
# ---------------------------------------------------------------------------

def _read_settings(cwd: Path, claude_dir: Path) -> tuple[int, int, str | None]:
    """Return (mcp_count, hook_count, output_style) by reading settings files."""
    settings_paths = [
        cwd / '.claude' / 'settings.json',
        cwd / '.claude' / 'settings.local.json',
        claude_dir / 'settings.json',
        claude_dir / 'settings.local.json',
    ]

    mcp_names: set[str] = set()
    hook_count = 0
    output_style: str | None = None

    for sp in settings_paths:
        if not sp.is_file():
            continue
        try:
            data = json.loads(sp.read_text())
        except Exception:
            continue

        # MCPs
        mcp_servers = data.get('mcpServers')
        if isinstance(mcp_servers, dict):
            mcp_names.update(mcp_servers.keys())

        # Hooks
        hooks = data.get('hooks')
        if isinstance(hooks, dict):
            for event_hooks in hooks.values():
                if isinstance(event_hooks, list):
                    for group in event_hooks:
                        if isinstance(group, dict):
                            inner = group.get('hooks', [])
                            if isinstance(inner, list):
                                hook_count += len(inner)

        # Permissions / output style (from the most-specific file that sets it)
        if output_style is None:
            perms = data.get('permissions') or {}
            mode = perms.get('defaultMode') or ''
            if mode:
                output_style = _mode_label(mode)

    return len(mcp_names), hook_count, output_style


_MODE_LABELS: dict[str, str] = {
    'acceptEdits':        'accept edits',
    'autoApprove':        'auto approve',
    'bypassPermissions':  'bypass perms',
}


def _mode_label(mode: str) -> str | None:
    return _MODE_LABELS.get(mode) or None
