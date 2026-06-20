"""Configuration loading for claude-status.

Reads ~/.claude/claude-status.json; all keys are optional with sane defaults.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GitConfig:
    enabled: bool = True
    show_dirty: bool = True
    show_ahead_behind: bool = False


@dataclass
class DisplayConfig:
    # Context bar
    context_warning_threshold: int = 70
    context_critical_threshold: int = 85
    # What to show next to the bar: "percent" | "tokens" | "remaining" | "both"
    context_value: str = 'percent'
    # Usage rate-limit bars
    show_usage: bool = True
    usage_bar_enabled: bool = True
    seven_day_threshold: int = 80   # only show 7d bar when >= this %
    # Environment counts
    show_config_counts: bool = True
    show_output_style: bool = True  # "accept edits" etc.
    environment_threshold: int = 0  # min total count to show env line
    # Activity
    show_tools: bool = True
    show_agents: bool = True
    show_todos: bool = True
    # Other
    show_duration: bool = True
    show_cost: bool = False
    path_levels: int = 1            # how many path segments to show for cwd
    bar_width: int = 10             # override; 0 = auto


@dataclass
class Config:
    layout: str = 'expanded'        # 'expanded' | 'compact'
    colors: dict = field(default_factory=dict)
    git: GitConfig = field(default_factory=GitConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)


def load(path: Path | None = None) -> Config:
    if path is None:
        path = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude')) / 'claude-status.json'

    if not path.exists():
        return Config()

    try:
        raw = json.loads(path.read_text())
    except Exception:
        return Config()

    cfg = Config()

    if 'layout' in raw:
        cfg.layout = raw['layout']

    if 'colors' in raw and isinstance(raw['colors'], dict):
        cfg.colors = raw['colors']

    if 'git' in raw and isinstance(raw['git'], dict):
        g = raw['git']
        cfg.git.enabled          = bool(g.get('enabled', cfg.git.enabled))
        cfg.git.show_dirty       = bool(g.get('showDirty', cfg.git.show_dirty))
        cfg.git.show_ahead_behind = bool(g.get('showAheadBehind', cfg.git.show_ahead_behind))

    if 'display' in raw and isinstance(raw['display'], dict):
        d = raw['display']
        dp = cfg.display
        dp.context_warning_threshold  = int(d.get('contextWarningThreshold', dp.context_warning_threshold))
        dp.context_critical_threshold = int(d.get('contextCriticalThreshold', dp.context_critical_threshold))
        dp.context_value              = d.get('contextValue', dp.context_value)
        dp.show_usage                 = bool(d.get('showUsage', dp.show_usage))
        dp.usage_bar_enabled          = bool(d.get('usageBarEnabled', dp.usage_bar_enabled))
        dp.seven_day_threshold        = int(d.get('sevenDayThreshold', dp.seven_day_threshold))
        dp.show_config_counts         = bool(d.get('showConfigCounts', dp.show_config_counts))
        dp.show_output_style          = bool(d.get('showOutputStyle', dp.show_output_style))
        dp.environment_threshold      = int(d.get('environmentThreshold', dp.environment_threshold))
        dp.show_tools                 = bool(d.get('showTools', dp.show_tools))
        dp.show_agents                = bool(d.get('showAgents', dp.show_agents))
        dp.show_todos                 = bool(d.get('showTodos', dp.show_todos))
        dp.show_duration              = bool(d.get('showDuration', dp.show_duration))
        dp.show_cost                  = bool(d.get('showCost', dp.show_cost))
        dp.path_levels                = int(d.get('pathLevels', dp.path_levels))
        dp.bar_width                  = int(d.get('barWidth', dp.bar_width))

    return cfg
