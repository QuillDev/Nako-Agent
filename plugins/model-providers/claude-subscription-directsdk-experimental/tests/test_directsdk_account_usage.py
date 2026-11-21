"""`/usage` plan limits through core's own dispatch and renderer. Claude Code answers its `get_usage`
control request; only that subprocess is faked. The plugin never reads or sends the CLI's token."""
from datetime import datetime, timezone
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

PROVIDER = 'claude-subscription-directsdk-experimental'
LIMITS = {'limits': [
    {'kind': 'session', 'percent': 45, 'resets_at': '2026-10-01T07:09:00Z'},
    {'kind': 'weekly_all', 'percent': 23, 'resets_at': '2026-10-02T06:59:00Z'},
    {'kind': 'weekly_model', 'percent': 17, 'resets_at': '2026-10-02T06:59:00Z', 'scope': {'model': {'display_name': 'Fable'}}},
    {'kind': 'something_new', 'percent': None, 'resets_at': None},
    {'kind': ['odd'], 'percent': float('nan'), 'scope': 'odd'},
]}
LEGACY = {'five_hour': {'utilization': 0.4, 'resets_at': '2026-10-01T07:09:00Z'},
          'seven_day': {'utilization': 12}, 'seven_day_opus': {'utilization': True}, 'seven_day_sonnet': 'odd',
          'extra_usage': {'is_enabled': True, 'used_credits': 1.5, 'monthly_limit': 10, 'currency': 'EUR'}}


def _answer(rate_limits=None, available=True, subtype='success'):
    import host
    return {'type': 'control_response', 'response': {'subtype': subtype, 'request_id': f'{host.NAME}-usage', 'response': {
        'subscription_type': 'max' if available else None, 'rate_limits_available': available,
        'rate_limits': rate_limits, 'session': {}, 'behaviors': None}}}


@pytest.fixture
def cli(profile, monkeypatch):
    """A fake Claude Code: `cli.reply` is its control answer; `cli.calls` records argv/env/stdin."""
    import agent.anthropic_credentials as credentials

    class State:
        reply, calls = _answer(LIMITS), []

    def run(argv, **kwargs):
        State.calls.append(SimpleNamespace(argv=argv, env=kwargs['env'], input=kwargs.get('input'), timeout=kwargs.get('timeout')))
        if isinstance(State.reply, Exception):
            raise State.reply
        reply = State.reply(kwargs['env']) if callable(State.reply) else State.reply
        out = reply if isinstance(reply, str) else json.dumps(reply, allow_nan=True)
        return SimpleNamespace(stdout='{"type":"system","subtype":"init"}\n' + out + '\n', returncode=0)

    def refuse(*_, **__):
        raise AssertionError('the usage hook must never read, refresh or send the Claude Code login')

    monkeypatch.setattr(subprocess, 'run', run)
    for name in ('read_claude_code_credentials', 'resolve_anthropic_token', '_refresh_oauth_token',
                 '_write_claude_code_credentials', '_read_claude_code_credentials_from_keychain'):
        monkeypatch.setattr(credentials, name, refuse, raising=False)
    monkeypatch.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND', sys.executable)
    return State


def _usage():
    from agent.account_usage import fetch_account_usage, render_account_usage_lines
    snapshot = fetch_account_usage(PROVIDER)
    return snapshot, render_account_usage_lines(snapshot)


def test_limits_come_from_claude_code_get_usage(cli):
    snapshot, lines = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [
        ('Current session', 45.0), ('Current week', 23.0), ('Fable week', 17.0), ('something_new', None), ("['odd']", None)]
    # Fable's own weekly cap only limits Fable, so clients can leave it out of the account's limits.
    assert [w.scope for w in snapshot.windows] == ['account', 'account', 'model', 'account', 'account']
    assert snapshot.windows[0].reset_at == datetime(2026, 10, 1, 7, 9, tzinfo=timezone.utc)
    assert lines[0] == '\U0001F4C8 Claude plan limits' and lines[1] == f'Provider: {PROVIDER}'
    assert lines[2].startswith('Current session: 55% remaining (45% used)')
    assert snapshot.details == () and snapshot.unavailable_reason is None
    call, = cli.calls
    assert json.loads(call.input)['request'] == {'subtype': 'get_usage', 'skip_behaviors': True}
    # Behind the unused admission gate (no inference can leave), and under core's 10 s hook deadline.
    assert call.env['ANTHROPIC_BASE_URL'].startswith('http://127.0.0.1:') and call.timeout < 10


