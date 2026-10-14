"""Unattended worker environment and deadline regressions."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_runner
from bridge_core import Bridge, BridgeError, database, load, save


@pytest.fixture
def local_bridge(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.STDOUT)
    git('init')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    (repo / 'initial.txt').write_text('base\n', encoding='utf-8')
    git('add', '.')
    git('commit', '-m', 'initial')
    fake = str(Path(__file__).with_name('fake_agent.py'))
    agents = {
        'builder': {'family': 'build', 'build': [sys.executable, fake, 'build', '{prompt}']},
        'reviewer': {'family': 'review', 'review': [sys.executable, fake, 'review', '{prompt}']},
    }
    config = tmp_path / 'agents.json'
    def configure():
        config.write_text(json.dumps({'agents': agents}), encoding='utf-8')
    configure()
    bridge = Bridge(roots=[str(tmp_path)], agents_file=config)
    # Exercise the real runner synchronously, without detached processes.
    def spawn(path, task_id):
        import psutil
        with database(path) as db:
            task = load(db, task_id)
            process = psutil.Process()
            task.update(runner_pid=process.pid, runner_created=process.create_time())
            save(db, task)
    monkeypatch.setattr(bridge, '_spawn', spawn)
    yield bridge, repo, agents, configure
    # Remove only these tests' owned worktrees/snapshots, including readonly Git objects.
    for task in bridge.list_tasks(str(repo)):
        if task['kind'] == 'build' and task['status'] not in ('queued', 'running', 'testing'):
            bridge.cleanup_task(str(repo), task['task_id'], force=True)


@pytest.mark.parametrize('invalid', [None, 'SSL_CERT_FILE', {}, [7], [True], [[]], [''], [' '],
                                    ['BAD=NAME'], ['BAD\x00NAME'], ['BAD NAME']])
@pytest.mark.parametrize('mode', ['build', 'review'])
def test_invalid_unset_env_refused_before_state_creation(local_bridge, invalid, mode):
    bridge, repo, agents, configure = local_bridge
    name = 'builder' if mode == 'build' else 'reviewer'
    agents[name]['unset_env'] = invalid
    configure()
    with pytest.raises(BridgeError, match='agent configuration'):
        if mode == 'build':
            bridge.dispatch_task(str(repo), 'change', agent=name)
        else:
            bridge.review_task(str(repo), '0' * 32, agent=name)
    assert not (repo / '.bridge').exists()


def test_runner_unsets_inherited_env_for_cli_and_tests_without_mutating_parent(local_bridge, monkeypatch):
    bridge, repo, agents, configure = local_bridge
    monkeypatch.setenv('SSL_CERT_FILE', 'parent-cert.pem')
    monkeypatch.setenv('BRIDGE_ENV_KEEP', 'preserved')
    monkeypatch.setenv('BRIDGE_ENV_OVERLAY', 'parent-value')
    agents['builder'].update(
        unset_env=['SSL_CERT_FILE', 'BRIDGE_ENV_OVERLAY', 'BRIDGE_ENV_MISSING'],
        env={'BRIDGE_ENV_OVERLAY': 'explicit-value'},
        build=[sys.executable, '-c',
               "import json,os; from pathlib import Path; "
               "Path('cli-env.json').write_text(json.dumps(dict(os.environ)))"])
    configure()
    test_cmd = [sys.executable, '-c',
                "import json,os; from pathlib import Path; "
                "Path('test-env.json').write_text(json.dumps(dict(os.environ)))"]
    task = bridge.dispatch_task(str(repo), 'change', agent='builder', test_cmd=test_cmd)
    bridge_runner.run(str(repo), task['task_id'])
    result = bridge.get_status(str(repo), task['task_id'])
    assert result['status'] == 'done', result
    for name in ('cli-env.json', 'test-env.json'):
        env = json.loads((Path(task['worktree']) / name).read_text())
        assert 'SSL_CERT_FILE' not in env
        assert env['BRIDGE_ENV_KEEP'] == 'preserved'
        assert env['BRIDGE_ENV_OVERLAY'] == 'explicit-value'
        assert 'BRIDGE_ENV_MISSING' not in env
    assert os.environ['SSL_CERT_FILE'] == 'parent-cert.pem'
    assert os.environ['BRIDGE_ENV_OVERLAY'] == 'parent-value'


@pytest.mark.skipif(os.name != 'nt', reason='Windows environment keys are case-insensitive')
def test_child_env_unsets_windows_names_case_insensitively(monkeypatch):
    from bridge_core import child_env
    monkeypatch.setenv('BRIDGE_CASE_ENV', 'parent-value')
    env = child_env({'unset_env': ['bridge_case_env']})
    assert all(key.upper() != 'BRIDGE_CASE_ENV' for key in env)
    assert os.environ['BRIDGE_CASE_ENV'] == 'parent-value'


@pytest.mark.parametrize('api', ['method', 'function'])
def test_review_explicit_timeout_persisted_and_enforced(local_bridge, api):
    from bridge_review import review_task
    bridge, repo, agents, configure = local_bridge
    agents['reviewer']['review'] = [sys.executable, '-c', 'import time; time.sleep(20)']
    configure()
    build = bridge.dispatch_task(str(repo), 'change', agent='builder')
    bridge_runner.run(str(repo), build['task_id'])
    if api == 'method':
        review = bridge.review_task(str(repo), build['task_id'], 'reviewer', '', timeout_min=.005)
    else:
        review = review_task(bridge, str(repo), build['task_id'], 'reviewer', '', timeout_min=.005)
    assert review['timeout_min'] == .005
    with database(repo) as db:
        assert load(db, review['task_id'])['timeout_min'] == .005
    bridge_runner.run(str(repo), review['task_id'])
    result = bridge.get_status(str(repo), build['task_id'])['review']
    assert result['status'] == 'timed_out', result
    assert result['timeout_min'] == .005
    # Old positional callers still use the Bridge default budget.
    default = bridge.review_task(str(repo), build['task_id'], 'reviewer', '')
    assert default['timeout_min'] == bridge.timeout_min
    bridge.cancel_task(str(repo), default['task_id'])
    bridge_runner.run(str(repo), default['task_id'])


@pytest.mark.parametrize('invalid', [0, -1, True, False, float('nan'), float('inf'),
                                     float('-inf'), '', 'not-a-number', [], {}])
@pytest.mark.parametrize('api', ['method', 'function'])
def test_invalid_review_timeout_refused_before_side_effects(local_bridge, invalid, api, monkeypatch):
    from bridge_review import review_task
    bridge, repo, agents, configure = local_bridge
    effects = []
    def init(path):
        effects.append('init')
        raise AssertionError('invalid timeout reached state initialization')
    monkeypatch.setattr(bridge, '_init', init)
    with pytest.raises(BridgeError, match='timeout_min must be finite and positive'):
        if api == 'method':
            bridge.review_task(str(repo), '0' * 32, 'reviewer', '', timeout_min=invalid)
        else:
            review_task(bridge, str(repo), '0' * 32, 'reviewer', '', timeout_min=invalid)
    assert not effects
    assert not (repo / '.bridge').exists()


def test_monitor_checks_expired_deadline_before_database_lock(local_bridge, monkeypatch):
    import time
    from contextlib import contextmanager
    bridge, repo, agents, configure = local_bridge
    task = bridge.dispatch_task(str(repo), 'change', agent='builder')
    bridge_runner.run(str(repo), task['task_id'])
    children = []
    real_popen = bridge_runner.subprocess.Popen
    real_database = bridge_runner.database
    real_monotonic = time.monotonic
    expired = False
    calls = 0
    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child
    @contextmanager
    def observed_database(*args, **kwargs):
        nonlocal expired, calls
        calls += 1
        if calls == 2:
            assert children[0].poll() is not None, 'expired deadline attempted a DB lock before child termination'
        with real_database(*args, **kwargs) as db:
            yield db
        if calls == 1:
            expired = True
    deadline = real_monotonic() + 10
    with monkeypatch.context() as patch:
        patch.setattr(bridge_runner.subprocess, 'Popen', capture)
        patch.setattr(bridge_runner, 'database', observed_database)
        patch.setattr(bridge_runner.time, 'monotonic', lambda: real_monotonic() + (30 if expired else 0))
        with pytest.raises(bridge_runner.Stopped, match='timed_out'):
            bridge_runner.execute(repo, task['task_id'], [sys.executable, '-c', 'import time; time.sleep(20)'],
                                  'expired.log', deadline, os.environ.copy())
    assert children[0].poll() is not None
    with database(repo) as db:
        assert load(db, task['task_id'])['child_pid'] is None


def test_monitor_database_contention_cannot_delay_child_timeout(local_bridge, monkeypatch):
    import sqlite3
    import threading
    import time
    from contextlib import contextmanager
    bridge, repo, agents, configure = local_bridge
    task = bridge.dispatch_task(str(repo), 'change', agent='builder')
    bridge_runner.run(str(repo), task['task_id'])
    children = []
    terminated_before_release = []
    real_popen = bridge_runner.subprocess.Popen
    real_database = bridge_runner.database
    calls = 0
    release = None
    blocker = None
    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child
    @contextmanager
    def contended_database(*args, **kwargs):
        nonlocal calls, release, blocker
        calls += 1
        with real_database(*args, **kwargs) as db:
            yield db
        if calls == 1:
            blocker = sqlite3.connect(str(repo / '.bridge' / 'state.sqlite'), check_same_thread=False)
            blocker.execute('BEGIN IMMEDIATE')
            def unlock():
                terminated_before_release.append(children[0].poll() is not None)
                blocker.rollback()
                blocker.close()
            release = threading.Timer(1.5, unlock)
            release.start()
    with monkeypatch.context() as patch:
        patch.setattr(bridge_runner.subprocess, 'Popen', capture)
        patch.setattr(bridge_runner, 'database', contended_database)
        try:
            with pytest.raises(bridge_runner.Stopped, match='timed_out'):
                bridge_runner.execute(repo, task['task_id'], [sys.executable, '-c', 'import time; time.sleep(20)'],
                                      'contended.log', time.monotonic() + .6, os.environ.copy())
        finally:
            if release is not None:
                release.join(timeout=5)
    assert terminated_before_release == [True], 'SQLite lock delayed termination beyond the deadline'
    assert children[0].poll() is not None
    with database(repo) as db:
        assert load(db, task['task_id'])['child_pid'] is None
