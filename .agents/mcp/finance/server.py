"""finance-mcp — FastMCP server for investment calculations and expense tracking.

Imports calc.py and expense-tracker scripts directly (no subprocess).
Set PROJECT_ROOT env var to the repository root, or it is auto-detected.
"""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP

# ── Path bootstrap ────────────────────────────────────────────────────────────

def _find_root() -> Path:
    env = os.environ.get("PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    # Auto-detect: walk up from this file looking for AGENTS.md
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "AGENTS.md").exists():
            return parent
    raise RuntimeError(
        "Cannot locate project root. Set the PROJECT_ROOT environment variable."
    )

ROOT = _find_root()
_FINANCE_DIR = Path(__file__).resolve().parent

# Add script dir to sys.path so we can import them directly
if str(_FINANCE_DIR) not in sys.path:
    sys.path.insert(0, str(_FINANCE_DIR))

import calc       # noqa: E402
import parse      # noqa: E402
import query      # noqa: E402

# ── Server ────────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "finance-mcp",
    instructions=(
        "Financial calculator and expense tracker for the agentic wiki. "
        "Use calc_* tools for investment math, stock_* for market data, "
        "and expense_* tools for personal finance queries and CSV ingestion."
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# Investment Toolset
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def calc_cagr(
    start: Annotated[float, "Starting value (must be > 0)"],
    end: Annotated[float, "Ending value"],
    years: Annotated[float, "Number of years (must be > 0)"],
) -> dict:
    """Compound Annual Growth Rate between two values over a number of years."""
    if start <= 0:
        raise ValueError("start must be > 0")
    if years <= 0:
        raise ValueError("years must be > 0")
    rate = calc.cagr(start, end, years)
    return {"cagr": round(rate, 6), "cagr_pct": f"{rate:.2%}"}


@mcp.tool()
def calc_fv(
    rate: Annotated[float, "Annual interest rate as a decimal (e.g. 0.07 for 7%)"],
    years: Annotated[float, "Investment horizon in years"],
    pmt: Annotated[float, "Annual payment / contribution"],
    pv: Annotated[float, "Present value / initial lump sum (default 0)"] = 0.0,
) -> dict:
    """Future Value of a lump sum plus periodic annual contributions."""
    fv = calc.future_value(rate, years, pmt, pv)
    return {"future_value": round(fv, 2)}


@mcp.tool()
def calc_dca(
    monthly: Annotated[float, "Monthly contribution amount"],
    rate: Annotated[float, "Expected annual return as a decimal (e.g. 0.07)"],
    years: Annotated[float, "Investment horizon in years"],
) -> dict:
    """Dollar-Cost Averaging projection: total contributed, projected value, gain."""
    return calc.dca_projection(monthly, rate, years)


@mcp.tool()
def calc_weights(
    holdings: Annotated[
        list[list],
        'Portfolio holdings as [[name, value], ...]. Example: [["VWCE", 5000], ["AAPL", 1200]]',
    ],
) -> dict:
    """Current portfolio weights and (if tickers are recognised) Max Sharpe optimal weights."""
    try:
        parsed = [(str(h[0]), float(h[1])) for h in holdings]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"holdings must be [[name, value], ...] pairs: {exc}") from exc
    return calc.portfolio_weights(parsed)


@mcp.tool()
def stock_price(
    ticker: Annotated[str, "Stock ticker symbol (e.g. AAPL, VWCE.DE)"],
) -> dict:
    """Current price, daily/monthly/6-month/annual change for a ticker via Yahoo Finance."""
    return calc.get_stock_data(ticker)


@mcp.tool()
def stock_news(
    ticker: Annotated[str, "Stock ticker symbol"],
    limit: Annotated[int, "Maximum number of headlines to return (default 5)"] = 5,
) -> dict:
    """Recent news headlines for a ticker, with FinBERT sentiment if available."""
    return calc.get_stock_news(ticker, limit)


