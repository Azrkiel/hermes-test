"""Durable local Git worktree bridge. CLI configuration is trusted, tasks are not sandboxes."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psutil

ACTIVE = ('queued', 'running', 'testing')


def safe_state(repo):
    """Reject symlinks/junctions before any state read or write."""
    repo = Path(repo)
    state = repo / '.bridge'
    for candidate in [state, *(state / n for n in ('tasks', 'worktrees', 'reviews', 'state.sqlite'))]:
        if candidate.is_symlink() or candidate.resolve() != candidate:
            raise BridgeError('bridge state may not redirect through symlinks or junctions')
    return state


def valid_task_paths(repo, task):
    task_id = task.get('task_id', '')
    owner = task.get('worktree_owner_id', task_id)
    if not isinstance(task_id, str) or not re.fullmatch(r'[0-9a-f]{32}', task_id):
        raise BridgeError('invalid persisted task ID')
    if not isinstance(owner, str) or not re.fullmatch(r'[0-9a-f]{32}', owner):
        raise BridgeError('invalid persisted worktree owner')
    state = safe_state(repo)
    folder = state / 'tasks' / task_id
    if Path(task.get('task_dir', '')).resolve() != folder or folder.resolve() != folder:
        raise BridgeError('invalid or redirected task directory')
    work = Path(task.get('worktree', ''))
    if task.get('kind') == 'build':
        expected = state / 'worktrees' / owner
        if work.resolve() != expected or expected.resolve() != expected:
            raise BridgeError('invalid or redirected task worktree')
    else:
        review_root = state / 'reviews' / task_id
        if work.resolve() != work or review_root not in work.parents:
            raise BridgeError('invalid or redirected review snapshot')


def log_tail(path, lines=40, max_bytes=65536):
    if not path.exists() or not lines:
        return ''
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes))
        return '\n'.join(stream.read(max_bytes).decode('utf-8', 'replace').splitlines()[-lines:])


@contextmanager
def database(repo):
    db = sqlite3.connect(str(safe_state(repo) / 'state.sqlite'), timeout=30)
    db.execute('PRAGMA busy_timeout=30000')
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def load(db, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r'[0-9a-f]{32}', task_id):
        raise BridgeError('invalid task ID')
    row = db.execute('SELECT data FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row:
        raise BridgeError('unknown task: ' + task_id)
    task = json.loads(row[0])
    repo = Path(db.execute('PRAGMA database_list').fetchone()[2]).parent.parent
    valid_task_paths(repo, task)
    return task


def save(db, task):
    db.execute('INSERT OR REPLACE INTO tasks(id,data) VALUES (?,?)', (task['task_id'], json.dumps(task)))


def records(db):
    return [json.loads(row[0]) for row in db.execute('SELECT data FROM tasks')]


def alive(pid, created):
    try:
        return bool(pid and created and abs(psutil.Process(pid).create_time() - created) < .01 and psutil.Process(pid).is_running())
    except psutil.Error:
        return False


def terminate_tree(pid, created):
    """Never signal a PID unless its recorded creation identity still matches."""
    if not alive(pid, created):
        return
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        if os.name == 'nt':
            # Required on Windows: kill descendants as well as the CLI wrapper.
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=15)
        else:
            for proc in reversed(children):
                try:
                    proc.kill()
                except psutil.Error:
                    pass
            parent.kill()
        psutil.wait_procs(children + [parent], timeout=5)
    except (psutil.Error, subprocess.TimeoutExpired):
        pass


def recover_states(db):
    for task in records(db):
        if task['status'] in ('running', 'testing') and not alive(task.get('runner_pid'), task.get('runner_created')):
            terminate_tree(task.get('child_pid'), task.get('child_created'))
            task.update(status=task.get('stop_request') or 'lost', error='runner process disappeared',
                        finished_at=time.time(), child_pid=None, child_created=None)
            save(db, task)


class BridgeError(ValueError):
    """Invalid request, unsafe path, or unavailable bridge resource."""


def git(repo, *args, strip=True):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise BridgeError(result.stderr.strip() or result.stdout.strip() or 'git failed')
    return result.stdout.strip() if strip else result.stdout


def windows_worker(argv, cwd):
    """WMI launches as the same user outside an MCP client's kill-on-close Job.

    No elevation or credentials are requested. Preserve the MCP-filtered env.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        service = win32com.client.GetObject(
            r'winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2')
        startup = service.Get('Win32_ProcessStartup').SpawnInstance_()
        startup.EnvironmentVariables = [f'{key}={value}' for key, value in os.environ.items()]
        startup.ShowWindow = 0
        params = service.Get('Win32_Process').Methods_('Create').InParameters.SpawnInstance_()
        params.CommandLine = subprocess.list2cmdline(argv)
        params.CurrentDirectory = str(cwd)
        params.ProcessStartupInformation = startup
        result = service.ExecMethod('Win32_Process', 'Create', params)
        if result.ReturnValue != 0:
            raise BridgeError(f'Windows worker launch refused: WMI code {result.ReturnValue}')
        return psutil.Process(int(result.ProcessId))
    finally:
        # Release COM wrappers while this thread's apartment is still alive.
        result = params = startup = service = None
        pythoncom.CoUninitialize()