def test_pool_usage_shows_each_account_without_exposing_login_directories(cli, tmp_path):
    from hermes_cli.config import get_config_path
    first, second = str(tmp_path / 'private-first'), str(tmp_path / 'private-second')
    get_config_path().write_text(json.dumps({'plugins': {'enabled': [], 'entries': {PROVIDER: {'settings': {
        'accounts': [{'label': 'first', 'config_dir': first}, {'label': 'second', 'config_dir': second}],
        'claude_config_dir': first}}}}}), encoding='utf-8')
    cli.reply = lambda env: _answer({'limits': [{'kind': 'session', 'percent': 45 if env.get('CLAUDE_CONFIG_DIR') == first else 10}]})
    snapshot, lines = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [
        ('first · Current session', 45.0), ('second · Current session', 10.0)]
    assert first not in '\n'.join(lines) and second not in '\n'.join(lines)


def test_each_linked_login_reports_its_own_limits_for_side_by_side_clients(cli, tmp_path):
    import providers
    from hermes_cli.config import get_config_path
    first, second = str(tmp_path / 'private-first'), str(tmp_path / 'private-second')
    get_config_path().write_text(json.dumps({'plugins': {'enabled': [], 'entries': {PROVIDER: {'settings': {
        'accounts': [{'label': 'first', 'config_dir': first}, {'label': 'second', 'config_dir': second}]}}}}}),
        encoding='utf-8')
    cli.reply = lambda env: (_answer({'limits': [{'kind': 'weekly_all', 'percent': 100}]}) if env.get('CLAUDE_CONFIG_DIR') == first
                             else _answer(subtype='error'))
    accounts = providers.get_provider_profile(PROVIDER).fetch_account_usage_by_account()
    assert [label for label, _ in accounts] == ['first', 'second']
    (_, used), (_, broken) = accounts
    assert [(w.label, w.used_percent) for w in used.windows] == [('Current week', 100.0)] and used.unavailable_reason is None
    assert broken.windows == () and broken.unavailable_reason
    assert first not in repr(accounts) and second not in repr(accounts)


def test_a_single_login_has_no_per_account_breakdown(cli):
    import providers
    assert providers.get_provider_profile(PROVIDER).fetch_account_usage_by_account() is None


def test_a_body_without_limits_reads_the_legacy_windows(cli):
    cli.reply = _answer(LEGACY)
    snapshot, _ = _usage()
    assert [(w.label, w.used_percent) for w in snapshot.windows] == [('Current session', 40.0), ('Current week', 12.0)]
    assert snapshot.details == ('Extra usage: 1.50 / 10.00 EUR',)


def test_works_with_adopt_external_logins_off_and_follows_the_profile_login(cli, monkeypatch):
    # #82: with auth.adopt_external_logins false core will not read the CLI login; the CLI itself still can.
    import directsdk_setup
    monkeypatch.setattr(directsdk_setup, '_setting', lambda key, default=None: '/logins/work' if key == 'claude_config_dir' else default)
    cli.reply = _answer(LEGACY)
    limits, reason = directsdk_setup.fetch_usage()
    assert reason is None and limits == LEGACY
    assert cli.calls[-1].env['CLAUDE_CONFIG_DIR'] == '/logins/work'


@pytest.mark.parametrize('reply, reason', [
    (_answer(None, available=False), 'no Claude subscription login found (run `claude` and log in)'),
    (_answer(None), 'Claude Code returned no plan limits'),
    (_answer(None, subtype='error'), 'Claude Code refused the usage request (needs a version with get_usage)'),
    ('not json', 'Claude Code gave no usage answer (needs a version with get_usage)'),
    (subprocess.TimeoutExpired('claude', 8), 'Claude Code did not answer in time'),
    (OSError('gone'), 'Claude Code gave no usage answer (needs a version with get_usage)'),
])
def test_failures_name_the_reason(cli, reply, reason):
    cli.reply = reply
    snapshot, lines = _usage()
    assert snapshot.windows == () and lines[-1] == f'Unavailable: {reason}'


def test_quiet_mode_says_why_limits_are_missing(cli, monkeypatch):
    import directsdk_setup
    monkeypatch.setattr(directsdk_setup, 'telemetry_enabled', lambda: False)
    cli.reply = _answer(None)
    limits, reason = directsdk_setup.fetch_usage()
    assert limits is None and 'nonessential traffic off' in reason


def test_missing_claude_code_makes_no_call(cli, monkeypatch, tmp_path):
    monkeypatch.setenv('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND', str(tmp_path / 'no-such-claude'))
    snapshot, lines = _usage()
    assert cli.calls == [] and lines[-1].startswith('Unavailable: Claude Code is not installed')
