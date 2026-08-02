import pytest
yf = pytest.importorskip("yfinance")

def test_yfinance_download():
    tickers = ["AAPL", "MSFT"]
    data = yf.download(tickers, period="1y")
    assert not data.empty
    # Verify we got pricing data back, checking for typical columns/indices
    assert len(data.columns) > 0
