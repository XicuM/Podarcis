'''QMD Search Engine setup helper for research-mcp.'''

import shutil
from pathlib import Path

def setup_qmd(root: Path) -> bool:
    '''Interactive setup for QMD Vector DB Search Engine in .podarcis/config.yaml.'''
    import questionary
    from common import load_yaml, set_engine_status
    from console import console

    style = questionary.Style([
        ('qmark', 'fg:#e5c07b bold'),
        ('question', 'bold white'),
        ('answer', 'fg:#29b8db bold'),
        ('pointer', 'fg:#29b8db bold'),
        ('highlighted', 'noinherit fg:white'),
        ('selected', 'noinherit fg:white'),
    ])

    pod_cfg = load_yaml(root/'.podarcis'/'config.yaml')
    qmd_enabled = bool(pod_cfg.get('engines', {}).get('qmd', False))
    qmd_bin = shutil.which('qmd')

    bin_str = f' ({qmd_bin})' if qmd_bin else ''
    console.print('[dim]──────────────────────────────────────────────────────────────────────────[/dim]')
    console.print(f'Binary {"available" if qmd_bin else "missing"}{bin_str}\n')

    choice = questionary.select(
        f'Enable QMD Vector DB Engine? (Currently: {"Enabled" if qmd_enabled else "Disabled"})',
        choices=['yes', 'no'],
        default='yes' if qmd_enabled else 'no',
        qmark='research-mcp /',
        style=style,
    ).ask()

    if choice is None:
        console.print('\n[yellow]Cancelled by user. Exiting.[/yellow]')
        raise SystemExit(1)

    enable = (choice == 'yes')
    set_engine_status(root, 'qmd', enable)
    console.print(
        f'[bold green]✓ QMD set to: {"Enabled" if enable else "Disabled"} in .podarcis/config.yaml[/bold green]\n'
    )
    return enable


def setup_research_credentials(root: Path) -> bool:
    '''Prompt for Semantic Scholar API key. Returns False if skipped.'''
    import questionary
    from rich.panel import Panel
    from common import load_yaml, save_yaml
    from console import console

    style = questionary.Style([
        ('qmark', 'fg:#e5c07b bold'),
        ('question', 'bold white'),
        ('answer', 'fg:#29b8db bold'),
        ('pointer', 'fg:#29b8db bold'),
        ('highlighted', 'noinherit fg:white'),
        ('selected', 'noinherit fg:white'),
    ])

    console.print(Panel(
        'Configures Semantic Scholar API access for peer-reviewed paper search, '
        'citation graph traversal, and automated literature ingestion. '
        'An API key increases rate limits from 100 to 5,000 requests per 5 minutes.\n\n'
        'Request an API key at: [link=https://www.semanticscholar.org/product/api#api-key-form]https://www.semanticscholar.org/product/api[/link]',
        title='[bold cyan]research-mcp[/bold cyan] [bold white]configuration[/bold white]',
        border_style='#29b8db',
        width=76,
        expand=False,
    ))

    key = questionary.text(
        'Semantic Scholar API key (leave empty to skip):',
        style=style,
    ).ask()
    if key is None:
        console.print('\n[yellow]Cancelled by user. Exiting.[/yellow]')
        raise SystemExit(1)
    if not key.strip():
        console.print('[yellow]⚠️ No API key provided.[/yellow]\n')
        return False

    yaml_path = root / '.podarcis' / 'config.yaml'
    data = load_yaml(yaml_path) if yaml_path.exists() else {}
    data.setdefault('apis', {})['semantic_scholar_api_key'] = key.strip()
    save_yaml(yaml_path, data)
    console.print('[green]✓ API key saved.[/green]\n')
    return True
