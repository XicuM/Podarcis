"""systemd user-timer backend for Podarcis jobs.

Replaces the previous crontab backend. Timers gain three properties cron
could not offer: ``Persistent=true`` re-runs a job missed while the machine
was suspended, ``RuntimeMaxSec`` bounds a hung agent run, and journald keeps
per-run history instead of a single overwritten status field.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

UNIT_DIR = Path.home() / '.config' / 'systemd' / 'user'

SERVICE_TEMPLATE = """\
[Unit]
Description=Podarcis job {job} ({root})
Documentation=file://{root}/.agents/jobs/{job}.yaml

[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart={exec_start} job run {job}
RuntimeMaxSec={timeout_s}
"""

TIMER_TEMPLATE = """\
[Unit]
Description=Schedule for Podarcis job {job} ({root})

[Timer]
OnCalendar={schedule}
Persistent=true
RandomizedDelaySec=300
AccuracySec=1min

[Install]
WantedBy=timers.target
"""


def unit_prefix(root_dir: Path) -> str:
    """Checkout-specific unit prefix, so several clones can coexist."""
    digest = hashlib.sha1(str(root_dir).encode()).hexdigest()[:6]
    slug = re.sub(r'[^a-z0-9]+', '-', root_dir.name.lower()).strip('-')
    return f'podarcis-{slug}-{digest}-'


def unit_name(root_dir: Path, job_name: str) -> str:
    """Unit basename for one job of this checkout."""
    return f'{unit_prefix(root_dir)}{job_name}'


def job_names(root_dir: Path) -> list[str]:
    """Jobs of this checkout that currently have a timer installed."""
    prefix = unit_prefix(root_dir)
    return sorted({
        u.stem[len(prefix):] for u in installed_units(root_dir)
        if u.suffix == '.timer'
    })


def validate_schedule(schedule: str) -> tuple[bool, str]:
    """Check an OnCalendar expression via systemd-analyze."""
    proc = subprocess.run(
        ['systemd-analyze', 'calendar', schedule],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip() or f'Invalid OnCalendar: {schedule!r}'
    return True, proc.stdout.strip()


def next_elapse(schedule: str) -> str:
    """Human-readable next run for a schedule, or '' if it cannot be read."""
    ok, out = validate_schedule(schedule)
    if not ok:
        return ''
    for line in out.splitlines():
        if line.strip().startswith('Next elapse:'):
            return line.split(':', 1)[1].strip()
    return ''


def _systemctl(*args: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ['systemctl', '--user', *args], capture_output=True, text=True,
    )
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def installed_units(root_dir: Path) -> list[Path]:
    """Every unit file this checkout has installed."""
    if not UNIT_DIR.is_dir():
        return []
    return sorted(UNIT_DIR.glob(f'{unit_prefix(root_dir)}*.*'))


def install(
    root_dir: Path, job_name: str, schedule: str, timeout_s: int = 3600,
) -> tuple[bool, str]:
    """Write and enable the service/timer pair for a job."""
    ok, detail = validate_schedule(schedule)
    if not ok:
        return False, detail

    exec_start = root_dir / '.venv' / 'bin' / 'podarcis'
    if not exec_start.exists():
        return False, f'Missing {exec_start}; run `podarcis install` first.'

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    base = unit_name(root_dir, job_name)
    fields = {
        'job': job_name, 'root': root_dir, 'schedule': schedule,
        'timeout_s': timeout_s, 'exec_start': exec_start,
    }
    (UNIT_DIR / f'{base}.service').write_text(
        SERVICE_TEMPLATE.format(**fields), encoding='utf-8',
    )
    (UNIT_DIR / f'{base}.timer').write_text(
        TIMER_TEMPLATE.format(**fields), encoding='utf-8',
    )

    ok, msg = _systemctl('daemon-reload')
    if not ok:
        return False, f'daemon-reload failed: {msg}'
    ok, msg = _systemctl('enable', '--now', f'{base}.timer')
    if not ok:
        return False, f'Failed to enable {base}.timer: {msg}'
    return True, f'{base}.timer enabled ({schedule}).'


def remove(root_dir: Path, job_name: str) -> tuple[bool, str]:
    """Disable and delete the service/timer pair for a job."""
    base = unit_name(root_dir, job_name)
    timer = UNIT_DIR / f'{base}.timer'
    service = UNIT_DIR / f'{base}.service'
    if not timer.exists() and not service.exists():
        return True, f'No timer installed for "{job_name}".'

    _systemctl('disable', '--now', f'{base}.timer')
    timer.unlink(missing_ok=True)
    service.unlink(missing_ok=True)
    _systemctl('daemon-reload')
    return True, f'{base}.timer removed.'
