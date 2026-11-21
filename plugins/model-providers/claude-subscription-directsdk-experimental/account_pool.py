"""Usage-aware routing over isolated, official-CLI-managed Claude logins.

Only config-directory names and usage answers live here. Tokens remain owned by Claude Code.
The pool is shared by clients in one process, but isolated by Hermes profile and configuration.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import os
import re
import threading
import time

try:
    from .directsdk_setup import _setting, _resolve, fetch_usage, telemetry_enabled
    from .model_catalog import native_model
    from .resets import fetch_cedar_ember, forget_cedar_ember
except ImportError:
    from directsdk_setup import _setting, _resolve, fetch_usage, telemetry_enabled
    from model_catalog import native_model
    from resets import fetch_cedar_ember, forget_cedar_ember


@dataclass(frozen=True)
class Account:
    label: str
    config_dir: str


def configured_accounts():
    rows = _setting('accounts', [])
    if not isinstance(rows, list):
        raise ValueError('accounts must be a list of {label, config_dir, enabled?} objects')
    if len(rows) > 8:
        raise ValueError('accounts supports at most eight Claude logins')
    accounts, labels, directories = [], set(), set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('enabled', True), bool):
            raise ValueError('accounts entries must be objects with an optional boolean enabled')
        label, directory = row.get('label'), row.get('config_dir')
        if not isinstance(label, str) or not label.strip() or not isinstance(directory, str) or not directory.strip():
            raise ValueError('accounts entries require nonempty label and config_dir strings')
        label = label.strip()
        if any(ord(c) < 32 for c in label) or len(label) > 80:
            raise ValueError('accounts labels must be short printable names')
        directory = os.path.realpath(os.path.expanduser(directory.strip()))
        if label in labels or os.path.normcase(directory) in directories:
            raise ValueError('accounts must have unique labels and config directories')
        labels.add(label)
        directories.add(os.path.normcase(directory))
        if row.get('enabled', True):
            accounts.append(Account(label, directory))
    if rows and not accounts:
        raise ValueError('accounts has no enabled Claude login')
    return tuple(accounts)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _reset(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError):
        return None


def _model_matches(scope: str, model: str):
    # The CLI reports display names (Sonnet, Opus, Fable) or canonical model ids.
    target = str(native_model(model)).lower().removesuffix('[1m]')
    name = re.sub(r'[ .]+', '-', scope.strip().lower())
    return name == target or target == 'claude-' + name or target.startswith('claude-' + name + '-')


def windows(limits, model=None):
    """Yield (percent used, reset timestamp) for quota windows relevant to the requested model."""
    if not isinstance(limits, dict):
        return
    entries = limits.get('limits')
    if isinstance(entries, list) and entries:
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('is_active') is False:
                continue
            raw_scope = entry.get('scope')
            scope = raw_scope if isinstance(raw_scope, dict) else {}
            raw_model = scope.get('model')
            scoped = raw_model if isinstance(raw_model, dict) else {}
            name = scoped.get('id') or scoped.get('display_name')
            if model and isinstance(name, str) and not _model_matches(name, model):
                continue
            percent = _number(entry.get('percent'))
            if percent is not None:
                yield percent, _reset(entry.get('resets_at'))
        return
    for key in ('five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet'):
        if model and key in ('seven_day_opus', 'seven_day_sonnet') and not _model_matches(key.removeprefix('seven_day_'), model):
            continue
        entry = limits.get(key)
        if not isinstance(entry, dict):
            continue
        percent = _number(entry.get('utilization'))
        if percent is not None:
            yield percent * 100 if percent <= 1 else percent, _reset(entry.get('resets_at'))


@dataclass
class Snapshot:
    limits: dict | None = None
    reason: str | None = None
    checked_at: float = -math.inf
    checked_wall: float = -math.inf
    cooldowns: dict = field(default_factory=dict)
    cedar_ember: dict | None = None


def _probe_account(command, env, config_dir):
    limits, reason = fetch_usage(command=command, env=env, timeout=3, config_dir=config_dir)
    cedar_ember = fetch_cedar_ember(config_dir=config_dir, command=command, env=env, timeout=4) if limits is not None else None
    return limits, reason, cedar_ember


class Pool:
    def __init__(self, accounts):
        self.accounts = accounts
        self.states = {account: Snapshot() for account in accounts}
        self.lock = threading.RLock()

    def refresh(self, command, env, ttl):
        # A bounded, cached control probe is not a Messages/inference request. The explicit dir
        # survives each worker's profile setting; copy_context preserves profile + traffic policy.
        with self.lock:
            now = time.monotonic()
            pending = [a for a, state in self.states.items() if now - state.checked_at >= ttl or any(
                used >= 100 and reset is not None and state.checked_wall < reset <= time.time()
                for used, reset in windows(state.limits))]
            if not pending:
                return
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='claude-usage') as executor:
                futures = [(account, executor.submit(copy_context().run, _probe_account, command, env, account.config_dir))
                           for account in pending]
                for account, future in futures:
                    limits, reason, cedar_ember = future.result()
                    state = self.states[account]
                    state.limits, state.reason, state.cedar_ember = limits, reason, cedar_ember
                    state.checked_at = time.monotonic()
                    state.checked_wall = time.time()

    def invalidate(self, config_dir):
        target = os.path.normcase(os.path.realpath(os.path.expanduser(config_dir)))
        with self.lock:
            for account, state in self.states.items():
                if os.path.normcase(os.path.realpath(account.config_dir)) == target:
                    state.checked_at = -math.inf
                    state.cedar_ember = None

    def cedar_ember_for(self, account):
        with self.lock:
            state = self.states.get(account)
            return state.cedar_ember if state is not None else None

    def candidates(self, model, command, env, ttl):
        self.refresh(command, env, ttl)
        with self.lock:
            now = time.time()
            ranked = []
            for index, account in enumerate(self.accounts):
                state = self.states[account]
                if max(state.cooldowns.get('*', 0), state.cooldowns.get(native_model(model), 0)) > now:
                    continue
                percentages = [used for used, _ in windows(state.limits, model)]
                score = max(percentages) if percentages else None
                if score is not None and score >= 100:
                    continue
                # Unknown usage remains usable, after accounts with measured headroom. Stable
                # ties preserve account affinity instead of throwing away warm caches each call.
                ranked.append((score is None, score or 0, index, account))
            return [row[-1] for row in sorted(ranked)]

    def snapshots(self, command, env, ttl):
        self.refresh(command, env, ttl)
        with self.lock:
            return [(account, state.limits, state.reason, max(state.cooldowns.values(), default=0) - time.time())
                    for account, state in self.states.items()]

    def failed(self, account, model, status):
        with self.lock:
            state = self.states[account]
            now = time.time()
            resets = [reset for used, reset in windows(state.limits, model)
                      if used >= 100 and reset is not None and reset > now]
            state.cooldowns['*' if status == 401 else native_model(model)] = max(resets, default=now + 60)


_pools = {}
_pools_lock = threading.Lock()


def get_pool(command, env):
    accounts = configured_accounts()
    if not accounts:
        return None
    from hermes_constants import get_hermes_home
    # Tokens are never keys, values or persisted pool state. Different profile/config/CLI/traffic
    # choices must not reuse another profile's usage or cooldowns.
    key = (str(get_hermes_home()), accounts, tuple(_resolve(command, env) or command), telemetry_enabled())
    with _pools_lock:
        if key not in _pools:
            _pools[key] = Pool(accounts)
        return _pools[key]


def invalidate_account(config_dir):
    forget_cedar_ember(config_dir)
    with _pools_lock:
        pools = list(_pools.values())
    for pool in pools:
        pool.invalidate(config_dir)


def cache_seconds():
    value = _number(_setting('usage_cache_seconds', 60))
    if value is None or value > 300:
        raise ValueError('usage_cache_seconds must be between 0 and 300')
    return value
