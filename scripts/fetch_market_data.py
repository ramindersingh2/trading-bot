#!/usr/bin/env python3
"""
Fetches live market data for the trading bot prompt.
Sources:
  - SPY quote, VIX         → Tradier API (already have access)
  - Fear & Greed Index     → CNN / alternative.me free API
  - Treasury yields        → FRED / US Treasury (free, no key)
  - RSI, Moving Averages   → calculated from Tradier historical data
"""

import json
import math
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tradier_client import get_quote, _get

# ── Fear & Greed ─────────────────────────────────────────────────────────────
def fetch_fear_greed():
    """
    Fetches CNN Fear & Greed index via alternative.me free API.
    Returns dict: { score: int, label: str, timestamp: str }
    """
    try:
        url = "https://api.alternative.me/fng/?limit=1&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "trading-bot/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        entry = data["data"][0]
        return {
            "score": int(entry["value"]),
            "label": entry["value_classification"],  # e.g. "Greed", "Fear"
            "timestamp": entry["timestamp"]
        }
    except Exception as e:
        print(f"  [WARN] Fear & Greed fetch failed: {e}")
        return {"score": 50, "label": "Neutral", "timestamp": "unavailable"}

# ── Treasury Yields via FRED (fast, reliable, free) ──────────────────────────
def fetch_treasury_yields():
    """
    Fetches US treasury yields from FRED (St. Louis Fed) CSV endpoint.
    Much faster and more reliable than treasury.gov XML.
    Returns dict: { yield_2y, yield_10y, yield_30y }
    """
    def _fred_csv(series_id):
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "trading-bot/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        lines = resp.read().decode().strip().splitlines()
        for line in reversed(lines):
            parts = line.strip().split(",")
            if len(parts) == 2 and parts[1] not in (".", ""):
                try:
                    return float(parts[1])
                except ValueError:
                    continue
        return None

    try:
        y2  = _fred_csv("DGS2")
        y10 = _fred_csv("DGS10")
        y30 = _fred_csv("DGS30")
        return {"yield_2y": y2, "yield_10y": y10, "yield_30y": y30}
    except Exception as e:
        print(f"  [WARN] Treasury yields fetch failed: {e}")
        return {"yield_2y": None, "yield_10y": None, "yield_30y": None}

# ── Historical Prices from Tradier ────────────────────────────────────────────
def fetch_historical_prices(symbol="SPY", days=60):
    """
    Fetches daily OHLCV data from Tradier for RSI + MA calculations.
    Returns list of dicts: [{ date, open, high, low, close, volume }, ...]
    """
    end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days+10)).strftime("%Y-%m-%d")

    status, data = _get("/markets/history", {
        "symbol":   symbol,
        "interval": "daily",
        "start":    start,
        "end":      end,
    })
    if status != 200:
        print(f"  [WARN] Historical prices fetch failed: HTTP {status}")
        return []

    history = data.get("history", {})
    if not history or history == "null":
        return []

    days_data = history.get("day", [])
    if not isinstance(days_data, list):
        days_data = [days_data]

    return sorted(days_data, key=lambda d: d.get("date", ""))

# ── RSI Calculator ────────────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    """Calculates RSI from a list of closing prices."""
    if len(prices) < period + 1:
        return None

    closes = [float(p["close"]) for p in prices]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]

    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)

# ── Moving Averages ───────────────────────────────────────────────────────────
def calc_ma(prices, period):
    """Simple moving average of closing prices."""
    if len(prices) < period:
        return None
    closes = [float(p["close"]) for p in prices[-period:]]
    return round(sum(closes) / period, 2)

# ── Support & Resistance (simple swing high/low) ──────────────────────────────
def calc_support_resistance(prices, lookback=20):
    """
    Simple support/resistance: lowest low and highest high over lookback days.
    """
    if len(prices) < lookback:
        return None, None
    recent = prices[-lookback:]
    support    = round(min(float(p["low"])  for p in recent), 2)
    resistance = round(max(float(p["high"]) for p in recent), 2)
    return support, resistance

# ── VIX ───────────────────────────────────────────────────────────────────────
def fetch_vix():
    """Fetches VIX level from Tradier quote."""
    try:
        status, data = _get("/markets/quotes", {"symbols": "VIX"})
        if status == 200:
            q = data.get("quotes", {}).get("quote", {})
            return {
                "level": q.get("last", 0),
                "change": q.get("change_percentage", 0),
            }
    except Exception as e:
        print(f"  [WARN] VIX fetch failed: {e}")
    return {"level": None, "change": None}

