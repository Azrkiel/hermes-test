"""Capability boundaries not covered by the shorter coding pipeline tests."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp import decode
from test_swarm import lab as lab
from test_swarm import shard, swarm

from bridge_core import BridgeError

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("size", [200, 201])
def test_queue_admission_limit_before_workers(lab, size):
    repo, bridge = lab
    manager = swarm(bridge)
    tasks = [shard(f"module_{i}") for i in range(size)]
    options = dict(agent="builder", reviewer="reviewer", workers=2, autostart=False)
    if size == 201:
        with pytest.raises(BridgeError, match="1 to 200"):
            manager.start(str(repo), tasks, **options)
        assert not (repo / ".bridge").exists()
    else:
        result = manager.start(str(repo), tasks, **options)
        assert result["counts"] == {"pending": 200}
        assert bridge.list_tasks(str(repo)) == []


@pytest.mark.parametrize("stop_tool", ["ask_human", "cancel_task"])
def test_stdio_list_and_task_stop_handlers(lab, stop_tool):
    repo, bridge = lab
    config = json.loads(bridge.agents_file.read_text())
    config["agents"]["builder"]["build"] = [sys.executable, "-c", "import time;time.sleep(30)"]
    bridge.agents_file.write_text(json.dumps(config))

    async def run():
        env = dict(os.environ, BRIDGE_ALLOWED_ROOTS=json.dumps([str(repo)]),
                   BRIDGE_AGENTS_FILE=str(bridge.agents_file), BRIDGE_MAX_PARALLEL="2")
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "bridge_server.py")], cwd=str(ROOT), env=env)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                task = decode(await client.call_tool("dispatch_task", {
                    "repo": str(repo), "agent": "builder", "prompt": "Synthetic stop-handler probe"}))
                args = {"repo": str(repo), "task_id": task["task_id"]}
                response = await client.call_tool("list_tasks", {"repo": str(repo)})
                assert not response.isError
                listed = response.structuredContent
                if listed is None:
                    listed = [json.loads(item.text) for item in response.content]
                    if len(listed) == 1 and isinstance(listed[0], list):
                        listed = listed[0]
                if isinstance(listed, dict) and set(listed) == {"result"}:
                    listed = listed["result"]
                assert isinstance(listed, list), response
                assert any(item["task_id"] == task["task_id"] for item in listed)
                stop_args = dict(args)
                if stop_tool == "ask_human":
                    stop_args["question"] = "Synthetic question: choose an acceptance criterion."
                decode(await client.call_tool(stop_tool, stop_args))
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    status = decode(await client.call_tool("get_status", args))
                    if status["status"] not in ("queued", "running", "testing"):
                        break
                    await asyncio.sleep(.1)
                expected = "needs_human" if stop_tool == "ask_human" else "cancelled"
                assert status["status"] == expected, status
                if stop_tool == "ask_human":
                    assert status["question"] == stop_args["question"]

    asyncio.run(run())
