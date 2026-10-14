"""Real builds bind passing tests to the exact reviewed artifact."""
import time
from pathlib import Path

import pytest
from test_swarm import lab as lab
from test_swarm import shard

from bridge_core import ACTIVE, BridgeError


def build(lab):
    repo, bridge = lab
    spec = shard('one')
    task = bridge.dispatch_task(str(repo), spec['prompt'], 'builder', spec['test_cmd'])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = bridge.get_status(str(repo), task['task_id'], log_lines=0)
        if result['status'] not in ACTIVE:
            break
        time.sleep(.1)
    assert result['status'] == 'done' and result['test_exit_code'] == 0, result
    return repo, bridge, result


def test_post_test_edit_cannot_receive_a_fresh_review(lab):
    repo, bridge, task = build(lab)
    (Path(task['worktree']) / 'one.py').write_text('VALUE=0\n')
    with pytest.raises(BridgeError, match='tested'):
        bridge.review_task(str(repo), task['task_id'], 'reviewer')


def test_invalidated_review_keeps_its_active_lifecycle(lab, monkeypatch):
    from bridge_core import database
    repo, bridge, task = build(lab)
    # Persist a real review snapshot but leave its worker queued to observe the seam.
    monkeypatch.setattr(bridge, '_spawn', lambda *args: None)
    review = bridge.review_task(str(repo), task['task_id'], 'reviewer')
    (Path(task['worktree']) / 'one.py').write_text('VALUE=0\n')
    status = bridge.get_status(str(repo), task['task_id'], log_lines=0)
    assert status['review']['status'] == 'queued'
    assert status['review_status'] == 'invalidated'
    assert status['review']['content_status'] == 'invalidated'
    # Retain owned metadata but ensure no synthetic queued fixture remains runnable.
    with database(repo) as db:
        from bridge_core import load, save
        record = load(db, review['task_id'])
        record['status'] = 'cancelled'
        save(db, record)


def test_test_command_mutation_cannot_be_reviewed_as_tested(lab):
    import sys
    repo, bridge = lab
    spec = shard('one')
    cmd = [sys.executable, '-c', "from pathlib import Path;p=Path('one.py');assert p.read_text()=='VALUE=42\\n';p.write_text('VALUE=0\\n')"]
    task = bridge.dispatch_task(str(repo), spec['prompt'], 'builder', cmd)
    deadline = time.monotonic()+60
    while time.monotonic()<deadline:
        task=bridge.get_status(str(repo),task['task_id'],log_lines=0)
        if task['status'] not in ACTIVE:
            break
        time.sleep(.1)
    assert task['test_exit_code']==0
    with pytest.raises(BridgeError,match='tested'):
        bridge.review_task(str(repo),task['task_id'],'reviewer')
