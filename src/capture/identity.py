"""Project identity for a captured session.

Under subtree matching the session cwd is no longer the project root: an
allowlist entry names a work tree holding dozens of repositories, and a
session may start anywhere beneath one. Recording the raw cwd would split a
single repository into `repo`, `repo/src` and `repo/tests`; recording the
allowlist root instead would merge every repository under it into one. The
useful identity is the nearest enclosing repository.
"""
import os
from pathlib import Path

MARKER = ".git"


def _is_repo_root(path: Path) -> bool:
    """True when path holds a usable git marker.

    The marker is a directory for an ordinary clone and a file holding a
    `gitdir:` pointer for a linked worktree or a submodule. The pointer is
    resolved relative to its own parent, because the submodules on disk use
    relative targets such as `gitdir: ../.git/modules/geoserver`.
    """
    marker = path / MARKER
    if marker.is_dir():
        return True
    if not marker.is_file():
        return False

    try:
        body = marker.read_text(errors="replace").strip()
    except OSError:
        # An unreadable marker is not a marker we can trust.
        return False

    if not body.startswith("gitdir:"):
        return False
    target = body[len("gitdir:"):].strip()
    if not target:
        return False

    resolved = Path(target)
    if not resolved.is_absolute():
        resolved = path / resolved
    return resolved.exists()


def project_for(cwd: str, root: str) -> str:
    """The nearest repository at or above cwd, bounded by root.

    `root` is the allowlist entry that admitted this session. The walk stops
    there inclusively and never inspects above it, so a repository that
    happens to contain the whole work tree cannot claim every session in it.
    Falls back to the resolved cwd when no repository is found.
    """
    try:
        here = Path(cwd).expanduser().resolve()
        boundary = Path(root).expanduser().resolve()
    except Exception:
        return cwd

    if here != boundary and not here.is_relative_to(boundary):
        # The caller matched this cwd against this root, so this cannot happen
        # in the drain; refuse to walk an unbounded path rather than assume.
        return str(here)

    current = here
    while True:
        if _is_repo_root(current):
            return str(current)
        if current == boundary:
            return str(here)
        parent = current.parent
        if parent == current:
            return str(here)
        current = parent
