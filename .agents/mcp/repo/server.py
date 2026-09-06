"""repo-mcp — FastMCP server for workspace repository synchronization.

Split out of wiki-mcp: syncing git remotes and Google Drive deltas has nothing
to do with the wiki, and a domain server that also moves repositories around is
a domain server nobody can reason about. Equivalent to `podarcis repo sync`.
Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP


def _find_root() -> Path:
    env = os.environ.get("PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists():
            return parent
    raise RuntimeError(
        "Cannot locate project root. Set the PROJECT_ROOT environment variable."
    )


ROOT = _find_root()

mcp = FastMCP(
    "repo-mcp",
    instructions=(
        "Synchronises the decoupled Podarcis repositories (wiki, workspace, sources) "
        "with their git remotes and Google Drive backends."
    ),
)


@mcp.tool()
async def repo_sync(
    action: Annotated[
        str,
        "Action to perform: 'pull' (sync/pull remotes and gdrive), 'push' (push local commits to remotes), or 'all'.",
    ] = "pull",
    auto_commit: Annotated[
        bool,
        "Automatically commit uncommitted local changes before pushing (only applicable for action='push' or 'all').",
    ] = False,
    message: Annotated[
        str,
        "Commit message for auto_commit.",
    ] = "chore: sync workspace changes",
) -> str:
    """Synchronize all configured workspace repositories (clones missing repos, pulls remote origins, ingests Google Drive deltas, and updates backend configs)."""
    podarcis_dir = ROOT / ".podarcis"
    if str(podarcis_dir) not in sys.path:
        sys.path.insert(0, str(podarcis_dir))
    from repos import sync_repos_full, push_repos, get_repo_status
    from components import sync_all_backends

    results = {}
    if action in ("pull", "all"):
        sync_all_backends(ROOT)
        results["pull"] = sync_repos_full(ROOT)
    if action in ("push", "all"):
        results["push"] = push_repos(ROOT, auto_commit=auto_commit, message=message)

    results["status"] = get_repo_status(ROOT)
    return json.dumps(results, indent=2)


if __name__ == "__main__":
    mcp.run()
