"""``nako_cli.chat_launch``: what a plugin's own chat launcher calls around its client process."""
import json
import os
import subprocess

import pytest

from nako_cli.main import PROJECT_ROOT


def test_exit_summary_names_the_launchers_command(tmp_path, monkeypatch, capsys):
    from nako_cli.chat_launch import print_exit_summary
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_messages, seed_session
    home = tmp_path / '.nako'
    home.mkdir()
    monkeypatch.setenv('NAKO_HOME', str(home))
    with open_persistence(profile_scope(home=home)) as db:
        seed_session(db, '20261110_120000_abc123', 'tui', title='Demo chat')
        seed_messages(db, '20261110_120000_abc123', {'role': 'user', 'content': 'q'},
                      {'role': 'assistant', 'content': 'a'})
        seed_session(db, '20261110_120001_empty0', 'tui')

    print_exit_summary('20261110_120000_abc123', command='nako demo')
    out = capsys.readouterr().out
    assert '  nako demo --resume 20261110_120000_abc123\n' in out
    assert '  nako demo -c "Demo chat"\n' in out
    assert '--tui' not in out

    print_exit_summary('20261110_120001_empty0', command='nako demo')
    assert capsys.readouterr().out == ''


def test_nako_command_runs_this_install_without_pythonpath(tmp_path):
    from nako_cli.chat_launch import nako_command
    home = tmp_path / '.nako'
    home.mkdir()
    (home / 'config.yaml').write_text('updates:\n  check: false\n', encoding='utf-8')
    env = {'NAKO_HOME': str(home), 'HOME': str(tmp_path), 'PATH': os.environ.get('PATH', '')}
    child = subprocess.run(nako_command(['--version']), env=env, cwd=str(tmp_path), capture_output=True,
                           text=True, timeout=60, check=False)
    assert child.returncode == 0, child.stderr
    assert child.stdout.startswith('Nako Agent v')
    assert f'Install directory: {PROJECT_ROOT}\n' in child.stdout


@pytest.mark.platforms('windows')  # only Windows publishes the launcher as nako.exe or nako.cmd
def test_nako_command_starts_with_a_file_windows_can_start(tmp_path, monkeypatch):
    from nako_cli import main
    from nako_cli.chat_launch import nako_command
    store = tmp_path / 'store'
    python = store / 'py' / 'python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'')
    (store / 'facts.json').write_text(json.dumps({'packages': {'python': {'entry': 'py'}}}), encoding='utf-8')
    monkeypatch.setenv('NAKO_RUNTIME_DIR', str(store))
    launchers = tmp_path / 'install' / '.nako' / 'bin'
    launchers.mkdir(parents=True)
    monkeypatch.setattr(main, 'PROJECT_ROOT', tmp_path / 'install')

    # Only the batch fallback is published: no child process starts it without a shell.
    (launchers / 'nako.cmd').write_text('@echo off\r\n', encoding='utf-8')
    command = nako_command(['--version'])
    assert os.path.samefile(command[0], python)
    assert command[-1] == '--version'

    (launchers / 'nako.exe').write_bytes(b'MZ')
    command = nako_command(['--version'])
    assert os.path.samefile(command[0], launchers / 'nako.exe')
    assert command[1:] == ['--version']
