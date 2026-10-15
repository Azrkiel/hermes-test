"""Persist independent coding shards; never merge or push their output."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import psutil

from bridge_core import (
    ACTIVE,
    BridgeError,
    SwarmStopped,
    alive,
    database,
    git,
    records,
    windows_worker,
)

_LOCK = threading.RLock()


class Swarm:
    def __init__(self, bridge):
        self.bridge = bridge

    @staticmethod
    def _load(db, swarm_id):
        if not isinstance(swarm_id, str) or not re.fullmatch(r'[0-9a-f]{32}', swarm_id):
            raise BridgeError('invalid swarm ID')
        row = db.execute('SELECT data FROM swarms WHERE id=?', (swarm_id,)).fetchone()
        if not row:
            raise BridgeError('unknown swarm')
        return json.loads(row[0])

    @staticmethod
    def _save(db, data):
        db.execute('INSERT OR REPLACE INTO swarms VALUES (?, ?)',
                   (data['swarm_id'], json.dumps(data)))

    @staticmethod
    def _public(data):
        result = {k: v for k, v in data.items() if k not in ('items', 'agents_file', 'roots')}
        result['counts'] = dict(Counter(item['status'] for item in data['items']))
        result['items'] = [{k: v for k, v in item.items() if k != 'spec'}
                           for item in data['items']]
        return result

    def start(self, repo, tasks, agent='opencode', reviewer='codex', workers=3,
              fallback=None, timeout_min=15, autostart=True):
        path = self.bridge._repo(repo)
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= self.bridge.max_parallel:
            raise BridgeError('workers must fit configured bridge capacity')
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 200:
            raise BridgeError('supply 1 to 200 independent shards')
        build_family = self.bridge._agent(agent)['family']
        review_family = self.bridge._agent(reviewer, 'review')['family']
        if build_family == review_family:
            raise BridgeError('independent reviewer family required')
        if fallback and self.bridge._agent(fallback)['family'] == review_family:
            raise BridgeError('fallback must differ from reviewer family')
        timeout_min = self.bridge._timeout(timeout_min)
        names, owned = set(), []
        for spec in tasks:
            if not isinstance(spec, dict) or spec.get('depends_on'):
                raise BridgeError('only independent shard objects are supported')
            name = spec.get('name')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name in names:
                raise BridgeError('unique simple shard names required')
            names.add(name)
            if not isinstance(spec.get('prompt'), str) or not spec['prompt'].strip() or '\x00' in spec['prompt']:
                raise BridgeError('nonempty shard prompt required')
            cmd = spec.get('test_cmd')
            if not isinstance(cmd, list) or not cmd or any(not isinstance(a, str) or not a or '\x00' in a for a in cmd):
                raise BridgeError('each shard requires an explicit trusted test argv')
            files = spec.get('files')
            if not isinstance(files, list) or not files:
                raise BridgeError('each shard requires file ownership')
            for file in files:
                if not isinstance(file, str) or not file or file.startswith('/') or any(c in file for c in ('\\', ':', '\x00', '*', '?')):
                    raise BridgeError('ownership uses literal relative POSIX paths')
                parts = file.rstrip('/').split('/')
                if any(part.lower() in ('', '.', '..', '.git', '.bridge', '.env') for part in parts):
                    raise BridgeError('unsafe ownership path')
                from bridge_contract import test_path
                if test_path(file):
                    raise BridgeError('frozen test ownership is forbidden')
                normalized = file.rstrip('/').casefold()
                if any(normalized == old or normalized.startswith(old + '/') or old.startswith(normalized + '/') for old in owned):
                    raise BridgeError('overlapping ownership across shards')
                owned.append(normalized)
        if git(path, 'status', '--porcelain'):
            raise BridgeError('swarm requires a clean committed baseline')
        from bridge_contract import contract_paths
        from bridge_review import fingerprint_worktree
        baseline_fingerprint = fingerprint_worktree(path, git(path, 'rev-parse', 'HEAD'))
        frozen = contract_paths(path, tasks)
        if any(name.casefold() == own or name.casefold().startswith(own + '/') or own.startswith(name.casefold() + '/')
               for name in frozen for own in owned):
            raise BridgeError('ownership overlaps a frozen test contract')
        self.bridge._init(path)
        data = dict(swarm_id=uuid.uuid4().hex, repo=str(path), status='queued',
                    baseline=git(path, 'rev-parse', 'HEAD'), baseline_fingerprint=baseline_fingerprint,
                    created_at=time.time(),
                    workers=workers, frozen_paths=frozen, agent=agent, reviewer=reviewer, fallback=fallback,
                    timeout_min=timeout_min, owner_pid=None, owner_created=None,
                    auto_run=autostart, coordinator_restarts=0, heartbeat=None,
                    items=[dict(name=spec['name'], spec=spec, status='pending', attempt=0,
                                task_id=None, review_id=None) for spec in tasks])
        with database(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS swarms(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            self._save(db, data)
        if autostart:
            self._spawn(path, data['swarm_id'])
        return self._public(data)

    @staticmethod
    def _main_changed(repo, data):
        from bridge_review import fingerprint_worktree
        return (git(repo, 'rev-parse', 'HEAD') != data['baseline']
                or bool(git(repo, 'status', '--porcelain'))
                or (data.get('baseline_fingerprint') is not None
                    and fingerprint_worktree(repo, data['baseline']) != data['baseline_fingerprint']))

    def _fresh_approvals(self, repo, data):
        """Never report cached approval as current after edits/continuations/cleanup."""
        for item in data['items']:
            if item['status'] != 'approved':
                continue
            try:
                task = self.bridge.get_status(str(repo), item['task_id'], log_lines=0)
                from bridge_contract import verify
                verify(task)
                valid = (task['status'] == 'done' and task.get('test_exit_code') == 0
                         and task.get('review_status') == 'approved'
                         and not self._scope_error(item, task))
                error = 'approval invalidated: source changed, continued, or cleaned'
            except (BridgeError, OSError) as exc:
                valid, error = False, 'cannot validate approval: ' + str(exc)
            if not valid:
                item.update(status='invalidated', error=error)
        if data['status'] == 'ready':
            if self._main_changed(repo, data):
                data.update(status='needs_attention', error='baseline changed after approval')
            elif any(item['status'] != 'approved' for item in data['items']):
                data['status'] = 'needs_attention'
        return data

    def status(self, repo, swarm_id):
        path = self.bridge._repo(repo)
        with database(path) as db:
            data = self._load(db, swarm_id)
        if data.get('auto_run') and data['status'] in ('queued', 'running'):
            self._spawn(path, swarm_id, recovery=True)
            with database(path) as db:
                data = self._load(db, swarm_id)
        return self._public(self._fresh_approvals(path, data))


    def list(self, repo, limit=20):
        path = self.bridge._repo(repo)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise BridgeError('limit must be between 1 and 100')
        if not (path / '.bridge' / 'state.sqlite').exists():
            return []
        with database(path) as db:
            present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='swarms'").fetchone()
            if not present:
                return []
            values = [json.loads(row[0]) for row in db.execute('SELECT data FROM swarms')]
        latest = sorted(values, key=lambda item: item['created_at'], reverse=True)[:limit]
        return [self.status(str(path), item['swarm_id']) for item in latest]

    @staticmethod
    def _owned_records(db, data):
        values = records(db)
        ids = {item['task_id'] for item in data['items'] if item.get('task_id')}
        prefix = f"SWARM {data['swarm_id']} SHARD "
        ids.update(task['task_id'] for task in values
                   if task['kind'] == 'build' and task.get('prompt', '').startswith(prefix))
        return [task for task in values if task['task_id'] in ids or
                (task['kind'] == 'review' and task.get('build_task_id') in ids)]

    def _stop_owned(self, db, data):
        from bridge_core import save
        for task in self._owned_records(db, data):
            if task['status'] in ACTIVE:
                task['stop_request'] = 'cancelled'
                save(db, task)

    def cancel(self, repo, swarm_id):
        path = self.bridge._repo(repo)
        with database(path) as db:
            data = self._load(db, swarm_id)
            data['cancel_requested'] = True
            self._stop_owned(db, data)
            for item in data['items']:
                if item['status'] in ('pending', 'retry_pending'):
                    item['status'] = 'cancelled'
            self._save(db, data)
        return self._public(data)

    @staticmethod
    def _changed(task):
        work = task['worktree']
        tracked = git(work, 'diff', '--no-renames', '--name-only', task['base_head'], '-z', '--', strip=False)
        untracked = git(work, 'ls-files', '--others', '--exclude-standard', '-z', strip=False)
        ignored = git(work, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z', strip=False)
        # Only known test/runtime caches are exempt. Ignoring a source file cannot
        # make an out-of-scope edit disappear from the ownership gate.
        def is_generated(name):
            return name in {'.pytest_cache/.gitignore', '.pytest_cache/CACHEDIR.TAG',
                            '.pytest_cache/README.md', '.pytest_cache/v/cache/nodeids',
                            '.pytest_cache/v/cache/lastfailed', '.pytest_cache/v/cache/stepwise'}
        relevant = [name for name in ignored.split('\x00') if name and not is_generated(name)]
        from bridge_contract import capture
        from bridge_review import _safe_path
        actual_changed = []
        for name, expected in task.get('baseline_file_hashes', {}).items():
            path = _safe_path(Path(work), name)
            if (path is None or capture(work, [name])[name] != expected or
                    path.stat().st_mode & 0o777 != task['baseline_file_modes'][name]):
                actual_changed.append(name)
        return sorted(set((tracked + untracked).split('\x00')) - {''} | set(relevant) | set(actual_changed))

    def _scope_error(self, item, task):
        from bridge_review import _files, _safe_path
        modes = task.get('baseline_file_modes')
        if not isinstance(modes, dict) or set(modes) != set(task.get('baseline_file_hashes', {})):
            return 'baseline file modes unavailable; rerun shard with the current bridge'
        names = self._changed(task)
        if any(not any(name.casefold() == own.rstrip('/').casefold() or
                       (own.endswith('/') and name.casefold().startswith(own.casefold()))
                       for own in item['spec']['files']) for name in names):
            return 'worker changed files outside ownership'
        reviewed_files = {name for name, _ in _files(task['worktree'])}
        for name in names:
            if name not in reviewed_files and _safe_path(Path(task['worktree']), name) is not None:
                return 'changed ignored source is outside the immutable review snapshot'
        return None

    def _evidence(self, item, task):
        from bridge_review import _safe_path
        pieces = [item['spec']['prompt'], 'Review the supplied full changed files and real tests. '
                  'Do not use tools or modify files. If evidence is insufficient request changes.',
                  'TEST COMMAND: ' + json.dumps(item['spec']['test_cmd']),
                  'ACTUAL TEST OUTPUT (bounded tail; earlier output may be omitted):\n' + task.get('test_log', '')]
        remaining = 200000 - sum(len(piece) + 2 for piece in pieces)
        for name in self._changed(task):
            path = _safe_path(Path(task['worktree']), name)
            header = 'FILE ' + name + '\n'
            remaining -= len(header) + 2
            if remaining < 0:
                raise BridgeError('review evidence exceeds 200000 characters')
            if path:
                with path.open(encoding='utf-8') as stream:
                    content = stream.read(remaining + 1)
            else:
                content = '[deleted]'
            if len(content) > remaining:
                raise BridgeError('review evidence exceeds 200000 characters')
            pieces.append(header + content)
            remaining -= len(content)
        if remaining < 0:
            raise BridgeError('review evidence exceeds 200000 characters')
        return '\n\n'.join(pieces)

    def tick(self, repo, swarm_id):
        with _LOCK:
            return self._tick(repo, swarm_id)

    def _tick(self, repo, swarm_id):
        path = self.bridge._repo(repo)
        pid, created = os.getpid(), psutil.Process().create_time()
        with database(path) as db:
            data = self._load(db, swarm_id)
            owner = data.get('owner_pid')
            owner_is_launcher = any(parent.pid == owner for parent in psutil.Process().parents())
            if alive(owner, data.get('owner_created')) and owner != pid and not owner_is_launcher:
                return self._public(data)
            data.update(owner_pid=pid, owner_created=created, heartbeat=time.time())
            self._save(db, data)
            existing = records(db)
        self._fresh_approvals(path, data)
        if data['status'] in ('ready', 'needs_attention', 'cancelled'):
            with database(path) as db:
                self._save(db, data)
            return self._public(data)
        data['status'] = 'running'
        baseline_changed = self._main_changed(path, data)
        slots = 0
        reviews = 0
        for item in data['items']:
            tag = f"SWARM {swarm_id} SHARD {item['name']} ATTEMPT {max(1, item['attempt'])}\n"
            if item['status'] not in ('approved', 'scope_violation', 'cancelled', 'blocked', 'failed', 'needs_human'):
                prefix = f"SWARM {swarm_id} SHARD {item['name']} ATTEMPT "
                candidates = sorted([t for t in existing if t['kind'] == 'build' and t.get('prompt', '').startswith(prefix)], key=lambda t: t['created_at'], reverse=True)
                if candidates and item['task_id'] != candidates[0]['task_id']:
                    attempt = int(candidates[0]['prompt'].split('\n', 1)[0].split()[-1])
                    item.update(task_id=candidates[0]['task_id'], review_id=None, status='building', attempt=attempt)
            if data.get('cancel_requested') or baseline_changed:
                if item['status'] in ('pending', 'retry_pending'):
                    item['status'] = 'cancelled' if data.get('cancel_requested') else 'blocked'
                for task_id in (item['task_id'], item['review_id']):
                    if task_id:
                        self.bridge.cancel_task(str(path), task_id)
            if item['status'] not in ('building', 'waiting_review', 'reviewing', 'draining'):
                continue
            task = self.bridge.get_status(str(path), item['task_id'], log_lines=400)
            review = task.get('review', {})
            if review:
                item['review_id'] = review['task_id']
            if item['status'] == 'draining':
                if task['status'] in ACTIVE or review.get('status') in ACTIVE:
                    for task_id in (item['task_id'], item['review_id']):
                        if task_id:
                            self.bridge.cancel_task(str(path), task_id)
                    slots += 1
                    continue
                item['status'] = item.pop('terminal_after_drain')
                continue
            if task['status'] in ACTIVE:
                slots += 1
                continue
            if data.get('cancel_requested') or baseline_changed:
                review = task.get('review', {})
                if review.get('status') in ACTIVE:
                    slots += 1
                    continue
                item['status'] = 'cancelled' if data.get('cancel_requested') else 'blocked'
                continue
            scope_error = self._scope_error(item, task)
            invalidated = task.get('review_status') == 'invalidated'
            if scope_error or invalidated:
                terminal = 'scope_violation' if scope_error else 'invalidated'
                error = scope_error or 'reviewed evidence invalidated'
                if review.get('status') in ACTIVE:
                    self.bridge.cancel_task(str(path), review['task_id'])
                    item.update(status='draining', terminal_after_drain=terminal, error=error)
                    slots += 1
                else:
                    item.update(status=terminal, error=error)
                continue
            eligible = task['status'] == 'tests_failed' or task.get('review_status') == 'changes_requested'
            if eligible and data['fallback'] and item['attempt'] == 1:
                item.update(status='retry_pending', error='tests or review requested an explicit fallback')
                continue
            if task['status'] != 'done' or task.get('test_exit_code') != 0:
                item.update(status='needs_human' if task['status'] == 'needs_human' else 'failed',
                            error=task.get('error') or 'builder or tests failed')
                continue
            review = task.get('review', {})
            if review:
                item['review_id'] = review['task_id']
                if review['status'] in ACTIVE:
                    item['status'] = 'reviewing'
                    slots += 1
                    reviews += 1
                elif task.get('review_status') == 'approved':
                    item['status'] = 'approved'
                else:
                    item.update(status='failed', error=review.get('error') or 'independent review did not approve')
            else:
                item['status'] = 'waiting_review'
        for item in sorted(data['items'], key=lambda i: i['status'] != 'waiting_review'):
            if slots >= data['workers']:
                break
            if item['status'] == 'waiting_review' and reviews == 0 and not data.get('cancel_requested'):
                task = self.bridge.get_status(str(path), item['task_id'], log_lines=400)
                try:
                    reviewed = self.bridge.review_task(str(path), item['task_id'], data['reviewer'],
                                                       self._evidence(item, task), timeout_min=data['timeout_min'],
                                                       swarm_id=swarm_id)
                except SwarmStopped:
                    data['cancel_requested'] = True
                    item['status'] = 'cancelled'
                    break
                item.update(status='reviewing', review_id=reviewed['task_id'])
                reviews += 1
                slots += 1
            elif item['status'] in ('pending', 'retry_pending') and not data.get('cancel_requested') and not baseline_changed:
                item['attempt'] += 1
                tag = f"SWARM {swarm_id} SHARD {item['name']} ATTEMPT {item['attempt']}\n"
                prompt = (tag + 'Edit ONLY these owned paths: ' + json.dumps(item['spec']['files']) +
                          '\nDo not invoke other agents, change tests, commit, merge, push, or read credentials. '
                          'Worktrees isolate edits, not OS access.\n' + item['spec']['prompt'])
                retry = item['status'] == 'retry_pending'
                if retry:
                    prior = self.bridge.get_status(str(path), item['task_id'], log_lines=100)
                    prompt += '\nPrevious test/review findings:\n' + prior.get('test_log', '') + '\n' + prior.get('review', {}).get('log', '')
                try:
                    task = self.bridge.dispatch_task(str(path), prompt, data['fallback'] if retry else data['agent'],
                                                     item['spec']['test_cmd'], continue_from=item['task_id'] if retry else None,
                                                     timeout_min=data['timeout_min'], frozen_paths=data.get('frozen_paths', []),
                                                     swarm_id=swarm_id)
                except SwarmStopped:
                    data['cancel_requested'] = True
                    item['status'] = 'cancelled'
                    break
                item.update(task_id=task['task_id'], status='building')
                slots += 1
        states = [item['status'] for item in data['items']]
        if all(state == 'approved' for state in states):
            data['status'] = 'ready'
        elif not any(state in ('pending', 'retry_pending', 'building', 'waiting_review', 'reviewing', 'draining') for state in states):
            data['status'] = 'cancelled' if data.get('cancel_requested') else 'needs_attention'
        with database(path) as db:
            latest = self._load(db, swarm_id)
            if latest.get('cancel_requested'):
                data['cancel_requested'] = True
                self._stop_owned(db, data)
                active = {task['task_id'] for task in self._owned_records(db, data)
                          if task['status'] in ACTIVE}
                for item in data['items']:
                    if active.intersection((item.get('task_id'), item.get('review_id'))):
                        item.update(status='draining', terminal_after_drain='cancelled')
                    elif item['status'] in ('pending', 'retry_pending', 'building', 'waiting_review', 'reviewing', 'draining'):
                        item['status'] = 'cancelled'
                        item.pop('terminal_after_drain', None)
                data['status'] = 'running' if active else 'cancelled'
            self._save(db, data)
        return self._public(data)


    def _spawn(self, repo, swarm_id, recovery=False):
        with database(repo) as db:
            data = self._load(db, swarm_id)
            if alive(data.get('owner_pid'), data.get('owner_created')):
                return
            if data['status'] not in ('queued', 'running'):
                return
            if recovery:
                count = data.get('coordinator_restarts', 0)
                if count >= 3:
                    self._stop_owned(db, data)
                    data.update(status='needs_attention', error='coordinator restart budget exhausted')
                    self._save(db, data)
                    return
                data['coordinator_restarts'] = count + 1
            argv = [sys.executable, str(Path(__file__).resolve()), '--run', str(repo), swarm_id,
                    '--roots', json.dumps([str(r) for r in self.bridge.roots]),
                    '--agents', str(self.bridge.agents_file.resolve()),
                    '--capacity', str(self.bridge.max_parallel),
                    '--default-timeout', str(self.bridge.timeout_min)]
            self._save(db, data)  # Persist retry budget even if native launch is refused.
            child = None
            try:
                in_job = False
                if os.name == 'nt':
                    import win32api
                    import win32job
                    in_job = win32job.IsProcessInJob(win32api.GetCurrentProcess(), None)
                if in_job:
                    child = windows_worker(argv, repo)
                else:
                    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True)
                    if os.name == 'nt':
                        kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                    else:
                        kwargs['start_new_session'] = True
                    child = subprocess.Popen(argv, **kwargs)
                data.update(owner_pid=child.pid, owner_created=psutil.Process(child.pid).create_time())
            except Exception as exc:
                data.update(status='needs_attention', error='coordinator launch failed: ' + str(exc))
            self._save(db, data)
        if child is not None:
            threading.Thread(target=child.wait, daemon=True).start()


def main():
    import argparse

    from bridge_core import Bridge
    from bridge_power import keep_awake
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--run', nargs=2, metavar=('REPO', 'SWARM_ID'))
    modes.add_argument('--start', nargs=2, metavar=('REPO', 'PLAN_JSON'))
    modes.add_argument('--status', nargs=2, metavar=('REPO', 'SWARM_ID'))
    modes.add_argument('--cancel', nargs=2, metavar=('REPO', 'SWARM_ID'))
    modes.add_argument('--list', metavar='REPO')
    parser.add_argument('--roots', help='JSON array of narrow allowed roots; defaults to env')
    parser.add_argument('--agents')
    parser.add_argument('--capacity', type=int, default=3)
    parser.add_argument('--default-timeout', type=float, default=15)
    args = parser.parse_args()
    manager = Swarm(Bridge(roots=json.loads(args.roots) if args.roots else None,
                          agents_file=args.agents, max_parallel=args.capacity,
                          timeout_min=args.default_timeout))
    if args.start:
        repo, plan_path = args.start
        plan = json.loads(Path(plan_path).read_text(encoding='utf-8'))
        print(json.dumps(manager.start(repo, **plan), indent=2))
        return
    if args.status or args.cancel:
        repo, swarm_id = args.status or args.cancel
        action = manager.status if args.status else manager.cancel
        print(json.dumps(action(repo, swarm_id), indent=2))
        return
    if args.list:
        print(json.dumps(manager.list(args.list), indent=2))
        return
    repo, swarm_id = args.run
    path = manager.bridge._repo(repo)
    power = None
    try:
        with keep_awake() as power:
            with database(path) as db:
                data = manager._load(db, swarm_id)
                data.update(keep_awake_supported=power['supported'], keep_awake_active=power['active'])
                manager._save(db, data)
            while True:
                state = manager.tick(repo, swarm_id)
                if state['status'] in ('ready', 'needs_attention', 'cancelled'):
                    break
                time.sleep(2)
    except Exception as exc:
        with database(path) as db:
            data = manager._load(db, swarm_id)
            manager._stop_owned(db, data)
            data.update(status='needs_attention', error=str(exc))
            manager._save(db, data)
        for item in data['items']:
            for task_id in (item['task_id'], item['review_id']):
                if task_id:
                    manager.bridge.cancel_task(repo, task_id)
        raise
    finally:
        with database(path) as db:
            data = manager._load(db, swarm_id)
            data.update(keep_awake_active=power['active'] if power is not None else False,
                        coordinator_finished_at=time.time())
            manager._save(db, data)


if __name__ == '__main__':
    main()
