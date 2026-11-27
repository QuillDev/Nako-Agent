"""Adding and refreshing Claude logins through the profile's account hooks.

Only Claude Code is faked: a script that answers `auth status` from its config directory and,
for `auth login`, prints a sign-in link and takes the pasted code. A code is
`<email>|<org>|<plan>` (the account the user signed in to) or `bad`.
"""
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

FAKE = textwrap.dedent('''\
    import json, os, sys
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    state = os.path.join(home, "fake-login.json")
    if sys.argv[1:3] == ["auth", "status"]:
        if os.path.exists(state):
            login = json.load(open(state))
            print(json.dumps({"loggedIn": True, "email": login["email"], "orgId": login["org"], "subscriptionType": login["plan"]}))
        else:
            print(json.dumps({"loggedIn": False, "authMethod": "none"}))
    elif sys.argv[1:3] == ["auth", "login"]:
        print("If the browser didn't open, visit: https://claude.example.test/cai/oauth/authorize?code=true&state=abc", flush=True)
        code = sys.stdin.readline().strip()
        if code == "bad":
            print("Invalid code"); sys.exit(1)
        email, org, plan = code.split("|")
        os.makedirs(home, exist_ok=True)
        json.dump({"email": email, "org": org, "plan": plan}, open(state, "w"))
    else:
        sys.exit(2)
''')


@pytest.fixture
def claude(profile, tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", str(script))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    return profile


def _signed_in(directory, email, org="org-1", plan="max"):
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fake-login.json").write_text(json.dumps({"email": email, "org": org, "plan": plan}), encoding="utf-8")


def _link_rows(*rows):
    from nako_cli.config import load_config, save_config
    config = load_config()
    config.setdefault("plugins", {}).setdefault("entries", {})["claude-subscription-directsdk-experimental"] = {"settings": {"accounts": [
        {"label": label, "config_dir": directory, "enabled": True} for label, directory in rows]}}
    save_config(config)


def _accounts():
    from nako_cli.config import load_config_readonly
    entry = load_config_readonly().get("plugins", {}).get("entries", {}).get("claude-subscription-directsdk-experimental", {})
    return (entry.get("settings") or {}).get("accounts")


def _sign_in(profile, code, target=None):
    started = profile.start_account_login(target)
    assert started["url"].startswith("https://claude.example.test/cai/oauth/authorize")
    return profile.finish_account_login(started["id"], code)


def _shown(profile):
    return {row["email"] or row["label"]: (row["label"], row["signed_in"], row["plan"]) for row in profile.account_logins()}


def test_a_new_account_is_added_and_signing_in_again_refreshes_it(claude):
    _signed_in("~/.claude", "alex@example.test")
    assert _shown(claude) == {"alex@example.test": ("alex@example.test", True, "Claude Max")}

    added = _sign_in(claude, "sam@example.test|org-2|pro")
    assert (added["outcome"], added["email"]) == ("added", "sam@example.test")
    assert _shown(claude) == {"alex@example.test": ("alex@example.test", True, "Claude Max"),
                              "sam@example.test": ("sam@example.test", True, "Claude Pro")}
    before = {row["label"]: row["config_dir"] for row in _accounts()}

    refreshed = _sign_in(claude, "alex@example.test|org-1|max")
    assert (refreshed["outcome"], refreshed["label"]) == ("refreshed", "alex@example.test")
    after = {row["label"]: row["config_dir"] for row in _accounts()}
    assert sorted(after) == ["alex@example.test", "sam@example.test"]
    assert after["sam@example.test"] == before["sam@example.test"]
    assert after["alex@example.test"] != before["alex@example.test"]
    assert _shown(claude)["alex@example.test"] == ("alex@example.test", True, "Claude Max")


def test_same_email_in_another_organization_is_a_different_account(claude):
    _signed_in("~/.claude", "alex@example.test", org="org-1")

    assert _sign_in(claude, "alex@example.test|org-9|team")["outcome"] == "added"
    assert len(_accounts()) == 2


def test_signing_in_to_a_signed_out_slot_fills_it_and_keeps_its_name(claude, tmp_path):
    _signed_in("~/.claude", "alex@example.test")
    (tmp_path / "user" / ".claude-secondary").mkdir(parents=True)
    from nako_cli.config import load_config, save_config
    config = load_config()
    config.setdefault("plugins", {}).setdefault("entries", {})["claude-subscription-directsdk-experimental"] = {"settings": {"accounts": [
        {"label": "primary", "config_dir": "~/.claude", "enabled": True},
        {"label": "secondary", "config_dir": "~/.claude-secondary", "enabled": True}]}}
    save_config(config)
    assert _shown(claude)["secondary"] == ("secondary", False, "")

    result = _sign_in(claude, "sam@example.test|org-2|pro", target="secondary")

    assert (result["outcome"], result["label"]) == ("refreshed", "secondary")
    assert [row["label"] for row in _accounts()] == ["primary", "secondary"]
    assert _shown(claude)["sam@example.test"] == ("secondary", True, "Claude Pro")


def test_signing_in_again_on_a_row_as_someone_else_adds_them_instead(claude):
    _signed_in("~/.claude", "alex@example.test")
    _sign_in(claude, "sam@example.test|org-2|pro")
    alex_dir = {row["label"]: row["config_dir"] for row in _accounts()}["alex@example.test"]

    result = _sign_in(claude, "kim@example.test|org-3|pro", target="alex@example.test")

    assert result["outcome"] == "added"
    rows = {row["label"]: row["config_dir"] for row in _accounts()}
    assert sorted(rows) == ["alex@example.test", "kim@example.test", "sam@example.test"]
    assert rows["alex@example.test"] == alex_dir


def test_a_code_that_fails_changes_nothing(claude, profile):
    _signed_in("~/.claude", "alex@example.test")
    from nako_constants import get_nako_home
    logins = Path(get_nako_home()) / "claude-logins"

    with pytest.raises(ValueError, match="didn't work"):
        _sign_in(claude, "bad")

    assert _accounts() is None
    assert list(logins.iterdir()) == []
    with pytest.raises(ValueError, match="expired"):
        claude.finish_account_login("no-such-attempt", "sam@example.test|org-2|pro")


def test_signing_in_to_an_empty_row_as_an_account_already_here_refreshes_that_account(claude, tmp_path):
    _signed_in("~/.claude", "alex@example.test")
    (tmp_path / "user" / ".claude-secondary").mkdir(parents=True)
    _link_rows(("primary", "~/.claude"), ("secondary", "~/.claude-secondary"))

    result = _sign_in(claude, "alex@example.test|org-1|max", target="secondary")

    assert (result["outcome"], result["label"]) == ("refreshed", "primary")
    assert [row["label"] for row in _accounts()] == ["primary", "secondary"]
    assert [row["email"] for row in claude.account_logins()] == ["alex@example.test", ""]


def test_a_lapsed_login_is_refreshed_not_added_again(claude, tmp_path):
    _signed_in("~/.claude", "alex@example.test")
    lapsed = tmp_path / "user" / ".claude-sam"
    lapsed.mkdir(parents=True)
    (lapsed / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "Sam@example.test", "organizationUuid": "org-2"}}), encoding="utf-8")
    _link_rows(("primary", "~/.claude"), ("sam", str(lapsed)))

    result = _sign_in(claude, "sam@example.test|org-2|pro")

    assert (result["outcome"], result["label"]) == ("refreshed", "sam")
    assert [row["label"] for row in _accounts()] == ["primary", "sam"]
    assert _shown(claude)["sam@example.test"] == ("sam", True, "Claude Pro")


