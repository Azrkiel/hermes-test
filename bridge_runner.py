"""Detached worker. Invoke with repository path and task ID; no external service needed."""
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from bridge_core import database, git, load, save, terminate_tree


class Stopped(Exception):
    pass


def invocation_job():
    """Contain before execution; never fall back to an uncontained Windows child."""
    import win32job
    job = win32job.CreateJobObject(None, '')
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        return job
    except BaseException:
        job.Close()
        raise


def close_invocation_job(job):
    import win32job
    try:
        win32job.TerminateJobObject(job, 1)
        deadline = time.monotonic() + 10
        while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
            if time.monotonic() >= deadline:
                raise RuntimeError('Invocation Job still has live processes after termination')
            time.sleep(.01)
    finally:
        job.Close()


def execute(repo, task_id, command, log_name, deadline, env, input_text=None):
    child = log = job = None
    created = None
    try:
        with database(repo) as db:
            task = load(db, task_id)
            if task.get('stop_request'):
                raise Stopped(task['stop_request'])
            if time.monotonic() >= deadline:
                raise Stopped('timed_out')
            log = (Path(task['task_dir']) / log_name).open('ab')
            if os.name == 'nt':
                job = invocation_job()
            child = subprocess.Popen(command, shell=isinstance(command, str), cwd=task['worktree'], env=env,
                                     stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=(os.name != 'nt'),
                                     creationflags=(0x4 | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0)
            created = psutil.Process(child.pid).create_time()
            if job is not None:
                import win32job
                # CREATE_SUSPENDED prevents even a fast-exiting parent escaping containment.
                win32job.AssignProcessToJobObject(job, int(child._handle))
                psutil.Process(child.pid).resume()
            task.update(child_pid=child.pid, child_created=created)
            save(db, task)
        # Feeding stdin must not block monitoring or timeout enforcement.
        if input_text is not None:
            import threading
            def feed():
                try:
                    child.stdin.write(input_text.encode('utf-8'))
                    child.stdin.close()
                except (OSError, ValueError):
                    pass
            threading.Thread(target=feed, daemon=True).start()
        while True:
            with database(repo) as db:
                current = load(db, task_id)
                current['heartbeat'] = time.time()
                stop = current.get('stop_request')
                save(db, current)
            if not stop and time.monotonic() >= deadline:
                stop = 'timed_out'
            if stop:
                raise Stopped(stop)
            code = child.poll()
            if code is not None:
                return code
            time.sleep(.1)
    finally:
        primary = sys.exception()
        try:
            if job is not None:
                close_invocation_job(job)
            elif child is not None and os.name != 'nt':
                import signal
                # Only this Popen's newly created session/group is owned by this invocation.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if child is not None:
                if child.poll() is None:
                    terminate_tree(child.pid, created)
                    if child.poll() is None:
                        child.kill()  # Popen owns the process handle even if identity lookup failed.
                child.wait(timeout=10)
            if log is not None:
                log.close()
            if child is not None:
                with database(repo) as db:
                    current = load(db, task_id)
                    current.update(child_pid=None, child_created=None)
                    save(db, current)
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note(f'Invocation cleanup also failed: {cleanup_error!r}')


def run(repo, task_id):
    from bridge_core import recover_states
    while True:
        acquired = False
        with database(repo) as db:
            recover_states(db)
            task = load(db, task_id)
            process = psutil.Process()
            launch_pids = {process.pid, *(p.pid for p in process.parents())}
            if task['status'] != 'queued' or task['runner_pid'] not in launch_pids:
                return
            # Windows venv launchers spawn a second Python process. Record the real worker.
            task['runner_pid'] = process.pid
            task['runner_created'] = process.create_time()
            task['heartbeat'] = time.time()
            if task.get('stop_request'):
                task.update(status=task['stop_request'], finished_at=time.time())
                save(db, task)
                return
            cap = int(db.execute("SELECT value FROM settings WHERE key='max_parallel'").fetchone()[0])
            from bridge_core import records
            running = sum(t['status'] in ('running', 'testing') for t in records(db))
            if running < cap:
                task.update(status='running', started_at=time.time())
                acquired = True
            save(db, task)
        if acquired:
            break
        time.sleep(.15)
    deadline = time.monotonic() + task['timeout_min'] * 60
    code = test_code = error = None
    try:
        result_file = str(Path(task['task_dir']) / 'result.txt')
        argv = [a.replace('{prompt}', task['prompt'])
                .replace('{prompt_file}', str(Path(task['task_dir']) / 'prompt.txt'))
                .replace('{result_file}', result_file)
                for a in task['config'][task['kind']]]
        env = os.environ.copy()
        env.update(task['config'].get('env', {}))
        code = execute(repo, task_id, argv, 'output.log', deadline, env, task['prompt'] if task['config'].get('stdin_prompt') else None)
        if task['kind'] == 'review':
            from bridge_review import finish_review
            finish_review(repo, task_id, code, 'done' if code == 0 else 'failed')
            return
        status = 'done' if code == 0 else 'failed'
        if code == 0 and task['test_cmd']:
            with database(repo) as db:
                current = load(db, task_id)
                current['status'] = 'testing'
                save(db, current)
            test_code = execute(repo, task_id, task['test_cmd'], 'test.log', deadline, env)
            if test_code:
                status = 'tests_failed'
        if status == 'done':
            if not git(task['worktree'], 'diff', '--no-ext-diff', task['base_head'], '--') and not git(task['worktree'], 'ls-files', '--others', '--exclude-standard'):
                status = 'no_changes'
    except Stopped as exc:
        status = str(exc)
    except Exception as exc:
        status, error = 'failed', str(exc)
    if task['kind'] == 'review':
        from bridge_review import finish_review
        finish_review(repo, task_id, code, status, error=error)
        return
    with database(repo) as db:
        task = load(db, task_id)
        task.update(status=task.get('stop_request') or status, exit_code=code, test_exit_code=test_code,
                    error=error, finished_at=time.time(), heartbeat=time.time())
        save(db, task)


if __name__ == '__main__':
    run(*sys.argv[1:3])