# ── Master Fetch ──────────────────────────────────────────────────────────────
def fetch_all_market_data(symbol="SPY"):
    """
    Fetches all market data needed for the prompt.
    Returns a single clean dict that gets saved to market_snapshots.jsonl
    and passed into the prompt.
    """
    print("  Fetching SPY quote...")
    spy = get_quote(symbol)

    print("  Fetching VIX...")
    vix = fetch_vix()

    print("  Fetching Fear & Greed index...")
    fg  = fetch_fear_greed()

    print("  Fetching treasury yields...")
    yields = fetch_treasury_yields()

    print("  Fetching historical prices for RSI/MA...")
    history = fetch_historical_prices(symbol, days=210)  # 200MA needs ~200 days

    rsi_14  = calc_rsi(history, 14)
    ma_20   = calc_ma(history, 20)
    ma_50   = calc_ma(history, 50)
    ma_200  = calc_ma(history, 200)
    support, resistance = calc_support_resistance(history, 20)

    # 52-week stats
    year_prices = history[-252:] if len(history) >= 252 else history
    week52_high = round(max(float(p["high"]) for p in year_prices), 2) if year_prices else None
    week52_low  = round(min(float(p["low"])  for p in year_prices), 2) if year_prices else None

    current_price = float(spy.get("last", 0))
    pct_from_high = round((current_price - week52_high) / week52_high * 100, 2) if week52_high else None
    pct_from_low  = round((current_price - week52_low)  / week52_low  * 100, 2) if week52_low  else None

    snapshot = {
        "timestamp":       datetime.utcnow().isoformat() + "Z",
        "symbol":          symbol,

        # Price
        "price":           current_price,
        "open":            spy.get("open"),
        "high":            spy.get("high"),
        "low":             spy.get("low"),
        "volume":          spy.get("volume"),
        "change_pct":      spy.get("change_percentage"),
        "prev_close":      spy.get("prevclose"),

        # Volatility
        "vix":             vix.get("level"),
        "vix_change_pct":  vix.get("change"),

        # Sentiment
        "fear_greed_score": fg.get("score"),
        "fear_greed_label": fg.get("label"),

        # Rates
        "treasury_2y":     yields.get("yield_2y"),
        "treasury_10y":    yields.get("yield_10y"),
        "treasury_30y":    yields.get("yield_30y"),

        # Technicals
        "rsi_14":          rsi_14,
        "ma_20":           ma_20,
        "ma_50":           ma_50,
        "ma_200":          ma_200,

        # Context vs MAs
        "price_vs_ma20":   round(current_price - ma_20,  2) if ma_20  else None,
        "price_vs_ma50":   round(current_price - ma_50,  2) if ma_50  else None,
        "price_vs_ma200":  round(current_price - ma_200, 2) if ma_200 else None,

        # Support / Resistance
        "support_20d":     support,
        "resistance_20d":  resistance,

        # 52-week range
        "week52_high":     week52_high,
        "week52_low":      week52_low,
        "pct_from_52h":    pct_from_high,
        "pct_from_52l":    pct_from_low,
    }
    return snapshot

# ── Save Snapshot ─────────────────────────────────────────────────────────────
def save_snapshot(snapshot):
    path = Path.home() / "trading-bot" / "state" / "market_snapshots.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(snapshot) + "\n")
    print(f"  Snapshot saved → {path}")

# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Fetching all market data...\n")
    data = fetch_all_market_data()

    print()
    print("=== MARKET SNAPSHOT ===")
    print(f"  SPY:           ${data['price']}  ({data['change_pct']:+.2f}%)")
    print(f"  VIX:           {data['vix']}  ({data['vix_change_pct']}%)")
    print(f"  Fear & Greed:  {data['fear_greed_score']} — {data['fear_greed_label']}")
    print(f"  10Y Yield:     {data['treasury_10y']}%")
    print(f"  2Y Yield:      {data['treasury_2y']}%")
    print()
    print(f"  RSI (14):      {data['rsi_14']}")
    print(f"  MA 20:         ${data['ma_20']}  (SPY is {data['price_vs_ma20']:+.2f} vs MA20)")
    print(f"  MA 50:         ${data['ma_50']}  (SPY is {data['price_vs_ma50']:+.2f} vs MA50)")
    ma200_str = f"${data['ma_200']}  (SPY is {data['price_vs_ma200']:+.2f} vs MA200)" if data['ma_200'] else "insufficient history"
    print(f"  MA 200:        {ma200_str}")
    print()
    print(f"  Support (20d): ${data['support_20d']}")
    print(f"  Resistance:    ${data['resistance_20d']}")
    print(f"  52W High:      ${data['week52_high']}  ({data['pct_from_52h']:+.2f}% from here)")
    print(f"  52W Low:       ${data['week52_low']}  ({data['pct_from_52l']:+.2f}% from here)")

    print()
    save_snapshot(data)
    print("\n=== ALL TESTS PASSED ===")
