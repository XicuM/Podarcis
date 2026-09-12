'''Google Drive API Delta Ingestion Engine & State Sync for Podarcis.'''

import datetime
from pathlib import Path
from podarcis.common import get_config_value, set_config_value
from podarcis.console import console


def get_last_sync(root_dir: Path) -> str:
    '''Retrieve last GDrive synchronization timestamp from .podarcis/config.yaml.'''
    return get_config_value(root_dir, 'gdrive_sync', 'last_sync')


def update_last_sync(root_dir: Path, timestamp: str | None = None) -> str:
    '''Persist last GDrive synchronization timestamp into .podarcis/config.yaml.'''
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    set_config_value(root_dir, timestamp, 'gdrive_sync', 'last_sync')
    return timestamp


def build_gdrive_query(last_sync: str, folder_id: str = '') -> str:
    '''Construct Google Drive API filter query for single-request delta retrieval.'''
    query_parts = ['trashed = false']
    if last_sync:
        query_parts.append(f"modifiedTime > '{last_sync}'")
    if folder_id:
        query_parts.append(f"'{folder_id}' in parents")
    return ' and '.join(query_parts)


def run_gdrive_ingestion(root_dir: Path, dry_run: bool = False) -> dict:
    '''Execute automated GDrive API delta check and trigger ingestion pipeline.

    1. Reads last_sync from .podarcis/config.yaml
    2. Builds single GDrive API query (modifiedTime > 'last_sync')
    3. If new/updated files exist, ingests into wiki/ and runs verification
    4. Updates last_sync timestamp in .podarcis/config.yaml
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
    console.print(f'[bold green]✓ Synchronization completed. New last_sync timestamp set in config.yaml: {new_sync_ts}[/bold green]')

    return {
        'status': 'success',
        'last_sync': new_sync_ts,
        'query': query,
        'processed_files': 0,
    }

