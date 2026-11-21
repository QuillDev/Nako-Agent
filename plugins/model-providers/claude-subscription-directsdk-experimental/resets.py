"""Banked usage resets (`cedar_ember`) for Claude Pro/Max accounts.

Claude Code's `get_usage` control request omits the `cedar_ember` grant block and `/limit-reset` is
interactive-only, so this module reads the login directory's OAuth credentials to query
`/api/oauth/usage?cedar_ember=1&skip_spend=1` and claim a grant through
`POST /api/organizations/{org_uuid}/reset_rate_limits`. Missing or unreadable credentials degrade
to `None` without affecting usage windows or ever touching Hermes core's credential-adoption hooks.
"""
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

try:
    from .directsdk_setup import _child_env, _resolve, apply_config_dir, fetch_usage, telemetry_enabled
except ImportError:
    from directsdk_setup import _child_env, _resolve, apply_config_dir, fetch_usage, telemetry_enabled

API_ORIGIN = "https://api.anthropic.com"
OAUTH_BETA = "oauth-2025-04-20"
PROGRAM = "cedar_ember"
DEFAULT_CLI_VERSION = "2.1.322"

_GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_ORG_UUID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")

_KIND_ORDER = ("session", "weekly", "fable", "full")
_KIND_LABELS = {
    "session": "5-hour reset",
    "weekly": "weekly reset",
    "fable": "Fable reset",
    "full": "full reset",
}

# Anthropic rate-limits the reset check far below the usage-window poll, and banked resets change
# rarely: ask at most every five minutes per login, and wait ten after a refusal or failure.
CHECK_SECONDS = 300
RETRY_SECONDS = 600

_version_cache: dict[str, str] = {}
_version_lock = threading.Lock()
_checks: dict[str, tuple[float, dict | None]] = {}
_checks_lock = threading.Lock()


def _parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _single_directory(env):
    child = dict(env)
    apply_config_dir(child)
    return os.path.expanduser(child.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def _cli_version(command=None, env=None):
    resolved = _resolve(command, env if env is not None else os.environ)
    exe = resolved[0] if resolved else ""
    if not exe:
        return DEFAULT_CLI_VERSION
    with _version_lock:
        cached = _version_cache.get(exe)
    if cached:
        return cached
    version = DEFAULT_CLI_VERSION
    try:
        run = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=3)
        match = _VERSION_RE.search((run.stdout or "") + " " + (run.stderr or ""))
        if match:
            version = match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    with _version_lock:
        _version_cache[exe] = version
    return version


def _headers(token, command=None, env=None):
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Content-Type": "application/json",
        "User-Agent": f"claude-cli/{_cli_version(command, env)} (external, cli)",
    }


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_credentials(config_dir):
    """Return `(access_token, org_uuid)` from a Claude Code config directory, or `(None, None)`."""
    if not config_dir:
        return None, None
    directory = os.path.expanduser(config_dir)
    creds = _read_json(os.path.join(directory, ".credentials.json"))
    oauth = creds.get("claudeAiOauth")
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    token = token.strip() if isinstance(token, str) and token.strip() else None

    config = _read_json(os.path.join(directory, ".claude.json"))
    account = config.get("oauthAccount")
    org_uuid = account.get("organizationUuid") if isinstance(account, dict) else None
    org_uuid = org_uuid.strip() if isinstance(org_uuid, str) and org_uuid.strip() else None
    return token, org_uuid


