"""Add or refresh Claude logins from a client such as a settings page, through the official CLI.

Every login runs `claude auth login` in a fresh config directory of its own, so an attempt can
never overwrite a working login. Which account came back decides what happens next: the
account's identity (email + organization, from the CLI's own `auth status`) is compared with
every linked login. A login that is already linked has its entry pointed at the fresh
directory (refreshed, label kept); a new one is linked as another account. Tokens stay with
Claude Code: nothing here reads, copies or sends them.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time

try:
    from .directsdk_setup import PLUGIN_ID, _child_env, _plan_label, _resolve, apply_config_dir
except ImportError:
    from directsdk_setup import PLUGIN_ID, _child_env, _plan_label, _resolve, apply_config_dir

MAX_ACCOUNTS = 8
URL_WAIT = 20
CODE_WAIT = 90
ATTEMPT_TTL = 15 * 60
_URL = re.compile(r"https://\S+/oauth/authorize\?\S+")


@dataclass
class _Attempt:
    process: subprocess.Popen
    directory: str
    target: str | None
    home: str
    output: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)


_attempts: dict[str, _Attempt] = {}
_lock = threading.Lock()


def _home():
    from nako_constants import get_nako_home
    return str(get_nako_home())


def _login_root():
    """Where new logins live: inside the active Nako profile, private to this user."""
    root = os.path.join(_home(), "claude-logins")
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _command(env):
    resolved = _resolve(None, env)
    if resolved is None:
        raise ValueError("Claude Code isn't installed on this machine")
    return resolved


def _status(command, env, directory, timeout=10):
    """`{signed_in, email, org, plan}` for one config directory, from the CLI itself."""
    try:
        run = subprocess.run(command + ["auth", "status"], env=_child_env(env, directory), stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        auth = json.loads(run.stdout) if run.stdout.strip().startswith("{") else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        auth = {}
    signed_in = auth.get("loggedIn") is True
    return {"signed_in": signed_in, "email": str(auth.get("email") or "") if signed_in else "",
            "org": str(auth.get("orgId") or "") if signed_in else "", "plan": _plan_label(auth.get("subscriptionType")) if signed_in else ""}


def _identity(status):
    return (status["email"].lower(), status["org"]) if status["signed_in"] and status["email"] else None


def _remembered(directory):
    """Who last signed in to a directory, from Claude Code's own account record (no tokens)."""
    try:
        with open(os.path.join(directory, ".claude.json"), encoding="utf-8-sig") as handle:
            account = json.load(handle).get("oauthAccount") or {}
    except (OSError, ValueError, AttributeError):
        return None
    email, org = account.get("emailAddress"), account.get("organizationUuid")
    return (email.lower(), str(org or "")) if isinstance(email, str) and email else None


def _who(row, status):
    """The account a row belongs to: signed in now, or last signed in when its login lapsed."""
    return _identity(status) or _remembered(row["config_dir"])


