import json
import sys

import pytest
from test_swarm import lab as lab
from test_swarm import shard, swarm

import bridge_swarm
from bridge_core import Bridge, database, records


def test_exception_stops_task_created_before_swarm_checkpoint(lab, monkeypatch):
    repo, bridge = lab
    manager = swarm(bridge)
    c = manager.start(
        str(repo), [shard("one")], agent="builder", reviewer="reviewer", workers=2, autostart=False
    )
    monkeypatch.setattr(Bridge, "_spawn", lambda *args: None)

    def fail(self, repo, swarm_id):
        self.bridge.dispatch_task(
            repo, f"SWARM {swarm_id} SHARD one ATTEMPT 1\nWRITE:one.py", agent="builder"
        )
        raise RuntimeError("checkpoint window")

    monkeypatch.setattr(bridge_swarm.Swarm, "tick", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bridge_swarm.py",
            "--run",
            str(repo),
            c["swarm_id"],
            "--roots",
            json.dumps([str(repo)]),
            "--agents",
            str(bridge.agents_file),
            "--capacity",
            "2",
        ],
    )
    with pytest.raises(RuntimeError, match="checkpoint window"):
        bridge_swarm.main()
    with database(repo) as db:
        owned = records(db)
    assert len(owned) == 1 and owned[0].get("stop_request") == "cancelled"
