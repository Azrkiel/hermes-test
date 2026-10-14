import json
import sys

import pytest
from test_swarm import lab as lab
from test_swarm import shard, swarm, wait_swarm

from bridge_core import BridgeError, git


def test_rename_cannot_delete_outside_owned_path(lab):
    repo, bridge = lab
    (repo / "source.py").write_text("VALUE=42\n")
    git(repo, "add", "source.py")
    git(repo, "commit", "-m", "rename source")
    d = json.loads(bridge.agents_file.read_text())
    d["agents"]["builder"]["build"] = [
        sys.executable,
        "-c",
        "from pathlib import Path;import subprocess;Path('source.py').rename('one.py');subprocess.run(['git','add','-A'],check=True)",
    ]
    bridge.agents_file.write_text(json.dumps(d))
    m = swarm(bridge)
    c = m.start(
        str(repo), [shard("one")], agent="builder", reviewer="reviewer", workers=2, autostart=False
    )
    state = wait_swarm(m, repo, c["swarm_id"])
    assert state["items"][0]["status"] == "scope_violation"


@pytest.mark.parametrize("name", ["tests/", "test_contract.py", "contract.test.js", "specs/"])
def test_test_ownership_rejected_before_state(lab, name):
    repo, bridge = lab
    spec = dict(shard("one"), files=[name])
    with pytest.raises(BridgeError, match="test"):
        swarm(bridge).start(
            str(repo), [spec], agent="builder", reviewer="reviewer", workers=2, autostart=False
        )
    assert not (repo / ".bridge").exists()


def test_hidden_main_edit_invalidates_ready(lab):
    repo, bridge = lab
    m = swarm(bridge)
    c = m.start(
        str(repo), [shard("one")], agent="builder", reviewer="reviewer", workers=2, autostart=False
    )
    assert wait_swarm(m, repo, c["swarm_id"])["status"] == "ready"
    git(repo, "update-index", "--assume-unchanged", "README.md")
    (repo / "README.md").write_text("hidden edit")
    assert git(repo, "status", "--porcelain") == ""
    assert m.status(str(repo), c["swarm_id"])["status"] == "needs_attention"


def test_frozen_tests_checked_before_the_test_command(lab):
    repo, bridge = lab
    (repo / "checks").mkdir()
    (repo / "checks/contract.py").write_text("assert True\n")
    git(repo, "add", "checks/contract.py")
    git(repo, "commit", "-m", "custom contract")
    d = json.loads(bridge.agents_file.read_text())
    d["agents"]["builder"]["build"] = [
        sys.executable,
        "-c",
        "from pathlib import Path;Path('checks/contract.py').write_text('assert False\\n');Path('one.py').write_text('VALUE=42\\n')",
    ]
    bridge.agents_file.write_text(json.dumps(d))
    task = bridge.dispatch_task(
        str(repo),
        "build",
        agent="builder",
        test_cmd=[sys.executable, "-c", "assert True"],
        frozen_paths=["checks/contract.py"],
    )
    import time

    from bridge_core import ACTIVE

    for _ in range(200):
        state = bridge.get_status(str(repo), task["task_id"], log_lines=0)
        if state["status"] not in ACTIVE:
            break
        time.sleep(0.1)
    assert state["test_exit_code"] is None
    assert state["status"] == "failed" and "frozen test" in state["error"]


def test_hidden_tracked_edit_cannot_escape_scope(lab):
    repo, bridge = lab
    d = json.loads(bridge.agents_file.read_text())
    d["agents"]["builder"]["build"] = [
        sys.executable,
        "-c",
        "import subprocess;from pathlib import Path;subprocess.run(['git','update-index','--assume-unchanged','README.md'],check=True);Path('README.md').write_text('hidden');Path('one.py').write_text('VALUE=42\\n')",
    ]
    bridge.agents_file.write_text(json.dumps(d))
    m = swarm(bridge)
    c = m.start(
        str(repo), [shard("one")], agent="builder", reviewer="reviewer", workers=2, autostart=False
    )
    state = wait_swarm(m, repo, c["swarm_id"])
    assert state["items"][0]["status"] == "scope_violation"


def test_custom_contract_ownership_rejected(lab):
    repo, bridge = lab
    (repo / "check.py").write_text("assert True\n")
    git(repo, "add", "check.py")
    git(repo, "commit", "-m", "custom check")
    spec = dict(shard("one"), files=["check.py"], test_cmd=[sys.executable, "check.py"])
    with pytest.raises(BridgeError, match="test"):
        swarm(bridge).start(
            str(repo), [spec], agent="builder", reviewer="reviewer", workers=2, autostart=False
        )
