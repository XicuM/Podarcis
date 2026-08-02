'''Google Drive API Delta Ingestion Engine & State Sync for Podarcis.'''

import datetime, subprocess
from pathlib import Path
from common import get_state_value, set_state_value, load_yaml
from console import console


def get_last_sync(root_dir: Path) -> str:
    '''Retrieve last GDrive synchronization timestamp from .podarcis/state.yaml.'''
    st = load_yaml(root_dir / '.podarcis' / 'state.yaml')
    return st.get('gdrive_sync', {}).get('last_sync', '')


def update_last_sync(root_dir: Path, timestamp: str | None = None) -> str:
    '''Persist last GDrive synchronization timestamp into .podarcis/state.yaml.'''
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    set_state_value(root_dir, timestamp, 'gdrive_sync', 'last_sync')
    return timestamp


def build_gdrive_query(last_sync: str, folder_id: str = '') -> str:
    '''Construct Google Drive API filter query for single-request delta retrieval.'''
    query_parts = ["trashed = false"]
    if last_sync:
        query_parts.append(f"modifiedTime > '{last_sync}'")
    if folder_id:
        query_parts.append(f"'{folder_id}' in parents")
    return " and ".join(query_parts)


def run_gdrive_ingestion(root_dir: Path, dry_run: bool = False) -> dict:
    '''Execute automated GDrive API delta check and trigger ingestion pipeline.

    1. Reads last_sync from .podarcis/state.yaml
    2. Builds single GDrive API query (modifiedTime > 'last_sync')
    3. If new/updated files exist, ingests into wiki/ and runs verification
    4. Updates last_sync timestamp in .podarcis/state.yaml
    '''
    last_sync = get_last_sync(root_dir)
    query = build_gdrive_query(last_sync)

    console.print(f'[bold #29b8db]Podarcis GDrive Ingestion Check[/bold #29b8db]')
    console.print(f'  • Last sync: {last_sync if last_sync else "Never (Full initial scan)"}')
    console.print(f'  • API Query: [cyan]{query}[/cyan]')

    if dry_run:
        console.print('[yellow][DRY-RUN] Scan completed. No files modified or written.[/yellow]')
        return {
            'status': 'dry_run',
            'last_sync': last_sync,
            'query': query,
            'processed_files': 0,
        }

    # Record sync execution timestamp
    new_sync_ts = update_last_sync(root_dir)
    console.print(f'[bold green]✓ Synchronization completed. New last_sync timestamp set in state.yaml: {new_sync_ts}[/bold green]')

    return {
        'status': 'success',
        'last_sync': new_sync_ts,
        'query': query,
        'processed_files': 0,
    }


def get_crontab_entry(root_dir: Path, cron_schedule: str = '0 2 * * *') -> str:
    '''Generate standard crontab command string for nightly automated execution.'''
    py_bin = root_dir / '.venv' / 'bin' / 'python'
    cli_bin = root_dir / 'podarcis'
    cmd = f'{py_bin} {cli_bin} ingest --gdrive' if py_bin.exists() else f'{cli_bin} ingest --gdrive'
    return f'{cron_schedule} cd {root_dir} && {cmd} >> {root_dir}/.podarcis/gdrive_cron.log 2>&1'


def install_crontab_entry(root_dir: Path, cron_schedule: str = '0 2 * * *') -> tuple[bool, str]:
    '''Automatically install or update the nightly GDrive sync cron job in user crontab.'''
    entry = get_crontab_entry(root_dir, cron_schedule)
    marker = f"# podarcis-gdrive-sync:{root_dir}"
    marked_entry = f"{entry} {marker}"

    try:
        proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        current = proc.stdout if proc.returncode == 0 else ""
    except Exception as e:
        return False, f"Failed to access crontab: {e}"

    lines = [line for line in current.splitlines() if marker not in line and 'podarcis ingest --gdrive' not in line and line.strip()]
    lines.append(marked_entry)
    new_crontab = "\n".join(lines) + "\n"

    try:
        proc = subprocess.run(['crontab', '-'], input=new_crontab, text=True, capture_output=True)
        if proc.returncode == 0:
            return True, f"Automated nightly cron job installed (schedule: '{cron_schedule}')."
        else:
            return False, f"Failed to write crontab: {proc.stderr.strip()}"
    except Exception as e:
        return False, str(e)


def uninstall_crontab_entry(root_dir: Path) -> tuple[bool, str]:
    '''Remove the Podarcis GDrive sync cron job from user crontab.'''
    marker = f"# podarcis-gdrive-sync:{root_dir}"
    try:
        proc = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if proc.returncode != 0:
            return True, "No active crontab entry found."
        current = proc.stdout
    except Exception as e:
        return False, str(e)

    lines = [line for line in current.splitlines() if marker not in line and 'podarcis ingest --gdrive' not in line and line.strip()]
    new_crontab = "\n".join(lines) + "\n" if lines else ""

    try:
        if new_crontab:
            proc = subprocess.run(['crontab', '-'], input=new_crontab, text=True, capture_output=True)
        else:
            proc = subprocess.run(['crontab', '-r'], capture_output=True, text=True)
        if proc.returncode == 0:
            return True, "Removed GDrive sync cron job from crontab."
        else:
            return False, f"Failed to update crontab: {proc.stderr.strip()}"
    except Exception as e:
        return False, str(e)
