"""Resolve a session's cwd to (repo_root, origin remote) without shelling out; cached per directory."""
from __future__ import annotations

import configparser
import functools
from pathlib import Path


@functools.lru_cache(maxsize=512)
def repo_info(cwd: str) -> tuple[str, str]:
    """(repo_root, remote_url) or ("", "") when cwd is not inside a git work tree (or a linked worktree)."""
    try:
        p = Path(cwd).resolve()
    except OSError:
        return "", ""
    for d in (p, *p.parents):
        g = d / ".git"
        if g.is_dir():
            return str(d), _origin(g / "config")
        if g.is_file():                                     # linked worktree: "gitdir: <common>/.git/worktrees/<n>"
            try:
                gitdir = g.read_text().strip().split(":", 1)[1].strip()
            except (OSError, IndexError):
                return str(d), ""
            common = Path(gitdir)
            if not common.is_absolute():
                common = (d / common).resolve()
            cf = common / "commondir"
            if cf.is_file():
                common = (common / cf.read_text().strip()).resolve()
            return str(d), _origin(common / "config")
    return "", ""


def _origin(config_path: Path) -> str:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(config_path)
    except (OSError, configparser.Error):
        return ""
    for sect in ('remote "origin"', *[s for s in cp.sections() if s.startswith("remote ")]):
        if cp.has_option(sect, "url"):
            return cp.get(sect, "url").strip()
    return ""
