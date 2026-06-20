"""Entry point: python -m claude_status (or the claude-status script)."""
from __future__ import annotations
import os
import sys
import time

from . import config as cfg_mod
from . import env_counts, git_status, render, stdin as stdin_mod, transcript


def main() -> None:
    if '--test' in sys.argv:
        _run_test()
        return

    cfg = cfg_mod.load()

    raw = stdin_mod.read_stdin()
    if raw is None:
        # No stdin — Claude Code is verifying the command works.
        print('[claude-status] Initializing...')
        return

    stdin   = stdin_mod.parse(raw)
    txcript = transcript.parse(stdin.transcript_path)
    git     = git_status.get(stdin.cwd) if stdin.cwd else None
    counts  = env_counts.count(stdin.cwd)

    render.render(stdin, txcript, git, counts, cfg)


def _run_test() -> None:
    """Render with a synthetic payload built from the current environment."""
    cwd = os.getcwd()

    # Find the most recent transcript for this project, if any.
    import glob
    claude_dir = os.environ.get('CLAUDE_CONFIG_DIR', os.path.expanduser('~/.claude'))
    slug = cwd.replace('/', '-').lstrip('-')
    pattern = os.path.join(claude_dir, 'projects', slug, '*.jsonl')
    transcripts = sorted(glob.glob(pattern))
    tx_path = transcripts[-1] if transcripts else ''

    raw = {
        'transcript_path': tx_path,
        'cwd': cwd,
        'model': {
            'id': 'claude-sonnet-4-6',
            'display_name': 'Claude Sonnet 4.6',
        },
        'context_window': {
            'context_window_size': 200000,
            'used_percentage': 38,
            'current_usage': {
                'input_tokens': 68000,
                'cache_creation_input_tokens': 4000,
                'cache_read_input_tokens': 4000,
            },
        },
        'rate_limits': {
            'five_hour': {
                'used_percentage': 42,
                'resets_at': time.time() + 7200,
            },
            'seven_day': {
                'used_percentage': 15,
                'resets_at': time.time() + 86400 * 5,
            },
        },
    }

    cfg     = cfg_mod.load()
    stdin   = stdin_mod.parse(raw)
    txcript = transcript.parse(stdin.transcript_path)
    git     = git_status.get(stdin.cwd)
    counts  = env_counts.count(stdin.cwd)

    render.render(stdin, txcript, git, counts, cfg)


if __name__ == '__main__':
    main()