def test_signing_in_again_folds_copies_of_the_same_account_into_one(claude, profile):
    _signed_in("~/.claude", "alex@example.test")
    from nako_constants import get_nako_home
    copy = Path(get_nako_home()) / "claude-logins" / "copy"
    _signed_in(copy, "alex@example.test")
    _signed_in("~/.claude-sam", "sam@example.test", org="org-2")
    _link_rows(("primary", "~/.claude"), ("secondary", str(copy)), ("sam", "~/.claude-sam"))

    result = _sign_in(claude, "alex@example.test|org-1|max")

    assert (result["outcome"], result["label"]) == ("refreshed", "primary")
    assert [row["label"] for row in _accounts()] == ["primary", "sam"]
    assert not copy.exists()
    assert (Path("~/.claude").expanduser() / "fake-login.json").exists()


def test_removing_an_account_unlinks_it_and_deletes_only_logins_made_here(claude):
    _signed_in("~/.claude", "alex@example.test")
    _sign_in(claude, "sam@example.test|org-2|pro")
    sam_dir = Path({row["label"]: row["config_dir"] for row in _accounts()}["sam@example.test"])

    assert claude.remove_account_login("sam@example.test") == {"label": "sam@example.test"}
    assert [row["label"] for row in _accounts()] == ["alex@example.test"]
    assert not sam_dir.exists()

    _sign_in(claude, "sam@example.test|org-2|pro")
    claude.remove_account_login("alex@example.test")
    assert [row["label"] for row in _accounts()] == ["sam@example.test"]
    assert (Path("~/.claude").expanduser() / "fake-login.json").exists()


def test_the_last_account_and_unknown_ones_cannot_be_removed(claude):
    _signed_in("~/.claude", "alex@example.test")
    with pytest.raises(ValueError, match="isn't linked"):
        claude.remove_account_login("default")

    _sign_in(claude, "sam@example.test|org-2|pro")
    claude.remove_account_login("sam@example.test")
    with pytest.raises(ValueError, match="only account"):
        claude.remove_account_login("alex@example.test")
    with pytest.raises(ValueError, match="isn't linked"):
        claude.remove_account_login("nobody")
    assert [row["label"] for row in _accounts()] == ["alex@example.test"]
