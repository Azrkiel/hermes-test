"""Bounded read-only live login probes. Does not access private projects."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import psutil


def run(argv, cwd, timeout=90):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.pop('CLAUDECODE', None)
    env.pop('ANTHROPIC_API_KEY', None)
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        output, _ = p.communicate(timeout=timeout)
        return {'returncode': p.returncode,
                'output': output.decode('utf-8', errors='replace')[-6000:]}
    except subprocess.TimeoutExpired:
        parent = psutil.Process(p.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        p.kill()
        output, _ = p.communicate()
        return {'timed_out': True, 'output': output.decode('utf-8', errors='replace')[-6000:]}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='bridge-login-probe-') as tmp:
        subprocess.run(['git', 'init', tmp], check=True, capture_output=True)
        (Path(tmp) / 'README.md').write_text('CLI read-only login probe.\n')
        subprocess.run(['git', '-C', tmp, 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', tmp, '-c', 'user.name=Bridge Test', '-c',
                        'user.email=bridge-test@localhost', 'commit', '-m', 'initial'],
                       check=True, capture_output=True)
        agents = {
            'claude': [shutil.which('claude'), '-p', '--tools', '',
                       '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--',
                       'Respond with exactly BRIDGE_CLAUDE_OK. Do not use tools.'],
            'codex': [shutil.which('codex'), 'exec', '--sandbox', 'read-only',
                      '--ignore-user-config', '--color', 'never',
                      'Respond with exactly BRIDGE_CODEX_OK. Do not use tools.'],
            'opencode': [str(Path.home() / 'AppData/Local/hermes/node/node_modules/@opencode/cli/node_modules/@opencode/cli-windows-x64/bin/opencode.exe'),
                         'run', '--standalone', '--agent', 'plan',
                         'Respond with exactly BRIDGE_OPENCODE_OK. Do not use tools.'],
        }
        results = {}
        for name, argv in agents.items():
            try:
                results[name] = run(argv, tmp)
            except OSError as e:
                results[name] = {'error': str(e)}
            print(name, json.dumps(results[name]), flush=True)
        dest = Path(__file__).resolve().parents[1] / 'verification' / 'cli-probes.json'
        dest.parent.mkdir(exist_ok=True)
        dest.write_text(json.dumps(results, indent=2), encoding='utf-8')
