'''TUI package initialization for Agentic Wiki Builder.'''

import sys
from pathlib import Path

# Ensure project root is accessible in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
