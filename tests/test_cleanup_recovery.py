"""Retry a partial filesystem cleanup without deleting unregistered paths."""
import json
from pathlib import Path

import pytest
from test_cleanup import wait
from test_safety import repo_and_bridge as repo_and_bridge

import bridge_core


def test_cleanup_retry_after_snapshot_removal_failure(repo_and_bridge, monkeypatch):
    repo, bridge = repo_and_bridge
    config = json.loads(bridge.agents_file.read_text())
    command = config['agents']['fake']['build'][0]
    config['agents']['reviewer'] = {
        'family': 'independent',
        'review': [command, '-c', "print('VERDICT: APPROVE')"],
    }
    bridge.agents_file.write_text(json.dumps(config))
    task = wait(bridge, repo, bridge.dispatch_task(str(repo), 'no change', agent='fake'))
    review = bridge.review_task(str(repo), task['task_id'], 'reviewer')
    assert wait(bridge, repo, review)['verdict'] == 'APPROVE'
    snapshot_root = repo / '.bridge' / 'reviews' / review['task_id']
    original_remove = bridge_core.shutil.rmtree
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if Path(path) == snapshot_root and not failed:
            failed = True
            raise PermissionError('injected transient snapshot lock')
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(bridge_core.shutil, 'rmtree', fail_once)
    with pytest.raises(PermissionError, match='transient snapshot lock'):
        bridge.cleanup_task(str(repo), task['task_id'])
    assert not Path(task['worktree']).exists()
    assert snapshot_root.exists()
    cleaned = bridge.cleanup_task(str(repo), task['task_id'])
    assert cleaned['cleaned_at']
    assert not snapshot_root.exists()
    assert bridge.cleanup_task(str(repo), task['task_id'])['cleaned_at'] == cleaned['cleaned_at']
