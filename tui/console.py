'''Console output handling with Rich integration and fallback.'''

import re

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


class FallbackConsole:
    '''Minimal text console when rich is not installed.'''

    def print(self, *args, **kwargs) -> None:
        text = ' '.join(str(x) for x in args)
        clean_text = re.sub(r'\[/?[a-zA-Z0-9_\s#=/-]+\]', '', text)
        print(clean_text)

    def rule(self, title: str = '', *args, **kwargs) -> None:
        print('\n' + '=' * 60)
        if title:
            print(f'  {title}\n' + '=' * 60)
        print()

    def clear(self) -> None:
        import os
        os.system('cls' if os.name == 'nt' else 'clear')


console = Console() if HAS_RICH else FallbackConsole()
