'''Google Drive OAuth and Service Account authentication helper.'''

from pathlib import Path
from tui.console import HAS_RICH, console

if HAS_RICH:
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt

DEFAULT_CLIENT_CONFIG = {
    'installed': {
        'client_id': 'YOUR_CLIENT_ID.apps.googleusercontent.com',
        'project_id': 'your-project-id',
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token',
        'auth_provider_x509_cert_url': 'https://www.googleapis.com/oauth2/v1/certs',
        'client_secret': 'YOUR_CLIENT_SECRET',
        'redirect_uris': ['http://localhost']
    }
}


def setup_google_drive(root: Path) -> None:
    '''Guide user through Google Drive OAuth or Service Account setup.'''
    if not HAS_RICH:
        console.print('[yellow]⚠️ Cannot start rich Google Drive OAuth helper. Installing dependencies first...[/yellow]')
        return

    gdrive_dir = root / '.agents' / 'mcp' / 'gdrive'
    service_account_path = gdrive_dir / 'service_account.json'
    token_path = gdrive_dir / 'token.json'

    console.print('\n')
    console.print(Panel.fit(
        '[bold #29b8db]Google Drive MCP Server Credentials Setup[/bold #29b8db]',
        border_style='#29b8db'
    ))
    console.print('Configure read-only Google Drive access for the Synthesizer and Researcher agents.\n')

    if token_path.exists():
        console.print('[bold green]✓ Existing Google Drive OAuth token.json detected![/bold green]')
        if not Confirm.ask('Would you like to re-authenticate or sign in with a different account?', default=False):
            console.print('[green]Keeping current credentials. Google Drive setup skipped.[/green]')
            return

    console.print('[bold]Select Authentication Method:[/bold]')
    console.print('  [bold #29b8db]1[/bold #29b8db] : [bold green]Automatic OAuth Flow[/bold green] (Easiest - opens browser, signs in automatically)')
    console.print('  [bold #29b8db]2[/bold #29b8db] : [bold yellow]Google Service Account[/bold yellow] (Recommended for non-interactive/headless use)')
    console.print('  [bold #29b8db]3[/bold #29b8db] : [bold red]Skip / Configure Later[/bold red]\n')

    choice = Prompt.ask('Choose option', choices=['1', '2', '3'], default='1')

    if choice == '3':
        console.print('[yellow]Google Drive setup skipped.[/yellow]')
        return

    if choice == '2':
        console.print(Panel(
            f'Please place your Service Account JSON file at:\n'
            f'  [bold]{service_account_path}[/bold]\n\n'
            f'[bold underline]Steps to acquire key:[/bold underline]\n'
            f'1. Open the [link=https://console.cloud.google.com/]Google Cloud Console[/link].\n'
            f'2. Enable [bold]Google Drive API[/bold].\n'
            f'3. Create a [bold]Service Account[/bold] and generate a [bold]JSON key[/bold].\n'
            f'4. Share your target Google Drive folders/files with the service account email address.',
            title='Service Account Setup Guide',
            border_style='yellow'
        ))
        status = '✓ service_account.json already exists! Setup complete.' if service_account_path.exists() else 'Status: Pending. Setup service_account.json when ready.'
        console.print(f'[bold green]{status}[/bold green]' if service_account_path.exists() else f'[yellow]{status}[/yellow]')
    else:
        console.print(Panel(
            'We will launch an interactive Google OAuth browser window.\n'
            'Once authorized, the access tokens will be automatically saved locally.\n'
            '[bold green]No manual API key creation or GCP project setup is required![/bold green]',
            title='Interactive OAuth Flow',
            border_style='#29b8db'
        ))

        if not Confirm.ask('Ready to open browser and authenticate?'):
            console.print('[yellow]Authentication canceled.[/yellow]')
            return

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            scopes = ['https://www.googleapis.com/auth/drive.readonly']

            console.print('[dim]Starting local web server to capture OAuth response...[/dim]')
            credentials_path = gdrive_dir / 'credentials.json'
            flow = (InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
                    if credentials_path.exists() else
                    InstalledAppFlow.from_client_config(DEFAULT_CLIENT_CONFIG, scopes))
            creds = flow.run_local_server(port=0)

            gdrive_dir.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding='utf-8')

            console.print('[bold green]✓ Success! Token successfully generated and saved to token.json.[/bold green]')
            console.print('[bold green]✓ Google Drive MCP is fully configured and ready to run![/bold green]')
        except Exception as e:
            console.print(f'[bold red]Interactive OAuth flow failed: {e}[/bold red]')
