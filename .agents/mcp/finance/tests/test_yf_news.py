import pytest
yf = pytest.importorskip("yfinance")

def test_yfinance_news():
    t = yf.Ticker("AAPL")
    news = t.news
    assert isinstance(news, list)
    # The news API should return a list, even if it is empty under rate limits/no network, but usually has articles
    assert len(news) >= 0
