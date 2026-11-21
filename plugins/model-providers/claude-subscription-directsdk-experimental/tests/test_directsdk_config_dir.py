"""#34: every native spawn selects the active profile's Claude login directory."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_NAME
from test_directsdk import FAKE
from test_directsdk_setup import PINNED_PICKER, PRO

OVERRIDE = "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"
NATIVE_DIR = "CLAUDE_CONFIG_DIR"
SETTING = "claude_config_dir"


def _set_config_dir(value):
    from hermes_cli.config import get_config_path
    get_config_path().write_text(
        "plugins:\n  enabled: []\n  entries:\n    %s:\n      settings:\n        %s: %s\n"
        % (PLUGIN_NAME, SETTING, json.dumps(value)), encoding="utf-8")


@pytest.mark.parametrize("setting,override,inherited,expected", [
    ("profile-login", "override-login", "inherited-login", "profile-login"),
    (None, "override-login", "inherited-login", "override-login"),
    (None, None, "inherited-login", "inherited-login"),
    (None, None, None, None),
])
def test_config_dir_precedence(profile, tmp_path, setting, override, inherited, expected):
    import directsdk_setup
    if setting is not None:
        _set_config_dir(str(tmp_path / setting))
    env = {"UNRELATED": "preserved"}
    if override is not None:
        env[OVERRIDE] = str(tmp_path / override)
    if inherited is not None:
        env[NATIVE_DIR] = str(tmp_path / inherited)
    assert directsdk_setup.apply_config_dir(env) is env
    assert env.get(NATIVE_DIR) == (str(tmp_path / expected) if expected else None)
    assert OVERRIDE not in env
    assert env["UNRELATED"] == "preserved"


@pytest.mark.parametrize("value", ["", " \t ", None, 42])
def test_empty_or_non_string_setting_falls_through(profile, tmp_path, value):
    import directsdk_setup
    _set_config_dir(value)
    fallback = str(tmp_path / "fallback")
    assert directsdk_setup.apply_config_dir({OVERRIDE: fallback}) == {NATIVE_DIR: fallback}


@pytest.mark.parametrize("source", ["setting", "override", "inherited"])
def test_chosen_directory_expands_user(profile, tmp_path, monkeypatch, source):
    import directsdk_setup
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    env = {}
    if source == "setting":
        _set_config_dir("  ~/login  ")
    else:
        env[OVERRIDE if source == "override" else NATIVE_DIR] = "~/login"
    chosen = directsdk_setup.apply_config_dir(env)
    # expanduser keeps the separator it was given; Windows accepts either.
    assert list(chosen) == [NATIVE_DIR] and os.path.normpath(chosen[NATIVE_DIR]) == str(tmp_path / "login")


def test_empty_override_is_removed_and_falls_through(profile, tmp_path):
    import directsdk_setup
    inherited = str(tmp_path / "inherited")
    assert directsdk_setup.apply_config_dir({OVERRIDE: "", NATIVE_DIR: inherited}) == {NATIVE_DIR: inherited}


def test_unreadable_config_means_not_set(profile, tmp_path, monkeypatch):
    import directsdk_setup
    import hermes_cli.config
    def unreadable():
        raise OSError("fixture config is unreadable")
    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", unreadable)
    fallback = str(tmp_path / "fallback")
    assert directsdk_setup.apply_config_dir({OVERRIDE: fallback}) == {NATIVE_DIR: fallback}
    assert directsdk_setup.telemetry_enabled() is True


def test_a_multi_profile_gateway_reads_each_profiles_own_setting(profile, tmp_path):
    import directsdk_setup
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    first = str(tmp_path / "first-login")
    second = str(tmp_path / "second-login")
    _set_config_dir(first)
    second_home = tmp_path / "second-profile"
    second_home.mkdir()
    token = set_hermes_home_override(second_home)
    try:
        _set_config_dir(second)
        assert directsdk_setup.apply_config_dir({})[NATIVE_DIR] == second
    finally:
        reset_hermes_home_override(token)
    assert directsdk_setup.apply_config_dir({})[NATIVE_DIR] == first


def test_native_requests_read_the_active_profile_on_every_spawn(profile, tmp_path):
    import directsdk
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    capture = tmp_path / "child-dirs.jsonl"
    script = tmp_path / "native.py"
    probe = ("open(os.environ['DIR_CAPTURE'], 'a').write(json.dumps({k: os.environ[k] "
             "for k in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR') "
             "if k in os.environ}) + '\\n')\n")
    script.write_text(FAKE.replace("rows=[]", probe + "rows=[]"), encoding="utf-8")
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "DIR_CAPTURE": str(capture),
           OVERRIDE: str(tmp_path / "override"), NATIVE_DIR: str(tmp_path / "inherited")}
    client = directsdk.Client(command=[sys.executable, str(script)], env=env)
    request = dict(model="sonnet", messages=[{"role": "user", "content": "go"}],
                   tools=[{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                                            "parameters": {"type": "object", "properties": {}}}}])
    first, second, changed = (str(tmp_path / name) for name in ("first-login", "second-login", "changed-login"))
    try:
        _set_config_dir(first)
        assert client.create(**request).choices[0].message.content == "hello\n"
        second_home = tmp_path / "second-profile"
        second_home.mkdir()
        token = set_hermes_home_override(second_home)
        try:
            _set_config_dir(second)
            client.create(**request)
        finally:
            reset_hermes_home_override(token)
        _set_config_dir(changed)
        client.create(**request)
    finally:
        client.close()
    assert [json.loads(line) for line in capture.read_text(encoding="utf-8-sig").splitlines()] == [
        {NATIVE_DIR: first}, {NATIVE_DIR: second}, {NATIVE_DIR: changed}]
    assert client.env == env  # Only the child's copy changes.


@pytest.mark.parametrize("probe", ["setup_status", "discover_models"])
def test_setup_probes_pass_the_profile_directory(profile, tmp_path, monkeypatch, probe):
    import directsdk_setup
    selected = str(tmp_path / "profile-login")
    _set_config_dir(selected)
    captured = []
    def run(argv, **kwargs):
        captured.append(kwargs["env"])
        if argv[1:3] == ["auth", "status"]:
            output = PRO["auth"]
        else:
            output = {"type": "control_response", "response": {"response": {
                "models": PINNED_PICKER, "account": PRO["account"]}}}
        return SimpleNamespace(stdout=json.dumps(output), returncode=0)
    monkeypatch.setattr(directsdk_setup.subprocess, "run", run)
    env = {"PATH": os.defpath, "HOME": str(tmp_path), OVERRIDE: str(tmp_path / "override")}
    result = getattr(directsdk_setup, probe)(command=[sys.executable], env=env)
    assert result["logged_in"] if probe == "setup_status" else result
    assert len(captured) == (1 if probe == "setup_status" else 2)
    assert all(child[NATIVE_DIR] == selected and OVERRIDE not in child for child in captured)
    assert NATIVE_DIR not in env and OVERRIDE in env


@pytest.fixture
def pool_cli(profile, tmp_path):
    """A native CLI fixture with independent logins, usage answers and generated text."""
    import directsdk
    from hermes_cli.config import get_config_path

    script = tmp_path / "pool-native.py"
    prelude = r'''
account = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
spec = json.loads((account / 'fixture.json').read_text())
if sys.argv[1:3] == ['auth', 'status']:
 print(json.dumps({'loggedIn': not spec.get('logged_out'), 'subscriptionType': 'max'})); sys.exit()
if '--settings' not in sys.argv:
 request = json.loads(sys.stdin.readline())
 if request['request']['subtype'] == 'get_usage':
  with (account / 'probes.jsonl').open('a') as f: f.write('usage\n')
  body = {'rate_limits_available': True, 'rate_limits': spec.get('limits')}
 else:
  body = {'models': [{'value': 'sonnet', 'resolvedModel': 'claude-sonnet-5-5', 'description': 'Sonnet 5.5'}], 'account': {'subscriptionType': 'max'}}
 print(json.dumps({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': request['request_id'], 'response': body}}), flush=True)
 sys.exit()
with (account / 'generations.jsonl').open('a') as f: f.write('request\n')
if spec.get('partial'):
 print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'partial'}}}), flush=True)
if spec.get('error'):
 os.environ['NATIVE_ERROR'] = spec['error'] + ':account unavailable'
'''
    native = FAKE.replace("rows=[]", prelude + "\nrows=[]")
    native = native.replace("'hello\\n'", "spec['label'] + '\\n'")
    native = native.replace("if os.environ.get('NATIVE_ERROR'):",
        "(account / 'frames.json').write_text(json.dumps(rows))\nif os.environ.get('NATIVE_ERROR'):")
    # The next request can use another account; its signed history belongs to the previous reply.
    native = native.replace("assert rows[1]['message']['content']==[dict(b, name=os.environ.get('REPLAY_NAME', b['name'])) if b['type']=='tool_use' else b for b in blocks]",
                            "assert rows[1]['message']['content'][0]['signature'] == 'signed-test'")
    script.write_text(native, encoding="utf-8")
    accounts = []

    def add(label, used=0, **spec):
        directory = tmp_path / label
        directory.mkdir()
        if 'limits' not in spec:
            spec['limits'] = {'limits': [{'kind': 'session', 'percent': used}]}
        (directory / 'fixture.json').write_text(json.dumps({'label': label, **spec}), encoding='utf-8')
        accounts.append({'label': label, 'config_dir': str(directory)})
        return directory

    def configure(**settings):
        settings = {'accounts': accounts, **settings}
        get_config_path().write_text(json.dumps({'plugins': {'enabled': [], 'entries': {
            PLUGIN_NAME: {'settings': settings}}}}), encoding='utf-8')

    def client():
        return directsdk.Client(command=[sys.executable, str(script)], env={'PATH': os.defpath, 'HOME': str(tmp_path)})

    request = dict(model='sonnet', messages=[{'role': 'user', 'content': 'go'}], tools=[{
        'type': 'function', 'function': {'name': 'probe', 'description': 'TAIL',
                                      'parameters': {'type': 'object', 'properties': {}}}}])
    return SimpleNamespace(add=add, configure=configure, client=client, request=request, accounts=accounts)


def test_pool_routes_to_available_usage_and_caches_across_clients(pool_cli):
    first = pool_cli.add('first', 90)
    second = pool_cli.add('second', 20)
    pool_cli.configure(claude_config_dir=str(first))  # A pool selection must override this legacy default.
    for _ in range(2):
        with_client = pool_cli.client()
        try:
            response = with_client.create(**pool_cli.request)
            assert response.choices[0].message.content == 'second\n'
            assert response.usage.model_dump()['native_account']['label'] == 'second'
        finally:
            with_client.close()
    assert not (first / 'generations.jsonl').exists()
    assert (second / 'generations.jsonl').read_text(encoding="utf-8-sig").splitlines() == ['request', 'request']
    assert (first / 'probes.jsonl').read_text(encoding="utf-8-sig").splitlines() == ['usage']
    assert (second / 'probes.jsonl').read_text(encoding="utf-8-sig").splitlines() == ['usage']


def test_pool_can_switch_accounts_between_tool_rounds_without_changing_signed_history(pool_cli):
    first = pool_cli.add('first', 0)
    second = pool_cli.add('second', 40)
    pool_cli.configure(usage_cache_seconds=0)
    client = pool_cli.client()
    try:
        initial = client.create(**pool_cli.request).choices[0].message
        assert initial.content == 'first\n'
        (first / 'fixture.json').write_text(json.dumps({'label': 'first', 'limits': {
            'limits': [{'kind': 'weekly_all', 'percent': 100}]}}), encoding='utf-8')
        request = {**pool_cli.request, 'messages': [*pool_cli.request['messages'], initial.model_dump(),
            {'role': 'tool', 'tool_call_id': 'toolu_test', 'content': 'tool finished'}]}
        response = client.create(**request)
        assert response.choices[0].message.content == 'done'
        assert response.usage.model_dump()['native_account']['label'] == 'second'
        frames = json.loads((second / 'frames.json').read_text(encoding="utf-8-sig"))
        assert frames[1]['message']['content'] == initial.reasoning_details[0]['messages'][0]['content']
        assert frames[-1]['message']['content'][0]['content'] == 'tool finished'
    finally:
        client.close()


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('error', ['rate_limit', 'authentication_failed'])
def test_pool_fails_over_before_output_and_skips_that_account_next_time(pool_cli, streaming, error, monkeypatch):
    first = pool_cli.add('first', 0, error=error)
    second = pool_cli.add('second', 10)
    pool_cli.configure()
    client = pool_cli.client()
    try:
        for _ in range(2):
            result = client.create(**pool_cli.request, stream=streaming)
            if streaming:
                result = list(result)[-1]._response
            assert result.choices[0].message.content == 'second\n'
            assert result.choices[0].message.tool_calls[0].function.name == 'probe'
        import time
        now = time.time()
        (first / 'fixture.json').write_text(json.dumps({'label': 'first', 'limits': {
            'limits': [{'kind': 'session', 'percent': 0}]}}), encoding='utf-8')
        monkeypatch.setattr(time, 'time', lambda: now + 61)
        assert client.create(**pool_cli.request).choices[0].message.content == 'first\n'
    finally:
        client.close()
    assert (first / 'generations.jsonl').read_text(encoding="utf-8-sig").splitlines() == ['request', 'request']
    assert (second / 'generations.jsonl').read_text(encoding="utf-8-sig").splitlines() == ['request', 'request']


@pytest.mark.parametrize('error,partial', [('rate_limit', True), ('server_error', False)])
def test_pool_never_replays_partial_answers_or_retries_nonquota_errors(pool_cli, error, partial):
    pool_cli.add('first', 0, error=error, partial=partial)
    second = pool_cli.add('second', 10)
    pool_cli.configure()
    client = pool_cli.client()
    try:
        stream = client.create(**pool_cli.request, stream=True)
        if partial:
            assert next(stream).choices[0].delta.content == 'partial'
        with pytest.raises(RuntimeError, match='account unavailable'):
            list(stream)
    finally:
        client.close()
    assert not (second / 'generations.jsonl').exists()


def test_pool_uses_model_limits_and_refuses_exhausted_accounts(pool_cli):
    import directsdk
    first = pool_cli.add('first', limits={'limits': [
        {'kind': 'session', 'percent': 5},
        {'kind': 'weekly_model', 'percent': 100, 'scope': {'model': {'display_name': 'Sonnet 5.5'}}},
        {'kind': 'weekly_all', 'percent': 100, 'is_active': False}]})
    second = pool_cli.add('second', limits={'seven_day': {'utilization': 1.0}})
    pool_cli.configure()
    client = pool_cli.client()
    try:
        with pytest.raises(directsdk.ClaudeAPIError, match='No Claude account') as error:
            client.create(**pool_cli.request)
        assert error.value.status_code == 429
        assert not (first / 'generations.jsonl').exists()
        assert not (second / 'generations.jsonl').exists()
        response = client.create(**{**pool_cli.request, 'model': 'opus'})
        assert response.choices[0].message.content == 'first\n'
    finally:
        client.close()


def test_pool_missing_usage_still_allows_native_auth_and_rate_limit_fallback(pool_cli):
    pool_cli.add('first', limits=None, error='rate_limit')
    pool_cli.add('second', limits=None)
    pool_cli.configure()
    client = pool_cli.client()
    try:
        assert client.create(**pool_cli.request).choices[0].message.content == 'second\n'
    finally:
        client.close()


def test_pool_recovers_at_reset_and_does_not_probe_again_until_cache_expiry(pool_cli, monkeypatch):
    from datetime import datetime, timezone
    import time
    now = time.time()
    reset = datetime.fromtimestamp(now + 10, tz=timezone.utc).isoformat()
    first = pool_cli.add('first', limits={'limits': [{'kind': 'session', 'percent': 100, 'resets_at': reset}]})
    pool_cli.configure()
    client = pool_cli.client()
    try:
        with pytest.raises(RuntimeError, match='No Claude account'):
            client.create(**pool_cli.request)
        (first / 'fixture.json').write_text(json.dumps({'label': 'first', 'limits': {
            'limits': [{'kind': 'session', 'percent': 0, 'resets_at': reset}]}}), encoding='utf-8')
        monkeypatch.setattr(time, 'time', lambda: now + 11)
        for _ in range(2):
            assert client.create(**pool_cli.request).choices[0].message.content == 'first\n'
    finally:
        client.close()
    assert (first / 'probes.jsonl').read_text(encoding="utf-8-sig").splitlines() == ['usage', 'usage']


def test_pool_ignores_disabled_logins_and_keeps_profiles_separate(pool_cli, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    first = pool_cli.add('first', 0)
    pool_cli.add('second', 50)
    pool_cli.accounts[0]['enabled'] = False
    pool_cli.configure()
    client = pool_cli.client()
    try:
        assert client.create(**pool_cli.request).choices[0].message.content == 'second\n'
        home = tmp_path / 'another-profile'
        home.mkdir()
        token = set_hermes_home_override(home)
        try:
            pool_cli.accounts[0]['enabled'] = True
            pool_cli.configure()
            assert client.create(**pool_cli.request).choices[0].message.content == 'first\n'
        finally:
            reset_hermes_home_override(token)
        assert client.create(**pool_cli.request).choices[0].message.content == 'second\n'
    finally:
        client.close()
    assert (first / 'generations.jsonl').read_text(encoding="utf-8-sig").splitlines() == ['request']


def test_pool_setup_and_picker_use_a_linked_login_not_the_legacy_default(pool_cli):
    import directsdk_setup
    pool_cli.add('first', logged_out=True)
    pool_cli.add('second')
    pool_cli.configure(claude_config_dir='/nonexistent/legacy-default')
    client = pool_cli.client()
    try:
        assert directsdk_setup.setup_status(command=client.command, env=client.env)['logged_in']
        models = directsdk_setup.discover_models(command=client.command, env=client.env)
        assert any(row['id'].startswith('claude-sonnet-5-5') for row in models)
        for row in pool_cli.accounts:
            (Path(row['config_dir']) / 'fixture.json').write_text(json.dumps({'logged_out': True}), encoding="utf-8")
        status = directsdk_setup.setup_status(command=client.command, env=client.env)
        assert not status['logged_in'] and status['login_command'] is None
        assert pool_cli.accounts[0]['config_dir'] in status['detail']
    finally:
        client.close()


def test_pool_does_not_allow_inherited_oauth_to_override_selected_logins(pool_cli):
    pool_cli.add('first', 80)
    pool_cli.add('second', 10)
    pool_cli.configure()
    client = pool_cli.client()
    # The external CLI fixture rejects this token if it reaches any child.
    script = Path(client.command[-1])
    script_source = script.read_text(encoding="utf-8-sig")
    script.write_text(script_source.replace("account = pathlib.Path", "assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ\naccount = pathlib.Path"), encoding="utf-8")
    client.env['CLAUDE_CODE_OAUTH_TOKEN'] = 'fixture-inherited-token'
    try:
        assert client.create(**pool_cli.request).choices[0].message.content == 'second\n'
    finally:
        client.close()


@pytest.mark.parametrize('accounts', [None, 'bad', [{'label': 'empty'}], [
    {'label': 'same', 'config_dir': '/fixture/a'}, {'label': 'same', 'config_dir': '/fixture/b'}]])
def test_pool_invalid_configuration_never_silently_uses_the_default_login(pool_cli, accounts):
    pool_cli.configure(accounts=accounts)
    client = pool_cli.client()
    try:
        with pytest.raises(ValueError, match='accounts'):
            client.create(**pool_cli.request)
    finally:
        client.close()


def test_manifest_declares_the_setting_under_the_key_the_code_reads(profile):
    from hermes_cli.plugins_settings import plugin_settings_fields
    import directsdk_setup
    root = Path(directsdk_setup.__file__).resolve().parent
    assert directsdk_setup.PLUGIN_ID == PLUGIN_NAME
    fields = {field["key"]: field for field in plugin_settings_fields(PLUGIN_NAME, root)}
    field = fields[SETTING]
    assert directsdk_setup.CONFIG_DIR_SETTING == SETTING
    assert field["type"] == "string" and field["default"] == ""
    assert field["label"] and field["description"]
