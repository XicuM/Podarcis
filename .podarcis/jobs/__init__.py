'''Podarcis Modular Jobs Engine & Crontab Synchronization Manager.'''

import datetime
import importlib
import subprocess
import sys
from pathlib import Path

# Ensure root_dir / .podarcis is in sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
podarcis_dir = root_dir / '.podarcis'
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from common import load_yaml, save_yaml, load_json
from console import console


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
        default_schedule = data.get('schedule', '0 2 * * *')

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


def _get_crontab_line(root_dir: Path, job_name: str, schedule: str) -> str:
    '''Construct standard crontab command string for a job.'''
    py_bin = root_dir / '.venv' / 'bin' / 'python'
    cli_bin = root_dir / 'podarcis'
    cmd = f'{py_bin} {cli_bin} job run {job_name}' if py_bin.exists() else f'{cli_bin} job run {job_name}'
    log_file = root_dir / '.podarcis' / f'job_{job_name}.log'
    marker = f'# podarcis-job:{job_name}:{root_dir}'
    return f'{schedule} cd {root_dir} && {cmd} >> {log_file} 2>&1 {marker}'


def sync_all_jobs_crontab(root_dir: Path) -> tuple[bool, str]:
    '''Sync system crontab to match all enabled jobs in .agents/jobs and state.yaml.'''
    jobs = discover_jobs(root_dir)
    marker_prefix = f'# podarcis-job:'
    dir_marker = f':{root_dir}'

    try:
        proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        current = proc.stdout if proc.returncode == 0 else ''
    except Exception as e:
        return False, f'Failed to access crontab: {e}'

    # Remove all existing podarcis job lines for this root directory
    lines = [
        line for line in current.splitlines()
        if not (marker_prefix in line and dir_marker in line) and line.strip()
    ]

    # Add active lines for enabled jobs
    for name, info in jobs.items():
        if info.get('enabled', False):
            lines.append(_get_crontab_line(root_dir, name, info.get('schedule', '0 2 * * *')))

    new_crontab = '\n'.join(lines) + '\n' if lines else ''

    try:
        if new_crontab:
            proc = subprocess.run(['crontab', '-'], input=new_crontab, text=True, capture_output=True)
        else:
            proc = subprocess.run(['crontab', '-r'], capture_output=True, text=True)
        if proc.returncode == 0:
            return True, 'Crontab synchronized successfully.'
        else:
            return False, f'Crontab error: {proc.stderr.strip()}'
    except Exception as e:
        return False, str(e)


def set_job_status(root_dir: Path, job_name: str, enabled: bool) -> tuple[bool, str]:
    '''Enable or disable a job, updating state.yaml and syncing system crontab.'''
    st_path = root_dir / '.podarcis' / 'state.yaml'
    st = load_yaml(st_path)
    jobs_st = st.setdefault('jobs', {})
    job_st = jobs_st.setdefault(job_name, {})
    job_st['enabled'] = enabled
    save_yaml(st_path, st)

    ok, msg = sync_all_jobs_crontab(root_dir)
    status_str = 'enabled and installed in crontab' if enabled else 'disabled and removed from crontab'
    if ok:
        return True, f'Job "{job_name}" {status_str}.'
    return False, f'Job status updated in state.yaml, but crontab sync failed: {msg}'


def run_job(root_dir: Path, job_name: str, dry_run: bool = False) -> dict:
    '''Execute a job by name and record execution metadata in state.yaml.'''
    jobs = discover_jobs(root_dir)
    if job_name not in jobs:
        console.print(f'[bold red]Error:[/bold red] Job "{job_name}" not found. Available jobs: {", ".join(jobs.keys())}')
        return {'status': 'error', 'message': f'Job "{job_name}" not found'}

    job = jobs[job_name]
    console.print(f'[bold #29b8db]Running Job:[/bold #29b8db] [white]{job_name}[/white] ({job["description"]})')

    run_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    res = {'status': 'success'}
    if job['type'] == 'python':
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
