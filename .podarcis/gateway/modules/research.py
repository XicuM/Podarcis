'''Research and literature discovery module binding for Podarcis MCP Gateway.'''
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = ['search_literature', 'download_paper', 'queue_list', 'queue_enqueue', 'queue_dequeue']

def register(mcp, root: Path) -> None:
    '''Register research tools and resources with the FastMCP instance.'''
    res_dir = root / '.agents' / 'mcp' / 'research'
    if str(res_dir) not in sys.path:
        sys.path.insert(0, str(res_dir))

    import server as research_server

    mcp.add_tool(research_server.search_literature)
    mcp.add_tool(research_server.download_paper)
    mcp.add_tool(research_server.queue_list)
    mcp.add_tool(research_server.queue_enqueue)
    mcp.add_tool(research_server.queue_dequeue)

    mcp.add_resource(research_server.resource_state)
    mcp.add_resource(research_server.resource_sources_index)

def unregister(mcp) -> None:
    '''Unregister research tools from the FastMCP instance.'''
    for tname in TOOLS:
        try:
            mcp.remove_tool(tname)
        except Exception:
            pass
