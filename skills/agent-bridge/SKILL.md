---
name: agent-bridge
description: "Use when delegating coding via agent-bridge MCP."
version: 0.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [coding, mcp, orchestration, worktrees, subscriptions]
    related_skills: [claude-code, codex, opencode, test-driven-development]
---

# Agent Bridge

## Overview

Hermes plans and checks work. Official coding CLIs implement it using their own
logins. The MCP bridge owns isolated worktrees and durable runners. The human
owns merging. This is a CLI bridge, not a model API router or Nimbalyst UI-session
integration. Nimbalyst can inspect the same repo's branches and worktree files.

## When to Use

- User asks for coding through Claude Code, Codex, OpenCode, or another configured hand.
- Independent build/review loops or concurrent coding tasks need isolated branches.
- Don't use for ordinary chat, direct model routing, or accessing another app's credentials.

## Workflow

1. Inspect the repo, project rules, git status, and actual test command. Call
   `mcp_agent_bridge_list_agents`. Resolve canonical absolute repo path and agent
   availability before dispatch. Never broaden allowed roots to the whole home.
   Completion: scope and test command are concrete; builder/reviewer are available.
2. Present a short plan and get approval unless this request already authorizes
   execution. Default builder Claude, reviewer Codex; reverse when appropriate.
   Completion: scope is approved and two different CLI families are chosen.
3. Call `mcp_agent_bridge_dispatch_task` with repo, task prompt, builder, test_cmd,
   and optional timeout_min. Include acceptance criteria, TDD, and instructions
   not to merge/push or touch files outside the worktree. Record returned task_id,
   branch and worktree. Dirty main-checkout edits are NOT copied; baseline is HEAD.
   Completion: a concrete task_id exists; don't claim completion from dispatch alone.
4. Read `get_status` at useful intervals, not a tight polling loop. The runners
   survive MCP/Hermes shutdown. Queued tasks count against their repo's shared
   concurrency limit when claimed. `testing` means tests still run.
   Completion: builder is in a terminal state, no active child remains.
5. For `needs_human`, relay its recorded question. After the answer, dispatch
   with `continue_from` and the answer; no CLI conversation history is implied.
   For failed/timed_out/lost tasks, inspect the log and retained work before
   retrying. Never silently switch to permissions bypass after sandbox failure.
6. Inspect `get_diff`: every changed file, patch, truncation, and original base.
   `done` without a test command is not proof that tests passed. Inspect test
   result and actual output. For `tests_failed`, continue with a precise fix task.
   Completion: no missing files, no truncated review evidence, tests are accounted for.
7. Call `review_task` using a different CLI family than the builder. Poll review
   state separately via `get_status`. Only a single strict final
   `VERDICT: APPROVE` counts. Missing/ambiguous verdict, review writes, stale
   content, or execution errors fail closed. Approval is invalid after edits.
   Completion: successful unchanged-snapshot review matches current task content.
   If Codex read-only shell access is blocked by policy, keep the sandbox and
   supply complete relevant files, requirements, and real test output in review
   prompt, requesting a no-tools review. If evidence is incomplete, retain
   CHANGES_REQUESTED; never bypass permissions to obtain an approval.
8. For `CHANGES_REQUESTED`, dispatch `continue_from` on the latest task with
   the findings, then rerun tests and a fresh review. Maximum three fix rounds;
   after that report unresolved findings instead of looping indefinitely.
9. Report branch/worktree/task_id, file summary, real test result, review agent
   and verdict, remaining risks, and manual merge steps. No automatic merge/push.
   Agent files may be uncommitted: the branch alone does not contain them.
   Completion: human has enough evidence and the exact paths to inspect/merge.
10. Only call `cleanup_task` after explicit merge/abandonment. It preserves the
    branch but removes the worktree. `force=True` discards uncommitted files;
    require explicit user approval for that loss. Never clean up active tasks.

## Choosing a Hand

| Role | Default | Alternative |
|---|---|---|
| Implement | claude | codex |
| Independent review | codex | claude |
| Provider-specific variant | opencode after auth check | a named custom CLI |

Aliases with the same `family` do not count as independent reviewers. OpenCode
installation does not prove model/provider authentication. Read `list_agents`
commands and live CLI results before choosing it.

## Safety and Permission Boundaries

- Allowed roots restrict bridge repository selection, not the child process's
  filesystem/network access. Worktrees are collision isolation, not an OS sandbox.
- Default Claude builder auto-approves file tools; it does not auto-approve arbitrary
  Bash. The bridge runs the explicit test_cmd afterward. Never enable unrestricted
  Bash or `--dangerously-skip-permissions` just to make an unattended task work.
- Codex uses `workspace-write` / `read-only` sandboxes; report sandbox failures.
- OpenCode's auto mode is not an OS sandbox. Reviews use plan mode on a separate
  snapshot and are checked for writes; unsupported review guarantees must be explicit.
- Agent argv config is trusted local configuration. Prompt placeholders are argv,
  never shell snippets. test_cmd strings intentionally execute trusted shell code;
  use a list when shell behavior is unnecessary.
- Logs/prompts and questions persist in repo/.bridge. Treat them as potentially sensitive.
- ask_human records/pauses; it does NOT send a Slack/Telegram notification itself.

## Common Pitfalls

1. Branch has no edits until files are committed. Inspect worktree git status.
2. continue_from keeps the same original base and worktree; don't continue an older
   superseded task or try concurrent continuations.
3. Review state is separate from build status. Build done is not review approval.
4. Main dirty changes are excluded by design. Commit desired baseline yourself,
   or explicitly approve another baseline workflow before dispatch.
5. On Windows use native absolute executables (not npm .cmd shims when avoidable),
   preserve SYSTEMROOT/WINDIR/COMSPEC and APPDATA/LOCALAPPDATA/USERPROFILE in the
   MCP env, and retain HOME/PATH for each CLI's subscription login.
6. New MCP tools may need a fresh conversation. Don't mutate this turn's cached
   toolset just to call them; a real MCP client can verify the protocol meanwhile.

## Verification Checklist

- [ ] Narrow allowed repo; two distinct authenticated hands
- [ ] Approved scope and concrete acceptance criteria/test command
- [ ] Builder terminal; every diff file and test result checked
- [ ] Fresh review matches current unchanged content
- [ ] Branch and uncommitted-worktree warning in the handoff
- [ ] No merge/push or premature forced cleanup
