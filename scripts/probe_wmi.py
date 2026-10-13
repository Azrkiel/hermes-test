"""Check Windows WMI can launch a same-user worker outside an MCP Job Object."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import win32com.client

with tempfile.TemporaryDirectory(prefix='bridge-wmi-probe-') as tmp:
    target = Path(tmp) / 'result.json'
    code = ('import os,sys,json,pathlib; '
            'pathlib.Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),'
            '"parent":os.getppid(),"user":os.environ.get("USERNAME"),'
            '"marker":os.environ.get("BRIDGE_WMI_PROBE"),"exe":sys.executable}))')
    service = win32com.client.GetObject('winmgmts:{impersonationLevel=impersonate}!\\\\.\\root\\cimv2')
    startup = service.Get('Win32_ProcessStartup').SpawnInstance_()
    startup.EnvironmentVariables = [f'{k}={v}' for k, v in os.environ.items()
                                    if k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'USERPROFILE',
                                             'USERNAME', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')]
    startup.EnvironmentVariables = list(startup.EnvironmentVariables) + ['BRIDGE_WMI_PROBE=OK']
    argv = [sys.executable, '-c', code, str(target)]
    process_class = service.Get('Win32_Process')
    params = process_class.Methods_('Create').InParameters.SpawnInstance_()
    params.CommandLine = subprocess.list2cmdline(argv)
    params.CurrentDirectory = tmp
    params.ProcessStartupInformation = startup
    result = service.ExecMethod('Win32_Process', 'Create', params)
    print('WMI_CREATE', result.ReturnValue, result.ProcessId, flush=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not target.exists():
        time.sleep(.1)
    assert target.exists(), 'WMI worker did not create result'
    observed = json.loads(target.read_text())
    assert observed['marker'] == 'OK', observed
    print(json.dumps(observed), flush=True)
