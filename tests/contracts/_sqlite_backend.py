"""The SQLite backend of the contract suites, registered as ``sqlite`` when the ``persistence.sqlite`` package is importable in this tree."""

from __future__ import annotations

import importlib.util
import os
import tempfile
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Sequence

from persistence import ProfileScope

if TYPE_CHECKING:
    from tests.contracts._sqlite_seams import ContractSQLiteBackend


def _sqlite_backend_available() -> bool:
    """Whether the SQLite backend package is importable in this tree."""
    try:
        return importlib.util.find_spec("persistence.sqlite") is not None
    except ImportError:
        return False


@contextmanager
def _open_sqlite_backend(scopes: Sequence[ProfileScope]) -> Iterator[ContractSQLiteBackend]:
    """The SQLite backend over the test scopes, each a profile home under a folder of its own inside the test's NAKO_HOME, so every factory call starts from empty stores; that folder is NAKO_HOME while the backend is open; a profile a suite names beyond the two is served on first open under the same folder, and every handle carries the seeding seams of ``tests/contracts/_sqlite_seams.py``."""
    from nako_constants import get_nako_home
    from persistence.sqlite import _registry
    from tests.contracts._sqlite_seams import ContractSQLiteBackend

    root = Path(tempfile.mkdtemp(prefix="sqlite-backend-", dir=get_nako_home()))
    stores = {}
    for scope in scopes:
        home = root / "profiles" / scope.name
        home.mkdir(parents=True, exist_ok=True)
        stores[scope.nako_home_key] = home / "state.db"
    # The backend's folder is the Nako root while it is open: the store derives its owner profile from its path under that root, as the reference derives it from its own.
    previous = os.environ.get("NAKO_HOME")
    os.environ["NAKO_HOME"] = str(root)
    backend = ContractSQLiteBackend(stores, root=root)
    try:
        yield backend
    finally:
        backend.close()
        _registry.close_all()
        if previous is None:
            os.environ.pop("NAKO_HOME", None)
        else:
            os.environ["NAKO_HOME"] = previous


def register(register_backend: Callable[..., None], scopes: Sequence[ProfileScope]) -> None:
    """Register ``sqlite`` over ``scopes`` when the backend package is importable."""
    if _sqlite_backend_available():
        register_backend("sqlite", partial(_open_sqlite_backend, tuple(scopes)))
