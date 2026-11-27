"""Resolve NAKO_HOME for standalone skill scripts.

Skill scripts may run outside the Nako process (e.g. system Python,
nix env, CI) where ``nako_constants`` is not importable.  This module
provides the same ``get_nako_home()`` and ``display_nako_home()``
contracts as ``nako_constants`` without requiring it on ``sys.path``.

When ``nako_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``nako_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``NAKO_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from nako_constants import display_nako_home
    from nako_constants import get_nako_home
except (ModuleNotFoundError, ImportError):

    def get_nako_home() -> Path:
        """Return the Nako home directory (default: ~/.nako).

        Mirrors ``nako_constants.get_nako_home()``."""
        val = os.environ.get("NAKO_HOME", "").strip()
        return Path(val) if val else Path.home() / ".nako"

    def display_nako_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``nako_constants.display_nako_home()``."""
        home = get_nako_home()
        try:
            return "~/" + home.relative_to(Path.home()).as_posix()
        except ValueError:
            return str(home)
