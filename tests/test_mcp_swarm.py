"""Exercise all four swarm tools through real stdio MCP, with deterministic CLIs."""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_swarm import lab as lab
from test_swarm import shard

ROOT = Path(__file__).resolve().parents[1]


def test_four_swarm_tools_end_to_end(lab):
    repo, bridge = lab
    async def run():
        env = dict(os.environ, BRIDGE_ALLOWED_ROOTS=json.dumps([str(repo)]),
                   BRIDGE_AGENTS_FILE=str(bridge.agents_file), BRIDGE_MAX_PARALLEL='2')
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'bridge_server.py')],
                                       cwd=str(ROOT), env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                async def call(name, args):
                    result = await client.call_tool(name, args)
                    assert not result.isError, result.content
                    parsed = result.structuredContent
                    if parsed is None:
                        parsed = json.loads(result.content[0].text)
                    return parsed['result'] if isinstance(parsed, dict) and set(parsed) == {'result'} else parsed
                state = await call('dispatch_swarm', {'repo': str(repo),
                    'tasks': [dict(shard('one'), prompt='SLEEP:8 WRITE:one.py implement assigned shard')],
                    'agent': 'builder', 'reviewer': 'reviewer', 'fallback': None, 'workers': 2})
                args = {'repo': str(repo), 'swarm_id': state['swarm_id']}
                fetched = await call('get_swarm', args)
                assert fetched['swarm_id'] == state['swarm_id']
                listed = await call('list_swarms', {'repo': str(repo)})
                assert any(item['swarm_id'] == state['swarm_id'] for item in listed)
                await call('cancel_swarm', args)
                for _ in range(100):
                    stopped = await call('get_swarm', args)
                    if stopped['status'] == 'cancelled':
                        assert stopped['counts'] == {'cancelled': 1}
                        return
                    await asyncio.sleep(.2)
                raise AssertionError(stopped)
    asyncio.run(run())
