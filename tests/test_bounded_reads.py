"""Byte reads and Git diff capture are bounded before allocating content."""
import subprocess
from pathlib import Path

from test_swarm import lab as lab
from test_tested_content import build

from bridge_review import _snapshot_fingerprint


def test_snapshot_hash_never_reads_whole_file(lab, monkeypatch):
    repo, bridge = lab
    (repo / 'large.txt').write_bytes(b'x' * 2000000)
    def refused(path):
        raise AssertionError('whole-file read is forbidden')
    monkeypatch.setattr(Path, 'read_bytes', refused)
    assert len(_snapshot_fingerprint(repo, 'baseline')) == 64


def test_git_diff_uses_bounded_streams(lab, monkeypatch):
    repo, bridge, task = build(lab)
    (Path(task['worktree']) / 'large.txt').write_text('large-source-line\n' * 20000)
    original = subprocess.run
    def guarded(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[0] == 'git' and 'diff' in argv:
            assert kwargs.get('stdout') != subprocess.PIPE and not kwargs.get('capture_output'), 'unbounded diff capture'
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', guarded)
    result = bridge.get_diff(str(repo), task['task_id'], max_chars=100)
    assert len(result['patch']) == 100
    assert result['truncated'] and result['total_chars'] > 100


def test_fingerprint_does_not_capture_whole_staged_diff(lab, monkeypatch):
    repo, bridge = lab
    original = subprocess.run
    def guarded(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[0] == 'git' and 'diff' in argv:
            assert not kwargs.get('capture_output'), 'unbounded staged diff capture'
        if isinstance(argv, list) and argv[0] == 'git' and 'ls-files' in argv:
            assert not kwargs.get('capture_output'), 'unbounded review file manifest'
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', guarded)
    assert len(_snapshot_fingerprint(repo, 'baseline')) == 64
