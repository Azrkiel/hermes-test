"""Swarm contracts use real Git and deterministic subprocess test agents."""
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

from bridge_core import Bridge, BridgeError, git

ROOT = Path(__file__).resolve().parents[1]


def swarm(bridge):
    assert (ROOT / 'bridge_swarm.py').exists(), 'Durable bounded swarm feature is missing'
    return importlib.import_module('bridge_swarm').Swarm(bridge)


@pytest.fixture
def lab(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    git(repo, 'config', 'user.name', 'Swarm Test')
    git(repo, 'config', 'user.email', 'swarm@localhost')
    (repo / 'README.md').write_text('synthetic baseline\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'baseline')
    config = tmp_path / 'agents.json'
    config.write_text(json.dumps({'agents': {
        'builder': {'family': 'builder', 'build': [sys.executable, '-c',
            "from pathlib import Path; import sys,time; time.sleep(.15); "
            "name=sys.argv[1].split('WRITE:')[1].split()[0]; Path(name).write_text('VALUE=42\\n')",
            '{prompt}']},
        'fallback': {'family': 'fallback', 'build': [sys.executable, '-c',
            "from pathlib import Path; import sys; "
            "name=sys.argv[1].split('WRITE:')[1].split()[0]; Path(name).write_text('VALUE=42\\n')",
            '{prompt}']},
        'reviewer': {'family': 'reviewer', 'review': [sys.executable, '-c',
            "print('VERDICT: APPROVE')"]},
    }}))
    return repo, Bridge(roots=[tmp_path], agents_file=config, max_parallel=2, timeout_min=1)


def shard(name):
    return {'name': name, 'prompt': f'WRITE:{name}.py implement the assigned module',
            'files': [f'{name}.py'], 'test_cmd': [sys.executable, '-c',
            f"from pathlib import Path; assert Path('{name}.py').read_text() == 'VALUE=42\\n'"]}


def test_plan_is_persisted_without_launching_one_process_per_pending_shard(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    result = manager.start(str(repo), [shard('one'), shard('two'), shard('three')],
                           agent='builder', reviewer='reviewer', workers=2, autostart=False)
    assert result['status'] == 'queued'
    assert result['workers'] == 2
    assert result['counts']['pending'] == 3
    assert bridge.list_tasks(str(repo)) == []
    assert swarm(bridge).status(str(repo), result['swarm_id'])['counts']['pending'] == 3
    assert git(repo, 'status', '--porcelain') == ''


@pytest.mark.parametrize('problem', [
    'overlap', 'duplicate_name', 'traversal', 'reserved', 'backslash',
    'no_tests', 'too_many_workers', 'empty', 'dependency', 'missing_agent',
])
def test_invalid_plans_fail_before_state_or_workers(lab, problem):
    repo, bridge = lab
    manager = swarm(bridge)
    tasks = [shard('one'), shard('two')]
    options = dict(agent='builder', reviewer='reviewer', workers=2, autostart=False)
    if problem == 'overlap':
        tasks[1]['files'] = tasks[0]['files']
    elif problem == 'duplicate_name':
        tasks[1]['name'] = tasks[0]['name']
    elif problem == 'traversal':
        tasks[0]['files'] = ['../outside.py']
    elif problem == 'reserved':
        tasks[0]['files'] = ['.git/config']
    elif problem == 'backslash':
        tasks[0]['files'] = ['src\\secret.py']
    elif problem == 'no_tests':
        tasks[0]['test_cmd'] = None
    elif problem == 'too_many_workers':
        options['workers'] = 3
    elif problem == 'empty':
        tasks = []
    elif problem == 'dependency':
        tasks[1]['depends_on'] = ['one']
    else:
        options['agent'] = 'nonexistent'
    with pytest.raises(BridgeError):
        manager.start(str(repo), tasks, **options)
    assert not (repo / '.bridge').exists()


def test_dirty_baseline_is_refused(lab):
    repo, bridge = lab
    (repo / 'README.md').write_text('uncommitted desired baseline')
    with pytest.raises(BridgeError, match='clean'):
        swarm(bridge).start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                           workers=2, autostart=False)
    assert not (repo / '.bridge').exists()


def wait_swarm(manager, repo, swarm_id, timeout=100):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.tick(str(repo), swarm_id)
        if state['status'] in {'ready', 'needs_attention', 'cancelled'}:
            return state
        time.sleep(.1)
    pytest.fail(f'swarm did not terminate: {state}')


def test_bounded_pipeline_requires_tests_and_independent_reviews(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    started = manager.start(str(repo), [shard('one'), shard('two'), shard('three')],
                            agent='builder', reviewer='reviewer', workers=2, autostart=False)
    first = manager.tick(str(repo), started['swarm_id'])
    assert sum(item['status'] == 'building' for item in first['items']) <= 2
    assert len(bridge.list_tasks(str(repo))) <= 2
    result = wait_swarm(manager, repo, started['swarm_id'])
    assert result['status'] == 'ready'
    assert result['counts'] == {'approved': 3}
    for item in result['items']:
        status = bridge.get_status(str(repo), item['task_id'], log_lines=0)
        assert status['test_exit_code'] == 0
        assert status['review_status'] == 'approved'
    assert not (repo / 'one.py').exists(), 'No auto-integration into main'
    assert git(repo, 'status', '--porcelain') == ''


def test_cancel_pending_swarm_does_not_launch_workers(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    started = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    manager.cancel(str(repo), started['swarm_id'])
    assert manager.tick(str(repo), started['swarm_id'])['status'] == 'cancelled'
    assert not bridge.list_tasks(str(repo))


def replace_builder(bridge, code):
    config = json.loads(bridge.agents_file.read_text())
    config['agents']['builder']['build'] = [sys.executable, '-c', code, '{prompt}']
    bridge.agents_file.write_text(json.dumps(config))


def test_scope_violation_cannot_be_approved_or_fallback(lab):
    repo, bridge = lab
    replace_builder(bridge, "from pathlib import Path; Path('one.py').write_text('VALUE=42\\n'); "
                    "Path('rogue.py').write_text('outside ownership')")
    manager = swarm(bridge)
    started = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            fallback='fallback', workers=2, autostart=False)
    result = wait_swarm(manager, repo, started['swarm_id'])
    assert result['status'] == 'needs_attention'
    assert result['items'][0]['status'] == 'scope_violation'
    assert result['items'][0]['attempt'] == 1
    assert not result['items'][0]['review_id']


def test_failed_tests_can_use_one_explicit_fallback(lab):
    repo, bridge = lab
    replace_builder(bridge, "from pathlib import Path; Path('one.py').write_text('VALUE=0\\n')")
    manager = swarm(bridge)
    started = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            fallback='fallback', workers=2, autostart=False)
    result = wait_swarm(manager, repo, started['swarm_id'])
    assert result['status'] == 'ready'
    assert result['items'][0]['attempt'] == 2
    current = bridge.get_status(str(repo), result['items'][0]['task_id'], log_lines=0)
    assert current['agent'] == 'fallback'
    assert current['continue_from']
    assert current['review_status'] == 'approved'


def test_autonomous_coordinator_runs_without_tick_calls(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    started = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=True)
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        result = manager.status(str(repo), started['swarm_id'])
        if result['status'] in ('ready', 'needs_attention'):
            break
        time.sleep(.2)
    assert result['status'] == 'ready', result
    assert result['counts'] == {'approved': 1}
    if sys.platform == 'win32':
        assert result.get('keep_awake_supported') is True


@pytest.mark.parametrize('baseline_change', [False, True])
def test_cancel_or_new_baseline_terminalizes_undispatched_fallback(lab, baseline_change):
    from bridge_core import database
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            fallback='fallback', workers=2, autostart=False)
    with database(repo) as db:
        data = manager._load(db, created['swarm_id'])
        data['items'][0].update(status='retry_pending', attempt=1)
        manager._save(db, data)
    if baseline_change:
        (repo / 'README.md').write_text('new baseline\n')
        git(repo, 'add', 'README.md')
        git(repo, 'commit', '-m', 'new baseline')
    else:
        manager.cancel(str(repo), created['swarm_id'])
    result = manager.tick(str(repo), created['swarm_id'])
    assert result['status'] == ('needs_attention' if baseline_change else 'cancelled')
    assert result['items'][0]['status'] == ('blocked' if baseline_change else 'cancelled')
    assert not bridge.list_tasks(str(repo))


def test_ready_status_rechecks_underlying_review_after_source_edit(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    ready = wait_swarm(manager, repo, created['swarm_id'])
    assert ready['status'] == 'ready'
    task = bridge.get_status(str(repo), ready['items'][0]['task_id'], log_lines=0)
    (Path(task['worktree']) / 'one.py').write_text('VALUE=0\n')
    status = manager.status(str(repo), created['swarm_id'])
    assert status['status'] == 'needs_attention'
    assert status['items'][0]['status'] == 'invalidated'
    assert manager.tick(str(repo), created['swarm_id'])['status'] == 'needs_attention'


def test_status_recovers_dead_autonomous_coordinator(lab):
    from bridge_core import database
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    with database(repo) as db:
        data = manager._load(db, created['swarm_id'])
        data.update(auto_run=True, owner_pid=None, owner_created=None)
        manager._save(db, data)
    first = manager.status(str(repo), created['swarm_id'])
    assert first['owner_pid'], 'polling must relaunch a dead autonomous coordinator'
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        state = manager.status(str(repo), created['swarm_id'])
        if state['status'] in ('ready', 'needs_attention'):
            break
        time.sleep(.2)
    assert state['status'] == 'ready', state
    assert len(bridge.list_tasks(str(repo))) == 2, 'recovery must not duplicate a shard'
    assert state['coordinator_restarts'] == 1


def test_coordinator_recovery_has_finite_budget(lab):
    from bridge_core import database
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    with database(repo) as db:
        data = manager._load(db, created['swarm_id'])
        data.update(auto_run=True, coordinator_restarts=3, owner_pid=None, owner_created=None)
        manager._save(db, data)
    state = manager.status(str(repo), created['swarm_id'])
    assert state['status'] == 'needs_attention'
    assert not bridge.list_tasks(str(repo))


def test_swarm_timeout_reaches_independent_review(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False, timeout_min=.5)
    state = wait_swarm(manager, repo, created['swarm_id'])
    assert state['status'] == 'ready'
    task = bridge.get_status(str(repo), state['items'][0]['task_id'], log_lines=0)
    assert task['review']['timeout_min'] == .5


def test_gitignored_rogue_source_is_still_a_scope_violation(lab):
    repo, bridge = lab
    (repo / '.gitignore').write_text('rogue.py\n__pycache__/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'ignored paths')
    replace_builder(bridge, "from pathlib import Path; Path('one.py').write_text('VALUE=42\\n'); "
                    "Path('rogue.py').write_text('outside ownership')")
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    state = wait_swarm(manager, repo, created['swarm_id'])
    assert state['items'][0]['status'] == 'scope_violation'
    assert state['status'] == 'needs_attention'


def test_owned_ignored_source_cannot_bypass_review_snapshot(lab):
    repo, bridge = lab
    (repo / '.gitignore').write_text('one.py\n__pycache__/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'ignored owned source')
    replace_builder(bridge, "from pathlib import Path; Path('one.py').write_text('VALUE=42\\n'); "
                    "Path('extra.py').write_text('VALUE=42\\n')")
    spec = shard('one')
    spec['files'].append('extra.py')
    manager = swarm(bridge)
    created = manager.start(str(repo), [spec], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    state = wait_swarm(manager, repo, created['swarm_id'])
    assert state['status'] == 'needs_attention'
    assert state['items'][0]['status'] == 'scope_violation'


def test_coordinator_launch_refusal_is_terminal_and_visible(lab, monkeypatch):
    import bridge_swarm
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    def refused(*args, **kwargs):
        raise OSError('synthetic launch refusal')
    monkeypatch.setattr(bridge_swarm.subprocess, 'Popen', refused)
    monkeypatch.setattr(bridge_swarm, 'windows_worker', refused)
    manager._spawn(repo, created['swarm_id'])
    # Inspect metadata without launching unrelated git processes under this patch.
    from bridge_core import database
    with database(repo) as db:
        data = manager._load(db, created['swarm_id'])
    assert data['status'] == 'needs_attention'
    assert 'launch' in data['error'] and 'refusal' in data['error']


def test_review_evidence_rejects_oversized_file_before_whole_read(lab, monkeypatch):
    repo, bridge = lab
    manager = swarm(bridge)
    (repo / 'one.py').write_text('x' * 200001)
    task = {'worktree': str(repo), 'base_head': git(repo, 'rev-parse', 'HEAD'), 'test_log': 'test passed'}
    original = Path.read_text
    def guarded(path, *args, **kwargs):
        if path.name == 'one.py':
            raise AssertionError('unbounded read occurred before evidence limit')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', guarded)
    with pytest.raises(BridgeError, match='evidence exceeds'):
        manager._evidence({'spec': shard('one')}, task)


def test_dirty_main_aborts_before_any_new_dispatch(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer', workers=2, autostart=False)
    (repo / 'README.md').write_text('unexpected main mutation')
    state = manager.tick(str(repo), created['swarm_id'])
    assert state['status'] == 'needs_attention'
    assert state['items'][0]['status'] == 'blocked'
    assert not bridge.list_tasks(str(repo))


def test_ready_status_refuses_dirty_main_checkout(lab):
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer', workers=2, autostart=False)
    assert wait_swarm(manager, repo, created['swarm_id'])['status']=='ready'
    (repo/'README.md').write_text('unexpected main edit')
    assert manager.status(str(repo), created['swarm_id'])['status']=='needs_attention'
