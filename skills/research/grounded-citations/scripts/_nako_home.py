"""Resolve NAKO_HOME for standalone skill scripts.

Skill scripts may run outside the Nako process (system Python, nix env,
CI) where ``nako_constants`` is not importable.  This module provides the
same ``get_nako_home()`` contract without requiring it on ``sys.path``.

When ``nako_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from nako_constants import get_nako_home
except (ModuleNotFoundError, ImportError):

    def get_nako_home() -> Path:
        """Return the Nako home directory (default: ``~/.nako``)."""
        val = os.environ.get("NAKO_HOME", "").strip()
        return Path(val) if val else Path.home() / ".nako"
