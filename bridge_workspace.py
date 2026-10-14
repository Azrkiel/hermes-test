"""Scoped OpenCode v2 file-tool guard. CLI policy, not an OS sandbox."""
import json
from contextlib import contextmanager
from pathlib import Path

from bridge_core import BridgeError


@contextmanager
def workspace_guard(task, cfg):
    policy = cfg.get('workspace_policy')
    if policy is None:
        yield
        return
    if policy not in ('opencode-files-only', 'opencode-headless-ask'):
        raise BridgeError('unsupported workspace_policy')
    ask = policy == 'opencode-headless-ask'
    if ask and any('--auto' in cfg.get(role, []) for role in ('build', 'review')):
        raise BridgeError('headless workspace policy forbids --auto')
    root = Path(task['worktree']).resolve()
    path = root / 'opencode.jsonc'
    if path.is_symlink() or path.resolve() != path:
        raise BridgeError('workspace policy file may not redirect')
    existed = path.exists()
    original = path.read_bytes() if existed else None
    # This native release's published schema uses V1 compatibility keys.
    # Do not use tool.use policies: 2.0.19 silently drops those statements.
    read = {'*': 'allow', '../*': 'deny', '/*': 'deny', '*:*': 'deny', '*.env*': 'deny'}
    edit = {'*': 'allow', '../*': 'deny', '/*': 'deny', '*:*': 'deny',
            '*/../*': 'deny', 'opencode.json': 'deny', 'opencode.jsonc': 'deny',
            '.opencode/*': 'deny', '.git*': 'deny', 'tests/*': 'deny'}
    permission = {'*': 'deny', 'read': read, 'edit': edit, 'bash': 'deny',
                  'external_directory': 'deny', 'task': 'deny', 'execute': 'deny'}
    if ask:
        permission = {key: ({p: ('ask' if effect == 'deny' else effect) for p, effect in value.items()}
                            if isinstance(value, dict) else 'ask')
                      for key, value in permission.items()}
    config = {'$schema': 'https://opencode.ai/config.json', 'formatter': False,
              'permission': permission}
    try:
        path.write_text(json.dumps(config), encoding='utf-8')
        yield
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
