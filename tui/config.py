#!/usr/bin/env python3
'''Interactive configuration tool entrypoint for Agentic Wiki Builder.'''

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from tui.interactive import interactive_config


def main() -> None:
    '''Launch interactive configuration menu.'''
    interactive_config(root)


if __name__ == '__main__':
    main()
