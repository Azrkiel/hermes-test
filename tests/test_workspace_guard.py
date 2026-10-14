import json
from pathlib import Path

import pytest
from test_swarm import lab as lab

from bridge_workspace import workspace_guard


def test_guard_blocks_shell_external_paths_and_config_edits_and_restores(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    config = work / 'opencode.jsonc'
    original = b'{"model":"opencode/big-pickle"}\n'
    config.write_bytes(original)
    with workspace_guard({'worktree': str(work)}, {'workspace_policy': 'opencode-files-only'}):
        data = json.loads(config.read_text())
        permission = data['permission']
        assert permission['bash'] == permission['external_directory'] == 'deny'
        assert permission['edit']['../*'] == permission['edit']['opencode.jsonc'] == 'deny'
        assert data['formatter'] is False
    assert config.read_bytes() == original


def test_guard_removes_generated_config_on_error(tmp_path):
    with pytest.raises(RuntimeError, match='primary'):
        with workspace_guard({'worktree': str(tmp_path)}, {'workspace_policy': 'opencode-files-only'}):
            raise RuntimeError('primary')
    assert not (tmp_path / 'opencode.jsonc').exists()


def test_real_runner_applies_guard_only_during_cli(lab):
    import sys

    from test_tested_content import build
    repo, bridge = lab
    data = json.loads(bridge.agents_file.read_text())
    agent = data['agents']['builder']
    agent['workspace_policy'] = 'opencode-headless-ask'
    script = "import os,json;from pathlib import Path;assert Path(os.environ['PWD']).resolve()==Path.cwd();assert Path(os.environ['XDG_DATA_HOME']).parents[1].name=='tasks';d=json.loads(Path('opencode.jsonc').read_text());assert d['formatter'] is False;Path('one.py').write_text('VALUE=42\\n')"
    agent['build'] = [sys.executable, '-c', script + ";import sys;assert 'Do not use shell' in sys.argv[1]", '{prompt}']
    bridge.agents_file.write_text(json.dumps(data))
    _, _, task = build((repo, bridge))
    assert not (Path(task['worktree']) / 'opencode.jsonc').exists()


def test_unrequested_guard_never_changes_worktree(tmp_path):
    with workspace_guard({'worktree': str(tmp_path)}, {}):
        assert list(tmp_path.iterdir()) == []


def test_headless_ask_requires_no_auto_and_preserves_permission_requests(tmp_path):
    with pytest.raises(Exception, match='auto'):
        with workspace_guard({'worktree': str(tmp_path)}, {'workspace_policy': 'opencode-headless-ask', 'build': ['opencode', 'run', '--auto']}):
            pass
    with workspace_guard({'worktree': str(tmp_path)}, {'workspace_policy': 'opencode-headless-ask', 'build': ['opencode', 'run']}):
        permission = json.loads((tmp_path / 'opencode.jsonc').read_text())['permission']
        assert permission['bash'] == permission['external_directory'] == 'ask'
        assert permission['edit']['../*'] == 'ask'


def test_unsafe_auto_config_rejected_before_state_initialization(lab):
    from bridge_core import BridgeError
    repo, bridge = lab
    data=json.loads(bridge.agents_file.read_text())
    data['agents']['builder']['workspace_policy']='opencode-headless-ask'
    data['agents']['builder']['build'].append('--auto')
    bridge.agents_file.write_text(json.dumps(data))
    with pytest.raises(BridgeError):
        bridge.dispatch_task(str(repo),'WRITE:one.py','builder')
    assert not (repo/'.bridge').exists()
