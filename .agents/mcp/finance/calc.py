#!/usr/bin/env python3
"""
Investment Calculator — CLI tool for the Agentic Life OS.

Provides financial math functions (CAGR, Future Value, DCA projection,
portfolio weight analysis) with structured JSON output for agentic consumption.
"""
import argparse
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET

# ── Calculation Functions ────────────────────────────────────────────────────

def cagr(start_val: float, end_val: float, years: float) -> float:
    """Compound Annual Growth Rate."""
    if start_val <= 0:
        print("Error: --start must be > 0.", file=sys.stderr)
        sys.exit(1)
    if years <= 0:
        print("Error: --years must be > 0.", file=sys.stderr)
        sys.exit(1)
    return (end_val / start_val) ** (1 / years) - 1


def future_value(rate: float, years: float, pmt: float, pv: float = 0) -> float:
    """
    Future Value of a lump sum + periodic contributions.
    rate:  annual interest rate (decimal, e.g. 0.07)
    years: number of years
    pmt:   payment per year (end-of-period)
    pv:    present value / initial investment
    """
    if rate == 0:
        return pv + (pmt * years)
    return pv * (1 + rate) ** years + pmt * (((1 + rate) ** years - 1) / rate)


def dca_projection(monthly: float, rate: float, years: float) -> dict:
    """
    Dollar-Cost Averaging projection.
    monthly: monthly contribution
    rate:    expected annual return (decimal)
    years:   investment horizon
    Returns total_contributed, projected_value, and total_gain.
    """
    months = int(years * 12)
    monthly_rate = (1 + rate) ** (1 / 12) - 1
    if monthly_rate == 0:
        fv = monthly * months
    else:
        fv = monthly * (((1 + monthly_rate) ** months - 1) / monthly_rate)
    total_contributed = monthly * months
    return {
        "total_contributed": round(total_contributed, 2),
        "projected_value": round(fv, 2),
        "total_gain": round(fv - total_contributed, 2),
        "gain_pct": round((fv / total_contributed - 1) * 100, 2) if total_contributed else 0,
    }


def portfolio_weights(holdings: list[tuple[str, float]]) -> dict:
    """
    Compute current weights and attempt to compute optimal Max Sharpe weights.
    """
    total = sum(v for _, v in holdings)
    if total == 0:
        print("Error: total portfolio value is 0.", file=sys.stderr)
        sys.exit(1)
        
    current_weights = [
        {"name": name, "value": round(val, 2), "weight_pct": round(val / total * 100, 2)}
        for name, val in holdings
    ]
    result = {"current_holdings": current_weights}
    
    tickers = [name for name, _ in holdings]
    if len(tickers) > 1:
        try:
            import yfinance as yf
            from pypfopt.expected_returns import mean_historical_return
            from pypfopt.risk_models import CovarianceShrinkage
            from pypfopt.efficient_frontier import EfficientFrontier
            
            data = yf.download(tickers, period="2y", progress=False)['Close']
            data = data.dropna()
            
            if not data.empty:
                mu = mean_historical_return(data)
                S = CovarianceShrinkage(data).ledoit_wolf()
                ef = EfficientFrontier(mu, S)
                raw_weights = ef.max_sharpe()
                cleaned_weights = ef.clean_weights()
                
                result["optimal_weights_pct"] = {k: round(v * 100, 2) for k, v in cleaned_weights.items()}
                perf = ef.portfolio_performance()
                result["optimal_performance"] = {
                    "expected_return_pct": round(perf[0] * 100, 2),
                    "volatility_pct": round(perf[1] * 100, 2),
                    "sharpe_ratio": round(perf[2], 2)
                }
        except Exception as e:
            print(f"PyPortfolioOpt optimization failed: {e}", file=sys.stderr)
            
    return result


