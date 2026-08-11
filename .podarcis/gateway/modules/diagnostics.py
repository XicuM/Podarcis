'''Diagnostics module binding for Podarcis MCP Gateway.'''
from __future__ import annotations

import sys
from pathlib import Path

def register(mcp, root: Path) -> None:
    '''Register diagnostics tools with the FastMCP instance.'''
    diag_dir = root / '.agents' / 'mcp' / 'diagnostics'
    if str(diag_dir) not in sys.path:
        sys.path.insert(0, str(diag_dir))

    import server as diag_server

    mcp.add_tool(diag_server.log_pain_point)
    mcp.add_tool(diag_server.get_pain_points)
    mcp.add_tool(diag_server.clear_pain_points)

def unregister(mcp) -> None:
    '''Unregister diagnostics tools from the FastMCP instance.'''
    for tname in ('log_pain_point', 'get_pain_points', 'clear_pain_points'):
        try:
            mcp.remove_tool(tname)
        except Exception:
            pass
