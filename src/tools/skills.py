# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>

"""Locate this package's Claude Code skills and install them for the user.

Skills live in ``skills/<name>/SKILL.md`` in the source tree.  Installed
wheels carry them under ``<prefix>/share/tools/skills`` (see ``data-files`` in
pyproject.toml).  Installing a skill puts it in ``<claude config>/skills``.
"""

import os
import shutil
import sysconfig
from dataclasses import dataclass
from pathlib import Path

SKILL_FILE = "SKILL.md"


def source_dir() -> Path:
    """Directory holding the skills: the source tree if present, else the wheel's data."""
    tree = Path(__file__).resolve().parent.parent.parent / "skills"
    if tree.is_dir():
        return tree
    return Path(sysconfig.get_path("data")) / "share" / "tools" / "skills"


def target_dir() -> Path:
    """Where Claude Code looks for user skills."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(base).expanduser() / "skills"


def available(src: Path) -> list[Path]:
    """Skill directories (those with a SKILL.md) under *src*."""
    if not src.is_dir():
        return []
    return sorted(p for p in src.iterdir() if (p / SKILL_FILE).is_file())


@dataclass
class Result:
    """Outcome of installing one skill."""

    name: str
    action: str   # linked | copied | skipped
    note: str = ""


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def install_one(skill: Path, target: Path, copy: bool, force: bool) -> Result:
    """Install one skill directory into *target* by symlink or copy."""
    dest = target / skill.name
    if dest.is_symlink() or dest.exists():
        ours = dest.is_symlink() and dest.resolve() == skill.resolve()
        if not (ours or force):
            return Result(skill.name, "skipped", f"{dest} exists; use --force to replace")
        _remove(dest)
    target.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(skill, dest)
        return Result(skill.name, "copied", str(dest))
    dest.symlink_to(skill.resolve(), target_is_directory=True)
    return Result(skill.name, "linked", str(dest))


def install_all(src: Path, target: Path, copy: bool = False, force: bool = False) -> list[Result]:
    """Install every available skill; symlink only when *src* is a source tree."""
    in_tree = (src.parent / "pyproject.toml").exists()
    return [install_one(s, target, copy or not in_tree, force) for s in available(src)]
