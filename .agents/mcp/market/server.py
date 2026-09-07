"""market-mcp — FastMCP server for market data.

Supplies facts the agent cannot otherwise obtain: live prices, historical
series, and issuer fundamentals. It deliberately ships no analytics. Canned
optimizers and backtests substitute false precision for reasoning — a
mean-variance solve over 37 estimated returns swings its weights wildly on
noise — so portfolio judgement stays with the agent, which can weigh these
facts against wiki/ knowledge and the constraints in workspace/profile.md.

Every tool is batched: a portfolio refresh is one call over N tickers, not N
calls. Set PROJECT_ROOT env var to the repository root.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

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
    "market-mcp",
    instructions=(
        "Batched market data (prices, history, fundamentals) via Yahoo Finance. "
        "Supplies facts only — portfolio analysis and advice remain the agent's work."
    ),
)

# Yahoo denominates FX and index tickers with suffixes (EURUSD=X, ^GSPC); they
# flow through unchanged, so currency conversion needs no separate tool.


def _split(tickers: list[str] | str) -> list[str]:
    """Accept a list or a comma/space-separated string; normalise to upper-case."""
    if isinstance(tickers, str):
        tickers = tickers.replace(",", " ").split()
    seen: dict[str, None] = {}
    for t in tickers:
        if cleaned := t.strip().upper():
            seen.setdefault(cleaned, None)
    return list(seen)


def _closes(frame: Any, ticker: str) -> Any:
    """Close-price series for one ticker out of a yf.download frame.

    yfinance has flipped single-ticker downloads between flat and per-ticker
    column indexes across releases, so read the shape rather than assume it.
    """
    col = frame[ticker]["Close"] if hasattr(frame.columns, "levels") else frame["Close"]
    return col.dropna()


def _pct(series: Any, back: int) -> float | None:
    """Percentage change over `back` observations, or None if the series is short."""
    if len(series) <= back:
        return None
    return round((float(series.iloc[-1]) / float(series.iloc[-1 - back]) - 1) * 100, 2)


@mcp.tool()
def market_quote(
    tickers: Annotated[
        list[str],
        "Ticker symbols, e.g. ['AAPL', 'VWCE.DE', 'EURUSD=X']. Batched in one request.",
    ],
) -> dict:
    """Latest close and daily/1m/6m/1y percentage changes for each ticker, with its trading currency."""
    import yfinance as yf

    symbols = _split(tickers)
    if not symbols:
        return {"error": "No tickers given."}

    frame = yf.download(
        symbols, period="1y", interval="1d",
        auto_adjust=True, progress=False, group_by="ticker",
    )
    if frame.empty:
        return {"error": f"No data returned for {symbols}."}

    quotes, failed = {}, []
    for sym in symbols:
        try:
            closes = _closes(frame, sym)
            if closes.empty:
                failed.append(sym)
                continue
            currency = ""
            try:
                currency = yf.Ticker(sym).fast_info.get("currency", "") or ""
            except Exception:
                pass
            quotes[sym] = {
                "price": round(float(closes.iloc[-1]), 4),
                "currency": currency,
                "as_of": str(closes.index[-1].date()),
                "change_1d_pct": _pct(closes, 1),
                "change_1m_pct": _pct(closes, 21),
                "change_6m_pct": _pct(closes, 126),
                "change_1y_pct": _pct(closes, len(closes) - 1),
            }
        except (KeyError, IndexError, ValueError):
            failed.append(sym)

    result: dict[str, Any] = {"quotes": quotes}
    if failed:
        result["failed"] = failed
    return result


@mcp.tool()
def market_history(
    tickers: Annotated[list[str], "Ticker symbols to fetch aligned close prices for."],
    period: Annotated[str, "Look-back window: 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, max."] = "1y",
    interval: Annotated[
        str,
        "Sampling interval: 1d, 1wk, 1mo. Weekly keeps multi-year responses small; "
        "daily over many tickers returns a large payload.",
    ] = "1wk",
) -> dict:
    """Aligned close-price series for correlation, volatility, and drawdown analysis. Returns shared dates plus one array per ticker."""
    import yfinance as yf

    symbols = _split(tickers)
    if not symbols:
        return {"error": "No tickers given."}

    frame = yf.download(
        symbols, period=period, interval=interval,
        auto_adjust=True, progress=False, group_by="ticker",
    )
    if frame.empty:
        return {"error": f"No data returned for {symbols} over {period}."}

    closes, failed = {}, []
    for sym in symbols:
        try:
            series = _closes(frame, sym).reindex(frame.index)
            closes[sym] = [None if v != v else round(float(v), 4) for v in series]
        except (KeyError, IndexError, ValueError):
            failed.append(sym)

    result: dict[str, Any] = {
        "period": period,
        "interval": interval,
        "dates": [str(d.date()) for d in frame.index],
        "close": closes,
    }
    if failed:
        result["failed"] = failed
    return result


@mcp.tool()
def market_fundamentals(
    tickers: Annotated[list[str], "Ticker symbols to describe."],
) -> dict:
    """Sector, industry, country, market cap, trailing/forward P/E, dividend yield, and beta per ticker — the issuer facts needed to reason about concentration and valuation."""
    import yfinance as yf

    symbols = _split(tickers)
    if not symbols:
        return {"error": "No tickers given."}

    fields = {
        "name": "longName", "sector": "sector", "industry": "industry",
        "country": "country", "currency": "currency", "market_cap": "marketCap",
        "trailing_pe": "trailingPE", "forward_pe": "forwardPE",
        "dividend_yield": "dividendYield", "beta": "beta",
        "instrument_type": "quoteType",
    }

    profiles, failed = {}, []
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info or {}
            if not info.get("quoteType"):
                failed.append(sym)
                continue
            profiles[sym] = {k: info.get(src) for k, src in fields.items()}
        except Exception:
            failed.append(sym)

    result: dict[str, Any] = {"profiles": profiles}
    if failed:
        result["failed"] = failed
    return result


if __name__ == "__main__":
    mcp.run()
