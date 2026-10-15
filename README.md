# agent-bridge

A local **MCP server** connecting Hermes (the planner) to official **Claude Code,
Codex and OpenCode CLIs** (the coding hands). Subscription logins remain with the
CLIs. No Anthropic API key is needed for a Claude Pro/Max CLI login.

```text
you <-> Hermes --MCP--> agent-bridge --> official CLI
                           |                |
                      durable state     task worktree
                           |
                    tests -> diff -> independent review -> human merge
```

This is not a model API router and does not create Nimbalyst UI sessions. Open the
same repo in Nimbalyst or another git GUI to inspect `bridge/...` branches. Open
returned worktree paths to see uncommitted agent edits.

## Tools

| Tool | Purpose |
|---|---|
| `dispatch_task` | Create an isolated worktree/branch and return `task_id` immediately |
| `get_status` | Build/test status, separate review state, question, bounded log tail |
| `list_tasks` | Recent persistent tasks in a repo |
| `get_diff` | Changes since original starting HEAD, including uncommitted/untracked files |
| `review_task` | Asynchronous review by a different CLI family on a separate snapshot |
| `cancel_task` | Stop work, retain branch and files |
| `ask_human` | Record a question, pause work, mark `needs_human`; Hermes relays it |
| `cleanup_task` | Remove an inactive worktree; branch retained |
| `list_agents` | Configured commands and availability |

Build statuses: `queued`, `running`, `testing`, `done`, `no_changes`,
`tests_failed`, `failed`, `timed_out`, `cancelled`, `lost`, `needs_human`.
Review state/verdict are separate from build status. `done` is NOT an approval,
and no test command means tests were not run.

`continue_from` reuses a previous terminal task's worktree and original diff base.
It starts a new task record, not a resumed CLI conversation. Use the latest task,
not a superseded ancestor. Never run concurrent continuations on the same worktree.

## Install

Python 3.11+, git, and at least one working CLI. For independent reviews, two
working CLI families. Prefer `uv`; no global Python dependency changes:

```bash
cd agent-bridge
uv venv .venv
# Linux/macOS:
uv pip install --python .venv/bin/python -r requirements-dev.txt
# Windows (Git Bash; uv is a native Windows executable):
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
```

Check real CLI help/auth before configuring flags:

```bash
claude auth status
claude --help
codex login status
codex exec --help
opencode auth list
opencode run --help
```

The checked-in commands target the CLI releases verified on this installation:
Claude Code 2.1.296, Codex 0.162.1, OpenCode 2.0.19. Flags can change. OpenCode v2
uses `--standalone` / `--auto`; these are not v1 command templates. The live
OpenCode probe on this machine currently fails with `Model exo-free has been
deprecated.` and `auth list` reports no authenticated integrations. Its adapter
is configured, but it is not ready until a usable provider/model is selected.
Claude and Codex subscription probes pass.

Copy `agents.example.json` to `~/.agent-bridge/agents.json`, or specify its path in
`BRIDGE_AGENTS_FILE`. Agent command config is trusted local code. Add custom hands
or variants without changing the MCP server:

```json
{
  "agents": {
    "custom": {
      "family": "my-cli",
      "build": ["my-cli", "run", "{prompt}"],
      "review": ["my-cli", "review", "{prompt}"],
      "env": {}
    }
  }
}
```

`{prompt}` and `{prompt_file}` are substituted inside argv, never shell code.
`stdin_prompt: true` sends the prompt to stdin instead; Codex defaults to this.
`{result_file}` expands to a private task result path. Setting `result_file: true`
reads the verdict from that file instead of the combined log; Codex uses
`--output-last-message` to avoid its duplicate final-output lines. The builder
can still use ordinary stdout logs.
Use explicit native `.exe` paths on Windows when an npm `.cmd` shim would
otherwise be selected. Templates must specify safe build/review modes.

## Register with Hermes (or any MCP client)

