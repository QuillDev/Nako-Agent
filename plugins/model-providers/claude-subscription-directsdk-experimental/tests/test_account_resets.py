"""Banked Claude usage resets (`cedar_ember`): reporting per account and redeeming grants."""
from datetime import datetime, timezone
import json
import subprocess
import sys
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

PROVIDER = "claude-subscription-directsdk-experimental"
LIMITS = {"limits": [
    {"kind": "session", "percent": 20, "resets_at": "2026-11-25T15:00:00Z"},
    {"kind": "weekly_all", "percent": 42, "resets_at": "2026-12-01T23:00:00Z"},
    {"kind": "weekly_model", "percent": 13, "resets_at": "2026-12-01T23:00:00Z", "scope": {"model": {"display_name": "Fable"}}},
]}
CEDAR_EMBER_MULTI = {
    "eligible": True,
    "ineligible_reason": None,
    "at_limit": False,
    "exhausted": [],
    "next_grant_id": "full-reset-2611",
    "grants": [
        {
            "id": "full-reset-2611",
            "label": "One full limit reset",
            "resets_total": 1,
            "resets_left": 1,
            "ends_at": "2026-12-10T22:00:00+00:00",
            "clears": ["seven_day", "seven_day_overage_included"],
            "paused": False,
            "usable_now": True,
            "type": "weekly",
            "gives_back_weekly": False,
        },
        {
            "id": "launch-2610-fable",
            "label": "Fable reset 1",
            "resets_total": 1,
            "resets_left": 1,
            "ends_at": "2027-03-09T07:59:00+00:00",
            "clears": ["seven_day_overage_included"],
            "paused": False,
            "usable_now": True,
            "type": "full",
            "gives_back_weekly": True,
        },
        {
            "id": "launch-2610-fable-6mo",
            "label": "Fable reset 2",
            "resets_total": 1,
            "resets_left": 1,
            "ends_at": "2027-03-09T07:59:00+00:00",
            "clears": ["seven_day_overage_included"],
            "paused": False,
            "usable_now": True,
            "type": "full",
            "gives_back_weekly": True,
        },
        {
            "id": "expired-grant",
            "resets_left": 1,
            "ends_at": "2020-01-01T00:00:00+00:00",
            "paused": False,
            "usable_now": True,
            "type": "weekly",
        },
        {
            "id": "paused-grant",
            "resets_left": 1,
            "ends_at": "2027-01-01T00:00:00+00:00",
            "paused": True,
            "usable_now": False,
            "type": "session",
        },
    ],
}


