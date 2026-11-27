"""Behavior tests for nako_agent.compat.upstream_names (rebrand compatibility)."""
from __future__ import annotations

import importlib
import importlib.util
import sys

from nako_agent.compat import upstream_names


def test_offered_honors_explicit_own_attribute_even_when_it_matches_default(monkeypatch):
    """An explicit own-spelled False/None must not fall through to an upstream-spelled attr."""
    monkeypatch.setattr(upstream_names, "_OWN", "forkname")
    monkeypatch.setattr(
        upstream_names,
        "_SPELLINGS",
        tuple((spell(upstream_names._UPSTREAM), spell("forkname")) for spell in (str.lower, str.upper, str.capitalize)),
    )
    upstream_flag = f"SUPPORTS_{upstream_names._UPSTREAM.upper()}_TOOL_CALLS"

    client = type("Client", (), {"SUPPORTS_FORKNAME_TOOL_CALLS": False, upstream_flag: True})()
    none_override = type("NoneOverride", (), {"SUPPORTS_FORKNAME_TOOL_CALLS": None, upstream_flag: True})()
    upstream_only = type("UpstreamOnly", (), {upstream_flag: True})()

    assert upstream_names.offered(client, "SUPPORTS_FORKNAME_TOOL_CALLS", False) is False
    assert upstream_names.offered(none_override, "SUPPORTS_FORKNAME_TOOL_CALLS", None) is None
    assert upstream_names.offered(upstream_only, "SUPPORTS_FORKNAME_TOOL_CALLS", False) is True


def test_upstream_names_find_spec_does_not_eagerly_import_target_module(tmp_path, monkeypatch):
    """importlib.util.find_spec on an upstream module name must stay side-effect-free until loaded."""
    pkg = tmp_path / "forkname_lazyprobe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "IMPORTED = True\nFORKNAME_VALUE = 42\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(upstream_names, "_OWN", "forkname")
    monkeypatch.setattr(
        upstream_names,
        "_SPELLINGS",
        tuple((spell(upstream_names._UPSTREAM), spell("forkname")) for spell in (str.lower, str.upper, str.capitalize)),
    )

    finder = upstream_names.UpstreamNames(upstream_names._TopLevelNames())
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    upstream_mod_name = f"{upstream_names._UPSTREAM}_lazyprobe"
    try:
        spec = importlib.util.find_spec(upstream_mod_name)
        assert spec is not None
        assert "forkname_lazyprobe" not in sys.modules
        assert upstream_mod_name not in sys.modules

        loaded = importlib.import_module(upstream_mod_name)
        assert loaded is sys.modules["forkname_lazyprobe"]
        assert getattr(loaded, f"{upstream_names._UPSTREAM.upper()}_VALUE") == 42
    finally:
        for mod in (upstream_mod_name, "forkname_lazyprobe"):
            sys.modules.pop(mod, None)
