"""Bitwarden Password Manager logins as a vault backend (``bw`` CLI).

This is the personal/org *password* vault (``bw``), distinct from the
Bitwarden Secrets Manager (``bws``) source that hydrates API keys at startup.
Unlock: ``bw unlock --raw --passwordenv VAR`` (the CLI rejects a piped password) mints a
``BW_SESSION`` token. List: ``bw list items`` filtered to type=1 (login) with
a URI. Resolve: ``bw get password <id>``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from agent.secret_sources.base import run_cli, scrub_ansi
from agent.vault_backends import unlock as _unlock
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_secret_env
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_ENV_KEEP = ("PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot",
             "TMPDIR", "TMP", "TEMP", "XDG_CONFIG_HOME", "BITWARDENCLI_APPDATA_DIR")


def _b64url_bytes(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _std_b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _credential_id_bytes(cred_id: str) -> bytes:
    # Bitwarden stores ids of passkeys it created as GUIDs, imported ones as "b64.<base64url>".
    return _b64url_bytes(cred_id[4:]) if cred_id.startswith("b64.") else uuid.UUID(cred_id).bytes


def _passkey_to_cdp(cred: dict) -> dict:
    """A Bitwarden ``fido2Credentials`` entry (PKCS#8 P-256 key) as a CDP ``WebAuthn.Credential``."""
    out = {"credentialId": _std_b64(_credential_id_bytes(cred["credentialId"])),
           "isResidentCredential": str(cred.get("discoverable")).lower() == "true",
           "rpId": cred["rpId"], "privateKey": _std_b64(_b64url_bytes(cred["keyValue"])),
           "signCount": int(cred.get("counter") or 0), "backupEligibility": True, "backupState": True}
    if cred.get("userHandle"):
        out["userHandle"] = _std_b64(_b64url_bytes(cred["userHandle"]))
    for key in ("userName", "userDisplayName"):
        if cred.get(key):
            out[key] = cred[key]
    return out


class BitwardenLoginBackend(LoginBackend):
    name = "bitwarden"
    display_name = "Bitwarden"
    prefix = "bw:"
    needs_unlock = True

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}

    def _bw(self) -> Path:
        from hermes_cli.profiles_run_elsewhere import run_elsewhere_refusal
        if (refused := run_elsewhere_refusal()) is not None:  # that backend writes the binary_path named here
            raise RuntimeError(refused)
        explicit = str(self.cfg.get("binary_path") or "")
        found = explicit or shutil.which("bw")
        if not found:
            raise RuntimeError("Bitwarden CLI (bw) not found — install it or set vault.bitwarden.binary_path")
        return Path(found)

    def _env(self, session_token: Optional[str]) -> dict[str, str]:
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        env["NO_COLOR"] = "1"
        if appdata := str(self.cfg.get("appdata_dir") or ""):
            env["BITWARDENCLI_APPDATA_DIR"] = os.path.expanduser(appdata)
        if session_token:
            env["BW_SESSION"] = session_token
        return env

    def _password_command(self) -> list[str]:
        """``vault.bitwarden.master_password_command``: an argv whose stdout is the master password, so the
        vault unlocks itself where nobody can type it (cron, headless). Never run through a shell."""
        cmd = self.cfg.get("master_password_command") or []
        return [str(part) for part in cmd] if isinstance(cmd, list) else []

    def is_unlocked(self) -> bool:
        return bool(self._password_command()) or _unlock.is_unlocked(self.name)

    def _unlock_unattended(self) -> None:
        cmd = self._password_command()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=_TIMEOUT, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Bitwarden master_password_command failed to run: {type(exc).__name__}") from exc
        master = (proc.stdout or "").rstrip("\r\n")
        if proc.returncode != 0 or not master:
            raise RuntimeError(f"Bitwarden master_password_command exited {proc.returncode} without a password")
        try:
            self.unlock(master)
        finally:
            del master
        # A fresh session reads the local cache; pull edits made elsewhere (best effort).
        run_cli([str(self._bw()), "sync", "--nointeraction"], env=self._env(_unlock.get_session_token(self.name)),
                timeout=_TIMEOUT, label="bw", timeout_message="bw sync timed out")

    def _allowed(self, origins: tuple) -> bool:
        """``vault.bitwarden.allowed_origins`` limits which logins are offered at all (unset: every login)."""
        allowed = self.cfg.get("allowed_origins")
        if not isinstance(allowed, list):
            return True
        wanted = {normalize_origin(str(o)) for o in allowed}
        return any(o in wanted for o in origins)

    def unlock(self, master_password: str) -> None:
        # bw refuses a piped password ("Master password is required"); its non-interactive contract is
        # --passwordenv: the variable exists only in the child's environment, never in argv or ours.
        generation = _unlock.begin_unlock(self.name)
        proc = run_with_secret_env([str(self._bw()), "unlock", "--raw", "--nointeraction", "--passwordenv", "HERMES_BW_MASTER"],
                                   env=self._env(None), secret_env="HERMES_BW_MASTER", secret=master_password,
                                   timeout=_TIMEOUT, label="bw")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            err = scrub_ansi(proc.stderr or "").strip()[:200]
            if "not logged in" in err.lower():
                err = "not logged in — run `bw login` once in a terminal first"
            raise RuntimeError(f"Bitwarden unlock failed: {err or 'no session key'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("Bitwarden was locked while unlocking; try again")

    def _run(self, *args: str, input: Optional[str] = None) -> str:
        unlocked_here = False
        token = _unlock.get_session_token(self.name)
        if not token and self._password_command():
            self._unlock_unattended()
            unlocked_here = True
            token = _unlock.get_session_token(self.name)
        if not token:
            raise UnlockRequired(self)
        argv = [str(self._bw()), *args, "--nointeraction"]
        while True:
            if input is None:
                proc = run_cli(argv, env=self._env(token), timeout=_TIMEOUT, label="bw", timeout_message="bw timed out",
                               stdin=subprocess.DEVNULL)
            else:
                # Item payloads carry key material: stdin only, never argv.
                try:
                    proc = subprocess.run(argv, env=self._env(token), input=input, capture_output=True,
                                          text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT, check=False)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("bw timed out") from exc
            if proc.returncode == 0:
                return proc.stdout or ""
            err = scrub_ansi(proc.stderr or "")
            if "locked" in err.lower() or "session" in err.lower():
                _unlock.lock(self.name)
                if not unlocked_here and self._password_command():
                    self._unlock_unattended()
                    unlocked_here = True
                    token = _unlock.get_session_token(self.name)
                    if token:
                        continue
                raise UnlockRequired(self)
            raise RuntimeError(f"bw failed: {err[:200]}")

    def list_items(self) -> list[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        raw = json.loads(self._run("list", "items") or "[]")
        out: list[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            if item.get("type") != 1 or not isinstance(item.get("login"), dict):
                continue
            login = item["login"]
            origins: list[str] = []
            for uri in login.get("uris") or []:
                if uri.get("match") == 5:  # Bitwarden URI match "Never": not a fill target
                    continue
                try:
                    origin = normalize_origin(str(uri.get("uri") or ""))
                except Exception:
                    continue
                if origin and origin not in origins:
                    origins.append(origin)
            if not origins:
                continue
            username = str(login.get("username") or "").strip() or None
            # Fill targets are browser pages, so app URIs (androidapp:// etc.) never widen
            # the fill set; an app-URI-only item keeps its single origin exactly as before.
            web_origins = tuple(o for o in origins if o.startswith(("http://", "https://"))) or (origins[0],)
            if not self._allowed(web_origins):
                continue
            out.append(VaultItemMeta(
                id=f"{self.prefix}{item.get('id')}", kind="login", label=str(item.get("name") or origins[0]),
                origin=origins[0], created_at=str(item.get("creationDate") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=web_origins,
                passkey_rp_ids=tuple(dict.fromkeys(str(c.get("rpId")) for c in login.get("fido2Credentials") or []
                                                   if c.get("rpId")))))
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    def resolve_password(self, handle: str) -> str:
        return self._run("get", "password", handle[len(self.prefix):]).rstrip("\r\n")

    def _item(self, handle: str) -> dict:
        return json.loads(self._run("get", "item", handle[len(self.prefix):]))

    def resolve_passkeys(self, handle: str, rp_id: str) -> list[dict]:
        login = self._item(handle).get("login") or {}
        target_rp = rp_id.lower()
        return [_passkey_to_cdp(c) for c in login.get("fido2Credentials") or []
                if str(c.get("rpId") or "").lower() == target_rp]

    def record_passkey_counters(self, handle: str, counters: dict[str, int]) -> None:
        item = self._item(handle)
        changed = False
        for cred in (item.get("login") or {}).get("fido2Credentials") or []:
            count = counters.get(_std_b64(_credential_id_bytes(cred["credentialId"])))
            if count is not None and count > int(cred.get("counter") or 0):
                cred["counter"] = str(count)  # bw stores the counter as a string
                changed = True
        if changed:
            encoded = base64.b64encode(json.dumps(item).encode()).decode()
            self._run("edit", "item", item["id"], input=encoded)

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `bw get totp <id>` mints the current code from the item's TOTP seed; "No TOTP available" otherwise.
        try:
            code = self._run("get", "totp", handle[len(self.prefix):]).strip()
        except Exception:
            return None
        return code if code.isdigit() else None
