"""Build a real wheel and exercise it outside the source checkout."""
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    project = tmp_path_factory.mktemp("wheel-build")
    for name in ("pyproject.toml", "LICENSE", "agents.example.json"):
        shutil.copyfile(ROOT / name, project / name)
    for source in ROOT.glob("bridge_*.py"):
        shutil.copyfile(source, project / source.name)
    if (ROOT / "bridge_defaults").is_dir():
        shutil.copytree(ROOT / "bridge_defaults", project / "bridge_defaults",
                        ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run([sys.executable, "-c", "import setuptools.build_meta as b; b.build_wheel('dist')"],
                            cwd=project, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return next((project / "dist").glob("*.whl"))


def test_wheel_carries_the_canonical_public_defaults(wheel):
    with zipfile.ZipFile(wheel) as archive:
        assert "bridge_defaults/agents.example.json" in archive.namelist()
        assert json.loads(archive.read("bridge_defaults/agents.example.json")) == json.loads(
            (ROOT / "agents.example.json").read_text())


@pytest.mark.parametrize("choice", ["argument", "environment", "user"])
def test_wheel_configuration_precedence(wheel, tmp_path, choice):
    installed = tmp_path / "site"
    with zipfile.ZipFile(wheel) as bundle:
        bundle.extractall(installed)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env.pop("BRIDGE_AGENTS_FILE", None)
    probe = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from bridge_core import Bridge
home = Path.home()
user = home / '.agent-bridge' / 'agents.json'
user.parent.mkdir()
environment = home / 'environment.json'
argument = home / 'argument.json'
for name, path in [('user', user), ('environment', environment), ('argument', argument)]:
    path.write_text(json.dumps({'agents': {name: {'family': 'python', 'build': [sys.executable, '-c', 'pass']}}}))
import os
choice = sys.argv[2]
if choice != 'user':
    os.environ['BRIDGE_AGENTS_FILE'] = str(environment)
bridge = Bridge(agents_file=argument if choice == 'argument' else None)
assert set(bridge.list_agents()) == {choice}, bridge.agents_file
print('CONFIG_PRECEDENCE_OK')
'''
    result = subprocess.run([sys.executable, "-I", "-c", probe, str(installed), choice],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CONFIG_PRECEDENCE_OK" in result.stdout


def test_wheel_defaults_work_without_checkout_or_private_configuration(wheel, tmp_path):
    installed = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    home = tmp_path / "empty-home"
    home.mkdir()
    env = os.environ.copy()
    env.update(HOME=str(home), USERPROFILE=str(home))
    env.pop("BRIDGE_AGENTS_FILE", None)
    script = """
import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import bridge_core
bridge = bridge_core.Bridge(roots=[])
assert Path(bridge_core.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert bridge.agents_file.is_file(), str(bridge.agents_file)
assert bridge.agents_file.resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert {'claude', 'codex', 'opencode', 'claude-haiku-max'} <= set(bridge.list_agents())
print(json.dumps({'packaged_defaults_work': True}))
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(installed)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["packaged_defaults_work"] is True
