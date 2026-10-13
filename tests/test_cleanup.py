"""Cleanup keeps branches and refuses active/superseded/tampered worktrees."""
import json
import subprocess
import time
from pathlib import Path

import pytest
from test_safety import repo_and_bridge as repo_and_bridge

from bridge_core import BridgeError, database, load, save


def wait(bridge, repo, task):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = bridge.get_status(str(repo), task['task_id'])
        if state['status'] not in {'queued', 'running', 'testing'}:
            return state
        time.sleep(.05)
    raise AssertionError(state)


def test_dirty_cleanup_requires_force_and_preserves_branch(repo_and_bridge):
    repo, bridge = repo_and_bridge
    task = wait(bridge, repo, bridge.dispatch_task(str(repo), 'test', agent='fake'))
    work = Path(task['worktree'])
    (work / 'added.txt').write_text('keep until abandoned')
    with pytest.raises(BridgeError):
        bridge.cleanup_task(str(repo), task['task_id'])
    assert work.exists()
    result = bridge.cleanup_task(str(repo), task['task_id'], force=True)
    assert result['cleaned_at']
    assert not work.exists()
    subprocess.run(['git', '-C', str(repo), 'show-ref', '--verify',
                    'refs/heads/' + task['branch']], check=True, capture_output=True)


def test_old_continuation_cannot_cleanup_latest_work(repo_and_bridge):
    repo, bridge = repo_and_bridge
    first = wait(bridge, repo, bridge.dispatch_task(str(repo), 'first', agent='fake'))
    second = wait(bridge, repo, bridge.dispatch_task(str(repo), 'second', agent='fake',
                                                  continue_from=first['task_id']))
    with pytest.raises(BridgeError):
        bridge.cleanup_task(str(repo), first['task_id'], force=True)
    assert Path(second['worktree']).exists()
    bridge.cleanup_task(str(repo), second['task_id'], force=True)
    assert not Path(second['worktree']).exists()


def test_tampered_worktree_cannot_delete_other_directory(repo_and_bridge, tmp_path):
    repo, bridge = repo_and_bridge
    task = wait(bridge, repo, bridge.dispatch_task(str(repo), 'test', agent='fake'))
    victim = tmp_path / 'victim'
    victim.mkdir()
    marker = victim / 'precious.txt'
    marker.write_text('untouched')
    with database(repo) as db:
        changed = load(db, task['task_id'])
        changed['worktree'] = str(victim)
        save(db, changed)
    with pytest.raises(BridgeError):
        bridge.cleanup_task(str(repo), task['task_id'], force=True)
    assert marker.read_text() == 'untouched'


def test_active_cleanup_refused(repo_and_bridge):
    repo, bridge = repo_and_bridge
    config = json.loads(bridge.agents_file.read_text())
    config['agents']['fake']['build'][-1] = 'import time; time.sleep(20)'
    bridge.agents_file.write_text(json.dumps(config))
    task = bridge.dispatch_task(str(repo), 'test', agent='fake')
    try:
        with pytest.raises(BridgeError):
            bridge.cleanup_task(str(repo), task['task_id'], force=True)
    finally:
        bridge.cancel_task(str(repo), task['task_id'])
        wait(bridge, repo, task)
        bridge.cleanup_task(str(repo), task['task_id'], force=True)
