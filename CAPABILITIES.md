# Capability review and performance

This bridge supports bounded unattended coding of independent shards. `ready`
means every isolated shard has passing tests and current independent approval.
It does not mean the shards have been integrated or the whole project approved.
See [UNATTENDED.md](UNATTENDED.md) for the operating contract.

## Capability matrix

| Capability | Verification | Limits |
|---|---|---|
| MCP protocol and discovery | Real stdio client, 13 tools; `test_mcp.py`, `test_mcp_swarm.py` | Refresh the MCP connection after installation |
| Isolated build, test, diff and review | Real Git plus deterministic subprocess actors; subscription-backed live runs | CLI authentication/configuration remain external prerequisites |
| Queue and concurrency | Independent shard pipeline, shared per-repository worker cap | 1–200 submitted shards; default cap 3; no machine-wide resource governor |
| Frozen test contracts | Inferred/explicit paths, continued builds and staged renames; `test_frozen_contracts.py` | Opaque custom runners need explicit `frozen_tests` |
| Tested-artifact binding | Versioned length-prefixed metadata and streamed file digests; before/after tests and review; binary-layout invalidation regressions | Trusted test commands; pre-v2 fingerprints require new tests/review; no hostile-code OS containment |
| Ownership | Literal paths, ignored source, hidden tracked edits, executable caches, baseline modes independent of `core.filemode` | Non-overlapping independent work only; legacy records without mode baselines must be rerun; no dependency DAG |
| Fresh approval | Strict final verdict, unchanged builder bytes, snapshot content/modes, source/root mutation invalidation | Passing tests or process exit 0 alone never approve |
| Continuation and fallback | Retained worktree, invalidated prior review; explicit Haiku attempt with fresh tests/review | One configured fallback; no silent provider/permission substitution |
| Cancellation | Durable dispatch checks, checkpoint reconciliation, owned-record discovery and reviewer draining | Cooperative stop plus invocation containment/deadline |
| Recovery and detached execution | Runner recovery and simulated coordinator-loss tests; live completion after MCP disconnect | Three coordinator recoveries driven by polling, not a reboot service; no live coordinator kill-at-checkpoint qualification |
| Human intervention | `ask_human`, recorded question, stopped process and continuation tests | No automatic external notification delivery |
| Resource guards | Bounded Git spooling, disclosed test-output tails, bounded review/log reads and evidence; deadline/process tests | Oversized evidence fails closed; split large jobs |
| Windows power handling | Scoped wake acquisition/release and real live-run evidence | Cannot override explicit sleep/shutdown; no global policy changes |
| Source and wheel installation | Actual wheel build/install in a new venv, isolated imports and stdio build/test/review | Native CLI paths/login must still be configured; defaults contain no credentials |
| Cleanup | Real worktrees/snapshots, dirty-work refusal, cleanup retry tests | Forced removal requires authorization; no automatic merge/push |

## Measured local speedups

Paired benchmark against `ece0b8738218433d5420df9884cab398a6d6957d`:
7 iterations, warmup, alternating before/after order, same machine and data.
Status uses four genuinely tested/reviewed deterministic subprocess shards, not
model responses. Baseline functions run with the shared current runtime guards;
this compares the targeted functions, not the entire historical application.

| Local operation | Baseline median | Optimized median | Ratio | Git launches |
|---|---:|---:|---:|---:|
| Ready status, four approved synthetic shards | 4.181 s | 2.317 s | 1.80x | 102 -> 52 |
| Snapshot fingerprint, 200 source files plus ignored content | 0.552 s | 0.256 s | 2.15x | 4 -> 2 |

The status path validates once after any recovery reload rather than twice.
Snapshot hashing streams each file once while retaining HEAD/index binding,
file modes, ignored-file coverage and unsafe-path refusal. There is no TTL or
mtime-only approval cache. Regression tests count validation calls and file reads,
check recovery ordering, and verify mode-only changes alter the fingerprint.

These are local overhead measurements, **not** a controlled model-generation or
whole-swarm speedup claim. Provider latency, test costs and host load still vary.
Reproduce from the source checkout:

```bash
PYTHONPATH=. .venv/Scripts/python.exe benchmarks/measure_bridge.py --baseline ece0b8738218433d5420df9884cab398a6d6957d --repeats 7
```

Add `--repo NATIVE_REPO_PATH --swarm APPROVED_SWARM_ID` to include status timing.
Only use trusted local baseline refs. The benchmark executes the selected baseline's
Python source to compare the old functions; it makes no model calls.

## Evidence and scope

The full suite covers real local Git/SQLite/process behavior using deterministic
CLI actors. Live subscription checks separately cover Big Pickle, Codex and the
explicit official Claude Pro Haiku 5.5 `max` fallback. Installed-wheel smoke uses
synthetic actors and is not a model authentication test. Full-suite byte manifests,
JUnit output, live state, paired benchmark samples and fresh source-audit artifacts
are retained under the ignored local `verification/` directory. Only source and
credential-free public documentation belong in commits.

This verification is performed on Windows. It is not a claim of exhaustive
cross-platform validation, hostile-code isolation, successful integrated-project
tests, automatic deployment, Nimbalyst session-board integration, universal model
availability or guaranteed completion through provider policy blocks/outages.
