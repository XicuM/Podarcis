'''Google Drive OAuth and Service Account authentication helper.'''

import sys
from pathlib import Path

def setup_google_drive(root: Path) -> bool:
    '''Guide user through Google Drive OAuth or Service Account setup.
    Returns True on success, False if skipped or failed.'''

    import questionary
    from rich.panel import Panel
    from common import load_yaml, save_yaml
    from console import console

    DEFAULT_CLIENT_ID = 'YOUR_CLIENT_ID.apps.googleusercontent.com'
    DEFAULT_CLIENT_CONFIG = {
        'installed': {
            'client_id': DEFAULT_CLIENT_ID,
            'project_id': 'pavilion-496615',
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'auth_provider_x509_cert_url': 'https://www.googleapis.com/oauth2/v1/certs',
            'client_secret': 'YOUR_CLIENT_SECRET',
            'redirect_uris': ['http://localhost'],
        }
    }

    gdrive_dir = root/'.agents'/'mcp'/'gdrive'
    service_account_path = gdrive_dir/'service_account.json'
    token_path = gdrive_dir/'token.json'

    style = questionary.Style([
        ('qmark', 'fg:#e5c07b bold'),
        ('question', 'bold white'),
        ('answer', 'fg:#29b8db bold'),
        ('pointer', 'fg:#29b8db bold'),
        ('highlighted', 'noinherit fg:white'),
        ('selected', 'noinherit fg:white'),
    ])

    console.print(Panel(
        'Configures Google Drive access for reading team documents, spreadsheets,\n'
        'and pre-prints directly into Podarcis. Supports OAuth web authorization\n'
        'or headless Service Accounts.',
        title='[bold cyan]google-drive-mcp[/bold cyan] [bold white]configuration[/bold white]',
        border_style='#29b8db',
        width=76,
        expand=False,
    ))

    if token_path.exists():
        console.print('[bold green]✓ Existing OAuth token.json detected.[/bold green]')
        choice = questionary.select(
            'Re-authenticate?', choices=['no', 'yes'], default='no', style=style,
        ).ask()
        if choice == 'no':
            console.print('[green]Keeping current credentials.[/green]\n')
            return True

    choice = questionary.select(
        'Authentication method:',
        choices=[
            'Automatic OAuth Flow (opens browser)',
            'Google Service Account (headless)',
            'Skip',
        ],
        default='Automatic OAuth Flow (opens browser)',
        style=style,
    ).ask()

    if choice is None or choice == 'Skip':
        console.print('[yellow]Google Drive setup skipped.[/yellow]')
        return False

    if 'Service Account' in choice:
        console.print(Panel(
            f'Place your Service Account JSON at:\n'
            f'  [bold]{service_account_path}[/bold]\n\n'
            f'[bold underline]Steps:[/bold underline]\n'
            f'1. Open [link=https://console.cloud.google.com/]Google Cloud Console[/link].\n'
            f'2. Enable [bold]Google Drive API[/bold].\n'
            f'3. Create a [bold]Service Account[/bold] → generate JSON key.\n'
            f'4. Share target folders with the service account email.',
            title='Service Account Setup',
            border_style='yellow',
        ))
        status = (
            '✓ service_account.json found.' if service_account_path.exists()
            else 'Status: Pending. Place file when ready.')
        console.print(
            f'[bold green]{status}[/bold green]' if service_account_path.exists()
            else f'[yellow]{status}[/yellow]')
        return service_account_path.exists()

    console.print(Panel(
        'We will launch an interactive Google OAuth browser window.\n'
        'Once authorized, tokens are saved locally.\n'
        '[bold green]No API key creation or GCP project setup required.[/bold green]',
        title='OAuth Flow',
        border_style='#29b8db',
    ))

    if questionary.select(
        'Open browser to authenticate?',
        choices=['no', 'yes'], default='yes', qmark='google-drive-mcp /', style=style,
    ).ask() != 'yes':
        console.print('[yellow]Authentication canceled.[/yellow]')
        return False

    credentials_path = gdrive_dir / 'credentials.json'

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        console.print('[dim]Starting local web server for OAuth...[/dim]')
        scopes = ['https://www.googleapis.com/auth/drive.readonly']
        if credentials_path.exists():
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), scopes)
        else:
            flow = InstalledAppFlow.from_client_config(
                DEFAULT_CLIENT_CONFIG, scopes)
        creds = flow.run_local_server(port=0)

        gdrive_dir.mkdir(parents=True, exist_ok=True)
        token_data = creds.to_json()
        token_path.write_text(token_data, encoding='utf-8')

        yaml_path = root / '.podarcis' / 'config.yaml'
        data = load_yaml(yaml_path) if yaml_path.exists() else {}
        data.setdefault('gdrive', {})['token'] = token_data
        save_yaml(yaml_path, data)

        console.print('[bold green]✓ Token saved to .podarcis/config.yaml.[/bold green]')
        return True
    except Exception as e:
        console.print(f'[bold red]OAuth flow failed: {e}[/bold red]')
        return False