def _write_login(directory, *, token="tok-1", org="org-1111"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8")
    (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": org}}), encoding="utf-8")


def _usage_reply(rate_limits=LIMITS):
    import host
    return json.dumps({
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": f"{host.NAME}-usage",
            "response": {"subscription_type": "max", "rate_limits_available": True, "rate_limits": rate_limits},
        },
    })


def test_summarize_resets_groups_kinds_and_skips_paused_and_expired():
    import resets

    now = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
    summary = resets.summarize_resets(CEDAR_EMBER_MULTI, account="work@example.test", now=now)
    assert summary == {
        "available": 3,
        "usable": True,
        "account": "work@example.test",
        "options": [
            {"id": "full-reset-2611", "label": "weekly reset", "available": 1, "usable": True,
             "expires_at": "2026-12-10T22:00:00+00:00"},
            {"id": "launch-2610-fable", "label": "Fable reset", "available": 2, "usable": True,
             "expires_at": "2027-03-09T07:59:00+00:00"},
        ],
    }

    assert resets.summarize_resets({"eligible": False, "grants": CEDAR_EMBER_MULTI["grants"]}, now=now) is None
    assert resets.summarize_resets({"eligible": True, "grants": []}, now=now) is None


def test_linked_usage_surfaces_resets_per_account_and_redeem_invalidates_cache(profile, tmp_path, monkeypatch):
    import providers
    import resets
    from hermes_cli.config import get_config_path

    first, second = tmp_path / "acct-a", tmp_path / "acct-b"
    _write_login(first, token="tok-a", org="org-aaaa")
    _write_login(second, token="tok-b", org="org-bbbb")
    get_config_path().write_text(json.dumps({"plugins": {"enabled": [], "entries": {PROVIDER: {"settings": {
        "accounts": [
            {"label": "alpha@example.test", "config_dir": str(first)},
            {"label": "beta@example.test", "config_dir": str(second)},
        ],
    }}}}}), encoding="utf-8")

    usage_calls = []

    def fake_run(argv, **kwargs):
        if "--version" in argv:
            return SimpleNamespace(stdout="2.1.322 (Claude Code)\n", stderr="", returncode=0)
        usage_calls.append(kwargs["env"].get("CLAUDE_CONFIG_DIR"))
        return SimpleNamespace(stdout=_usage_reply() + "\n", returncode=0)

    http_calls = []
    state = {"alpha_left": 1}

    class FakeResp:
        def __init__(self, payload):
            self._data = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        auth = req.get_header("Authorization")
        beta = req.get_header("Anthropic-beta")
        ua = req.get_header("User-agent")
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        http_calls.append((method, url, auth, beta, ua, body))
        assert beta == "oauth-2025-04-20"
        assert ua.startswith("claude-cli/") and "(external, cli)" in ua
        if method == "GET":
            assert url == "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
            if auth == "Bearer tok-a":
                grants = [dict(CEDAR_EMBER_MULTI["grants"][0], resets_left=state["alpha_left"], usable_now=state["alpha_left"] > 0)]
                return FakeResp({"cedar_ember": {"eligible": True, "next_grant_id": "full-reset-2611", "grants": grants}})
            return FakeResp({"cedar_ember": {"eligible": False, "ineligible_reason": "no_grant", "grants": []}})
        assert method == "POST" and url == "https://api.anthropic.com/api/organizations/org-aaaa/reset_rate_limits"
        assert body["program"] == "cedar_ember" and body["grant_id"] == "full-reset-2611"
        assert body["request_id"]
        state["alpha_left"] = 0
        return FakeResp({"result": "reset", "resets_left": 0, "cleared": ["seven_day"]})

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", sys.executable)

    prof = providers.get_provider_profile(PROVIDER)
    by_acct = dict(prof.fetch_account_usage_by_account())
    assert by_acct["alpha@example.test"].raw == {"usage_resets": {
        "available": 1,
        "usable": True,
        "account": "alpha@example.test",
        "options": [{"id": "full-reset-2611", "label": "weekly reset", "available": 1, "usable": True,
                     "expires_at": "2026-12-10T22:00:00+00:00"}],
    }}
    assert by_acct["beta@example.test"].raw is None

    # Redeem the grant for alpha@example.test and ensure the pool cache is invalidated.
    outcome = prof.redeem_account_reset(account="alpha@example.test", grant="full-reset-2611")
    assert outcome == {"status": "reset", "redeemed": True, "message": "Limits reset.", "available": 0}

    before_count = len(usage_calls)
    refreshed = dict(prof.fetch_account_usage_by_account())
    assert len(usage_calls) == before_count + 1
    assert refreshed["alpha@example.test"].raw is None


def test_reset_check_runs_every_five_minutes_and_waits_ten_after_a_refusal(profile, tmp_path, monkeypatch):
    from email.message import Message
    import time
    import providers
    from hermes_cli.config import get_config_path

    login = tmp_path / "acct-a"
    _write_login(login, token="tok-a", org="org-aaaa")
    get_config_path().write_text(json.dumps({"plugins": {"enabled": [], "entries": {PROVIDER: {"settings": {
        "accounts": [{"label": "alpha@example.test", "config_dir": str(login)}],
    }}}}}), encoding="utf-8")

    def fake_run(argv, **kwargs):
        if "--version" in argv:
            return SimpleNamespace(stdout="2.1.322 (Claude Code)\n", stderr="", returncode=0)
        return SimpleNamespace(stdout=_usage_reply() + "\n", returncode=0)

    checks = []
    refuse = {"now": False, "retry_after": None}

    class FakeResp:
        def __init__(self, payload):
            self._data = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        checks.append(req.full_url)
        if refuse["now"]:
            headers = Message()
            if refuse["retry_after"] is not None:
                headers["Retry-After"] = str(refuse["retry_after"])
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", headers, None)
        return FakeResp({"cedar_ember": {"eligible": True, "grants": [CEDAR_EMBER_MULTI["grants"][0]]}})

    clock = {"now": 10_000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", sys.executable)
    prof = providers.get_provider_profile(PROVIDER)

    def shown_resets(at):
        clock["now"] = 10_000.0 + at
        raw = dict(prof.fetch_account_usage_by_account())["alpha@example.test"].raw
        return raw and raw["usage_resets"]["available"]

    # Usage polls every 60s, but reset checks wait 5 minutes (300s) between healthy calls.
    assert shown_resets(0) == 1 and len(checks) == 1
    assert shown_resets(61) == 1 and shown_resets(240) == 1 and len(checks) == 1
    # Past 300s (and past the 60s usage TTL since 240s), a 429 keeps showing the last good answer
    # and backs off for 10 minutes (600s -> until 901s).
    refuse["now"] = True
    assert shown_resets(301) == 1 and len(checks) == 2
    assert shown_resets(600) == 1 and shown_resets(840) == 1 and len(checks) == 2
    # Past 901s, a 429 with Retry-After: 900 backs off until 1802s while keeping the last good answer.
    refuse["retry_after"] = 900
    assert shown_resets(902) == 1 and len(checks) == 3
    assert shown_resets(1740) == 1 and len(checks) == 3
    # Past 1802s, the next poll checks Anthropic again and succeeds.
    refuse["now"] = False
    assert shown_resets(1803) == 1 and len(checks) == 4


def test_redeem_handles_errors_and_missing_accounts(profile, tmp_path, monkeypatch):
    import providers
    import resets
    from hermes_cli.config import get_config_path

    first = tmp_path / "acct-a"
    _write_login(first, token="tok-a", org="org-aaaa")
    get_config_path().write_text(json.dumps({"plugins": {"enabled": [], "entries": {PROVIDER: {"settings": {
        "accounts": [{"label": "alpha@example.test", "config_dir": str(first)}],
    }}}}}), encoding="utf-8")

    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", sys.executable)
    prof = providers.get_provider_profile(PROVIDER)

    with pytest.raises(ValueError, match="Account not found"):
        prof.redeem_account_reset(account="missing@example.test")

    class FakeResp:
        def __init__(self, payload):
            self._data = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: FakeResp({"cedar_ember": {"eligible": True, "grants": [
        {"id": "full-reset-2611", "resets_left": 1, "usable_now": False, "paused": False, "type": "weekly"}
    ]}}))
    unusable = prof.redeem_account_reset(account="alpha@example.test", grant="full-reset-2611")
    assert unusable["redeemed"] is False and unusable["available"] == 1

    def raise_429(req, timeout=None):
        if req.get_method() == "GET":
            return FakeResp({"cedar_ember": CEDAR_EMBER_MULTI})
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", raise_429)
    limited = prof.redeem_account_reset(account="alpha@example.test", grant="launch-2610-fable")
    assert limited["redeemed"] is False and limited["status"] == "rate_limited"
