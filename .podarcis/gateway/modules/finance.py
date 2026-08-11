'''Finance and investment module binding for Podarcis MCP Gateway.'''
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = [
    'calc_cagr', 'calc_fv', 'calc_dca', 'calc_weights',
    'stock_price', 'stock_news', 'stock_backtest',
    'expense_parse', 'expense_monthly', 'expense_range',
    'expense_category', 'expense_uncategorized', 'expense_summary',
    'expense_trading', 'expense_transfers', 'expense_top',
    'expense_export_monthly',
]

def register(mcp, root: Path) -> None:
    '''Register finance tools and resources with the FastMCP instance.'''
    fin_dir = root / '.agents' / 'mcp' / 'finance'
    if str(fin_dir) not in sys.path:
        sys.path.insert(0, str(fin_dir))

    import server as finance_server

    for tname in TOOLS:
        if fn := getattr(finance_server, tname, None):
            mcp.add_tool(fn)

    mcp.add_resource(finance_server.resource_transactions)
    mcp.add_resource(finance_server.resource_category_rules)

def unregister(mcp) -> None:
    '''Unregister finance tools from the FastMCP instance.'''
    for tname in TOOLS:
        try:
            mcp.remove_tool(tname)
        except Exception:
            pass
