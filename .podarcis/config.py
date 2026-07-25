#!/usr/bin/env python3
'''Interactive configuration tool entrypoint for Agentic Wiki Builder.'''

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
podarcis_dir = Path(__file__).resolve().parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
if str(podarcis_dir) not in sys.path:
    sys.path.insert(0, str(podarcis_dir))

from interactive import interactive_config


def main() -> None:
    '''Launch interactive configuration menu.'''
    interactive_config(root)


if __name__ == '__main__':
    main()
