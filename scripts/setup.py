#!/usr/bin/env python3
"""
Automated bootstrap, Git configuration, dependency setup, and Google Drive integration for Agentic Wiki Builder.
"""

import subprocess
import sys
import shutil
import os
from pathlib import Path

# Embedded default Google Drive Client Configuration for OAuth 2.0
DEFAULT_CLIENT_CONFIG = {
    "installed": {
        "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
        "project_id": "your-project-id",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "YOUR_CLIENT_SECRET",
        "redirect_uris": ["http://localhost"]
    }
}

# Try to import rich; if not available, define a fallback console
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

class FallbackConsole:
    def print(self, *args, **kwargs):
        import re
        text = " ".join(str(x) for x in args)
        # Strip simple rich formatting tags
        text = re.sub(r'\[/?[a-zA-Z0-9_\s#=/-]+\]', '', text)
        print(text, **kwargs)
        
    def rule(self, title="", *args, **kwargs):
        print("\n" + "=" * 60)
        if title:
            print(f"  {title}")
            print("=" * 60 + "\n")

# Initialize console
if HAS_RICH:
    console = Console()
else:
    console = FallbackConsole()


def run_command(cmd, check=True, cwd=None):
    console.print(f"[dim]Running command: {' '.join(cmd)}[/dim]")
    try:
        subprocess.run(cmd, check=check, cwd=cwd)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Error running command: {e}[/bold red]")
        if check:
            sys.exit(1)


def setup_google_drive(root):
    if not HAS_RICH:
        console.print("[yellow]⚠️ Cannot start rich Google Drive OAuth helper. Installing dependencies first...[/yellow]")
        return

    gdrive_dir = root / ".agents" / "mcp" / "gdrive"
    service_account_path = gdrive_dir / "service_account.json"
    token_path = gdrive_dir / "token.json"

    console.print("\n")
    console.print(Panel.fit(
        "[bold cyan]Google Drive MCP Server Credentials Setup[/bold cyan]",
        border_style="cyan"
    ))
    console.print("Configure read-only Google Drive access for the Synthesizer and Researcher agents.")
    console.print()

    # Check if already authenticated
    if token_path.exists():
        console.print("[bold green]✓ Existing Google Drive OAuth token.json detected![/bold green]")
        reauth = Confirm.ask("Would you like to re-authenticate or sign in with a different account?", default=False)
        if not reauth:
            console.print("[green]Keeping current credentials. Google Drive setup skipped.[/green]")
            return

    console.print("[bold]Select Authentication Method:[/bold]")
    console.print("  [bold cyan]1[/bold cyan] : [bold green]Automatic OAuth Flow[/bold green] (Easiest - opens browser, signs in automatically)")
    console.print("  [bold cyan]2[/bold cyan] : [bold yellow]Google Service Account[/bold yellow] (Recommended for non-interactive/headless use)")
    console.print("  [bold cyan]3[/bold cyan] : [bold red]Skip / Configure Later[/bold red]")
    console.print()

    choice = Prompt.ask("Choose option", choices=["1", "2", "3"], default="1")

    if choice == "3":
        console.print("[yellow]Google Drive setup skipped.[/yellow]")
        return

    if choice == "2":
        console.print(Panel(
            f"Please place your Service Account JSON file at:\n"
            f"  [bold]{service_account_path}[/bold]\n\n"
            f"[bold underline]Steps to acquire key:[/bold underline]\n"
            f"1. Open the [link=https://console.cloud.google.com/]Google Cloud Console[/link].\n"
            f"2. Enable [bold]Google Drive API[/bold].\n"
            f"3. Create a [bold]Service Account[/bold] and generate a [bold]JSON key[/bold].\n"
            f"4. Share your target Google Drive folders/files with the service account email address.",
            title="Service Account Setup Guide",
            border_style="yellow"
        ))
        if service_account_path.exists():
            console.print("[bold green]✓ service_account.json already exists! Setup complete.[/bold green]")
        else:
            console.print("[yellow]Status: Pending. Setup service_account.json when ready.[/yellow]")
    else:
        # Automatic OAuth Flow
        console.print(Panel(
            "We will launch an interactive Google OAuth browser window.\n"
            "Once authorized, the access tokens will be automatically saved locally.\n"
            "[bold green]No manual API key creation or GCP project setup is required![/bold green]",
            title="Interactive OAuth Flow",
            border_style="cyan"
        ))

        if not Confirm.ask("Ready to open browser and authenticate?"):
            console.print("[yellow]Authentication canceled.[/yellow]")
            return

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            scopes = ["https://www.googleapis.com/auth/drive.readonly"]

            console.print("[dim]Starting local web server to capture OAuth response...[/dim]")
            credentials_path = gdrive_dir / "credentials.json"
            if credentials_path.exists():
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            else:
                flow = InstalledAppFlow.from_client_config(DEFAULT_CLIENT_CONFIG, scopes)
            creds = flow.run_local_server(port=0)

            # Ensure directory exists
            gdrive_dir.mkdir(parents=True, exist_ok=True)
            with open(token_path, "w") as token:
                token.write(creds.to_json())

            console.print("[bold green]✓ Success! Token successfully generated and saved to token.json.[/bold green]")
            console.print("[bold green]✓ Google Drive MCP is fully configured and ready to run![/bold green]")
        except Exception as e:
            console.print(f"[bold red]Interactive OAuth flow failed: {e}[/bold red]")


