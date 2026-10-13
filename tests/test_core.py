import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge_core import Bridge, BridgeError


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'initial.txt').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')
        fake = str(Path(__file__).with_name('fake_agent.py'))
        self.config = self.root / 'agents.json'
        self.config.write_text(json.dumps({'agents': {
            'claude': {'family': 'claude', 'build': [sys.executable, fake, 'build', '{prompt}'], 'review': [sys.executable, fake, 'review', '{prompt}']},
            'codex': {'family': 'codex', 'build': [sys.executable, fake, 'build', '{prompt}'], 'review': [sys.executable, fake, 'review', '{prompt}']},
            'alias': {'family': 'claude', 'build': [sys.executable, fake, 'build', '{prompt}'], 'review': [sys.executable, fake, 'review', '{prompt}']}
        }}))
        self.bridge = Bridge(roots=[str(self.root)], agents_file=str(self.config))

    def tearDown(self):
        import psutil
        prefix = str(self.root)
        for proc in psutil.process_iter(['pid', 'cmdline', 'cwd']):
            try:
                if prefix in ' '.join(proc.info.get('cmdline') or []) or (proc.info.get('cwd') or '').startswith(prefix):
                    for child in proc.children(recursive=True):
                        child.kill()
                    proc.kill()
                    proc.wait(timeout=5)
            except psutil.Error:
                pass
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', '-C', str(cwd or self.repo), *args], stderr=subprocess.STDOUT, text=True).strip()

    def wait(self, task, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.bridge.get_status(str(self.repo), task['task_id'])
            if status['status'] not in ('queued', 'running', 'testing'):
                return status
            time.sleep(.05)
        self.fail('task did not terminate: ' + repr(status))

    def test_dispatch_is_durable_isolated_and_reports_base_head(self):
        head = self.git('rev-parse', 'HEAD')
        (self.repo / 'initial.txt').write_text('dirty main\n')
        task = self.bridge.dispatch_task(str(self.repo), 'change')
        self.assertIn(task['status'], ('queued', 'running', 'testing'))
        result = self.wait(task)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['base_head'], head)
        self.assertFalse(result['dirty_main_copied'])
        self.assertEqual((self.repo / 'initial.txt').read_text(), 'dirty main\n')
        self.assertEqual((Path(result['worktree']) / 'initial.txt').read_text(), 'base\n')
        self.assertEqual((Path(result['worktree']) / 'output.txt').read_text(), 'change\n')
        other = Bridge(roots=[str(self.root)], agents_file=str(self.config))
        self.assertEqual(other.get_status(str(self.repo), task['task_id'])['status'], 'done')
        self.assertEqual(other.list_tasks(str(self.repo))[0]['task_id'], task['task_id'])
        self.assertEqual(set(other.list_agents()), {'claude', 'codex', 'alias'})

    def test_diff_contains_committed_tracked_untracked_without_staging(self):
        task = self.bridge.dispatch_task(str(self.repo), 'commit-change')
        result = self.wait(task)
        work = Path(result['worktree'])
        (work / 'initial.txt').write_text('unstaged\n')
        (work / 'new.txt').write_text('untracked\n')
        before = self.git('diff', '--cached', cwd=work)
        diff = self.bridge.get_diff(str(self.repo), task['task_id'])
        self.assertIn('committed change', diff['patch'])
        self.assertIn('unstaged', diff['patch'])
        self.assertIn('untracked', diff['patch'])
        self.assertFalse(diff['truncated'])
        self.assertEqual(before, self.git('diff', '--cached', cwd=work))
        small = self.bridge.get_diff(str(self.repo), task['task_id'], max_chars=10)
        self.assertTrue(small['truncated'])
        self.assertEqual(len(small['patch']), 10)

    def test_terminal_outcomes_include_tests_and_no_changes(self):
        cases = [('no-change', None, 'no_changes'), ('fail', None, 'failed'),
                 ('change', [sys.executable, '-c', 'import sys; sys.exit(7)'], 'tests_failed'),
                 ('change', [sys.executable, '-c', 'print("TEST-PASS")'], 'done'),
                 ('change', '"' + sys.executable + '" -c "print(123)"', 'done')]
        for prompt, command, expected in cases:
            with self.subTest(expected=expected, command=command):
                result = self.wait(self.bridge.dispatch_task(str(self.repo), prompt, test_cmd=command))
                self.assertEqual(result['status'], expected, result)
                if expected == 'tests_failed':
                    self.assertEqual(result['test_exit_code'], 7)
                if command:
                    self.assertTrue((Path(result['task_dir']) / 'test.log').exists())

    def await_child(self, task):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.bridge.get_status(str(self.repo), task['task_id'])
            if result.get('child_pid'):
                return result
            time.sleep(.05)
        self.fail('no child launched')

    def test_cancel_terminates_child_tree_and_keeps_changes(self):
        import psutil
        task = self.bridge.dispatch_task(str(self.repo), 'sleep-tree')
        running = self.await_child(task)
        deadline = time.monotonic() + 5
        while not (Path(running['worktree']) / 'grandchild.pid').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        grandchild = int((Path(running['worktree']) / 'grandchild.pid').read_text())
        self.bridge.cancel_task(str(self.repo), task['task_id'])
        result = self.wait(task)
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertFalse(psutil.pid_exists(grandchild))
        self.assertTrue((Path(result['worktree']) / 'output.txt').exists())

    def test_timeout_budget_covers_agent_and_test(self):
        task = self.bridge.dispatch_task(str(self.repo), 'sleep', timeout_min=.025)
        self.assertEqual(self.wait(task)['status'], 'timed_out')
        task = self.bridge.dispatch_task(str(self.repo), 'change', test_cmd=[sys.executable, '-c', 'import time; time.sleep(60)'], timeout_min=.025)
        self.assertEqual(self.wait(task)['status'], 'timed_out')

    def test_human_question_safely_stops_running_work(self):
        task = self.bridge.dispatch_task(str(self.repo), 'sleep')
        self.await_child(task)
        self.bridge.ask_human(str(self.repo), task['task_id'], 'Choose a design?')
        result = self.wait(task)
        self.assertEqual(result['status'], 'needs_human', result)
        self.assertEqual(result['question'], 'Choose a design?')
        self.assertTrue((Path(result['task_dir']) / 'question.txt').exists())

    def test_cap_is_transactional_across_independent_servers(self):
        self.bridge = Bridge(roots=[str(self.root)], agents_file=str(self.config), max_parallel=1)
        first = self.bridge.dispatch_task(str(self.repo), 'short-sleep')
        self.await_child(first)
        code = 'import json,sys; from bridge_core import Bridge; print(json.dumps(Bridge(roots=[sys.argv[1]],agents_file=sys.argv[2],max_parallel=1).dispatch_task(sys.argv[3],"short-sleep")))'
        second = json.loads(subprocess.check_output([sys.executable, '-c', code, str(self.root), str(self.config), str(self.repo)], text=True))
        while True:
            items = self.bridge.list_tasks(str(self.repo))
            self.assertLessEqual(sum(t['status'] == 'running' for t in items), 1)
            if all(t['status'] not in ('running', 'queued') for t in items):
                break
            time.sleep(.1)
        a, b = self.wait(first), self.wait(second)
        self.assertGreaterEqual(b['started_at'], a['finished_at'])

    def test_dead_runner_is_lost_and_validated_child_is_terminated(self):
        import psutil
        task = self.bridge.dispatch_task(str(self.repo), 'sleep-tree')
        running = self.await_child(task)
        psutil.Process(running['runner_pid']).kill()
        psutil.Process(running['runner_pid']).wait(timeout=5)
        result = self.bridge.get_status(str(self.repo), task['task_id'])
        self.assertEqual(result['status'], 'lost', result)
        self.assertFalse(psutil.pid_exists(running['child_pid']))

    def test_queued_worker_restarts_after_loss(self):
        import psutil
        self.bridge = Bridge(roots=[str(self.root)], agents_file=str(self.config), max_parallel=1)
        first = self.bridge.dispatch_task(str(self.repo), 'sleep')
        self.await_child(first)
        second = self.bridge.dispatch_task(str(self.repo), 'change')
        time.sleep(.8)
        queued = self.bridge.get_status(str(self.repo), second['task_id'])
        self.assertEqual(queued['status'], 'queued')
        proc = psutil.Process(queued['runner_pid'])
        proc.kill()
        proc.wait(timeout=5)
        self.bridge.get_status(str(self.repo), second['task_id'])
        self.bridge.cancel_task(str(self.repo), first['task_id'])
        self.wait(first)
        self.assertEqual(self.wait(second)['status'], 'done')

    def test_continuation_reuses_terminal_worktree_once_preserving_original_base(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.bridge.dispatch_task(str(self.repo), 'commit-change')
        original = self.wait(first)
        work = Path(original['worktree'])
        (work / 'keep.txt').write_text('preserve me')
        (self.repo / 'new-main.txt').write_text('new main')
        self.git('add', 'new-main.txt')
        self.git('commit', '-m', 'advance main')
        def attempt():
            try:
                return self.bridge.dispatch_task(str(self.repo), 'continuation', continue_from=first['task_id'])
            except BridgeError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(lambda _: attempt(), range(2)))
        accepted = [a for a in attempts if a]
        self.assertEqual(len(accepted), 1)
        result = self.wait(accepted[0])
        self.assertEqual(result['worktree'], original['worktree'])
        self.assertEqual(result['base_head'], original['base_head'])
        self.assertEqual((work / 'keep.txt').read_text(), 'preserve me')
        self.assertFalse((work / 'new-main.txt').exists())
        self.assertIn('committed change', self.bridge.get_diff(str(self.repo), result['task_id'])['patch'])
        with self.assertRaises(BridgeError):
            self.bridge.dispatch_task(str(self.repo), 'again', continue_from=first['task_id'])

    def test_review_uses_different_family_and_isolated_entire_snapshot(self):
        task = self.bridge.dispatch_task(str(self.repo), 'commit-change')
        built = self.wait(task)
        (Path(built['worktree']) / 'initial.txt').write_text('tracked edit\n')
        with self.assertRaises(BridgeError):
            self.bridge.review_task(str(self.repo), task['task_id'], agent='alias')
        review = self.bridge.review_task(str(self.repo), task['task_id'], agent='codex', prompt='verify-snapshot')
        self.assertIn(review['status'], ('queued', 'running', 'testing'))
        result = self.wait(review)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual(result['verdict'], 'APPROVE', result)
        self.assertNotEqual(result['worktree'], built['worktree'])
        snapshot = Path(result['worktree'])
        self.assertTrue((snapshot / '.git').is_dir())
        self.assertNotIn(str(self.repo), self.git('rev-parse', '--git-common-dir', cwd=snapshot))
        self.assertEqual(self.bridge.get_status(str(self.repo), task['task_id'])['status'], 'done')
        self.assertEqual(self.bridge.get_status(str(self.repo), task['task_id'])['review_status'], 'approved')
        self.assertTrue((Path(result['task_dir']) / 'review.json').exists())
        (Path(built['worktree']) / 'initial.txt').write_text('later change\n')
        self.assertEqual(self.bridge.get_status(str(self.repo), task['task_id'])['review_status'], 'invalidated')

    def test_configured_roots_fail_closed_before_creating_state(self):
        with self.assertRaises(BridgeError):
            Bridge(roots=[]).dispatch_task(str(self.repo), 'change')
        self.assertFalse((self.repo / '.bridge').exists())
        with self.assertRaises(BridgeError):
            self.bridge.dispatch_task(str(self.root), 'change')
        with self.assertRaises(BridgeError):
            self.bridge.dispatch_task(str(self.repo), '')
        with self.assertRaises(BridgeError):
            self.bridge.dispatch_task(str(self.repo), 'change', agent='missing')


if __name__ == '__main__':
    unittest.main()
