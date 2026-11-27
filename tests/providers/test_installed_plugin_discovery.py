"""A provider installed by ``nako plugins install`` must actually be found.

The installer clones into ``$NAKO_HOME/plugins/<name>/`` (flat), provider
discovery only scanned ``plugins/model-providers/<name>/``, and PluginManager
skips ``kind: model-provider`` on purpose — so the documented install path
reported success and registered nothing. These tests pin the join, and that
discovery keeps its hands off every other plugin in that directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
import time
from pathlib import Path

import pytest

_PROFILE_SOURCE = textwrap.dedent(
    """
    from providers import register_provider
    from providers.base import ProviderProfile

    register_provider(ProviderProfile(name="{name}", aliases=("{name}-alias",),
                                      base_url="acp://{name}", auth_type="external_process"))
    """
)


def _clear_provider_caches():
    import providers as _pkg

    _pkg._REGISTRY.clear()
    _pkg._ALIASES.clear()
    _pkg._PROVIDER_LIST_CACHE = None
    _pkg._discovered = False
    for mod in list(sys.modules):
        if mod.startswith(("plugins.model_providers", "_nako_user_provider")):
            del sys.modules[mod]


def _write_plugin(directory: Path, *, name: str, manifest: str | None):
    directory.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (directory / "plugin.yaml").write_text(manifest, encoding="utf-8")
    # Registers a provider on import, so an unwanted import is *visible*.
    (directory / "__init__.py").write_text(_PROFILE_SOURCE.format(name=name), encoding="utf-8")


@pytest.fixture
def nako_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NAKO_HOME", str(tmp_path))
    _clear_provider_caches()
    yield tmp_path
    _clear_provider_caches()


def test_flat_installed_model_provider_plugins_are_discovered_alongside_nested_ones(nako_home):
    _write_plugin(nako_home / "plugins" / "installed-acp", name="installed-acp",
                  manifest='name: installed-acp\nkind: "model-provider"\n')
    _write_plugin(nako_home / "plugins" / "model-providers" / "nested-acp", name="nested-acp",
                  manifest="name: nested-acp\nkind: model-provider\n")
    from providers import get_provider_profile

    assert get_provider_profile("installed-acp").base_url == "acp://installed-acp"
    assert get_provider_profile("installed-acp-alias") is not None
    assert get_provider_profile("nested-acp") is not None


def test_other_plugins_in_the_flat_directory_are_left_to_the_plugin_manager(nako_home):
    _write_plugin(nako_home / "plugins" / "other-standalone", name="other-standalone",
                  manifest="name: other-standalone\nkind: standalone\n")
    _write_plugin(nako_home / "plugins" / "manifestless", name="manifestless", manifest=None)
    _write_plugin(nako_home / "plugins" / "broken-manifest", name="broken-manifest",
                  manifest="kind: [this is: not valid\n")
    from providers import get_provider_profile, list_providers

    assert not [p for p in list_providers() if p.name in ("other-standalone", "manifestless", "broken-manifest")]
    assert get_provider_profile("openrouter") is not None  # bundled set still intact


def test_disabled_plugins_are_never_imported_or_registered(nako_home):
    # Flat install keys by manifest name; legacy layout keys as model-providers/<dir> (#137671).
    (nako_home / "config.yaml").write_text(
        "plugins:\n  disabled:\n    - installed-acp\n    - model-providers/nested-acp\n", encoding="utf-8")
    _write_plugin(nako_home / "plugins" / "installed-acp", name="installed-acp",
                  manifest='name: installed-acp\nkind: "model-provider"\n')
    _write_plugin(nako_home / "plugins" / "kept-acp", name="kept-acp",
                  manifest='name: kept-acp\nkind: "model-provider"\n')
    _write_plugin(nako_home / "plugins" / "model-providers" / "nested-acp", name="nested-acp",
                  manifest="name: nested-acp\nkind: model-provider\n")
    from providers import get_provider_profile, list_providers

    assert not [p for p in list_providers() if p.name in ("installed-acp", "nested-acp")]
    assert get_provider_profile("installed-acp") is None
    assert get_provider_profile("installed-acp-alias") is None  # alias registration never happened
    assert get_provider_profile("nested-acp") is None
    assert not [m for m in sys.modules if "installed_acp" in m or "nested_acp" in m]  # no import side effects
    assert get_provider_profile("openrouter") is not None  # bundled set unaffected
    assert get_provider_profile("kept-acp").base_url == "acp://kept-acp"


def test_disabled_by_manifest_name_when_directory_name_differs(nako_home):
    # A flat manifest keys the plugin by its manifest name, not its directory name.
    (nako_home / "config.yaml").write_text(
        "plugins:\n  disabled:\n    - fixture-manifest-acp\n", encoding="utf-8")
    _write_plugin(nako_home / "plugins" / "fixture-directory-acp",
                  name="fixture-manifest-acp",
                  manifest="name: fixture-manifest-acp\nkind: model-provider\n")
    from providers import get_provider_profile

    assert get_provider_profile("fixture-manifest-acp") is None
    assert not [m for m in sys.modules if "fixture_directory_acp" in m]


def test_enabling_a_disabled_plugin_finds_it_without_touching_the_plugin_dirs(nako_home, monkeypatch):
    # Desktop's Plugins page edits only config.yaml; the plugin dirs keep their settled mtimes.
    import providers

    monkeypatch.setattr(providers, "_PLUGIN_DIR_STAMP_TTL_SECONDS", 0.0)
    config = nako_home / "config.yaml"
    config.write_text("plugins:\n  disabled:\n    - installed-acp\n", encoding="utf-8")
    _write_plugin(nako_home / "plugins" / "installed-acp", name="installed-acp",
                  manifest="name: installed-acp\nkind: model-provider\n")
    settled = time.time() - 60
    os.utime(nako_home / "plugins", (settled, settled))
    assert providers.get_provider_profile("installed-acp") is None

    config.write_text("plugins:\n  disabled: []\n", encoding="utf-8")

    assert providers.get_provider_profile("installed-acp").base_url == "acp://installed-acp"


def test_optional_bundle_disable_applies_to_one_profile_and_its_installed_copy(nako_home, monkeypatch):
    from nako_constants import reset_nako_home_override, set_nako_home_override
    from providers import get_provider_profile, refresh_bound_home
    import providers

    bundled = nako_home / "bundled"
    directory = bundled / "optional-acp"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "class OptionalProfile(ProviderProfile):\n"
        "    plugin_disable_id = 'optional-acp'\n"
        "register_provider(OptionalProfile(name='example-acp', aliases=('example-alias',), "
        "base_url='acp://bundled', auth_type='external_process'))\n",
        encoding="utf-8",
    )
    # Keep real core providers in discovery as a control.
    shutil.copytree(providers._BUNDLED_PLUGINS_DIR / "openrouter", bundled / "openrouter")
    monkeypatch.setattr(providers, "_BUNDLED_PLUGINS_DIR", bundled)

    def set_disabled(home, slug=None):
        disabled = ["openrouter", slug] if slug else []
        (home / "config.yaml").write_text(f"plugins:\n  disabled: {disabled!r}\n", encoding="utf-8")

    set_disabled(nako_home, "model-providers/optional-acp")
    from nako_cli.auth_plugin_providers import registry_lookup
    from nako_cli.models_catalog_static import listed_canonical_providers

    def assert_disabled():
        assert get_provider_profile("example-acp") is None
        assert get_provider_profile("example-alias") is None
        assert registry_lookup("example-acp") is None
        assert registry_lookup("example-alias") is None
        assert registry_lookup("example-acp-alias") is None
        assert "example-acp" not in {entry.slug for entry in listed_canonical_providers()}
        assert get_provider_profile("openrouter") is not None

    assert_disabled()  # first discovery in this process is from the disabled home
    other = nako_home / "other"
    other.mkdir()
    _write_plugin(other / "plugins" / "optional-acp", name="example-acp",
                  manifest="name: optional-acp\nkind: model-provider\n")
    token = set_nako_home_override(other)
    try:
        profile = get_provider_profile("example-acp")
        assert profile is not None and profile.base_url == "acp://example-acp"
        row = registry_lookup("example-acp-alias")
        assert row is not None and row.id == "example-acp"
        assert "example-acp" in {entry.slug for entry in listed_canonical_providers()}
        set_disabled(other, "optional-acp")
        refresh_bound_home()
        assert_disabled()  # a previously loaded standalone copy cannot bypass the new setting
    finally:
        reset_nako_home_override(token)
    assert_disabled()
    set_disabled(nako_home)
    refresh_bound_home()
    profile = get_provider_profile("example-alias")
    assert profile is not None and profile.base_url == "acp://bundled"
    row = registry_lookup("example-alias")
    assert row is not None and row.inference_base_url == "acp://bundled"
    assert registry_lookup("example-acp-alias") is None
    assert "example-acp" in {entry.slug for entry in listed_canonical_providers()}
