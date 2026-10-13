"""Real stdio protocol integration, not mocked MCP handlers."""
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    'dispatch_task', 'get_status', 'list_tasks', 'get_diff', 'review_task',
    'cancel_task', 'ask_human', 'cleanup_task', 'list_agents',
}


def decode(result):
    assert not result.isError, result
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_stdio_discovery_and_root_refusal(tmp_path):
    env = dict(os.environ, BRIDGE_ALLOWED_ROOTS=json.dumps([str(tmp_path)]))
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(ROOT / 'bridge_server.py')], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=15)) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == TOOLS
            agents = decode(await session.call_tool('list_agents', {}))
            assert agents
            refused = await session.call_tool('dispatch_task', {
                'repo': str(tmp_path.parent), 'prompt': 'do not run', 'agent': 'claude',
            })
            assert refused.isError or 'error' in json.loads(refused.content[0].text)


@pytest.mark.asyncio
async def test_stdio_task_survives_server_restart_then_reviews(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    for args in [('init',), ('config', 'user.name', 'Bridge Test'),
                 ('config', 'user.email', 'bridge@localhost')]:
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    (repo / 'README.md').write_text('original\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'initial'],
                   check=True, capture_output=True)
    agents_file = tmp_path / 'agents.json'
    build = [sys.executable, '-c',
             "from pathlib import Path; import time; time.sleep(4); "
             "Path('result.txt').write_text('MCP lifecycle result\\n')"]
    agents_file.write_text(json.dumps({'agents': {
        'builder': {'family': 'builder', 'build': build},
        'reviewer': {'family': 'reviewer',
                     'review': [sys.executable, '-c', "print('VERDICT: APPROVE')"]},
    }}))
    env = dict(os.environ, BRIDGE_ALLOWED_ROOTS=json.dumps([str(tmp_path)]),
               BRIDGE_AGENTS_FILE=str(agents_file))
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(ROOT / 'bridge_server.py')], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=15)) as session:
            await session.initialize()
            task = decode(await session.call_tool('dispatch_task', {
                'repo': str(repo), 'prompt': 'make result', 'agent': 'builder',
                'test_cmd': [sys.executable, '-c',
                             "from pathlib import Path; assert Path('result.txt').exists()"],
            }))
            task_id = task['task_id']
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                started = decode(await session.call_tool('get_status', {
                    'repo': str(repo), 'task_id': task_id,
                }))
                if started.get('child_pid'):
                    break
                await asyncio.sleep(.1)
            assert started.get('child_pid'), started
    # A fresh server must observe the detached runner and its persisted result.
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=15)) as session:
            await session.initialize()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = decode(await session.call_tool('get_status', {
                    'repo': str(repo), 'task_id': task_id,
                }))
                if status['status'] not in {'queued', 'running', 'testing'}:
                    break
                await asyncio.sleep(.1)
            assert status['status'] == 'done', status
            diff = decode(await session.call_tool('get_diff', {
                'repo': str(repo), 'task_id': task_id,
            }))
            assert 'MCP lifecycle result' in diff['patch']
            assert not diff['truncated']
            assert not (repo / 'result.txt').exists()
            review = decode(await session.call_tool('review_task', {
                'repo': str(repo), 'task_id': task_id, 'agent': 'reviewer',
            }))
            assert review
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = decode(await session.call_tool('get_status', {
                    'repo': str(repo), 'task_id': task_id,
                }))
                if status.get('review', {}).get('verdict') == 'APPROVE':
                    break
                await asyncio.sleep(.1)
            assert status.get('review', {}).get('verdict') == 'APPROVE', status
            result = decode(await session.call_tool('cleanup_task', {
                'repo': str(repo), 'task_id': task_id, 'force': True,
            }))
            assert result
            assert not Path(task['worktree']).exists()
