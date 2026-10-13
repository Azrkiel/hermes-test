"""Stdio MCP facade for durable agent-bridge runners.

All lifecycle operations live in bridge_core; closing this server does not stop
workers. stdout is reserved for the MCP protocol.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP

from bridge_core import Bridge

mcp = FastMCP('agent-bridge')


@lru_cache(maxsize=1)
def bridge() -> Bridge:
    return Bridge()


@mcp.tool()
def dispatch_task(repo: str, prompt: str, agent: str = 'claude',
                  test_cmd: str | list[str] | None = None,
                  continue_from: str | None = None,
                  timeout_min: float | None = None) -> dict[str, Any]:
    """Start an isolated coding task; return task_id immediately. No merge/push.

    Repo must be under allowed roots. Dirty main-checkout edits are not copied.
    test_cmd is an explicitly trusted shell command or argv list. continue_from
    retains an earlier task's worktree and original diff base, not CLI chat history.
    """
    return bridge().dispatch_task(repo=repo, prompt=prompt, agent=agent,
                                  test_cmd=test_cmd, continue_from=continue_from,
                                  timeout_min=timeout_min)


@mcp.tool()
def get_status(repo: str, task_id: str, log_lines: int = 40) -> dict[str, Any]:
    """Get build/test state, separate review state/verdict, and bounded log tail."""
    return bridge().get_status(repo=repo, task_id=task_id, log_lines=log_lines)


@mcp.tool()
def list_tasks(repo: str, limit: int = 20) -> list[dict[str, Any]]:
    """List recent persisted tasks in an allowed repo, including restart survivors."""
    return bridge().list_tasks(repo=repo, limit=limit)


@mcp.tool()
def get_diff(repo: str, task_id: str, max_chars: int = 50000) -> dict[str, Any]:
    """Get committed, unstaged and untracked changes vs original starting HEAD.

    Check truncation before relying on a patch. Does not stage the user index.
    """
    return bridge().get_diff(repo=repo, task_id=task_id, max_chars=max_chars)


@mcp.tool()
def review_task(repo: str, task_id: str, agent: str = 'codex',
                prompt: str = '') -> dict[str, Any]:
    """Queue isolated review by a different agent family; return immediately.

    Review result appears in get_status. Approval requires a single strict verdict
    and an unchanged snapshot. A changed task invalidates its old approval.
    """
    return bridge().review_task(repo=repo, task_id=task_id, agent=agent, prompt=prompt)


@mcp.tool()
def cancel_task(repo: str, task_id: str) -> dict[str, Any]:
    """Request worker/process-tree cancellation while preserving branch and files."""
    return bridge().cancel_task(repo=repo, task_id=task_id)


@mcp.tool()
def ask_human(repo: str, task_id: str, question: str) -> dict[str, Any]:
    """Record a question, stop running work and mark needs_human. Hermes must relay it."""
    return bridge().ask_human(repo=repo, task_id=task_id, question=question)


@mcp.tool()
def cleanup_task(repo: str, task_id: str, force: bool = False) -> dict[str, Any]:
    """Remove an inactive owned worktree. Dirty work requires explicit force.

    Confirm human merge/abandonment first. Branch is retained; uncommitted files
    discarded by force cannot be recovered from the branch.
    """
    return bridge().cleanup_task(repo=repo, task_id=task_id, force=force)


@mcp.tool()
def list_agents() -> dict[str, Any] | list[dict[str, Any]]:
    """List configured CLI workers, families, commands and availability."""
    return bridge().list_agents()


if __name__ == '__main__':
    mcp.run(transport='stdio')
