"""Independent full-file review snapshots; no builder files are staged or edited.

Agent configuration may set ``result_file: true`` to read task_dir/result.txt,
with CLI argv containing ``{result_file}``. A relative string selects another
result path (and may contain {result_file}/{prompt_file}); paths cannot escape
that task's state directory. Without it, output.log is the verdict source.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

from bridge_core import ACTIVE, BridgeError, database, git, load, records, save

MAX_PATCH = 2 * 1024 * 1024


def _safe_path(root, name):
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise BridgeError('unsafe snapshot path: ' + name)
    path = root
    for part in relative.parts:
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BridgeError('symlink/reparse entry is unsafe for review: ' + name)
    if not stat.S_ISREG(info.st_mode):
        raise BridgeError('nonregular snapshot file: ' + name)
    return path


def _files(worktree):
    root = Path(worktree)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise BridgeError('unsafe review worktree')
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    if result.returncode:
        raise BridgeError('cannot enumerate review files')
    names = sorted(set(result.stdout.decode('utf-8', 'surrogateescape').split('\0')) - {''})
    output = []
    for name in names:
        if any(part.lower() in ('.git', '.bridge') for part in Path(name).parts):
            continue
        path = _safe_path(root, name)
        if path is not None:
            output.append((name, path))
    return output


def fingerprint_worktree(worktree, basehead):
    """Hash HEAD, index changes, filenames/modes and actual tracked/untracked bytes."""
    digest = hashlib.sha256()
    for value in (basehead, git(worktree, 'rev-parse', 'HEAD'),
                  git(worktree, 'diff', '--cached', '--no-ext-diff', '--binary', '--')):
        digest.update(value.encode('utf-8', 'surrogateescape') + b'\0')
    for name, path in _files(worktree):
        digest.update(name.encode('utf-8', 'surrogateescape') + b'\0')
        digest.update(str(path.stat().st_mode & 0o777).encode() + b'\0')
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        digest.update(b'\0')
    return digest.hexdigest()


def fingerprint(bridge, repo, task):
    return fingerprint_worktree(task['worktree'], task['base_head'])


def _validate(db, task_id, family):
    task = load(db, task_id)
    if task['kind'] != 'build' or task['status'] in (*ACTIVE, 'testing') or task.get('continued_by') or task.get('cleaned_at'):
        raise BridgeError('review requires latest terminal uncleaned build')
    if task['family'] == family:
        raise BridgeError('review requires a different agent family')
    if any(t['status'] in (*ACTIVE, 'testing') and
           (t.get('build_task_id') == task_id or t.get('continue_from') == task_id)
           for t in records(db)):
        raise BridgeError('review or continuation already active')
    return task


def review_task(bridge, repo, task_id, agent='codex', prompt=''):
    path = bridge._repo(repo)
    if not isinstance(prompt, str):
        raise BridgeError('review prompt must be a string')
    config = bridge._agent(agent, 'review')
    state = bridge._init(path)
    with database(path) as db:
        builder = _validate(db, task_id, config['family'])
    source_fingerprint = fingerprint(bridge, path, builder)
    # get_diff acquires a database lock itself, so keep it outside our transaction.
    # Public diff requests have a tighter 1,000,000-character safety cap.
    # Refusing its truncation remains fail-closed below the review upper bound.
    patch = bridge.get_diff(str(path), task_id, max_chars=min(MAX_PATCH, 1000000))
    if patch['truncated']:
        raise BridgeError('review diff exceeds maximum size')
    review_id = uuid.uuid4().hex
    folder = state / 'tasks' / review_id
    snapshot = state / 'reviews' / review_id / 'snapshot'
    with database(path) as db:
        builder = _validate(db, task_id, config['family'])
        if fingerprint(bridge, path, builder) != source_fingerprint:
            raise BridgeError('builder changed while preparing review')
        try:
            folder.mkdir()
            snapshot.mkdir(parents=True)
            for name, source in _files(builder['worktree']):
                destination = snapshot / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                os.chmod(destination, source.stat().st_mode & 0o777)
            if fingerprint(bridge, path, builder) != source_fingerprint:
                raise BridgeError('builder changed while copying review snapshot')
            git(snapshot, 'init')
            git(snapshot, 'config', 'user.name', 'Agent Bridge Review')
            git(snapshot, 'config', 'user.email', 'review@agent-bridge.invalid')
            git(snapshot, 'config', 'core.autocrlf', 'false')
            git(snapshot, 'add', '--force', '--all')
            git(snapshot, '-c', 'core.hooksPath=', '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'Isolated review snapshot')
            base = git(snapshot, 'rev-parse', 'HEAD')
            review_prompt = ('Review this isolated snapshot of the builder output. Do not modify files or commit. '
                             'Use the full files and the supplied builder diff. Finish with exactly one final line '
                             'VERDICT: APPROVE or VERDICT: CHANGES_REQUESTED.\n\n' + prompt +
                             '\n\nBUILDER DIFF:\n' + patch['patch'])
            (folder / 'prompt.txt').write_text(review_prompt, encoding='utf-8')
            (folder / 'diff.patch').write_text(patch['patch'], encoding='utf-8')
            task = dict(task_id=review_id, repo=str(path), kind='review', build_task_id=task_id,
                        status='queued', created_at=time.time(), worktree=str(snapshot), task_dir=str(folder),
                        base_head=base, branch=None, agent=agent, family=config['family'], config=config,
                        prompt=review_prompt, timeout_min=bridge.timeout_min, test_cmd=None,
                        runner_pid=None, runner_created=None, child_pid=None, child_created=None,
                        heartbeat=None, source_fingerprint=source_fingerprint,
                        snapshot_fingerprint=_snapshot_fingerprint(snapshot, base))
            save(db, task)
            builder.update(review_task_id=review_id, review_status='queued', review_fingerprint=source_fingerprint)
            save(db, builder)
        except BaseException:
            shutil.rmtree(snapshot, ignore_errors=True)
            shutil.rmtree(folder, ignore_errors=True)
            raise
    bridge._spawn(path, review_id)
    return bridge._public(task)


def _snapshot_fingerprint(worktree, basehead):
    """Include ignored new files too: a reviewer must not mutate its snapshot."""
    root = Path(worktree)
    digest = hashlib.sha256(fingerprint_worktree(root, basehead).encode())
    for directory, dirs, files in os.walk(root, followlinks=False):
        if Path(directory) == root:
            dirs[:] = [d for d in dirs if d != '.git']
        for name in sorted(dirs):
            candidate = Path(directory) / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise BridgeError('review snapshot contains unsafe directory')
            digest.update(('dir:' + candidate.relative_to(root).as_posix()).encode() + b'\0')
        dirs.sort()
        for name in sorted(files):
            candidate = Path(directory) / name
            relative = candidate.relative_to(root).as_posix()
            safe = _safe_path(root, relative)
            digest.update(relative.encode('utf-8', 'surrogateescape') + b'\0')
            digest.update(hashlib.sha256(safe.read_bytes()).digest())
    return digest.hexdigest()


def _result_path(task):
    root = Path(task['task_dir'])
    configured = task['config'].get('result_file')
    if configured is True:
        return root / 'result.txt'
    if not configured:
        return root / 'output.log'
    if not isinstance(configured, str):
        raise BridgeError('invalid result_file configuration')
    expanded = configured.replace('{result_file}', str(root / 'result.txt')).replace('{prompt_file}', str(root / 'prompt.txt'))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = root / candidate
    if root.resolve() not in candidate.resolve().parents:
        raise BridgeError('result_file must be within review task directory')
    return candidate


def finish_review(repo, task_id, exit_code, status, error=None):
    """Called once the CLI has stopped. Fail closed on stale or ambiguous output."""
    with database(repo) as db:
        task = load(db, task_id)
        if task['kind'] != 'review':
            raise BridgeError('not a review task')
        builder = load(db, task['build_task_id'])
        status = task.get('stop_request') or status
        verdict = None
        if status == 'done' and exit_code == 0:
            try:
                if _snapshot_fingerprint(task['worktree'], task['base_head']) != task['snapshot_fingerprint']:
                    raise BridgeError('reviewer mutated the isolated snapshot')
                if builder.get('continued_by') or builder.get('cleaned_at') or builder['status'] in (*ACTIVE, 'testing'):
                    raise BridgeError('review is stale: builder was continued or cleaned')
                if fingerprint_worktree(builder['worktree'], builder['base_head']) != task['source_fingerprint']:
                    raise BridgeError('review is stale: builder changed')
                result_path = _result_path(task)
                safe = _safe_path(Path(task['task_dir']), str(result_path.relative_to(Path(task['task_dir']))))
                if safe is None:
                    raise BridgeError('review result is missing')
                with safe.open('rb') as stream:
                    data = stream.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise BridgeError('review result exceeds 1 MiB')
                text = data.decode('utf-8', 'strict')
                lines = [line for line in text.splitlines() if line.strip()]
                match = re.fullmatch(r'VERDICT: (APPROVE|CHANGES_REQUESTED)', lines[-1] if lines else '')
                if not match or len(re.findall(r'\bVERDICT\s*:', text)) != 1:
                    raise BridgeError('missing or ambiguous final review verdict')
                verdict = match.group(1)
            except (BridgeError, OSError, UnicodeError, ValueError) as exc:
                status, error = 'failed', str(exc)
        elif status == 'done':
            status, error = 'failed', error or 'review CLI did not exit successfully'
        task.update(status=status, exit_code=exit_code, verdict=verdict, error=error,
                    finished_at=time.time(), heartbeat=time.time(), child_pid=None, child_created=None)
        save(db, task)
        artifact = {key: task.get(key) for key in ('task_id', 'build_task_id', 'status', 'verdict', 'error',
                                                  'exit_code', 'source_fingerprint', 'snapshot_fingerprint')}
        (Path(task['task_dir']) / 'review.json').write_text(json.dumps(artifact, indent=2), encoding='utf-8')
        if builder.get('review_task_id') == task_id:
            builder['review_status'] = ('approved' if verdict == 'APPROVE' else
                                        'changes_requested' if verdict == 'CHANGES_REQUESTED' else status)
            save(db, builder)
    return task


def attach_review(bridge, repo, build_task):
    """Enrich a public build record without holding SQLite during fingerprinting."""
    result = dict(build_task)
    review_id = build_task.get('review_task_id')
    if not review_id:
        return result
    with database(repo) as db:
        review = load(db, review_id)
    nested = bridge._public(review)
    logfile = Path(review['task_dir']) / 'output.log'
    # Logs are metadata, not the verdict source. Bound reads as well as display.
    try:
        with logfile.open('rb') as stream:
            stream.seek(max(0, logfile.stat().st_size - 16384))
            nested['log'] = '\n'.join(stream.read(16384).decode('utf-8', 'replace').splitlines()[-40:])
    except OSError:
        nested['log'] = ''
    invalidated = bool(build_task.get('continued_by') or build_task.get('cleaned_at') or
                       build_task.get('review_status') == 'invalidated')
    if not invalidated:
        try:
            invalidated = fingerprint(bridge, repo, build_task) != review['source_fingerprint']
        except (BridgeError, OSError):
            invalidated = True
    nested.setdefault('verdict', None)
    if invalidated:
        nested.update(status='invalidated', verdict=None, error='builder changed or was continued/cleaned')
    result['review'] = nested
    result['review_status'] = ('invalidated' if invalidated else
                               'approved' if nested['verdict'] == 'APPROVE' else
                               'changes_requested' if nested['verdict'] == 'CHANGES_REQUESTED' else nested['status'])
    return result
