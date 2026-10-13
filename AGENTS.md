# agent-bridge development

- Python 3.11+, MCP SDK 1.x, psutil. CLI configuration is trusted argv JSON.
- Run tests: `PYTHONPATH= .venv/Scripts/python.exe -m pytest -q` on this Windows
  Git-Bash host. Native uv requires `C:/...`, not `/c/...` paths.
- Public success status is `done`, not `completed`. Review is a separate object
  in build `get_status`, with `status` and `verdict` keys.
- Strict final review verdict: `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`.
- All nine tools must work through an actual stdio MCP client, not only in-process.
  `tests/test_mcp.py` tests discovery/refusal and dispatch/server restart/review.
- Safety tests include NTFS junction redirection of `.bridge`, nonfinite/nonpositive
  timeout inputs, and malformed test argv. Refuse before filesystem side effects.
- Default Claude file tools only, with restricted mode, acceptEdits and no Bash.
  Codex uses workspace-write/read-only. No automatic sandbox-bypass fallback.
- Worktrees isolate collisions, not OS access. Root allowlist is mandatory.
- No automatic merge/push. Forced cleanup discards uncommitted files.
- Ownership during implementation was split between core, review-layer, and MCP
  workers; all source files are now maintained as one tested project.
- Before shipping, run real git/fake-agent tests, independent review, and a live
  subscription-backed Claude build plus Codex review on the throwaway demo.
