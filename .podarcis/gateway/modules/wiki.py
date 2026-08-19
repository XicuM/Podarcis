'''Wiki and auditing module binding for Podarcis MCP Gateway.'''
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = ['wiki_search', 'wiki_get', 'wiki_multi_get', 'wiki_update_index', 'complete_source_synthesis', 'lint_check_links', 'sync_workspaces']

def register(mcp, root: Path) -> None:
    '''Register wiki tools and resources with the FastMCP instance.'''
    wiki_dir = root / '.agents' / 'mcp' / 'wiki'
    if str(wiki_dir) not in sys.path:
        sys.path.insert(0, str(wiki_dir))

    import server as wiki_server

    mcp.add_tool(wiki_server.wiki_search)
    mcp.add_tool(wiki_server.wiki_get)
    mcp.add_tool(wiki_server.wiki_multi_get)
    mcp.add_tool(wiki_server.wiki_update_index)
    mcp.add_tool(wiki_server.complete_source_synthesis)
    mcp.add_tool(wiki_server.lint_check_links)
    mcp.add_tool(wiki_server.sync_workspaces)
    mcp.add_resource(wiki_server.resource_wiki_index)
    mcp.add_resource(wiki_server.resource_protocols_index)
    mcp.add_resource(wiki_server.resource_sources_index)

def unregister(mcp) -> None:
    '''Unregister wiki tools from the FastMCP instance.'''
    for tname in TOOLS:
        try:
            mcp.remove_tool(tname)
        except Exception:
            pass
