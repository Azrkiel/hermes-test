# Bounded unattended swarms

A swarm is available through four MCP tools (13 tools total). Every shard must have non-overlapping ownership and an explicit test command. OpenCode uses `workspace_policy: opencode-headless-ask`: no `--auto`, fresh per-job approval/session storage, native `PWD` equal to the worktree, and automatic rejection of permission requests in the headless client. Tests run through the trusted bridge wrapper, not through unrestricted model shell commands. This is a CLI tool boundary, not an OS sandbox.

The swarm queues independent coding shards, runs their supplied tests and requests fresh independent review. It does **not** merge, push, edit the main checkout or approve a whole integrated project. `ready` means each isolated shard is currently tested and independently approved relative to the recorded baseline. Integration is a separate action.

## Operating contract

- Clean committed Git repository inside the configured narrow allowed roots.
- Up to 200 shards; non-overlapping literal owned files or directory prefixes. Tests must be supplied as trusted argv, not a shell string. The worker must not change frozen tests.
- Concurrency is bounded **per repository** by both `workers` and `BRIDGE_MAX_PARALLEL`. Default 3. Different repositories are not protected by a machine-wide RAM budget; lower capacity when memory is tight.
- OpenCode `opencode/big-pickle` builds. An explicitly configured `claude-haiku-max` fallback runs once for a failed test or `CHANGES_REQUESTED`, using Claude Pro, Haiku 5.5 and effort `max`.
- Codex reviews read-only immutable snapshots. Only one unambiguous final `VERDICT: APPROVE`, successful tests for those bytes and current evidence permit approval.
- Authentication, routing, permissions, timeouts and failed reviewers stop fail-closed as `needs_attention`. There is no silent sandbox relaxation, model substitution or unlimited retry.
- `timeout_min` is the budget for one build-plus-test invocation or one review invocation, starting when its runner acquires a slot. Queueing and snapshot preparation are separate. Set realistic budgets for large suites.
- On Windows, an active coordinator holds a thread-scoped system wake request. The display may sleep. The request is restored on normal completion and exceptions; process exit releases it. This does not override explicit user sleep, lid/power policy, shutdown, reboot or hardware failure.
- The coordinator survives closing the initiating MCP server. Status polling recovers a dead coordinator at most three times; no boot-time service or automatic reboot recovery is installed. An alive but hung coordinator is not forcibly replaced.
- Cancellation is cooperative: all discovered associated builds/reviews receive stop requests, including crash-window reviews missing from the swarm checkpoint. Task/review creation checks the durable swarm state inside its SQLite transaction, and checkpoints reconcile concurrent cancellation before saving. Cancelled queued tasks never launch a worker; live work drains before a normal terminal result. Invocation containment/deadlines still apply.
- Git output is spooled with an 8 MiB safety ceiling and bounded RAM reads. File/test review evidence is limited to 200,000 characters. Large jobs may need smaller shards rather than truncating required evidence.
- Ignored source changes are rejected, even inside owned paths, when omitted from the immutable review snapshot. Only known pytest metadata files are exempt from ignored-file scope scans. Executable `.pyc`/`.pyo` files are not exempt: they are included in fingerprints and snapshots. Before acceptance commands, executable Python caches are removed from the isolated worktree; normal Python tests receive a fresh private `PYTHONPYCACHEPREFIX`. Commands that ignore this environment and generate worktree bytecode must use `-B` or otherwise avoid artifact changes; changed tested bytes fail closed.
- Worktrees isolate Git collisions, **not OS access**. Trusted CLI configuration and root allowlists are not an OS sandbox. Big Pickle's free service may use submitted data to improve the model; do not submit confidential repositories without approval.

## MCP usage

The existing nine task tools remain available. Four swarm tools are added:

- `dispatch_swarm(repo, tasks, agent="opencode", reviewer="codex", fallback="claude-haiku-max", workers=3, timeout_min=15)`
- `get_swarm(repo, swarm_id)` — refresh approvals and recover an eligible dead coordinator.
- `cancel_swarm(repo, swarm_id)` — request cancellation for the full associated task group.
- `list_swarms(repo, limit=20)` — return fresh status, newest first.

Restart/reload the MCP connection to refresh discovery after installation. The server itself requires no always-running UI. The native alias and subscription authentication must already be configured; `list_agents` does not prove login/model availability.

Example task element:

```json
{
  "name": "normalize",
  "files": ["normalize_text.py"],
  "prompt": "Implement normalize_text.py to satisfy the frozen tests. Edit only the owned file.",
  "test_cmd": ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_normalize.py"]
}
```

Inspect per-item `task_id` and `review_id` with `get_status` when a swarm needs attention. Correct the reported issue, rerun tests and request fresh review; never overwrite an old verdict or reuse approval after a continuation. Do not delete failed-run records to make readiness look green.

## Standalone CLI

Use the source checkout's Python environment with native Windows paths. Set `BRIDGE_ALLOWED_ROOTS` to a JSON array of narrow allowed roots and `BRIDGE_AGENTS_FILE` to trusted agent JSON, or pass `--roots` and `--agents` explicitly.

A plan is a JSON object with `tasks`, and optionally `agent`, `reviewer`, `fallback`, `workers`, `timeout_min`. The Python API/CLI default has no fallback unless supplied; the MCP dispatch default is `claude-haiku-max`.

```bash
.venv/Scripts/python.exe bridge_swarm.py --start "C:/path/to/repo" plan.json --agents "C:/path/to/agents.json"
.venv/Scripts/python.exe bridge_swarm.py --status "C:/path/to/repo" SWARM_ID
.venv/Scripts/python.exe bridge_swarm.py --cancel "C:/path/to/repo" SWARM_ID
.venv/Scripts/python.exe bridge_swarm.py --list "C:/path/to/repo"
```

The source-checkout installation is supported. An external agent configuration is required for installed wheels because the legacy fallback JSON is not packaged. Never include credentials, `.venv`, `.bridge` or private verification evidence in publication.

## Frozen contracts

Ownership cannot include test/spec folders or `test_*`, `*_test.*`, `*.test.*`, `*.spec.*` files. Baseline tests and existing test-command script paths are inferred. For opaque/custom runners, provide `frozen_tests` paths in each shard (for example `"frozen_tests": ["checks/"]`). All identified files are byte-hashed in the new checkout before the builder launches. Their hashes are inherited across fallback continuations and verified before the test command, independent review and fresh readiness. Renames are treated as deletion plus addition so both paths must satisfy ownership.
