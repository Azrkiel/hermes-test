"""Force cancellation across real task persistence, without timing races."""
from test_swarm import lab as lab
from test_swarm import shard, swarm

from bridge_core import database, load, records, save


def test_cancel_before_dispatch_prevents_new_task(lab, monkeypatch):
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder',
                            reviewer='reviewer', workers=2, autostart=False)
    original = bridge.dispatch_task
    monkeypatch.setattr(bridge, '_spawn', lambda *args: None)

    def boundary(*args, **kwargs):
        manager.cancel(str(repo), created['swarm_id'])
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, 'dispatch_task', boundary)
    state = manager.tick(str(repo), created['swarm_id'])
    with database(repo) as db:
        tasks = records(db)
        for task in tasks:
            task['status'] = 'cancelled'
            save(db, task)
    assert not tasks, 'dispatch created a task after cancellation was durable'
    assert state['status'] == 'cancelled'
    assert state['items'][0]['status'] == 'cancelled'


def test_cancel_after_dispatch_is_reconciled_before_checkpoint(lab, monkeypatch):
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder',
                            reviewer='reviewer', workers=2, autostart=False)
    original = bridge.dispatch_task
    monkeypatch.setattr(bridge, '_spawn', lambda *args: None)

    def boundary(*args, **kwargs):
        task = original(*args, **kwargs)
        manager.cancel(str(repo), created['swarm_id'])
        return task

    monkeypatch.setattr(bridge, 'dispatch_task', boundary)
    state = manager.tick(str(repo), created['swarm_id'])
    with database(repo) as db:
        task = load(db, state['items'][0]['task_id'])
        assert task.get('stop_request') == 'cancelled'
        task['status'] = 'cancelled'
        save(db, task)
    assert state['items'][0]['status'] == 'draining', 'checkpoint overwrote cancellation'
    assert state['items'][0]['terminal_after_drain'] == 'cancelled'
    assert manager.tick(str(repo), created['swarm_id'])['status'] == 'cancelled'


def test_cancel_before_review_prevents_new_reviewer(lab, monkeypatch):
    from test_tested_content import build
    repo, bridge = lab
    manager = swarm(bridge)
    created = manager.start(str(repo), [shard('one')], agent='builder',
                            reviewer='reviewer', workers=2, autostart=False)
    _, _, task = build(lab)
    with database(repo) as db:
        data = manager._load(db, created['swarm_id'])
        data['items'][0].update(task_id=task['task_id'], status='waiting_review', attempt=1)
        manager._save(db, data)
    original = bridge.review_task
    monkeypatch.setattr(bridge, '_spawn', lambda *args: None)

    def boundary(*args, **kwargs):
        manager.cancel(str(repo), created['swarm_id'])
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, 'review_task', boundary)
    state = manager.tick(str(repo), created['swarm_id'])
    with database(repo) as db:
        assert not any(task['kind'] == 'review' for task in records(db))
    assert state['status'] == 'cancelled'
