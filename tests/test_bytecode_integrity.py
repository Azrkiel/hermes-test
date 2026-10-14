"""Acceptance commands must execute frozen Python source, not forged caches."""
import json
import sys
import time

import pytest
from test_swarm import lab as lab

from bridge_core import ACTIVE, git


@pytest.mark.parametrize('flags', [[], ['-E'], ['-I']])
def test_forged_unchecked_bytecode_cannot_replace_a_frozen_contract(lab, flags):
    repo, bridge = lab
    (repo / 'test_contract.py').write_text('VALUE=0\n')
    (repo / '.gitignore').write_text('__pycache__/\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'frozen failing contract')
    config = json.loads(bridge.agents_file.read_text())
    config['agents']['builder']['build'] = [sys.executable, '-c',
        "from pathlib import Path;import py_compile;"
        "p=Path('test_contract.py');original=p.read_bytes();"
        "p.write_text('VALUE=1\\n');"
        "py_compile.compile(str(p),invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH);"
        "p.write_bytes(original);Path('one.py').write_text('VALUE=42\\n')"]
    bridge.agents_file.write_text(json.dumps(config))
    task = bridge.dispatch_task(str(repo), 'build', 'builder',
        [sys.executable, *flags, '-c', 'import os,sys;sys.path.insert(0,os.getcwd());import test_contract;assert test_contract.VALUE==1'],
        frozen_paths=['test_contract.py'])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        task = bridge.get_status(str(repo), task['task_id'], log_lines=0)
        if task['status'] not in ACTIVE:
            break
        time.sleep(.1)
    assert task['test_exit_code'] == 1, 'forged cached test was accepted instead of frozen source'
    assert task['status'] == 'tests_failed'


def test_ignored_bytecode_changes_invalidate_the_artifact_fingerprint(lab):
    from bridge_review import fingerprint_worktree
    repo, _ = lab
    (repo / '.gitignore').write_text('__pycache__/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'ignored caches')
    head = git(repo, 'rev-parse', 'HEAD')
    before = fingerprint_worktree(repo, head)
    cache = repo / '__pycache__'
    cache.mkdir()
    (cache / 'contract.pyc').write_bytes(b'executable cache')
    assert fingerprint_worktree(repo, head) != before


def test_executable_cache_is_not_exempt_from_shard_ownership(lab):
    from bridge_swarm import Swarm
    repo, bridge = lab
    (repo / '.gitignore').write_text('__pycache__/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'ignored caches')
    head = git(repo, 'rev-parse', 'HEAD')
    cache = repo / '__pycache__'
    cache.mkdir()
    (cache / 'contract.pyc').write_bytes(b'executable cache')
    task = {'worktree': str(repo), 'base_head': head}
    item = {'spec': {'files': ['one.py']}}
    assert Swarm(bridge)._scope_error(item, task) == 'worker changed files outside ownership'
