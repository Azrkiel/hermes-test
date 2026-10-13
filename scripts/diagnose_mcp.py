"""Read-only code instrumentation plus disposable local repos for Windows MCP diagnosis.
Run: PYTHONPATH= .venv/Scripts/python.exe scripts/diagnose_mcp.py core|mcp|jobs
"""
import asyncio
import faulthandler
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
T0 = time.monotonic()

def log(*values):
    print(f"[diagnose +{time.monotonic()-T0:.3f}s pid={os.getpid()}]", *values, file=sys.stderr, flush=True)

def instrument():
    import bridge_core as c
    if os.environ.get('DIAG_BREAKAWAY'):
        original_popen = subprocess.Popen
        class BreakawayPopen(original_popen):
            def __init__(self, args, *a, **kw):
                if isinstance(args, list) and any(str(x).endswith('bridge_runner.py') for x in args):
                    kw['creationflags'] = kw.get('creationflags', 0) | subprocess.CREATE_BREAKAWAY_FROM_JOB
                super().__init__(args, *a, **kw)
        subprocess.Popen = BreakawayPopen
    if os.environ.get('DIAG_GIT_DEVNULL'):
        original_run = subprocess.run
        def git_run(*args, **kwargs):
            if args and isinstance(args[0], list) and args[0][0] == 'git':
                kwargs['stdin'] = subprocess.DEVNULL
            return original_run(*args, **kwargs)
        subprocess.run = git_run
    if os.environ.get('DIAG_THREAD_TOOLS'):
        from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
        original_call = FuncMetadata.call_fn_with_arg_validation
        async def threaded_call(self, fn, fn_is_async, arguments_to_validate, arguments_to_pass_directly=None):
            if not fn_is_async:
                def sync_call():
                    return asyncio.run(original_call(self, fn, fn_is_async, arguments_to_validate, arguments_to_pass_directly))
                return await asyncio.to_thread(sync_call)
            return await original_call(self, fn, fn_is_async, arguments_to_validate, arguments_to_pass_directly)
        FuncMetadata.call_fn_with_arg_validation = threaded_call
    for name in ('git', 'alive', 'save', 'load'):
        original = getattr(c, name)
        def wrapper(*args, _fn=original, _name=name, **kw):
            log('ENTER', _name, repr(args)[:180])
            try:
                return _fn(*args, **kw)
            finally:
                log('LEAVE', _name)
        setattr(c, name, wrapper)
    for name in ('__init__', 'dispatch_task', '_init', '_spawn', '_public'):
        original = getattr(c.Bridge, name)
        def wrapper(*args, _fn=original, _name=name, **kw):
            log('ENTER Bridge.' + _name)
            try:
                return _fn(*args, **kw)
            finally:
                log('LEAVE Bridge.' + _name)
        setattr(c.Bridge, name, wrapper)
    faulthandler.dump_traceback_later(4, repeat=True, file=sys.stderr)

def fixture(sleep=1):
    folder = Path(tempfile.mkdtemp(prefix='bridge-diagnose-'))
    repo = folder / 'repo'
    repo.mkdir()
    for args in [('init',), ('config', 'user.name', 'Diagnostic'), ('config', 'user.email', 'diagnostic@localhost')]:
        subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
    (repo / 'README.md').write_text('original\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], capture_output=True, check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'initial'], capture_output=True, check=True)
    agents = folder / 'agents.json'
    agents.write_text(json.dumps({'agents': {'builder': {'family': 'builder', 'build': [sys.executable, '-c', f"import time; from pathlib import Path; time.sleep({sleep}); Path('result.txt').write_text('result')"]}}}))
    log('FIXTURE', folder)
    return folder, repo, agents

def snapshot(repo):
    import sqlite3

    import psutil
    db = repo / '.bridge' / 'state.sqlite'
    if not db.exists():
        return
    try:
        with sqlite3.connect(str(db), timeout=.1) as con:
            for row in con.execute('SELECT data FROM tasks'):
                task = json.loads(row[0])
                pid = task.get('runner_pid')
                log('STATE', task)
                try:
                    proc = psutil.Process(pid)
                    log('RUNNER', proc.pid, proc.ppid(), proc.cmdline(), 'children', [(p.pid, p.cmdline()) for p in proc.children(recursive=True)])
                except psutil.Error as e:
                    log('RUNNER', repr(e))
    except Exception as e:
        log('SNAPSHOT', repr(e))

async def mcp_probe():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    if os.environ.get('DIAG_BREAKAWAY'):
        import mcp.os.win32.utilities as win
        import win32job
        original_job = win._create_job_object
        def breakaway_job():
            job = original_job()
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            return job
        win._create_job_object = breakaway_job
    folder, repo, agents = fixture(5)
    env = dict(os.environ, BRIDGE_ALLOWED_ROOTS=json.dumps([str(folder)]), BRIDGE_AGENTS_FILE=str(agents), PYTHONPATH='')
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve()), 'server'], env=env)
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=10)) as session:
                await session.initialize()
                log('INITIALIZED')
                try:
                    result = await session.call_tool('dispatch_task', {'repo': str(repo), 'prompt': 'make result', 'agent': 'builder'})
                    log('CALL RESULT', result)
                except BaseException as e:
                    log('CALL EXCEPTION before context teardown', repr(e))
                    traceback.print_exc()
                snapshot(repo)
    except BaseException as e:
        log('CONTEXT EXCEPTION', repr(e))
    snapshot(repo)
    await asyncio.sleep(7 if os.environ.get('DIAG_BREAKAWAY') else 1)
    snapshot(repo)