Use your active `$HERMES_HOME`, not a hardcoded `~/.hermes` on Windows. Hermes
configuration belongs under `mcp_servers.agent_bridge`. Example config shape:

```yaml
mcp_servers:
  agent_bridge:
    command: "C:/Users/you/agent-bridge/.venv/Scripts/python.exe"
    args: ["C:/Users/you/agent-bridge/bridge_server.py"]
    env:
      HOME: "C:/Users/you"
      USERPROFILE: "C:/Users/you"
      APPDATA: "C:/Users/you/AppData/Roaming"
      LOCALAPPDATA: "C:/Users/you/AppData/Local"
      SYSTEMROOT: "C:/Windows"
      WINDIR: "C:/Windows"
      COMSPEC: "C:/Windows/System32/cmd.exe"
      PATH: "<native Windows PATH including git and CLI executables>"
      BRIDGE_ALLOWED_ROOTS: '["C:/Users/you/code/myapp"]'
      BRIDGE_AGENTS_FILE: "C:/Users/you/.agent-bridge/agents.json"
      BRIDGE_MAX_PARALLEL: "3"
      BRIDGE_TIMEOUT_MIN: "45"
```

Register using `hermes mcp add` (don't overwrite the full Hermes config):

```bash
hermes mcp add agent_bridge \
  --command 'C:/Users/you/agent-bridge/.venv/Scripts/python.exe' \
  --env 'BRIDGE_ALLOWED_ROOTS=["C:/Users/you/code/myapp"]' \
        'HOME=C:/Users/you' 'USERPROFILE=C:/Users/you' \
        'SYSTEMROOT=C:/Windows' 'WINDIR=C:/Windows' \
        'COMSPEC=C:/Windows/System32/cmd.exe' \
        'APPDATA=C:/Users/you/AppData/Roaming' \
        'LOCALAPPDATA=C:/Users/you/AppData/Local' \
  --args 'C:/Users/you/agent-bridge/bridge_server.py'
hermes mcp test agent_bridge
```

Hermes filters subprocess environments, so preserve native PATH plus the Windows
variables above and any provider config paths you intentionally use. Do not copy
API tokens into source/config examples. On macOS/Linux use `.venv/bin/python`,
HOME/PATH and narrow POSIX roots; Windows essentials are unnecessary.

Tools appear as `mcp_agent_bridge_dispatch_task`, etc. Use a **new conversation**
after registration, or `/reload-mcp` when intentionally refreshing tools.

The same `command`, `args`, `env` works in other stdio MCP clients (the JSON parent
key is often `mcpServers`). This shares a CLI orchestration layer, not tokens or
Nimbalyst's private session protocol.

## Safety and guarantees

- Root allowlist is mandatory; unset/empty configuration refuses repository work.
  JSON arrays avoid Windows drive-letter/path-separator ambiguity.
- Tasks use a dedicated branch and git worktree. Your checkout's files/index are
  not changed. Dirty main-checkout edits are not copied; tasks start from HEAD.
- Shared repo state lives in `<repo>/.bridge/`, excluded through git info exclude.
  Detached workers survive MCP/Hermes exit. On Windows, when the server is inside
  the MCP SDK's kill-on-close Job Object, it launches workers through Windows WMI
  as the current user with the same filtered environment. No elevation, password,
  or Hermes-core patch is used. If local WMI denies the launch, the task fails
  visibly and retains its worktree. PID identity checks guard stale PIDs.
- Concurrent builders/reviewers share a per-repo limit (default 3). Dead runners
  are reported `lost`; timeouts and cancellation preserve work.
- Bridge code never merges or pushes. **This is not an OS security boundary:**
  a custom CLI with shell/git permissions can still execute those operations.
- Default Claude uses restricted file tools and `acceptEdits`, without Bash;
  the bridge runs the explicit `test_cmd` afterward. This avoids broad permission
  bypass while retaining OAuth. Claude reviews have only read tools.
- Codex uses workspace-write/read-only sandbox modes; no automatic bypass fallback.
- OpenCode auto mode is not an OS sandbox; plan-mode review uses a separate
  snapshot and mutation checks. Use trusted repos or an external container/VM.
- Reviewers see an isolated snapshot, not the builder's writable worktree. Missing
  or ambiguous verdicts and snapshot mutations do not yield an approval. Content
  changes invalidate old approvals. Aliases in the same family cannot self-review.
  On this Windows host, Codex's read-only shell commands can be rejected by policy.
  Keep its sandbox: supply complete relevant source, requirements, and actual test
  output through the review `prompt`, requesting a no-tools review. If complete
  evidence cannot be supplied, retain `CHANGES_REQUESTED`; do not bypass permissions.
- `test_cmd` is trusted executable input: strings intentionally run through a
  shell, lists run as argv. Never construct it by interpolating untrusted content.
- Worktree cleanup preserves branches, **not uncommitted files**. `force=True`
  discards those files. Never force cleanup before explicit merge/abandonment.
- Logs/prompts/questions can contain private source. Keep `.bridge` local.

## Test

```bash
# Linux/macOS
.venv/bin/python -m pytest -q
# Windows Git Bash (clear Hermes's inherited PYTHONPATH to use this venv)
PYTHONPATH= .venv/Scripts/python.exe -m pytest -q
PYTHONPATH= .venv/Scripts/python.exe tests/smoke_test.py
```

Tests use real temporary git repos, fake subprocess agents, and a real stdio MCP
client. They require no API keys or subscriptions. `scripts/probe_clis.py` is a
separate **live** login/model probe; it does consume subscription/model requests.

Verified on this Windows installation:

- Smoke suite: **50 tests passed**, plus **5 subtests**; Ruff clean.
- Hermes MCP connection: **9 tools discovered**.
- Real subscription flow: Claude build -> 5 passing acceptance tests -> Codex
  `CHANGES_REQUESTED` -> Claude continuation -> Codex `APPROVE`.
- Main demo checkout unchanged; no merge or push performed.
- Actual running task survived MCP server shutdown/reconnect. Per-invocation
  Windows Jobs reap children and background descendants before task completion.
- Regressions cover ignored-file cleanup refusal, monitor/database errors,
  immediate parent exit, Job assignment failure, and partial-cleanup retry.

Linux/macOS paths are supported in code but were not exercised on this Windows
host. The local `verification/` folder holds execution evidence and is excluded
from source control because prompts/logs may contain private project content.

Install `skills/agent-bridge/SKILL.md` in your active Hermes skills directory. It
teaches plan/approval -> dispatch -> tests/diff -> different-family review -> fix
(up to 3 rounds) -> evidence report -> human merge.

## Merge manually

Agent edits may be uncommitted. Review and commit them **in the returned worktree**
first, then merge the retained branch from your normal checkout:

```bash
git -C '<task-worktree>' status
# Review the patch; stage only intended files, then commit.
git -C '<task-worktree>' add <reviewed-files>
git -C '<task-worktree>' commit -m 'Your reviewed change'
git -C '<main-checkout>' merge --no-ff '<bridge-branch>'
```

No remote push is needed. If your main checkout has unrelated dirty changes, resolve
those before merging. Call cleanup only after merge or approved abandonment.

## Not included

Webhook/Slack/Telegram completion delivery, automatic test-command discovery,
Nimbalyst session-board mirroring, auto-merge, and GUI-only agent control. Hermes
can relay questions and poll status; external notifications are future work.

## Bounded unattended swarms

Four swarm MCP tools supplement the original nine task tools. They provide a bounded queue of isolated shards, explicit Haiku 5.5 `max` fallback, real tests and fresh independent review. No automatic merge or push. See [UNATTENDED.md](UNATTENDED.md) for setup, operating limits, cancellation/recovery and standalone CLI usage. See [CAPABILITIES.md](CAPABILITIES.md) for capability coverage, installation checks and reproducible local performance measurements.
