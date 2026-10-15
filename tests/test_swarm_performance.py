"""Avoid duplicate integrity work without caching approvals across requests."""
from pathlib import Path

import pytest
from test_swarm import lab as lab
from test_swarm import shard, swarm

from bridge_core import database, git
from bridge_review import _snapshot_fingerprint


@pytest.mark.parametrize("recover", [False, True])
def test_status_checks_fresh_approvals_once_after_any_recovery(lab, monkeypatch, recover):
    repo, bridge = lab
    manager = swarm(bridge)
    started = manager.start(str(repo), [shard("one")], agent="builder", reviewer="reviewer",
                            workers=2, autostart=False)
    with database(repo) as db:
        data = manager._load(db, started["swarm_id"])
        data["auto_run"] = recover
        manager._save(db, data)
    seen = []
    original = manager._fresh_approvals

    def checked(path, data):
        seen.append(data["status"])
        return original(path, data)

    def recovered(path, swarm_id, recovery):
        assert recovery is True
        with database(path) as db:
            data = manager._load(db, swarm_id)
            data["status"] = "needs_attention"
            data["items"][0]["status"] = "blocked"
            manager._save(db, data)

    monkeypatch.setattr(manager, "_fresh_approvals", checked)
    monkeypatch.setattr(manager, "_spawn", recovered)
    result = manager.status(str(repo), started["swarm_id"])
    assert seen == ["needs_attention" if recover else "queued"]
    assert result["status"] == seen[0]


def test_snapshot_hash_reads_each_source_file_once(lab, monkeypatch):
    repo, _ = lab
    head = git(repo, "rev-parse", "HEAD")
    source = repo / "README.md"
    opened = []
    original = Path.open

    def counted(path, *args, **kwargs):
        if path == source and args and args[0] == "rb":
            opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    assert len(_snapshot_fingerprint(repo, head)) == 64
    assert len(opened) == 1


def test_snapshot_hash_keeps_file_mode_binding(lab):
    repo, _ = lab
    head = git(repo, "rev-parse", "HEAD")
    source = repo / "README.md"
    original_mode = source.stat().st_mode & 0o777
    before = _snapshot_fingerprint(repo, head)
    try:
        source.chmod(0o444)
        assert _snapshot_fingerprint(repo, head) != before
    finally:
        source.chmod(original_mode)