def main():
    root = Path(__file__).resolve().parent
    venv_dir = root / ".venv"
    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
        venv_pip = venv_dir / "Scripts" / "pip.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
        venv_pip = venv_dir / "bin" / "pip"

    # Re-execute under virtual environment if available to get rich styling
    if sys.executable != str(venv_python) and venv_python.exists():
        try:
            subprocess.run([str(venv_python), "-c", "import rich"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.execv(str(venv_python), [str(venv_python)] + sys.argv)
        except subprocess.CalledProcessError:
            pass

    # Print Header
    if HAS_RICH:
        console.print(Panel(
            "[bold green]Agentic Wiki Builder[/bold green]\n"
            "[dim]Complete Bootstrap, Setup, and Google Drive Integration[/dim]",
            border_style="green",
            expand=False
        ))
    else:
        console.rule("Agentic Wiki Builder — Complete Setup & Bootstrap")

    # 1. Git LFS Initialization
    if not shutil.which("git-lfs"):
        console.print("[bold yellow]⚠️  WARNING: 'git-lfs' command not found.[/bold yellow]")
        console.print("   Please install Git LFS (https://git-lfs.com/) for tracking large raw sources.")
    else:
        console.print("[green]✓ Git LFS found. Registering settings...[/green]")
        run_command(["git", "lfs", "install"])
    console.print()

    # 2. Check and Setup QMD CLI (Quick Markdown Search)
    if not shutil.which("qmd"):
        console.print("[bold yellow]⚠️  WARNING: 'qmd' command (Quick Markdown Search CLI) not found.[/bold yellow]")
        console.print("   qmd is required for semantic and hybrid search in the wiki MCP server.")
        console.print("   To install it, run: [bold]npm install -g @tobilu/qmd[/bold]")
        
        installed = False
        if shutil.which("npm"):
            install_now = False
            if HAS_RICH:
                install_now = Confirm.ask("Would you like this script to attempt installing it globally via npm?", default=True)
            else:
                choice = input("Would you like this script to attempt installing it globally via npm? (y/n): ").strip().lower()
                install_now = (choice == "y")

            if install_now:
                console.print("[cyan]Installing @tobilu/qmd globally...[/cyan]")
                run_command(["npm", "install", "-g", "@tobilu/qmd"], check=False)
                if shutil.which("qmd"):
                    installed = True
            else:
                console.print("[yellow]Skipping automatic installation.[/yellow]")
        else:
            console.print("[bold red]⚠️ npm not found. Please install Node.js/npm and install qmd manually.[/bold red]")
            
        if not installed:
            console.print("[bold red]❌ ERROR: 'qmd' is a required dependency for the agentic tools to function.[/bold red]")
            console.print("   Please install it (e.g. 'npm install -g @tobilu/qmd') and run scripts/setup.py again.")
            sys.exit(1)
    else:
        console.print("[green]✓ QMD CLI found.[/green]")
    console.print()

    # 3. Submodule Initialization
    console.print("[green]✓ Initializing and updating git submodules...[/green]")
    run_command(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"], check=False)
    console.print()

    # 4. Configure Git settings
    console.print("[green]✓ Configuring local Git settings for team collaboration...[/green]")
    run_command(["git", "config", "submodule.recurse", "true"])
    run_command(["git", "config", "push.recurseSubmodules", "on-demand"])
    console.print()

    # 5. Virtual Environment Creation
    if not venv_dir.exists():
        console.print("[green]✓ Creating Python virtual environment (.venv)...[/green]")
        run_command([sys.executable, "-m", "venv", str(venv_dir)])
    else:
        console.print("[green]✓ Python virtual environment (.venv) already exists.[/green]")
    console.print()

    # 6. Installing Dependencies
    console.print("[green]✓ Installing and updating dependencies from requirements.txt...[/green]")
    if venv_pip.exists():
        run_command([str(venv_pip), "install", "--upgrade", "pip"])
        run_command([str(venv_pip), "install", "-r", "requirements.txt"])
    else:
        console.print("[bold yellow]⚠️ Could not find pip in virtual environment. Using fallback...[/bold yellow]")
        run_command([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    console.print()

    # 7. Environment Configuration (.env)
    env_file = root / ".env"
    example_env_file = root / ".example.env"
    if not env_file.exists():
        if example_env_file.exists():
            console.print("[green]✓ Creating .env file from .example.env...[/green]")
            shutil.copy(example_env_file, env_file)
            console.print("👉 Created .env file. Please update it with your API credentials if necessary.")
        else:
            console.print("[bold yellow]⚠️ Could not find .example.env to generate .env.[/bold yellow]")
    else:
        console.print("[green]✓ .env file already exists.[/green]")
    console.print()

    # Re-execute under virtual environment if we just installed rich to run the Google Drive setup with rich styling
    if sys.executable != str(venv_python) and venv_python.exists() and not HAS_RICH:
        console.print("[cyan]Re-launching setup inside virtual environment to finalize Google Drive configuration...[/cyan]")
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)

    # 8. Google Drive Credentials Setup
    try:
        setup_google_drive(root)
    except Exception as e:
        console.print(f"[bold yellow]⚠️ Google Drive credentials setup encountered an issue: {e}[/bold yellow]")
    console.print()

    console.print("\n")
    if HAS_RICH:
        console.print(Panel(
            "[bold green]✓ Bootstrap & Setup Complete![/bold green]\n\n"
            "- Submodules will now automatically pull updates during a standard 'git pull'.\n"
            "- Commits in submodules will automatically push when you run 'git push' from the parent repository.\n"
            "- Virtual environment is configured and dependencies are installed.\n"
            "- Environment (.env) has been prepared.\n"
            "- Google Drive MCP server is authenticated and ready to run.",
            border_style="green",
            expand=False
        ))
    else:
        console.rule("Setup Complete!")


if __name__ == "__main__":
    main()
