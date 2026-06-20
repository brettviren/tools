"""Parse Claude Code JSONL transcript files."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


@dataclass
class ToolEntry:
    id: str
    name: str
    target: str | None
    status: Literal['running', 'completed', 'error']
    start_time: datetime
    end_time: datetime | None = None


@dataclass
class AgentEntry:
    id: str
    agent_type: str
    description: str | None
    status: Literal['running', 'completed']
    start_time: datetime
    end_time: datetime | None = None


@dataclass
class TodoItem:
    content: str
    status: Literal['pending', 'in_progress', 'completed']


@dataclass
class TranscriptData:
    tools: list[ToolEntry] = field(default_factory=list)
    agents: list[AgentEntry] = field(default_factory=list)
    todos: list[TodoItem] = field(default_factory=list)
    session_start: datetime | None = None
    session_name: str | None = None


_EMPTY = TranscriptData()


def parse(path: str) -> TranscriptData:
    if not path:
        return _EMPTY
    p = Path(path)
    if not p.exists():
        return _EMPTY

    tool_map: dict[str, ToolEntry] = {}
    agent_map: dict[str, AgentEntry] = {}
    latest_todos: list[TodoItem] = []
    task_id_to_index: dict[str, int] = {}
    session_start: datetime | None = None
    custom_title: str | None = None
    latest_slug: str | None = None

    try:
        with p.open(encoding='utf-8', errors='replace') as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                ts = _parse_ts(entry.get('timestamp'))
                if ts and session_start is None:
                    session_start = ts

                etype = entry.get('type')

                if etype == 'custom-title':
                    custom_title = entry.get('customTitle') or None
                elif entry.get('slug'):
                    latest_slug = entry['slug']

                content = (entry.get('message') or {}).get('content')
                if not isinstance(content, list):
                    continue

                for block in content:
                    btype = block.get('type')

                    if btype == 'tool_use':
                        bid   = block.get('id') or ''
                        bname = block.get('name') or ''
                        if not bid or not bname:
                            continue
                        binput = block.get('input') or {}

                        if bname in ('Task', 'Agent'):
                            agent_map[bid] = AgentEntry(
                                id=bid,
                                agent_type=str(binput.get('subagent_type') or bname),
                                description=str(binput.get('description') or '') or None,
                                status='running',
                                start_time=ts or datetime.now(timezone.utc),
                            )
                        elif bname == 'TodoWrite':
                            raw_todos = binput.get('todos') or []
                            latest_todos = [_parse_todo(t) for t in raw_todos if isinstance(t, dict)]
                            task_id_to_index.clear()
                        elif bname == 'TaskCreate':
                            subject = str(binput.get('subject') or binput.get('description') or 'Untitled task')
                            status  = _norm_status(binput.get('status')) or 'pending'
                            latest_todos.append(TodoItem(content=subject, status=status))
                            raw_tid = binput.get('taskId') or bid
                            task_id_to_index[str(raw_tid)] = len(latest_todos) - 1
                        elif bname == 'TaskUpdate':
                            idx = _resolve_task_index(binput.get('taskId'), task_id_to_index, latest_todos)
                            if idx is not None:
                                new_status = _norm_status(binput.get('status'))
                                if new_status:
                                    latest_todos[idx].status = new_status
                                new_content = str(binput.get('subject') or binput.get('description') or '')
                                if new_content:
                                    latest_todos[idx].content = new_content
                        else:
                            tool_map[bid] = ToolEntry(
                                id=bid,
                                name=bname,
                                target=_extract_target(bname, binput),
                                status='running',
                                start_time=ts or datetime.now(timezone.utc),
                            )

                    elif btype == 'tool_result':
                        tuid = block.get('tool_use_id') or ''
                        is_err = bool(block.get('is_error'))
                        if tuid in tool_map:
                            tool_map[tuid].status   = 'error' if is_err else 'completed'
                            tool_map[tuid].end_time = ts
                        if tuid in agent_map:
                            agent_map[tuid].status   = 'completed'
                            agent_map[tuid].end_time = ts

    except OSError:
        return _EMPTY

    return TranscriptData(
        tools=list(tool_map.values())[-20:],
        agents=list(agent_map.values())[-10:],
        todos=latest_todos,
        session_start=session_start,
        session_name=custom_title or latest_slug,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Python 3.11+ handles 'Z'; earlier versions need manual replacement.
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


def _parse_todo(raw: dict) -> TodoItem:
    return TodoItem(
        content=str(raw.get('content') or ''),
        status=_norm_status(raw.get('status')) or 'pending',
    )


def _norm_status(v) -> Literal['pending', 'in_progress', 'completed'] | None:
    if not isinstance(v, str):
        return None
    v = v.lower()
    if v in ('pending', 'not_started'):
        return 'pending'
    if v in ('in_progress', 'running'):
        return 'in_progress'
    if v in ('completed', 'complete', 'done'):
        return 'completed'
    return None


def _resolve_task_index(task_id, mapping: dict[str, int], todos: list[TodoItem]) -> int | None:
    if task_id is None:
        return None
    key = str(task_id)
    if key in mapping:
        return mapping[key]
    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(todos):
            return idx
    return None


def _extract_target(name: str, inp: dict) -> str | None:
    if not inp:
        return None
    if name in ('Read', 'Write', 'Edit'):
        return inp.get('file_path') or inp.get('path') or None
    if name in ('Glob', 'Grep'):
        return inp.get('pattern') or None
    if name == 'Skill':
        s = str(inp.get('skill') or '').strip()
        return s or None
    if name == 'Bash':
        cmd = str(inp.get('command') or '')
        return cmd[:30] + ('...' if len(cmd) > 30 else '') if cmd else None
    return None