def git_probe():
    folder, repo, agents = fixture()
    for null, reading in ((False, False), (False, True), (True, True)):
        prefix = "import threading,sys,time; threading.Thread(target=sys.stdin.buffer.readline, daemon=True).start(); time.sleep(.1); " if reading else ""
        code = prefix + f"import subprocess,os; print(subprocess.run(['git','-C',{str(repo)!r},'rev-parse','--is-bare-repository'], capture_output=True, text=True{',stdin=subprocess.DEVNULL' if null else ''}),flush=True); os._exit(0)"
        log('GIT CONFIG', 'DEVNULL', null, 'reading', reading)
        proc = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        hung = False
        try:
            proc.wait(timeout=2)
            log('GIT inherited open pipe returned', null, proc.returncode)
        except subprocess.TimeoutExpired:
            hung = True
            log('GIT inherited open pipe HUNG', 'DEVNULL', null)
        proc.stdin.close()
        proc.stdin = None
        out, err = proc.communicate(timeout=5)
        log('GIT after pipe close', 'DEVNULL', null, out, err)
        assert hung == (reading and not null), (null, reading, hung, out, err)
        assert proc.returncode == 0 and b"stdout='false" in out and not err, (out, err)

def test_git_stdin_isolation():
    """Prove the hang needs an active stdin reader and DEVNULL removes it."""
    git_probe()

def test_cooperative_job_breakaway_survives_mcp_shutdown():
    """Exercise real MCP with runtime-only stdin/job/launch-flag fixes."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), 'mcp'],
        env=dict(os.environ, PYTHONPATH='', DIAG_GIT_DEVNULL='1', DIAG_BREAKAWAY='1'),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert 'CALL RESULT' in result.stderr and 'CONTEXT EXCEPTION' not in result.stderr, result.stderr
    assert "'status': 'done'" in result.stderr and "'exit_code': 0" in result.stderr, result.stderr

def core_probe():
    import bridge_core
    folder, repo, agents = fixture()
    instrument()
    bridge = bridge_core.Bridge(roots=[folder], agents_file=agents)
    result = bridge.dispatch_task(str(repo), 'make result', agent='builder')
    log('CORE DISPATCH RESULT', result)
    snapshot(repo)
    time.sleep(2)
    snapshot(repo)
    faulthandler.cancel_dump_traceback_later()

async def jobs_probe():
    import psutil
    import win32job
    from mcp.os.win32.utilities import create_windows_process, terminate_windows_process_tree
    for parent_exe, allow_breakaway, breakaway in (
        (sys.executable, False, False),
        (sys.executable, False, True),
        (sys._base_executable, False, True),
        (sys._base_executable, True, True),
        (sys.executable, True, True),
    ):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if breakaway:
            flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
        log('JOB CONFIG', parent_exe, 'allow_breakaway', allow_breakaway)
        # Parent launches detached child, emits PID, then deliberately blocks stdin.
        child_code = "import time; time.sleep(120)"
        parent_code = f"import subprocess,sys,time; time.sleep(.8); p=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags={flags}); print(p.pid,flush=True); time.sleep(120)"
        proc = await create_windows_process(parent_exe, ['-c', parent_code], dict(os.environ))
        job = getattr(proc, '_job_object', None)
        if allow_breakaway and job:
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        child_pid = None
        try:
            data = await asyncio.wait_for(proc.stdout.receive(), timeout=5)
            child_pid = int(data.strip())
            log('JOB CHILD', child_pid, 'flags', hex(flags), 'alive before', psutil.pid_exists(child_pid), 'job', getattr(proc, '_job_object', None))
        except BaseException as e:
            log('JOB LAUNCH EXCEPTION', repr(e), 'flags', hex(flags))
        await terminate_windows_process_tree(proc)
        await asyncio.sleep(.3)
        log('JOB CHILD alive after termination', child_pid, bool(child_pid and psutil.pid_exists(child_pid)))
        if child_pid and psutil.pid_exists(child_pid):
            # Only disposable diagnostic sleeper, never unrelated processes.
            p = psutil.Process(child_pid)
            for ch in p.children(recursive=True):
                ch.kill()
            p.kill()
        await proc.aclose()

if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'server':
        instrument()
        import runpy
        runpy.run_path(str(ROOT / 'bridge_server.py'), run_name='__main__')
    elif mode == 'core':
        core_probe()
    elif mode == 'mcp':
        asyncio.run(mcp_probe())
    elif mode == 'jobs':
        asyncio.run(jobs_probe())
    elif mode == 'git':
        git_probe()
