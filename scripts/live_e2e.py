"""Exercise real subscription-backed build -> tests -> diff -> independent review.

Run after scripts/probe_clis.py. This consumes real CLI requests, not fake agents.
The demo main checkout is intentionally left unchanged. Evidence stays local.
"""
import asyncio
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def decode(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    return json.loads(result.content[0].text)


async def main():
    repo = ROOT / 'examples' / 'demo-repo'
    launch = json.loads((ROOT / 'verification' / 'mcp-launch.json').read_text())
    # Use exactly the registered MCP env, not the parent agent's API credentials.
    env = dict(launch['env'], BRIDGE_TIMEOUT_MIN='3')
    params = StdioServerParameters(command=launch['command'], args=launch['args'], env=env)
    target = ROOT / 'verification' / 'live-e2e.json'
    continuing = '--continue' in sys.argv
    if continuing:
        evidence = json.loads(target.read_text())
        evidence.setdefault('history', []).append({
            key: evidence.get(key) for key in ('dispatch', 'build_status', 'diff', 'final_status')
        })
        previous_id = evidence['dispatch']['task_id']
    else:
        evidence = {'repo': str(repo), 'builder': 'claude', 'reviewer': 'codex'}
        previous_id = None

    def persist():
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(evidence, indent=2), encoding='utf-8')

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()
            before = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'],
                                             text=True)
            evidence['main_status_before'] = before
            task = decode(await session.call_tool('dispatch_task', {
                'repo': str(repo), 'agent': 'claude', 'timeout_min': 3,
                'continue_from': previous_id,
                'prompt': ('Add .gitignore with __pycache__/ and *.pyc to exclude generated '
                           'Python bytecode from the diff. Keep slugify.py and acceptance tests '
                           'unchanged. Use file tools only; no shell, commits, merge or push.'
                           if continuing else
                           'Implement slugify.py according to README.md and the existing '
                           'tests/test_slugify.py. Keep existing tests intact. Add no dependencies. '
                           'Use Read and Write/Edit tools; do not execute shell commands. '
                           'The bridge runs the acceptance tests after you finish. '
                           'Do not commit, merge, push, change .git or modify anything outside '
                           'this worktree. Report what you changed.'),
                'test_cmd': [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
            }))
            evidence['dispatch'] = task
            task_id = task['task_id']
            persist()
            print('DISPATCHED', task_id, task['worktree'], flush=True)
            deadline = time.monotonic() + 210
            while time.monotonic() < deadline:
                status = decode(await session.call_tool('get_status', {
                    'repo': str(repo), 'task_id': task_id, 'log_lines': 80,
                }))
                if status['status'] not in {'queued', 'running', 'testing'}:
                    break
                await asyncio.sleep(2)
            evidence['build_status'] = status
            persist()
            print('BUILD', status['status'], status.get('test_exit_code'), flush=True)
            if status['status'] != 'done' or status.get('test_exit_code') != 0:
                raise RuntimeError('Live build/test did not pass; inspect verification/live-e2e.json')
            diff = decode(await session.call_tool('get_diff', {
                'repo': str(repo), 'task_id': task_id,
            }))
            evidence['diff'] = diff
            assert not diff['truncated']
            assert 'slugify.py' in diff['patch']
            source = Path(task['worktree'])
            context = '\n\n'.join('FILE: ' + name + '\n' + (source / name).read_text(encoding='utf-8')
                                   for name in ('README.md', 'slugify.py', 'tests/test_slugify.py', '.gitignore')
                                   if (source / name).exists())
            review = decode(await session.call_tool('review_task', {
                'repo': str(repo), 'task_id': task_id, 'agent': 'codex',
                'prompt': 'Review from the complete relevant file contents below and the '
                          'freshly executed acceptance test output. Do not call tools, shell '
                          'commands, external skills, or network services. The read-only shell '
                          'is blocked by policy on this host; all material needed for this '
                          'small review is supplied here. Check requirements, edge cases, '
                          'source logic and test coverage. Do not claim you ran tests; the '
                          'bridge already ran them.\n\n' + context +
                          '\n\nBRIDGE TEST OUTPUT:\n' + status.get('test_log', ''),
            }))
            evidence['review_dispatch'] = review
            persist()
            print('REVIEW_QUEUED', flush=True)
            deadline = time.monotonic() + 210
            while time.monotonic() < deadline:
                status = decode(await session.call_tool('get_status', {
                    'repo': str(repo), 'task_id': task_id, 'log_lines': 80,
                }))
                review_data = status.get('review', {})
                if review_data.get('status') not in {None, 'queued', 'running', 'testing'}:
                    break
                await asyncio.sleep(2)
            evidence['final_status'] = status
            after = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'],
                                            text=True)
            evidence['main_status_after'] = after
            assert before == after, 'main checkout changed'
            assert not (repo / 'slugify.py').exists(), 'implementation leaked into main checkout'
            persist()
            print('REVIEW', review_data.get('status'), review_data.get('verdict'), flush=True)
            if review_data.get('verdict') != 'APPROVE':
                raise RuntimeError('Live reviewer did not approve; inspect the saved evidence')
            print('LIVE_E2E_PASSED', target, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
