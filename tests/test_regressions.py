"""Real Git/process regressions for lossless cleanup and invocation ownership."""
import os
import sys
import time
from pathlib import Path

import psutil
import pytest
from test_cleanup import wait
from test_safety import repo_and_bridge as repo_and_bridge

import bridge_runner
from bridge_core import BridgeError, git, load


def completed_task(repo, bridge):
    return wait(bridge, repo, bridge.dispatch_task(str(repo), 'no changes', agent='fake'))


def test_cleanup_refuses_ignored_uncommitted_files(repo_and_bridge):
    repo, bridge = repo_and_bridge
    (repo / '.gitignore').write_text('valuableignored.txt\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'ignore valuable file')
    task = completed_task(repo, bridge)
    valuable = Path(task['worktree']) / 'valuableignored.txt'
    valuable.write_text('irreplaceable ignored data')
    with pytest.raises(BridgeError, match='ignored'):
        bridge.cleanup_task(str(repo), task['task_id'], force=False)
    assert valuable.read_text() == 'irreplaceable ignored data'
    bridge.cleanup_task(str(repo), task['task_id'], force=True)
    assert not valuable.exists()


@pytest.mark.parametrize('cleanup_database_fails', [False, True])
def test_monitor_failure_reaps_child_and_preserves_primary_error(repo_and_bridge, monkeypatch, cleanup_database_fails):
    from contextlib import contextmanager
    repo, bridge = repo_and_bridge
    task = completed_task(repo, bridge)
    spawned = []
    real_popen = bridge_runner.subprocess.Popen
    real_database = bridge_runner.database
    calls = 0

    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        spawned.append(child)
        return child

    @contextmanager
    def failing_database(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('primary monitor failure')
        if calls == 3 and cleanup_database_fails:
            raise RuntimeError('secondary database failure')
        with real_database(*args) as db:
            yield db

    monkeypatch.setattr(bridge_runner.subprocess, 'Popen', capture)
    monkeypatch.setattr(bridge_runner, 'database', failing_database)
    try:
        with pytest.raises(RuntimeError) as caught:
            bridge_runner.execute(repo, task['task_id'], [sys.executable, '-c', 'import time; time.sleep(20)'],
                                  'regression.log', time.monotonic() + 10, os.environ.copy())
        assert spawned[0].poll() is not None, 'owned child survived monitor exception'
        assert str(caught.value) == 'primary monitor failure'
        if not cleanup_database_fails:
            with real_database(repo) as db:
                assert load(db, task['task_id'])['child_pid'] is None
    finally:
        for child in spawned:
            if child.poll() is None:
                from bridge_core import terminate_tree
                terminate_tree(child.pid, psutil.Process(child.pid).create_time())
            child.wait(timeout=10)


@pytest.mark.parametrize('exit_code', [0, 7])
def test_fast_parent_exit_reaps_background_descendant(repo_and_bridge, exit_code):
    repo, bridge = repo_and_bridge
    task = completed_task(repo, bridge)
    marker = Path(task['worktree']) / 'background.pid'
    script = (
        'import subprocess,sys,psutil,json; from pathlib import Path; '
        'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(20)"], '
        'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
        f'Path({str(marker)!r}).write_text(json.dumps([p.pid,psutil.Process(p.pid).create_time()])); '
        f'sys.exit({exit_code})'
    )
    identity = None
    try:
        assert bridge_runner.execute(repo, task['task_id'], [sys.executable, '-c', script],
                                     'background.log', time.monotonic() + 10, os.environ.copy()) == exit_code
        import json
        identity = json.loads(marker.read_text())
        from bridge_core import alive
        deadline = time.monotonic() + 3
        while alive(*identity) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not alive(*identity), 'background descendant outlived finished invocation'
    finally:
        if marker.exists():
            import json

            from bridge_core import terminate_tree
            terminate_tree(*json.loads(marker.read_text()))


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object containment')
def test_job_assignment_failure_never_runs_uncontained_child(repo_and_bridge, monkeypatch):
    import win32job
    repo, bridge = repo_and_bridge
    task = completed_task(repo, bridge)
    marker = Path(task['worktree']) / 'must-not-run.txt'
    children = []
    real_popen = bridge_runner.subprocess.Popen

    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def refuse(*args):
        raise RuntimeError('Job assignment refused')

    monkeypatch.setattr(bridge_runner.subprocess, 'Popen', capture)
    monkeypatch.setattr(win32job, 'AssignProcessToJobObject', refuse)
    with pytest.raises(RuntimeError, match='Job assignment refused'):
        bridge_runner.execute(repo, task['task_id'],
                              [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).write_text("escaped")'],
                              'refused.log', time.monotonic() + 10, os.environ.copy())
    assert children[0].poll() is not None
    assert not marker.exists()
