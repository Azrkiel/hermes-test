"""Crash-window reviews and cancellations must not leave live work behind."""
from pathlib import Path

from test_swarm import lab as lab
from test_swarm import shard, swarm
from test_tested_content import build

from bridge_core import database, load, save


def prepared(lab, monkeypatch):
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder', reviewer='reviewer',
                            workers=2, autostart=False)
    repo, bridge, task = build(lab)
    monkeypatch.setattr(bridge, '_spawn', lambda *args: None)
    review = bridge.review_task(str(repo), task['task_id'], 'reviewer')
    with database(repo) as db:
        data = manager._load(db, created['swarm_id'])
        data['items'][0].update(task_id=task['task_id'], status='building', attempt=1)
        manager._save(db, data)
    return repo, bridge, manager, created, task, review


def test_cancel_discovers_review_committed_before_swarm_checkpoint(lab, monkeypatch):
    repo, bridge, manager, created, task, review = prepared(lab, monkeypatch)
    manager.cancel(str(repo), created['swarm_id'])
    with database(repo) as db:
        record = load(db, review['task_id'])
        assert record.get('stop_request') == 'cancelled'
        record['status'] = 'cancelled'
        save(db, record)


def test_source_mutation_cancels_and_drains_active_reviewer(lab, monkeypatch):
    repo, bridge, manager, created, task, review = prepared(lab, monkeypatch)
    (Path(task['worktree']) / 'one.py').write_text('VALUE=0\n')
    state = manager.tick(str(repo), created['swarm_id'])
    assert state['status'] == 'running'
    assert state['items'][0]['status'] == 'draining'
    with database(repo) as db:
        record = load(db, review['task_id'])
        assert record.get('stop_request') == 'cancelled'
        record['status'] = 'cancelled'
        save(db, record)
    assert manager.tick(str(repo), created['swarm_id'])['status'] == 'needs_attention'


def test_exhausted_recovery_requests_stop_for_discovered_review(lab, monkeypatch):
    repo, bridge, manager, created, task, review = prepared(lab, monkeypatch)
    with database(repo) as db:
        data = manager._load(db, created['swarm_id'])
        data.update(auto_run=True, coordinator_restarts=3, owner_pid=None, owner_created=None)
        manager._save(db, data)
    assert manager.status(str(repo), created['swarm_id'])['status'] == 'needs_attention'
    with database(repo) as db:
        record = load(db, review['task_id'])
        assert record.get('stop_request') == 'cancelled'
        record['status'] = 'cancelled'
        save(db, record)
