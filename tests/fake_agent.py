"""Authentication-free real subprocess test CLI."""
import sys
from pathlib import Path

mode, prompt = sys.argv[1:3]
if mode == 'build':
    if prompt == 'no-change':
        sys.exit(0)
    if prompt == 'fail':
        sys.exit(9)
    if prompt == 'commit-change':
        import subprocess
        Path('committed.txt').write_text('committed change\n')
        subprocess.check_call(['git', 'add', 'committed.txt'])
        subprocess.check_call(['git', 'commit', '-m', 'fake commit'])
    Path('output.txt').write_text(prompt + '\n')
    if prompt == 'short-sleep':
        import time
        time.sleep(2)
    if prompt.startswith('sleep'):
        import time
        if prompt == 'sleep-tree':
            import subprocess
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            Path('grandchild.pid').write_text(str(child.pid))
        time.sleep(60)
    print('fake build complete', flush=True)
else:
    if 'verify-snapshot' in prompt:
        assert Path('committed.txt').read_text() == 'committed change\n'
        assert Path('initial.txt').read_text() == 'tracked edit\n'
        assert Path('output.txt').read_text() == 'commit-change\n'
    print('VERDICT: APPROVE', flush=True)
