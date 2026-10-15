"""Real builds bind passing tests to the exact reviewed artifact."""
import time
from pathlib import Path

import pytest
from test_swarm import lab as lab
from test_swarm import shard

from bridge_core import ACTIVE, BridgeError


def build(lab, continue_from=None):
    repo, bridge = lab
    spec = shard('one')
    task = bridge.dispatch_task(str(repo), spec['prompt'], 'builder', spec['test_cmd'],
                                continue_from=continue_from)
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


def binary_layout(worktree):
    first = Path(worktree) / 'z/a.dat'
    first.parent.mkdir()
    first.write_bytes(b'')
    mode = first.stat().st_mode & 0o777
    separator = bytes([0])
    first.write_bytes(b'X' + separator + b'z/b.dat' + separator +
                      str(mode).encode() + separator + b'Y')
    return first, first.with_name('b.dat'), mode


def split_layout(first, second, mode):
    first.write_bytes(b'X')
    second.write_bytes(b'Y')
    second.chmod(mode)
    assert second.stat().st_mode & 0o777 == mode


def test_source_fingerprint_unambiguously_frames_binary_files(lab):
    from bridge_core import git
    from bridge_review import fingerprint_worktree
    repo, _ = lab
    first, second, mode = binary_layout(repo)
    head = git(repo, 'rev-parse', 'HEAD')
    before = fingerprint_worktree(repo, head)
    split_layout(first, second, mode)
    assert fingerprint_worktree(repo, head) != before


def test_binary_layout_change_invalidates_an_approved_artifact(lab):
    repo, bridge, original = build(lab)
    first, second, mode = binary_layout(original['worktree'])
    # A real continuation reruns tests on the original binary layout before review.
    repo, bridge, task = build(lab, continue_from=original['task_id'])
    bridge.review_task(str(repo), task['task_id'], 'reviewer')
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = bridge.get_status(str(repo), task['task_id'], log_lines=0)
        if status.get('review', {}).get('status') not in ACTIVE:
            break
        time.sleep(.1)
    assert status['review_status'] == 'approved', status
    split_layout(first, second, mode)
    assert bridge.get_status(str(repo), task['task_id'], log_lines=0)['review_status'] == 'invalidated'