def get_stock_data(ticker: str) -> dict:
    """Fetch current stock price and trends using Yahoo Finance API."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="1y")
        if not hist.empty:
            current = float(hist['Close'].iloc[-1])
            previous = float(hist['Close'].iloc[-2]) if len(hist) > 1 else current
            days_1m = min(len(hist)-1, 21)
            days_6m = min(len(hist)-1, 126)
            days_1y = len(hist)-1
            
            return {
                "ticker": ticker,
                "currency": t.info.get("currency", ""),
                "current_price": round(current, 2),
                "daily_change_pct": round((current / previous - 1) * 100, 2) if previous else 0.0,
                "1m_change_pct": round((current / float(hist['Close'].iloc[-days_1m - 1]) - 1) * 100, 2) if days_1m > 0 else 0.0,
                "6m_change_pct": round((current / float(hist['Close'].iloc[-days_6m - 1]) - 1) * 100, 2) if days_6m > 0 else 0.0,
                "1y_change_pct": round((current / float(hist['Close'].iloc[0]) - 1) * 100, 2) if days_1y > 0 else 0.0,
                "exchange": t.info.get("exchange", ""),
                "instrument_type": t.info.get("quoteType", "")
            }
    except Exception as e:
        print(f"yfinance failed for {ticker}: {e}. Falling back to urllib.", file=sys.stderr)

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            result = data.get("chart", {}).get("result", [])
            if not result:
                return {"error": f"No data found for {ticker}"}
            
            meta = result[0].get("meta", {})
            indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
            close_prices = [p for p in indicators.get("close", []) if p is not None]
            
            if not close_prices:
                return {"error": f"No price data available for {ticker}"}
            
            current = close_prices[-1]
            previous = close_prices[-2] if len(close_prices) > 1 else current
            
            days_1m = min(len(close_prices)-1, 21)
            days_6m = min(len(close_prices)-1, 126)
            days_1y = len(close_prices)-1
            
            return {
                "ticker": ticker,
                "currency": meta.get("currency", ""),
                "current_price": round(current, 2),
                "daily_change_pct": round((current / previous - 1) * 100, 2) if previous else 0.0,
                "1m_change_pct": round((current / close_prices[-days_1m - 1] - 1) * 100, 2) if days_1m > 0 else 0.0,
                "6m_change_pct": round((current / close_prices[-days_6m - 1] - 1) * 100, 2) if days_6m > 0 else 0.0,
                "1y_change_pct": round((current / close_prices[0] - 1) * 100, 2) if days_1y > 0 else 0.0,
                "exchange": meta.get("exchangeName", ""),
                "instrument_type": meta.get("instrumentType", "")
            }
    except Exception as e:
        return {"error": f"Failed to fetch data for {ticker}: {str(e)}"}


def get_stock_news(ticker: str, limit: int = 5) -> dict:
    """Fetch recent news headlines and run FinBERT sentiment analysis."""
    news = []
    
    # Primary: yfinance
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        yf_news = t.news
        for item in yf_news[:limit]:
            content = item.get("content", {})
            title = content.get("title", "")
            link = content.get("clickThroughUrl", {}).get("url", "")
            if not link:
                link = content.get("canonicalUrl", {}).get("url", "")
            if title:
                news.append({"title": title, "link": link})
    except Exception as e:
        print(f"yfinance news failed for {ticker}: {e}. Falling back to RSS.", file=sys.stderr)
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
                root = ET.fromstring(xml_data)
                for item in root.findall('.//item')[:limit]:
                    title = item.findtext('title')
                    if title:
                        news.append({"title": title, "link": item.findtext('link')})
        except Exception as rss_e:
            return {"error": f"Failed to fetch news for {ticker}: {str(rss_e)}"}

    if not news:
        return {"ticker": ticker, "news": []}
        
    try:
        from transformers import pipeline
        import warnings
        warnings.filterwarnings("ignore")
        sentiment_pipeline = pipeline("sentiment-analysis", model="ProsusAI/finbert")
        headlines = [n["title"] for n in news]
        results = sentiment_pipeline(headlines)
        
        score_map = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}
        total_score = 0
        for i, n in enumerate(news):
            label = results[i]['label']
            n["sentiment"] = label
            n["sentiment_confidence"] = round(results[i]['score'], 2)
            total_score += score_map.get(label, 0.0)
            
        return {
            "ticker": ticker, 
            "news": news,
            "average_sentiment_score": round(total_score / len(news), 2)
        }
    except Exception as e:
        print(f"FinBERT sentiment analysis failed: {e}", file=sys.stderr)

    return {"ticker": ticker, "news": news}

def backtest_strategy(ticker: str) -> dict:
    """Run a basic SMA crossover backtest using vectorbt."""
    try:
        import yfinance as yf
        import vectorbt as vbt
        import warnings
        warnings.filterwarnings("ignore")
        
        data = yf.Ticker(ticker).history(period="2y")
        if data.empty:
            return {"error": f"No data found for {ticker}"}
            
        close = data['Close']
        fast_ma = vbt.MA.run(close, 20)
        slow_ma = vbt.MA.run(close, 50)
        
        entries = fast_ma.ma_crossed_above(slow_ma)
        exits = fast_ma.ma_crossed_below(slow_ma)
        
        portfolio = vbt.Portfolio.from_signals(close, entries, exits, init_cash=10000)
        
        return {
            "ticker": ticker,
            "strategy": "SMA_20_50_Crossover",
            "total_return_pct": round(portfolio.total_return() * 100, 2),
            "win_rate_pct": round(portfolio.trades.win_rate() * 100, 2),
            "max_drawdown_pct": round(portfolio.max_drawdown() * 100, 2),
            "total_trades": len(portfolio.trades)
        }
    except Exception as e:
        return {"error": f"Backtest failed: {e}"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calc.py",
        description="Investment Calculator for the Agentic Life OS.",
    )
    sub = parser.add_subparsers(dest="command", help="Available calculations")

    # cagr
    p_cagr = sub.add_parser("cagr", help="Compound Annual Growth Rate")
    p_cagr.add_argument("--start", type=float, required=True, help="Starting value (> 0)")
    p_cagr.add_argument("--end", type=float, required=True, help="Ending value")
    p_cagr.add_argument("--years", type=float, required=True, help="Number of years (> 0)")

    # fv
    p_fv = sub.add_parser("fv", help="Future Value of lump sum + periodic contributions")
    p_fv.add_argument("--rate", type=float, required=True, help="Annual rate (decimal, e.g. 0.07)")
    p_fv.add_argument("--years", type=float, required=True, help="Number of years")
    p_fv.add_argument("--pmt", type=float, required=True, help="Annual payment/contribution")
    p_fv.add_argument("--pv", type=float, default=0, help="Present value (default: 0)")

    # dca
    p_dca = sub.add_parser("dca", help="Dollar-Cost Averaging projection")
    p_dca.add_argument("--monthly", type=float, required=True, help="Monthly contribution")
    p_dca.add_argument("--rate", type=float, required=True, help="Expected annual return (decimal)")
    p_dca.add_argument("--years", type=float, required=True, help="Investment horizon in years")

    # weights
    p_wt = sub.add_parser("weights", help="Portfolio weight analysis")
    p_wt.add_argument(
        "--holdings", type=str, required=True,
        help='JSON array of [name, value] pairs. Example: \'[["VWCE", 5000], ["AAPL", 1200]]\'',
    )

    # stock
    p_stock = sub.add_parser("stock", help="Fetch stock data and trends")
    p_stock.add_argument("--ticker", type=str, required=True, help="Stock ticker symbol (e.g. AAPL, BRK-B)")

    # news
    p_news = sub.add_parser("news", help="Fetch recent stock news headlines")
    p_news.add_argument("--ticker", type=str, required=True, help="Stock ticker symbol")

    # backtest
    p_bt = sub.add_parser("backtest", help="Run algorithmic backtest (SMA Crossover)")
    p_bt.add_argument("--ticker", type=str, required=True, help="Stock ticker symbol")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "cagr":
        result = {"cagr": round(cagr(args.start, args.end, args.years), 6)}
        result["cagr_pct"] = f"{result['cagr']:.2%}"

    elif args.command == "fv":
        fv = future_value(args.rate, args.years, args.pmt, args.pv)
        result = {"future_value": round(fv, 2)}

    elif args.command == "dca":
        result = dca_projection(args.monthly, args.rate, args.years)

    elif args.command == "weights":
        try:
            raw = json.loads(args.holdings)
            holdings = [(str(h[0]), float(h[1])) for h in raw]
        except (json.JSONDecodeError, IndexError, TypeError) as e:
            print(f"Error: invalid --holdings JSON. {e}", file=sys.stderr)
            sys.exit(1)
        result = portfolio_weights(holdings)

    elif args.command == "stock":
        result = get_stock_data(args.ticker)

    elif args.command == "news":
        result = get_stock_news(args.ticker)

    elif args.command == "backtest":
        result = backtest_strategy(args.ticker)

    else:
        parser.print_help()
        sys.exit(0)

    print(json.dumps(result, separators=(',', ':')))


if __name__ == "__main__":
    main()