def _raw_accounts():
    """The `accounts` setting as written, disabled entries included."""
    try:
        from .directsdk_setup import _setting
    except ImportError:
        from directsdk_setup import _setting
    rows = _setting("accounts", [])
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _single_directory(env):
    """The one login used while no accounts are linked."""
    child = dict(env)
    apply_config_dir(child)
    return os.path.expanduser(child.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def _linked(env):
    """`[{label, config_dir, enabled}]`: the linked accounts, or the single login as one row."""
    rows = _raw_accounts()
    if rows:
        return rows
    return [{"label": "", "config_dir": _single_directory(env), "enabled": True, "single": True}]


def _statuses(command, env, rows):
    with ThreadPoolExecutor(max_workers=min(8, len(rows)) or 1) as pool:
        return list(pool.map(lambda row: _status(command, env, row["config_dir"]), rows))


def account_logins(env=None):
    """Every login this provider can run on: `[{id, label, email, plan, signed_in}]`."""
    env = dict(env if env is not None else os.environ)
    command = _command(env)
    rows = _linked(env)
    out = []
    for row, status in zip(rows, _statuses(command, env, rows)):
        if row.get("single") and not status["signed_in"] and not os.path.isdir(row["config_dir"]):
            continue  # nothing was ever signed in here
        out.append({"id": row["label"] or "default", "label": row["label"] or status["email"] or "Claude",
                    "email": status["email"], "plan": status["plan"], "signed_in": status["signed_in"],
                    "enabled": row.get("enabled", True) is not False})
    return out


def _sweep():
    now = time.monotonic()
    for key, attempt in list(_attempts.items()):
        if now - attempt.started > ATTEMPT_TTL:
            _drop(key)


def _drop(key):
    attempt = _attempts.pop(key, None)
    if attempt is None:
        return
    if attempt.process.poll() is None:
        attempt.process.kill()
        attempt.process.wait(timeout=5)
    shutil.rmtree(attempt.directory, ignore_errors=True)


def start_account_login(target=None, env=None):
    """Start a sign-in. Returns `{id, url}`: open the url, then pass the code it shows to finish."""
    env = dict(env if env is not None else os.environ)
    command = _command(env)
    directory = os.path.join(_login_root(), secrets.token_hex(6))
    os.makedirs(directory, mode=0o700)
    child = _child_env(env, directory)
    child["BROWSER"] = "/bin/true"  # the page opens the link itself
    process = subprocess.Popen(command + ["auth", "login", "--claudeai"], env=child, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                               start_new_session=True)
    attempt = _Attempt(process, directory, str(target) if target else None, _home())

    def read():
        for line in process.stdout:
            attempt.output.append(line)

    threading.Thread(target=read, name="claude-login-output", daemon=True).start()
    key = secrets.token_urlsafe(12)
    with _lock:
        _sweep()
        _attempts[key] = attempt
    deadline = time.monotonic() + URL_WAIT
    while time.monotonic() < deadline:
        found = _URL.search("".join(attempt.output))
        if found:
            return {"id": key, "url": found.group(0)}
        if process.poll() is not None:
            break
        time.sleep(0.1)
    with _lock:
        _drop(key)
    raise RuntimeError("Claude Code didn't give a sign-in link")


def _pop(key):
    with _lock:
        _sweep()
        attempt = _attempts.pop(str(key), None)
    if attempt is None:
        raise ValueError("That sign-in expired. Start it again")
    if attempt.home != _home():
        with _lock:
            _attempts[str(key)] = attempt
        raise ValueError("That sign-in belongs to another profile")
    return attempt


def cancel_account_login(attempt_id):
    with _lock:
        _drop(str(attempt_id))


def finish_account_login(attempt_id, code, env=None):
    """Redeem the pasted code, then link the account or refresh the one it already is.

    Returns `{outcome: "added" | "refreshed", label, email, plan}`.
    """
    env = dict(env if env is not None else os.environ)
    code = str(code or "").strip()
    if not code:
        raise ValueError("Paste the code first")
    attempt = _pop(attempt_id)
    try:
        try:
            attempt.process.stdin.write(code + "\n")
            attempt.process.stdin.flush()
            attempt.process.stdin.close()
            attempt.process.wait(timeout=CODE_WAIT)
        except (OSError, subprocess.TimeoutExpired):
            pass
        command = _command(env)
        fresh = _status(command, env, attempt.directory)
        if not fresh["signed_in"]:
            raise ValueError("That code didn't work. Start the sign-in again")
        result = _link(command, env, attempt, fresh)
    except BaseException:
        if attempt.process.poll() is None:
            attempt.process.kill()
        shutil.rmtree(attempt.directory, ignore_errors=True)
        raise
    return result


def _link(command, env, attempt, fresh):
    rows = [{k: v for k, v in row.items() if k != "single"} for row in _linked(env)]
    statuses = _statuses(command, env, rows)
    identity = _identity(fresh)
    owners = [_who(row, status) for row, status in zip(rows, statuses)]
    same = [i for i, owner in enumerate(owners) if owner == identity]
    index = None
    if attempt.target is not None:
        target = next((i for i, row in enumerate(rows) if (row["label"] or "default") == attempt.target), None)
        # A row takes its own account back. An empty row takes a new one, but never an account
        # that is already linked elsewhere: that one is refreshed where it is.
        if target is not None and (target in same or (owners[target] is None and not same)):
            index = target
    if index is None and same:
        index = same[0]
    stale = []
    if index is not None:
        stale.append(rows[index]["config_dir"])
        rows[index]["config_dir"] = attempt.directory
        rows[index]["label"] = rows[index]["label"] or fresh["email"]
        rows[index]["enabled"] = True
        outcome, label = "refreshed", rows[index]["label"]
        # Copies of this account from before duplicates were caught go; one row per account.
        stale += [rows[i]["config_dir"] for i in same if i != index]
        rows = [row for i, row in enumerate(rows) if i == index or i not in same]
    else:
        # The single login becomes the first linked account, unless nobody is signed in to it.
        kept = [(row, status) for row, status in zip(rows, statuses) if row["label"] or status["signed_in"]]
        for row, status in kept:
            row["label"] = row["label"] or status["email"] or "primary"
        rows = [row for row, _ in kept]
        if len(rows) >= MAX_ACCOUNTS:
            raise ValueError(f"Claude can link at most {MAX_ACCOUNTS} accounts")
        taken = {row["label"] for row in rows}
        label, n = fresh["email"], 2
        while label in taken:
            label, n = f"{fresh['email']} {n}", n + 1
        rows.append({"label": label, "config_dir": attempt.directory, "enabled": True})
        outcome = "added"
    _save_accounts(rows)
    for directory in stale:
        _forget(directory)
    return {"outcome": outcome, "label": label, "email": fresh["email"], "plan": fresh["plan"]}


def _forget(directory):
    """Delete a login directory this module made, once no account points at it."""
    root = os.path.realpath(_login_root())
    if directory and os.path.realpath(directory).startswith(root + os.sep):
        shutil.rmtree(directory, ignore_errors=True)


def remove_account_login(account_id):
    """Unlink one account. Its login is deleted when this module made it; others are left alone.

    The last account can't be removed: with none linked, the provider falls back to the
    machine's own Claude Code login, which would bring an account back on its own.
    """
    rows = [dict(row) for row in _raw_accounts()]
    index = next((i for i, row in enumerate(rows) if (row.get("label") or "default") == str(account_id)), None)
    if index is None:
        raise ValueError("That account isn't linked here")
    if len(rows) == 1:
        raise ValueError("That's the only account. Add another before removing it")
    removed = rows.pop(index)
    _save_accounts(rows)
    _forget(str(removed.get("config_dir") or ""))
    return {"label": str(removed.get("label") or "")}


def _save_accounts(rows):
    from nako_cli.config import load_config, save_config
    config = load_config()
    settings = config.setdefault("plugins", {}).setdefault("entries", {}).setdefault(PLUGIN_ID, {}).setdefault("settings", {})
    settings["accounts"] = [{"label": r["label"], "config_dir": r["config_dir"], "enabled": r.get("enabled", True) is not False}
                            for r in rows]
    save_config(config)
