#!/usr/bin/env python3
"""
collect_data.py — Pre-run context script for hermes-trading-bot cron job.

Fetches live market data and outputs a structured context block to stdout.
Hermes cron injects this stdout as context before the LLM prompt runs.

Outputs:
  - Current timestamp and market session info
  - SPY price, VIX, Fear & Greed, RSI, MAs, yields
  - Portfolio summary
  - Open positions
  - Brief trade history summary
  - Upcoming economic events reminder
"""

import json
import sys
from datetime import datetime, timezone, time as dtime
from pathlib import Path

# Add trading-bot scripts to path
sys.path.insert(0, str(Path.home() / "trading-bot" / "scripts"))

from fetch_market_data import fetch_all_market_data, save_snapshot

STATE_DIR = Path.home() / "trading-bot" / "state"

def load_portfolio():
    path = STATE_DIR / "portfolio.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}

def load_open_trades():
    path = STATE_DIR / "trades.jsonl"
    if not path.exists():
        return []
    trades = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    t = json.loads(line)
                    if t.get("status") == "open":
                        trades.append(t)
                except Exception:
                    pass
    return trades

def load_closed_summary():
    path = STATE_DIR / "trades.jsonl"
    if not path.exists():
        return {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0, "win_rate": 0}
    closed = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    t = json.loads(line)
                    if t.get("status") != "open":
                        closed.append(t)
                except Exception:
                    pass
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    total_pnl = sum(t.get("pnl") or 0 for t in closed)
    return {
        "total":     len(closed),
        "wins":      len(wins),
        "losses":    len(closed) - len(wins),
        "total_pnl": round(total_pnl, 2),
        "win_rate":  round(len(wins) / len(closed) * 100, 1) if closed else 0,
    }

def is_market_open():
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://worldtimeapi.org/api/timezone/America/New_York",
            headers={"User-Agent": "trading-bot/1.0"}
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        dt = datetime.fromisoformat(data["datetime"])
        weekday = dt.weekday()
        t = dt.time().replace(tzinfo=None)
        if weekday >= 5:
            return False, "Weekend"
        if t < dtime(9, 30):
            return False, f"Pre-market ({t.strftime('%H:%M')} ET)"
        if t >= dtime(16, 0):
            return False, f"After-hours ({t.strftime('%H:%M')} ET)"
        return True, f"OPEN ({t.strftime('%H:%M')} ET)"
    except Exception:
        return True, "Unknown (proceeding)"

def main():
    now = datetime.now(timezone.utc)
    is_open, market_status = is_market_open()

    print(f"=== TRADING BOT CONTEXT — {now.strftime('%Y-%m-%d %H:%M')} UTC ===")
    print(f"Market: {market_status}")
    print()

    if not is_open:
        print("NOTE: Market is currently closed. If you are running a plan review")
        print("or pre-market analysis, proceed. Otherwise hold all actions.")
        print()

    # Fetch fresh market data
    print("--- LIVE MARKET DATA ---")
    try:
        snap = fetch_all_market_data("SPY")
        save_snapshot(snap)

        print(f"SPY Price:      ${snap.get('price')}  ({snap.get('change_pct', 0):+.2f}% today)")
        print(f"VIX:            {snap.get('vix')} ({snap.get('vix_change_pct', 0):+.1f}% today)")
        print(f"Fear & Greed:   {snap.get('fear_greed_score')} — {snap.get('fear_greed_label')}")
        print(f"RSI (14):       {snap.get('rsi_14')}")
        print(f"MA 20:          ${snap.get('ma_20')}")
        print(f"MA 50:          ${snap.get('ma_50')}")
        print(f"MA 200:         {snap.get('ma_200') or 'N/A'}")
        print(f"Support (20d):  ${snap.get('support_20d')}")
        print(f"Resistance:     ${snap.get('resistance_20d')}")
        print(f"10Y Yield:      {snap.get('treasury_10y') or 'N/A'}%")
        print(f"2Y Yield:       {snap.get('treasury_2y') or 'N/A'}%")
        print(f"52W High:       ${snap.get('week52_high')}  ({snap.get('pct_from_52h', 0):+.1f}% from current)")
        print(f"52W Low:        ${snap.get('week52_low')}  ({snap.get('pct_from_52l', 0):+.1f}% from current)")
    except Exception as e:
        print(f"[WARN] Market data fetch failed: {e}")
        print("Use trading_get_market_snapshot() tool for cached data.")
    print()

    # Portfolio summary
    print("--- PORTFOLIO ---")
    try:
        port = load_portfolio()
        if port:
            print(f"Cash:           ${port.get('total_cash', 0):,.2f}")
            print(f"Total Equity:   ${port.get('total_equity', 0):,.2f}")
            print(f"Option BP:      ${port.get('option_buying_power', 0):,.2f}")
        else:
            print("(use trading_get_portfolio for live data)")
    except Exception as e:
        print(f"[WARN] Portfolio load failed: {e}")
    print()

    # Open positions
    print("--- OPEN POSITIONS ---")
    try:
        open_trades = load_open_trades()
        if open_trades:
            for t in open_trades:
                print(f"  [{t.get('id')}] {t.get('option_symbol')}  "
                      f"entry=${t.get('entry_price')}  qty={t.get('quantity')}  "
                      f"cost=${t.get('cost', 0):,.2f}  "
                      f"opened={t.get('opened_at', '')[:10]}")
        else:
            print("  None")
    except Exception as e:
        print(f"[WARN] Open trades load failed: {e}")
    print()

    # Trade summary
    print("--- ALL-TIME TRADE SUMMARY ---")
    try:
        summary = load_closed_summary()
        print(f"Total closed:   {summary['total']}")
        print(f"Wins / Losses:  {summary['wins']} / {summary['losses']}")
        print(f"Win Rate:       {summary['win_rate']}%")
        print(f"Total P&L:      ${summary['total_pnl']:,.2f}")
    except Exception as e:
        print(f"[WARN] Trade summary failed: {e}")
    print()

    print("=== END CONTEXT ===")

if __name__ == "__main__":
    main()
