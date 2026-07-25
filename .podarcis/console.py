'''Console output handling with Rich integration.'''

import subprocess, sys
from pathlib import Path

def _ensure_package(pkg: str) -> None:
    try:
        __import__(pkg)
    except ImportError:
        print(f'{pkg} is required but missing. Installing {pkg}...')
        cmd = [sys.executable, '-m', 'pip', 'install', pkg]
        subprocess.run(cmd, check=True)

_ensure_package('rich')
_ensure_package('questionary')

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

console = Console()

QSTYLE = [
    ('qmark', 'fg:#e5c07b bold'),
    ('question', 'bold white'),
    ('answer', 'fg:#29b8db bold'),
    ('pointer', 'fg:#29b8db bold'),
    ('highlighted', 'noinherit fg:white'),
    ('selected', 'noinherit fg:white'),
    ('separator', 'fg:yellow bold'),
    ('instruction', 'fg:#888888 italic'),
    ('choice-title', 'fg:white'),
    ('checkbox-checked', 'fg:#29b8db bold'),
    ('checkbox-unchecked', 'fg:#888888'),
    ('checkbox-selected', 'fg:#29b8db bold'),
]
