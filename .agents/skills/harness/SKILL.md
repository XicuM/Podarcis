---
name: harness
description: Runtime state, context compaction, and permission gating.
metadata: { "openclaw": { "emoji": "🛡️" } }
---

# Role: Agent Harness (Universal Utility)

Shared runtime infrastructure providing state management, automated context compaction, and execution permission gating.

## Scripts & Entrypoints
- `core_io_helper.py`: Automates context compaction on file reads/writes.
- `runtime_gate.py`: Enforces authorization check gates for high-risk operations.

## Usage & Guidelines
All skill scripts must import from the harness module to perform I/O operations and run commands or APIs. 

To import the harness, scripts must ensure the skills directory is in the Python path:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from harness.scripts.core_io_helper import read_file, write_file
from harness.scripts.runtime_gate import check_permission
```

1. Use `core_io_helper.py` for loading or writing literature text or log files.
2. Route any command execution or outbound HTTP request through `runtime_gate.py`.
3. Use the `tmp/` directory in the project root for temporal edits, scratchpad files, or temporary user data additions.
   - **Permission Gate**: The gate blocks unapproved network domains or shell commands. If a script fails with a `PermissionError`, ask the user for verbal approval in the chat.
   - **Bypass Flag**: If the user approves, re-run your script with the `HARNESS_BYPASS_GATE=1` environment variable to bypass the interactive prompt (e.g., `HARNESS_BYPASS_GATE=1 python your_script.py`).
