"""Git repository status for the current working directory."""
from __future__ import annotations
import subprocess
from dataclasses import dataclass


@dataclass
class GitStatus:
    branch: str
    is_dirty: bool
    ahead: int = 0
    behind: int = 0


def get(cwd: str) -> GitStatus | None:
    if not cwd:
        return None
    branch = _run(['rev-parse', '--abbrev-ref', 'HEAD'], cwd)
    if not branch:
        return None

    dirty_out = _run(['--no-optional-locks', 'status', '--porcelain'], cwd)
    is_dirty = bool(dirty_out and dirty_out.strip())

    ahead = behind = 0
    rev = _run(['rev-list', '--left-right', '--count', '@{upstream}...HEAD'], cwd)
    if rev:
        parts = rev.split()
        if len(parts) == 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
            except ValueError:
                pass

    return GitStatus(branch=branch, is_dirty=is_dirty, ahead=ahead, behind=behind)


def _run(args: list[str], cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ['git'] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None
