"""Unit tests for market-mcp server tools.

Network calls are stubbed: these assert the shaping of yfinance output, not
Yahoo's availability.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

SERVER_DIR = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("market_mcp_server", SERVER_DIR / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def _frame(symbols, rows=300):
    """A group_by='ticker' style frame, as yf.download returns for many tickers."""
    idx = pd.date_range("2025-01-01", periods=rows, freq="D")
    cols = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = {(s, "Close"): [100.0 + i for i in range(rows)] for s in symbols}
    return pd.DataFrame(data, index=idx, columns=cols)


@pytest.fixture
def fake_yf(monkeypatch):
    """Install a stub yfinance module and hand the test its call recorder."""
    mod = types.ModuleType("yfinance")
    calls = {}

    def download(symbols, **kw):
        calls["symbols"] = symbols
        calls["kw"] = kw
        return calls.get("frame", _frame(symbols))

    class Ticker:
        def __init__(self, sym):
            self.sym = sym
            self.fast_info = {"currency": "EUR"}
            self.info = {"quoteType": "EQUITY", "sector": "Technology", "longName": f"{sym} Inc"}

    mod.download = download
    mod.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return calls


def test_split_normalises_and_dedupes():
    assert server._split("aapl, msft aapl") == ["AAPL", "MSFT"]
    assert server._split([" vwce.de ", "AAPL"]) == ["VWCE.DE", "AAPL"]
    assert server._split("") == []


def test_market_quote_batches_all_tickers_in_one_download(fake_yf):
    res = server.market_quote(["AAPL", "MSFT", "VWCE.DE"])
    assert fake_yf["symbols"] == ["AAPL", "MSFT", "VWCE.DE"]  # one call, not three
    assert set(res["quotes"]) == {"AAPL", "MSFT", "VWCE.DE"}
    q = res["quotes"]["AAPL"]
    assert q["currency"] == "EUR"
    assert q["change_1d_pct"] is not None and q["change_1y_pct"] is not None
    assert "failed" not in res


def test_market_quote_reports_unknown_ticker_without_failing_the_batch(fake_yf):
    fake_yf["frame"] = _frame(["AAPL"])
    res = server.market_quote(["AAPL", "NOSUCH"])
    assert "AAPL" in res["quotes"]
    assert res["failed"] == ["NOSUCH"]


def test_short_series_yields_none_not_an_exception(fake_yf):
    fake_yf["frame"] = _frame(["AAPL"], rows=5)
    q = server.market_quote(["AAPL"])["quotes"]["AAPL"]
    assert q["change_1d_pct"] is not None
    assert q["change_6m_pct"] is None  # 5 rows cannot span six months


def test_market_history_returns_aligned_dates_and_defaults_to_weekly(fake_yf):
    res = server.market_history(["AAPL", "MSFT"])
    assert fake_yf["kw"]["interval"] == "1wk"
    assert len(res["dates"]) == len(res["close"]["AAPL"]) == len(res["close"]["MSFT"])


def test_market_fundamentals_shapes_requested_fields(fake_yf):
    prof = server.market_fundamentals(["AAPL"])["profiles"]["AAPL"]
    assert prof["sector"] == "Technology"
    assert prof["name"] == "AAPL Inc"
    assert "beta" in prof  # absent upstream keys are present as None


def test_no_tickers_is_an_error_not_a_crash(fake_yf):
    assert "error" in server.market_quote([])
    assert "error" in server.market_history([])
    assert "error" in server.market_fundamentals([])