class Bridge:
    def __init__(self, roots=None, agents_file=None, max_parallel=None, timeout_min=None):
        if roots is None:
            raw = os.environ.get('BRIDGE_ALLOWED_ROOTS', '')
            try:
                roots = json.loads(raw) if raw.lstrip().startswith('[') else [r for r in raw.split(os.pathsep) if r]
            except ValueError as exc:
                raise BridgeError('invalid BRIDGE_ALLOWED_ROOTS') from exc
        if not isinstance(roots, (list, tuple)) or any(
                not isinstance(r, (str, os.PathLike)) or not str(r).strip() for r in roots):
            raise BridgeError('allowed roots must be a list of nonempty paths')
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        default_agents = Path.home() / '.agent-bridge' / 'agents.json'
        if not default_agents.exists():
            default_agents = Path(__file__).with_name('agents.example.json')
        self.agents_file = Path(agents_file or os.environ.get('BRIDGE_AGENTS_FILE') or default_agents)
        raw_cap = max_parallel if max_parallel is not None else os.environ.get('BRIDGE_MAX_PARALLEL', '3')
        if isinstance(raw_cap, bool) or not re.fullmatch(r'[1-9][0-9]*', str(raw_cap)):
            raise BridgeError('max_parallel must be a positive integer')
        self.max_parallel = int(raw_cap)
        self.timeout_min = self._timeout(timeout_min if timeout_min is not None else os.environ.get('BRIDGE_TIMEOUT_MIN', '45'))

    @staticmethod
    def _timeout(value):
        try:
            if isinstance(value, bool):
                raise ValueError()
            result = float(value)
            if not math.isfinite(result) or result <= 0:
                raise ValueError()
            return result
        except (TypeError, ValueError) as exc:
            raise BridgeError('timeout_min must be finite and positive') from exc

    def _repo(self, repo):
        path = Path(repo).expanduser().resolve(strict=True)
        if not self.roots or not any(path == r or r in path.parents for r in self.roots):
            raise BridgeError('repository is outside configured allowed roots')
        if git(path, 'rev-parse', '--is-bare-repository') != 'false':
            raise BridgeError('bare repositories are not allowed')
        if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
            raise BridgeError('repo must be repository root')
        if '.bridge' in (part.lower() for part in path.parts):
            raise BridgeError('bridge-owned worktree input is forbidden')
        safe_state(path)
        return path

    def _agent(self, name, mode='build', check_executable=True):
        try:
            configs = json.loads(self.agents_file.read_text(encoding='utf-8'))['agents']
            value = configs[name]
            argv = value[mode]
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise ValueError('argv must be a nonempty string list')
            if not isinstance(value.get('family'), str) or not value['family']:
                raise ValueError('family is required')
            env = value.get('env', {})
            if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                for k, v in env.items()):
                raise ValueError('agent env must be a string mapping')
            executable = shutil.which(argv[0])
            if check_executable and not executable:
                raise ValueError('agent executable is unavailable')
            if check_executable and os.name == 'nt' and Path(executable).suffix.lower() in ('.cmd', '.bat'):
                raise ValueError('use a native executable or node script argv, not a batch shim')
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BridgeError('invalid or missing agent configuration: ' + str(name)) from exc

    def dispatch_task(self, repo, prompt, agent='claude', test_cmd=None, continue_from=None, timeout_min=None):
        path = self._repo(repo)
        if not isinstance(prompt, str) or not prompt.strip() or '\x00' in prompt:
            raise BridgeError('prompt must be a nonempty string')
        config = self._agent(agent)
        timeout = self._timeout(self.timeout_min if timeout_min is None else timeout_min)
        if test_cmd is not None and not (
                isinstance(test_cmd, str) and test_cmd.strip() or
                isinstance(test_cmd, list) and test_cmd and
                all(isinstance(a, str) and a and '\x00' not in a for a in test_cmd)):
            raise BridgeError('test_cmd must be a nonempty string or argv string list')
        if isinstance(test_cmd, str) and '\x00' in test_cmd:
            raise BridgeError('test_cmd contains a null byte')
        state = self._init(path)
        task_id = uuid.uuid4().hex
        folder = state / 'tasks' / task_id
        with database(path) as db:
            prior = load(db, continue_from) if continue_from else None
            if prior:
                if prior['kind'] != 'build' or prior['status'] in ACTIVE or prior.get('continued_by') or prior.get('cleaned_at'):
                    raise BridgeError('continuation requires latest terminal build with a retained worktree')
                if any(t['status'] in ACTIVE and t.get('build_task_id') == prior['task_id'] for t in records(db)):
                    raise BridgeError('cannot continue while review is active')
                base, branch, worktree = prior['base_head'], prior['branch'], Path(prior['worktree'])
                prior['continued_by'] = task_id
                prior['review_status'] = 'invalidated'
                save(db, prior)
            else:
                base = git(path, 'rev-parse', 'HEAD')
                branch = 'bridge/' + time.strftime('%Y%m%d') + '-' + task_id
                worktree = state / 'worktrees' / task_id
                git(path, 'worktree', 'add', '-b', branch, str(worktree), base)
            folder.mkdir()
            (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
            task = dict(task_id=task_id, repo=str(path), status='queued', kind='build',
                        created_at=time.time(), base_head=base, branch=branch, worktree=str(worktree),
                        worktree_owner_id=prior.get('worktree_owner_id', prior['task_id']) if prior else task_id,
                        continue_from=continue_from, task_dir=str(folder), agent=agent, family=config['family'], config=config,
                        prompt=prompt, test_cmd=test_cmd, timeout_min=timeout,
                        dirty_main_copied=False, runner_pid=None, runner_created=None,
                        child_pid=None, child_created=None, heartbeat=None)
            save(db, task)
        self._spawn(path, task_id)
        return self._public(task)

    def _init(self, repo):
        state = safe_state(repo)
        state.mkdir(exist_ok=True)
        (state / 'tasks').mkdir(exist_ok=True)
        (state / 'worktrees').mkdir(exist_ok=True)
        with database(repo) as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)')
            old = db.execute("SELECT value FROM settings WHERE key='max_parallel'").fetchone()
            cap = min(int(old[0]), self.max_parallel) if old else self.max_parallel
            db.execute("INSERT OR REPLACE INTO settings VALUES ('max_parallel', ?)", (str(cap),))
        exclude = Path(git(repo, 'rev-parse', '--git-path', 'info/exclude'))
        if not exclude.is_absolute():
            exclude = repo / exclude
        text = exclude.read_text(encoding='utf-8') if exclude.exists() else ''
        if '/.bridge/' not in text.splitlines():
            with exclude.open('a', encoding='utf-8') as f:
                f.write('\n/.bridge/\n')
        return state

    def _spawn(self, repo, task_id):
        kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
                  'close_fds': True}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        with database(repo) as db:
            task = load(db, task_id)
            if task['status'] != 'queued' or alive(task.get('runner_pid'), task.get('runner_created')):
                return
            argv = [sys.executable, str(Path(__file__).with_name('bridge_runner.py')), str(repo), task_id]
            try:
                in_job = False
                if os.name == 'nt':
                    import win32api
                    import win32job
                    in_job = win32job.IsProcessInJob(win32api.GetCurrentProcess(), None)
                child = windows_worker(argv, repo) if in_job else subprocess.Popen(argv, **kwargs)
                task['runner_pid'] = child.pid
                task['runner_created'] = psutil.Process(child.pid).create_time()
                task['heartbeat'] = time.time()
                task['launch_method'] = 'wmi' if in_job else 'detached_subprocess'
            except Exception as exc:
                task.update(status='failed', error='worker launch failed: ' + str(exc),
                            finished_at=time.time())
                save(db, task)
                return
            save(db, task)
        # Reap while this server lives; detached worker survives server exit.
        import threading
        threading.Thread(target=child.wait, daemon=True).start()

    def _recover(self, repo):
        if not (repo / '.bridge' / 'state.sqlite').exists():
            return
        with database(repo) as db:
            recover_states(db)
            restart = [t['task_id'] for t in records(db) if t['status'] == 'queued' and not alive(t.get('runner_pid'), t.get('runner_created'))]
        for task_id in restart:
            self._spawn(repo, task_id)

    def _public(self, task):
        return {k: v for k, v in task.items() if k not in ('config', 'prompt')}

    def get_status(self, repo, task_id, log_lines=40):
        if not isinstance(log_lines, int) or isinstance(log_lines, bool) or not 0 <= log_lines <= 400:
            raise BridgeError('log_lines must be between 0 and 400')
        path = self._repo(repo)
        self._recover(path)
        with database(path) as db:
            task = load(db, task_id)
        result = self._public(task)
        folder = Path(task['task_dir'])
        result['log'] = log_tail(folder / 'output.log', log_lines)
        result['test_log'] = log_tail(folder / 'test.log', log_lines)
        result['test_result'] = {'configured': bool(task.get('test_cmd')),
                                 'exit_code': task.get('test_exit_code'),
                                 'passed': task.get('test_exit_code') == 0
                                 if task.get('test_exit_code') is not None else None}
        if task['kind'] == 'build' and task.get('review_task_id'):
            from bridge_review import attach_review
            result = attach_review(self, path, result)
        return result

    def list_tasks(self, repo, limit=20):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise BridgeError('limit must be between 1 and 100')
        path = self._repo(repo)
        if not (path / '.bridge' / 'state.sqlite').exists():
            return []
        self._recover(path)
        with database(path) as db:
            return [self._public(t) for t in sorted(records(db), key=lambda t: t['created_at'], reverse=True)[:limit]]

    def get_diff(self, repo, task_id, max_chars=50000):
        path = self._repo(repo)
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 1 <= max_chars <= 1000000:
            raise BridgeError('max_chars must be between 1 and 1000000')
        with database(path) as db:
            task = load(db, task_id)
        work = Path(task['worktree'])
        patch = git(work, 'diff', '--no-ext-diff', '--no-textconv', '--binary', task['base_head'], '--', strip=False)
        files = subprocess.check_output(['git', '-C', str(work), 'ls-files', '--others', '--exclude-standard', '-z'], stdin=subprocess.DEVNULL, timeout=60).decode('utf-8', 'surrogateescape').split('\0')
        for name in files:
            if not name:
                continue
            result = subprocess.run(['git', '-C', str(work), 'diff', '--no-ext-diff', '--binary', '--no-index', '--', os.devnull, name], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
            if result.returncode not in (0, 1):
                raise BridgeError('cannot diff untracked file: ' + name)
            patch += '\n' + result.stdout.decode('utf-8', 'replace')
        return {'task_id': task_id, 'base_head': task['base_head'], 'patch': patch[:max_chars],
                'truncated': len(patch) > max_chars, 'total_chars': len(patch), 'has_changes': bool(patch.strip())}

    def cancel_task(self, repo, task_id):
        path = self._repo(repo)
        with database(path) as db:
            task = load(db, task_id)
            if task['status'] in ACTIVE:
                task['stop_request'] = 'cancelled'
                save(db, task)
        return self._public(task)

    def ask_human(self, repo, task_id, question):
        path = self._repo(repo)
        if not isinstance(question, str) or not question.strip():
            raise BridgeError('question must be nonempty')
        with database(path) as db:
            task = load(db, task_id)
            task['question'] = question
            (Path(task['task_dir']) / 'question.txt').write_text(question, encoding='utf-8')
            if task['status'] in ACTIVE:
                task['stop_request'] = 'needs_human'
            else:
                task['status'] = 'needs_human'
            save(db, task)
        return self._public(task)

    def review_task(self, repo, task_id, agent='codex', prompt=''):
        from bridge_review import review_task
        return review_task(self, repo, task_id, agent, prompt)

    def cleanup_task(self, repo, task_id, force=False):
        path = self._repo(repo)
        if not isinstance(force, bool):
            raise BridgeError('force must be boolean')
        self._recover(path)
        with database(path) as db:
            task = load(db, task_id)
            if task['kind'] != 'build':
                raise BridgeError('clean up review snapshots through their build task')
            if task.get('continued_by'):
                raise BridgeError('clean up the latest continuation, not an old task')
            if task.get('cleaned_at'):
                return self._public(task)
            owner = task.get('worktree_owner_id', task_id)
            builds = [t for t in records(db) if t['kind'] == 'build' and
                      t.get('worktree_owner_id', t['task_id']) == owner]
            ids = {t['task_id'] for t in builds}
            related = builds + [t for t in records(db) if t.get('build_task_id') in ids]
            if any(t['status'] in ACTIVE for t in related):
                raise BridgeError('cannot clean up an active build or review')
            for item in related:
                valid_task_paths(path, item)
            work = Path(task['worktree'])
            registered = {Path(line[len('worktree '):]).resolve()
                          for line in git(path, 'worktree', 'list', '--porcelain').splitlines()
                          if line.startswith('worktree ')}
            if work.resolve() in registered:
                if not force and git(work, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z', strip=False):
                    raise BridgeError('cannot clean up ignored uncommitted files without force')
                args = ['worktree', 'remove'] + (['--force'] if force else []) + [str(work)]
                git(path, *args)
            elif work.exists():
                raise BridgeError('worktree is not registered with this repository')
            # Filesystem removal cannot roll back with SQLite. If an earlier
            # cleanup removed the owned worktree but failed deleting a snapshot,
            # retry only the remaining owned snapshots and metadata.
            now = time.time()
            for item in related:
                if item['kind'] == 'review':
                    review_root = path / '.bridge' / 'reviews' / item['task_id']
                    if review_root.resolve() != review_root:
                        raise BridgeError('review snapshot root redirected')
                    if review_root.exists():
                        def remove_readonly(function, filename, exc_info):
                            # Git objects are read-only on Windows; this path is
                            # already validated as an owned review snapshot.
                            os.chmod(filename, 0o700)
                            function(filename)
                        shutil.rmtree(review_root, onerror=remove_readonly)
                item['cleaned_at'] = now
                save(db, item)
            task['cleaned_at'] = now
        return self._public(task)

    def list_agents(self):
        try:
            configs = json.loads(self.agents_file.read_text(encoding='utf-8'))['agents']
            result = {}
            for name, cfg in configs.items():
                modes = {mode: self._agent(name, mode, check_executable=False)[mode] for mode in ('build', 'review') if mode in cfg}
                if not modes:
                    raise BridgeError('agent needs a build or review command')
                result[name] = {'family': cfg['family'], **modes,
                                'review_available': 'review' in modes,
                                'authentication': 'not_checked',
                                'available': all(shutil.which(argv[0]) is not None
                                                 for argv in modes.values())}
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BridgeError('cannot load agents') from exc
