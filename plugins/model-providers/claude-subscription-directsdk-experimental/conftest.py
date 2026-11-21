"""Test bootstrap and shared fixture.

The standalone plugin imports Hermes core (`providers`, `agent`) from a checkout: set
``HERMES_AGENT_REPO`` to your hermes-agent clone (default ``~/.hermes/hermes-agent``).

``profile`` loads this repo exactly as `hermes plugins install` leaves it —
``$HERMES_HOME/plugins/<name>/`` with ``kind: model-provider`` — through core's provider discovery.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

HERMES_AGENT_REPO = Path(os.environ.get("HERMES_AGENT_REPO") or Path.home() / ".hermes" / "hermes-agent").expanduser()
REPO_ROOT = Path(__file__).resolve().parent
PLUGIN_NAME = "claude-subscription-directsdk-experimental"
for path in (str(HERMES_AGENT_REPO), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import host

collect_ignore = ["__init__.py", "host.py", "directsdk.py", "directsdk_setup.py", "admission.py", "model_catalog.py",
                  "inert_mcp.py", "evals"]


def host_name() -> str:
    """Return the active core's short name (`hermes`, or a rebranded build's own name)."""
    return host._name()


def user_provider_prefix() -> str:
    return f"_{host_name()}_user_provider_"


def isolate_home(monkeypatch, home: Path) -> None:
    """Point the active core at *home* (both HERMES_HOME and any rebranded <HOST>_HOME) and fail closed."""
    home.mkdir(parents=True, exist_ok=True)
    name = host_name()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(f"{name.upper()}_HOME", str(home))
    try:
        import hermes_constants
        if hasattr(hermes_constants, "reset_hermes_home_key_cache"):
            hermes_constants.reset_hermes_home_key_cache()
    except ImportError:
        pass
    try:
        from hermes_cli.config import get_config_path
        resolved_cfg = get_config_path().resolve()
        assert resolved_cfg.parent == home.resolve(), (
            f"Refusing to run tests outside isolated home: get_config_path()={resolved_cfg}, expected under {home.resolve()}"
        )
    except ImportError:
        pass
    prefixes = ("_hermes_user_provider_", f"_{name}_user_provider_")
    for mod in tuple(sys.modules):
        if mod.startswith(prefixes):
            monkeypatch.delitem(sys.modules, mod)
    try:
        import providers
        if hasattr(providers, "_HOME_LAYERS"):
            monkeypatch.setattr(providers, "_HOME_LAYERS", {})
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _isolate_default_home(tmp_path, monkeypatch):
    """Ensure no test can ever read or overwrite the user's real agent config directory."""
    isolate_home(monkeypatch, tmp_path / "isolated-home")


@pytest.fixture
def profile(tmp_path, monkeypatch):
    import providers

    home = tmp_path / "hermes-home"
    installed = home / "plugins" / PLUGIN_NAME
    shutil.copytree(REPO_ROOT, installed, ignore=shutil.ignore_patterns(".git", "tests", "evals", "__pycache__"))
    isolate_home(monkeypatch, home)
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    # A machine with Claude Code installed must not leak it into offline tests.
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", str(tmp_path / "no-such-claude"))
    monkeypatch.setattr(providers, "_REGISTRY", {})
    monkeypatch.setattr(providers, "_ALIASES", {})
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    monkeypatch.setattr(providers, "_discovered", False)
    providers._discover_providers()
    result = providers.get_provider_profile(PLUGIN_NAME)
    assert result is not None, "installed-plugin discovery (step 2b) did not register the provider"
    return result