@mcp.tool()
def stock_backtest(
    ticker: Annotated[str, "Stock ticker symbol"],
) -> dict:
    """SMA 20/50 crossover backtest over 2 years via vectorbt (requires vectorbt installed)."""
    return calc.backtest_strategy(ticker)


# ─────────────────────────────────────────────────────────────────────────────
# Expense Tracker
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def expense_parse(
    csv_path: Annotated[
        str,
        "Path to a Trade Republic CSV export. Relative paths are resolved from PROJECT_ROOT.",
    ],
) -> dict:
    """Ingest a bank CSV export into the canonical transactions.csv.

    Deduplicates by transaction_id, maps MCC codes to spend categories,
    and returns a summary of new rows, skipped duplicates, and category breakdown.
    """
    path = Path(csv_path)
    if not path.is_absolute():
        path = ROOT / path
    # Safety: must stay inside the project
    path.resolve().relative_to(ROOT)  # raises ValueError if outside
    return parse.run(str(path))


def _capture_query(fn, *args) -> str:
    """Run a query.py cmd_* function and capture its stdout as a string."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


@mcp.tool()
def expense_monthly(
    year_month: Annotated[str, "Month to query in YYYY-MM format (e.g. 2025-03)"],
) -> str:
    """Category breakdown and top merchants for a single month. Returns a markdown table."""
    return _capture_query(query.cmd_monthly, year_month)


@mcp.tool()
def expense_range(
    start: Annotated[str, "Start month in YYYY-MM format"],
    end: Annotated[str, "End month in YYYY-MM format"],
) -> str:
    """Category totals over a date range with average monthly spend. Returns a markdown table."""
    return _capture_query(query.cmd_range, start, end)


@mcp.tool()
def expense_category(
    name: Annotated[str, "Spend category name (e.g. 'Groceries & Supermarkets')"],
) -> str:
    """All transactions in a given spend category. Returns a markdown table."""
    return _capture_query(query.cmd_category, name)


@mcp.tool()
def expense_uncategorized() -> str:
    """List all transactions with spend_category = 'Uncategorized'. Returns a markdown table."""
    return _capture_query(query.cmd_uncategorized)


@mcp.tool()
def expense_summary() -> str:
    """All-time spending totals by category, descending. Returns a markdown table."""
    return _capture_query(query.cmd_summary)


@mcp.tool()
def expense_trading() -> str:
    """BUY/SELL/trading activity with ticker, quantity, price. Returns a markdown table."""
    return _capture_query(query.cmd_trading)


@mcp.tool()
def expense_transfers() -> str:
    """Peer-to-peer transfers and partner payments overview. Returns a markdown table."""
    return _capture_query(query.cmd_transfers)


@mcp.tool()
def expense_top(
    n: Annotated[int, "Number of top merchants to return (default 15)"] = 15,
) -> str:
    """Top N merchants by all-time absolute spending. Returns a markdown table."""
    return _capture_query(query.cmd_top, n)


@mcp.tool()
def expense_export_monthly(
    dir_path: Annotated[
        str,
        "Directory to write monthly .md summaries into. Relative to PROJECT_ROOT.",
    ],
) -> str:
    """Export one markdown summary file per month into the given directory."""
    path = Path(dir_path)
    if not path.is_absolute():
        path = ROOT / path
    path.resolve().relative_to(ROOT)  # safety check
    buf = io.StringIO()
    with redirect_stdout(buf):
        query.cmd_export_monthly(str(path))
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Resources
# ─────────────────────────────────────────────────────────────────────────────

@mcp.resource("finance://transactions")
def resource_transactions() -> str:
    """Live contents of the canonical transactions CSV."""
    p = ROOT / "workspace" / "finance" / "transactions.csv"
    if not p.exists():
        return "# No transactions found\n\ntransactions.csv does not exist yet."
    return p.read_text(encoding="utf-8")


@mcp.resource("finance://category-rules")
def resource_category_rules() -> str:
    """Live contents of category_rules.json (partner patterns and custom rules)."""
    p = ROOT / "workspace" / "finance" / "category_rules.json"
    if not p.exists():
        return json.dumps({}, indent=2)
    return p.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