def _quiet(env, config_dir):
    if not telemetry_enabled():
        return True
    child = _child_env(env, config_dir=config_dir)
    return bool(child.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"))


def _http_json(method, path, *, token, body=None, command=None, env=None, timeout=5):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API_ORIGIN + path, data=data, headers=_headers(token, command, env), method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {}


def _retry_after(exc):
    headers = getattr(exc, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    try:
        return max(0.0, float(raw)) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def fetch_cedar_ember(config_dir=None, *, command=None, env=None, timeout=5):
    """Return the raw `cedar_ember` block from Anthropic's OAuth usage endpoint, else `None`."""
    env = dict(env if env is not None else os.environ)
    directory = config_dir or _single_directory(env)
    if _quiet(env, directory):
        return None
    token, _ = _read_credentials(directory)
    if not token:
        return None
    key = os.path.normcase(os.path.realpath(os.path.expanduser(directory)))
    with _checks_lock:
        next_at, last = _checks.get(key, (-float("inf"), None))
    if time.monotonic() < next_at:
        return last
    try:
        payload = _http_json("GET", "/api/oauth/usage?cedar_ember=1&skip_spend=1",
                             token=token, command=command, env=env, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        wait = max(RETRY_SECONDS, _retry_after(exc))
        with _checks_lock:
            _checks[key] = (time.monotonic() + wait, last)
        return last
    block = payload.get("cedar_ember")
    block = block if isinstance(block, dict) else None
    with _checks_lock:
        _checks[key] = (time.monotonic() + CHECK_SECONDS, block)
    return block


def forget_cedar_ember(config_dir):
    """Drop a login's remembered reset check so the next read asks Anthropic again."""
    if not config_dir:
        return
    key = os.path.normcase(os.path.realpath(os.path.expanduser(config_dir)))
    with _checks_lock:
        _checks.pop(key, None)


def _grant_kind(grant):
    if grant.get("gives_back_weekly") is True:
        return "fable"
    raw_type = grant.get("type")
    if raw_type in ("session", "weekly", "full"):
        return raw_type
    clears = grant.get("clears") if isinstance(grant.get("clears"), list) else []
    has_session = "five_hour" in clears
    has_other = any(isinstance(c, str) and c != "five_hour" for c in clears)
    if has_session and not has_other:
        return "session"
    if has_other and not has_session:
        return "weekly"
    return "full"


def _active_grants(cedar_ember, now):
    if not isinstance(cedar_ember, dict) or cedar_ember.get("eligible") is not True:
        return []
    raw_grants = cedar_ember.get("grants")
    if not isinstance(raw_grants, list):
        return []
    out = []
    for grant in raw_grants:
        if not isinstance(grant, dict):
            continue
        gid = grant.get("id")
        if not isinstance(gid, str) or not _GRANT_ID_RE.match(gid):
            continue
        if grant.get("paused") is True:
            continue
        ends_at_raw = grant.get("ends_at")
        ends_at = _parse_iso(ends_at_raw)
        if ends_at is not None and ends_at <= now:
            continue
        left = grant.get("resets_left")
        if isinstance(left, bool) or not isinstance(left, (int, float)) or int(left) <= 0:
            continue
        out.append({
            "id": gid,
            "kind": _grant_kind(grant),
            "resets_left": int(left),
            "usable_now": grant.get("usable_now") is True,
            "ends_at": ends_at_raw if ends_at is not None else None,
            "_ends_dt": ends_at,
        })
    return out


def summarize_resets(cedar_ember, *, account="", now=None):
    """Turn a `cedar_ember` block into the portal-facing `{available, usable, options, account?}` summary."""
    now = now or datetime.now(timezone.utc)
    grants = _active_grants(cedar_ember, now)
    if not grants:
        return None
    next_id = cedar_ember.get("next_grant_id") if isinstance(cedar_ember, dict) else None

    grouped: dict[str, list[dict]] = {}
    for grant in grants:
        grouped.setdefault(grant["kind"], []).append(grant)

    options = []
    for kind in _KIND_ORDER:
        items = grouped.get(kind)
        if not items:
            continue
        available = sum(g["resets_left"] for g in items)
        usable_items = [g for g in items if g["usable_now"]]
        preferred = next((g for g in usable_items if g["id"] == next_id), None)
        target = preferred or (usable_items[0] if usable_items else items[0])
        dated = [g for g in items if g["_ends_dt"] is not None]
        earliest = min(dated, key=lambda g: g["_ends_dt"])["ends_at"] if dated else None
        option = {
            "id": target["id"],
            "label": _KIND_LABELS[kind],
            "available": available,
            "usable": bool(usable_items),
        }
        if earliest:
            option["expires_at"] = earliest
        options.append(option)

    if not options:
        return None
    summary = {
        "available": sum(o["available"] for o in options),
        "usable": any(o["usable"] for o in options),
        "options": options,
    }
    if account:
        summary["account"] = account
    return summary


def _resolve_target_dir(account, env):
    try:
        from .account_pool import configured_accounts
    except ImportError:
        from account_pool import configured_accounts

    accounts = configured_accounts()
    wanted = str(account or "").strip()
    if accounts:
        if not wanted:
            if len(accounts) == 1:
                return accounts[0].label, accounts[0].config_dir
            raise ValueError("Pick a Claude account to reset")
        for entry in accounts:
            if entry.label == wanted:
                return entry.label, entry.config_dir
        lowered = wanted.lower()
        for entry in accounts:
            if entry.label.lower() == lowered:
                return entry.label, entry.config_dir
        raise ValueError("Account not found")

    if wanted and wanted.lower() != "default":
        raise ValueError("Account not found")
    return "", _single_directory(env)


def _resolve_org_uuid(config_dir, command, env):
    _, org_uuid = _read_credentials(config_dir)
    if org_uuid:
        return org_uuid
    try:
        from . import logins
    except ImportError:
        import logins
    resolved = _resolve(command, env)
    if not resolved:
        return None
    status = logins._status(resolved, env, config_dir)
    org = str(status.get("org") or "").strip()
    return org or None


def _invalidate_pool(config_dir):
    try:
        from .account_pool import invalidate_account
    except ImportError:
        from account_pool import invalidate_account
    invalidate_account(config_dir)


def _fetch_with_refresh(config_dir, command, env, timeout):
    token, _ = _read_credentials(config_dir)
    if not token:
        return None, None
    try:
        payload = _http_json("GET", "/api/oauth/usage?cedar_ember=1&skip_spend=1",
                             token=token, command=command, env=env, timeout=timeout)
        return payload.get("cedar_ember"), token
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
    # Let Claude Code refresh its own OAuth token, then re-read.
    fetch_usage(command=command, env=env, timeout=8, config_dir=config_dir)
    token, _ = _read_credentials(config_dir)
    if not token:
        return None, None
    payload = _http_json("GET", "/api/oauth/usage?cedar_ember=1&skip_spend=1",
                         token=token, command=command, env=env, timeout=timeout)
    return payload.get("cedar_ember"), token


def _claim_message(result, reason):
    messages = {
        "reset": "Limits reset.",
        "already_used": "That reset was already used.",
        "not_limited": "This account isn't at a usage limit right now.",
        "cooldown": "Reset is on cooldown. Try again in a moment.",
        "ineligible": "This account isn't eligible for that reset.",
        "unavailable": "Reset isn't available right now.",
    }
    base = messages.get(result, "Could not redeem reset.")
    if result not in ("reset", "already_used") and isinstance(reason, str) and reason and reason not in ("unknown", "unavailable"):
        return f"{base} ({reason})"
    return base


def redeem_reset(account="", *, grant="", force=False, command=None, env=None, timeout=15):
    """Redeem one banked Claude reset grant (`cedar_ember`) for the named account."""
    del force  # Claude's API enforces per-grant usability (`usable_now` / `use_requires_limit`) server-side.
    env = dict(env if env is not None else os.environ)
    _, config_dir = _resolve_target_dir(account, env)
    if _quiet(env, config_dir):
        raise ValueError("Turn on Claude Code telemetry to check or use resets")

    try:
        cedar_ember, token = _fetch_with_refresh(config_dir, command, env, timeout=min(timeout, 8))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("Claude login expired. Refresh this account and try again") from exc
        if exc.code == 429:
            raise ValueError("Claude rate-limited the reset check. Try again in a moment") from exc
        raise ValueError(f"Could not check Claude resets (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise ValueError("Could not reach Claude to check resets") from exc

    if not token:
        raise ValueError("Claude login credentials not found for this account")

    now = datetime.now(timezone.utc)
    grants = _active_grants(cedar_ember, now)
    total_available = sum(g["resets_left"] for g in grants)
    if not grants:
        return {"status": "no_credits_banked", "redeemed": False,
                "message": "No banked resets available on this account.", "available": 0}

    wanted_grant = str(grant or "").strip()
    if wanted_grant:
        if not _GRANT_ID_RE.match(wanted_grant):
            raise ValueError("Invalid reset grant id")
        chosen = next((g for g in grants if g["id"] == wanted_grant), None)
        if chosen is None:
            return {"status": "not_eligible", "redeemed": False,
                    "message": "That reset is no longer available.", "available": total_available}
    else:
        next_id = cedar_ember.get("next_grant_id") if isinstance(cedar_ember, dict) else None
        usable = [g for g in grants if g["usable_now"]]
        chosen = next((g for g in usable if g["id"] == next_id), None) or (usable[0] if usable else None)
        if chosen is None:
            return {"status": "not_eligible", "redeemed": False,
                    "message": "No reset can be used right now.", "available": total_available}

    if not chosen["usable_now"]:
        return {"status": "not_eligible", "redeemed": False,
                "message": "That reset can't be used right now.", "available": total_available}

    org_uuid = _resolve_org_uuid(config_dir, command, env)
    if not org_uuid or not _ORG_UUID_RE.match(org_uuid):
        raise ValueError("No Claude organization found for this account")

    body = {"program": PROGRAM, "grant_id": chosen["id"], "request_id": str(uuid.uuid4())}
    path = f"/api/organizations/{org_uuid}/reset_rate_limits"
    try:
        reply = _http_json("POST", path, token=token, body=body, command=command, env=env, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            fetch_usage(command=command, env=env, timeout=8, config_dir=config_dir)
            token, _ = _read_credentials(config_dir)
            if not token:
                raise ValueError("Claude login expired. Refresh this account and try again") from exc
            try:
                reply = _http_json("POST", path, token=token, body=body, command=command, env=env, timeout=timeout)
            except urllib.error.HTTPError as retry_exc:
                raise ValueError(f"Could not redeem Claude reset (HTTP {retry_exc.code})") from retry_exc
        elif exc.code == 429:
            return {"status": "rate_limited", "redeemed": False,
                    "message": "Claude rate-limited the reset request. Try again in a moment.", "available": total_available}
        elif exc.code in (401, 403):
            raise ValueError("Claude login expired. Refresh this account and try again") from exc
        else:
            raise ValueError(f"Could not redeem Claude reset (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise ValueError("Could not reach Claude to redeem reset") from exc

    result = str(reply.get("result") or "unavailable")
    reason = reply.get("reason")
    redeemed = result == "reset"
    status = "already_redeemed" if result == "already_used" else result
    raw_left = reply.get("resets_left")
    if not isinstance(raw_left, bool) and isinstance(raw_left, (int, float)):
        grant_left = max(0, int(raw_left))
        remaining = max(0, total_available - chosen["resets_left"] + grant_left)
    elif redeemed:
        remaining = max(0, total_available - 1)
    elif result == "already_used":
        remaining = max(0, total_available - chosen["resets_left"])
    else:
        remaining = total_available

    if redeemed or result == "already_used":
        _invalidate_pool(config_dir)

    return {
        "status": status,
        "redeemed": redeemed,
        "message": _claim_message(result, reason),
        "available": remaining,
    }
