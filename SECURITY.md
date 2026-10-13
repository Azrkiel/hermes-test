# Security model

## Trust boundaries

1. **Human -> Hermes:** The human approves coding scope. Hermes supplies a concrete
   repo, prompt and trusted test command. MCP registration itself allows mutation
   tools; approval remains an orchestration policy, not authentication per tool.
2. **Hermes -> bridge:** The repo allowlist must be configured. Canonical paths,
   including Windows junctions, are checked before state/worktree operations.
   Task IDs and persisted ownership prevent arbitrary cleanup targets.
3. **Bridge -> CLI:** Commands are trusted argv arrays. Prompt text stays one
   argv value or stdin. Custom agent configuration and its environment are trusted
   executable configuration, not user task data. No shell interpolation of prompts.
4. **CLI -> source/system:** A worktree prevents checkout collisions. It does NOT
   prevent arbitrary filesystem, network, git or credential access. Use each CLI's
   safe modes, narrow tool sets, and external isolation for untrusted workloads.
5. **Builder -> reviewer:** The reviewer receives a separate snapshot and diff.
   Source comments, diffs and task prompts are untrusted review data. Review mode
   reduces available mutations, and mutation checks fail closed. CLI hooks/plugins
   and inherited project instructions can affect behavior; review remains fallible.

## What the bridge does not guarantee

- A custom shell-enabled worker cannot be prevented from running git merge/push
  merely by telling it not to. Bridge code never invokes those commands itself.
- Root allowlists don't jail test commands or model tools. Test code is arbitrary
  execution under the current account. Never run an untrusted repo outside a VM/container.
- Persisted state/logs are not encrypted, and OS-owner access can tamper with them.
  The bridge is a local single-user coordinator, not a multi-tenant service.
- Snapshot mutation detection is not an OS sandbox. A hostile process can modify
  outside the snapshot or revert a write before inspection. Avoid shell tools in
  reviews, and don't grant sensitive mounts or network credentials in a container.
- Default Claude uses file tools without Bash. This narrows its capabilities; the
  bridge runs the explicit trusted test command afterward. Adding Bash changes
  the trust boundary and must be a conscious user configuration choice.
- Strict review verdicts are evidence, not a proof of correctness. Human diff
  inspection and actual tests remain necessary.
- Detached runners survive server/session exits. Windows SDK Job Objects require
  WMI launch outside the client job, under the same current user; if WMI is denied,
  worker launch fails visibly. Explicit worker process kills, machine shutdown,
  removed code/venv, and OS policies can still end them; persisted state must
  report/recover that rather than pretend success.

## Destructive operations

Cancellation retains work. Cleanup deletes a worktree only for inactive owned
work. A retained branch contains committed changes only. `force=True` can discard
uncommitted source and must follow explicit merge/abandonment approval. The demo
and automated tests force-clean only their own disposable repos.

## Credentials

Let each official CLI use its own OAuth/subscription login. Do not scrape, copy,
or re-export Nimbalyst/Claude/Codex auth tokens. Pin the correct home/config paths
and native executable. MCP's filtered environment should carry only necessary
Windows baseline paths and deliberately configured provider values. `.bridge`
logs/prompts may contain private source; keep them excluded from commits.
