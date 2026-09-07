"""Podarcis modular jobs engine, scheduled through systemd user timers."""

import datetime
import importlib
import os
import subprocess
import sys
from pathlib import Path

# Ensure root_dir / .podarcis is in sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
podarcis_dir = root_dir / '.podarcis'
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from common import load_yaml, save_yaml
from console import console

from . import scheduler


JOBS_DIR = lambda root: root / '.agents' / 'jobs'


def discover_jobs(root_dir: Path) -> dict[str, dict]:
    '''Discover all job specifications under .agents/jobs/*.yaml and merge state.yaml runtime info.'''
    jobs: dict[str, dict] = {}
    jdir = JOBS_DIR(root_dir)
    if not jdir.exists():
        return jobs

    st = load_yaml(root_dir / '.podarcis' / 'state.yaml')
    st_jobs = st.get('jobs', {})

    for file_path in sorted(jdir.glob('*.yaml')):
        data = load_yaml(file_path)
        if not data or not isinstance(data, dict):
            continue
        job_name = data.get('name', file_path.stem)
        default_enabled = data.get('enabled', True)
        default_schedule = data.get('schedule', 'daily')

        job_st = st_jobs.get(job_name, {})
        enabled = job_st.get('enabled', default_enabled)
        schedule = job_st.get('schedule', default_schedule)
        last_run = job_st.get('last_run', '')
        last_status = job_st.get('last_status', '')

        jobs[job_name] = {
            'name': job_name,
            'description': data.get('description', 'Podarcis job'),
            'schedule': schedule,
            'enabled': enabled,
            'type': data.get('type', 'python'),
            'handler': data.get('handler', job_name),
            'command': data.get('command', ''),
            'options': data.get('options', {}),
            'last_run': last_run,
            'last_status': last_status,
            'file_path': file_path,
        }

    return jobs


def set_job_status(root_dir: Path, job_name: str, enabled: bool) -> tuple[bool, str]:
    """Enable or disable a job, installing or removing its systemd timer."""
    jobs = discover_jobs(root_dir)
    if job_name not in jobs:
        return False, f'Job "{job_name}" not found.'

    job = jobs[job_name]
    opts = job.get('options') or {}
    if enabled:
        # A timer that fires into an unrunnable job is worse than no timer, so
        # say so now rather than leaving it to a silent 03:00 failure.
        if job['type'] == 'agent' and not opts.get('harness'):
            from .runners import available_harnesses
            installed = ', '.join(available_harnesses()) or 'none installed'
            console.print(
                f'[yellow]Warning:[/yellow] agent job "{job_name}" declares no '
                f'options.harness in {job["file_path"].name} and will fail when '
                f'the timer fires. Installed harnesses: {installed}.'
            )
        timeout_s = int(opts.get('timeout_s', 1800)) + 300
        ok, msg = scheduler.install(
            root_dir, job_name, job['schedule'], timeout_s=timeout_s,
        )
    else:
        ok, msg = scheduler.remove(root_dir, job_name)

    if not ok:
        return False, msg

    st_path = root_dir / '.podarcis' / 'state.yaml'
    st = load_yaml(st_path)
    st.setdefault('jobs', {}).setdefault(job_name, {})['enabled'] = enabled
    save_yaml(st_path, st)
    return True, msg


def run_job(root_dir: Path, job_name: str, dry_run: bool = False) -> dict:
    '''Execute a job by name and record execution metadata in state.yaml.'''
    jobs = discover_jobs(root_dir)
    if job_name not in jobs:
        console.print(f'[bold red]Error:[/bold red] Job "{job_name}" not found. Available jobs: {", ".join(jobs.keys())}')
        return {'status': 'error', 'message': f'Job "{job_name}" not found'}

    job = jobs[job_name]
    # Without this, an agent job whose prompt mentions running a job would
    # fork harness processes recursively.
    if job['type'] == 'agent' and os.environ.get('PODARCIS_JOB_RUN'):
        return {'status': 'error', 'message': 'Refusing to nest agent jobs.'}

    console.print(f'[bold #29b8db]Running Job:[/bold #29b8db] [white]{job_name}[/white] ({job["description"]})')

    run_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    res = {'status': 'success'}
    if job['type'] == 'agent':
        from . import agent
        try:
            res = agent.run(root_dir, job, dry_run=dry_run)
        except Exception as e:
            console.print(f'[bold red]Error running job {job_name}: {e}[/bold red]')
            res = {'status': 'error', 'message': str(e)}

    elif job['type'] == 'python':
        handler_name = job['handler']
        try:
            mod = importlib.import_module(f'jobs.{handler_name}')
            if hasattr(mod, 'run'):
                res = mod.run(root_dir, dry_run=dry_run)
            else:
                console.print(f'[bold red]Error:[/bold red] Module jobs.{handler_name} has no run() function.')
                res = {'status': 'error', 'message': 'Missing run() handler'}
        except Exception as e:
            console.print(f'[bold red]Error running job {job_name}: {e}[/bold red]')
            res = {'status': 'error', 'message': str(e)}

    elif job['type'] == 'shell':
        cmd = job.get('command', '')
        if not cmd:
            console.print(f'[bold red]Error:[/bold red] Job {job_name} specifies shell type but no command.')
            res = {'status': 'error', 'message': 'No command specified'}
        else:
            console.print(f'[dim]Executing: {cmd}[/dim]')
            if not dry_run:
                p = subprocess.run(cmd, shell=True, cwd=root_dir)
                if p.returncode != 0:
                    res = {'status': 'error', 'message': f'Command exited with code {p.returncode}'}
            else:
                console.print('[yellow][DRY-RUN] Shell command skipped.[/yellow]')
                res = {'status': 'dry_run'}

    if not dry_run:
        st_path = root_dir / '.podarcis' / 'state.yaml'
        st = load_yaml(st_path)
        jobs_st = st.setdefault('jobs', {})
        job_st = jobs_st.setdefault(job_name, {})
        job_st['last_run'] = run_ts
        job_st['last_status'] = res.get('status', 'unknown')
        save_yaml(st_path, st)

    return res
