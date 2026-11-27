"""Plugins written for the upstream project keep working on a renamed build.

A build that renames the project (its packages, helpers and environment variables) would break
every plugin that imports the upstream spellings. On such a build this serves the upstream module
names as the real modules, resolves upstream-spelled attributes on them, and reads an upstream-spelled
environment variable from its renamed one. Core also asks plugins for names by string (a capability
flag on a client, a manifest key, a session variable); ``offered``, ``own_keys`` and ``own_spelling``
are what those call sites use to hear the upstream spelling too. On an unrenamed build it does nothing.
"""
from __future__ import annotations

import os

from .finder import Finder, _Alias

_UPSTREAM = bytes((104, 101, 114, 109, 101, 115)).decode()
_OWN = __name__.partition("_")[0]
_SPELLINGS = tuple((spell(_UPSTREAM), spell(_OWN)) for spell in (str.lower, str.upper, str.capitalize))
_MISSING = object()


def own_spelling(name: str) -> str:
    """``name`` with the upstream project name replaced by this build's, case kept."""
    for upstream, own in _SPELLINGS:
        name = name.replace(upstream, own)
    return name


def upstream_spelling(name: str) -> str:
    """``name`` with this build's project name replaced by the upstream one, case kept."""
    for upstream, own in _SPELLINGS:
        name = name.replace(own, upstream)
    return name


def offered(obj, name: str, default=None):
    """An attribute core asks a plugin's object for, under this build's spelling or the upstream one."""
    value = getattr(obj, name, _MISSING)
    if value is _MISSING and _OWN != _UPSTREAM:
        value = getattr(obj, upstream_spelling(name), _MISSING)
    return default if value is _MISSING else value


def own_keys(mapping):
    """A plugin's manifest with its top-level keys in this build's spelling."""
    if _OWN == _UPSTREAM or not isinstance(mapping, dict):
        return mapping
    return {own_spelling(key) if isinstance(key, str) else key: value for key, value in mapping.items()}


class _TopLevelNames(dict):
    """Every upstream-named top-level module maps to its renamed one; nothing is listed."""

    def __contains__(self, name) -> bool:
        return "." not in name and (name == _UPSTREAM or name.startswith(f"{_UPSTREAM}_"))

    def __getitem__(self, name):
        return own_spelling(name)


def _serve_upstream_attributes(module) -> None:
    if module.__dict__.get("_serves_upstream_names"):
        return
    earlier = module.__dict__.get("__getattr__")

    def __getattr__(name: str):
        if earlier is not None:
            try:
                return earlier(name)
            except AttributeError:
                pass
        own = own_spelling(name)
        if own != name:
            if own in module.__dict__:
                return module.__dict__[own]
            if earlier is not None:
                return earlier(own)
        raise AttributeError(f"module {module.__name__!r} has no attribute {name!r}")

    module.__getattr__ = __getattr__
    module._serves_upstream_names = True


class _UpstreamAlias(_Alias):
    def create_module(self, spec):
        module = super().create_module(spec)
        _serve_upstream_attributes(module)
        return module


class UpstreamNames(Finder):
    def find_spec(self, fullname, path=None, target=None):
        spec = super().find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, _Alias):
            spec.loader = _UpstreamAlias(spec.loader.target)
        return spec


class _Environ(type(os.environ)):
    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            if isinstance(key, str) and key.startswith(f"{_UPSTREAM.upper()}_"):
                return super().__getitem__(own_spelling(key))
            raise


def install(meta_path: list) -> None:
    """Idempotent; a no-op on a build that kept the upstream name."""
    if _OWN == _UPSTREAM or any(isinstance(finder, UpstreamNames) for finder in meta_path):
        return
    meta_path.insert(0, UpstreamNames(_TopLevelNames()))
    if not isinstance(os.environ, _Environ):
        os.environ.__class__ = _Environ
