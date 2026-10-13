"""Adversarial path and input tests against real git repositories."""
import json
import os
import subprocess
import sys

import pytest

from bridge_core import Bridge, BridgeError


@pytest.fixture
def repo_and_bridge(tmp_path):
    repo = tmp_path / 'allowed' / 'repo'
    repo.mkdir(parents=True)
    for args in [('init',), ('config', 'user.name', 'Test'),
                 ('config', 'user.email', 'test@localhost')]:
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    (repo / 'readme.txt').write_text('baseline\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'baseline'],
                   check=True, capture_output=True)
    config = tmp_path / 'agents.json'
    config.write_text(json.dumps({'agents': {
        'fake': {'family': 'fake', 'build': [sys.executable, '-c', 'pass']},
    }}))
    return repo, Bridge(roots=[str(repo.parent)], agents_file=config)


def test_bridge_directory_cannot_redirect_state_outside_root(repo_and_bridge, tmp_path):
    repo, bridge = repo_and_bridge
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = repo / '.bridge'
    if os.name == 'nt':
        cmd = os.environ.get('COMSPEC', 'C:/Windows/System32/cmd.exe')
        result = subprocess.run([cmd, '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True)
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(BridgeError):
            bridge.dispatch_task(str(repo), 'do not run', agent='fake')
        assert list(outside.iterdir()) == [], 'unsafe state directories were created'
    finally:
        if link.exists():
            link.rmdir() if os.name == 'nt' else link.unlink()


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_invalid_timeout_refused_before_state_creation(repo_and_bridge, timeout):
    repo, bridge = repo_and_bridge
    with pytest.raises(BridgeError):
        bridge.dispatch_task(str(repo), 'do not run', agent='fake', timeout_min=timeout)
    assert not (repo / '.bridge').exists()


def test_malformed_test_argv_refused_before_state_creation(repo_and_bridge):
    repo, bridge = repo_and_bridge
    with pytest.raises(BridgeError):
        bridge.dispatch_task(str(repo), 'do not run', agent='fake', test_cmd=[sys.executable, 123])
    assert not (repo / '.bridge').exists()


def test_env_resource_limits_are_respected(repo_and_bridge, monkeypatch):
    repo, bridge = repo_and_bridge
    monkeypatch.setenv('BRIDGE_MAX_PARALLEL', '1')
    monkeypatch.setenv('BRIDGE_TIMEOUT_MIN', '0.25')
    configured = Bridge(roots=[str(repo)], agents_file=bridge.agents_file)
    assert configured.max_parallel == 1
    assert configured.timeout_min == 0.25
