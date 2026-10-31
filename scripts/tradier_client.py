#!/usr/bin/env python3
"""
Tradier API client — all communication with Tradier sandbox/production.
Handles: balances, positions, orders, options chains, quotes.
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
def load_config():
    env_path = os.path.expanduser("~/trading-bot/.env")
    config = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip()
    return config

_cfg = load_config()
MODE = _cfg.get("TRADING_MODE", "sandbox")

if MODE == "sandbox":
    BASE_URL = "https://sandbox.tradier.com/v1"
    TOKEN    = _cfg.get("TRADIER_SANDBOX_TOKEN", "")
    ACCOUNT  = _cfg.get("TRADIER_SANDBOX_ACCOUNT", "")
else:
    BASE_URL = "https://api.tradier.com/v1"
    TOKEN    = _cfg.get("TRADIER_PROD_TOKEN", "")
    ACCOUNT  = _cfg.get("TRADIER_PROD_ACCOUNT", "")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}

# ── HTTP helpers ─────────────────────────────────────────────────────────────
def _get(path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return e.code, {"error": body}

def _post(path, data: dict):
    url = f"{BASE_URL}{path}"
    encoded = urllib.parse.urlencode(data).encode()
    post_headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=encoded, headers=post_headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return e.code, {"error": body}

# ── Account ──────────────────────────────────────────────────────────────────
def get_balances():
    """Returns dict with total_equity, total_cash, option_buying_power etc."""
    status, data = _get(f"/accounts/{ACCOUNT}/balances")
    if status == 200:
        return data.get("balances", {})
    raise RuntimeError(f"get_balances failed HTTP {status}: {data}")

def get_positions():
    """Returns list of open positions, empty list if none."""
    status, data = _get(f"/accounts/{ACCOUNT}/positions")
    if status != 200:
        raise RuntimeError(f"get_positions failed HTTP {status}: {data}")
    pos = data.get("positions")
    if not pos or pos == "null":
        return []
    raw = pos.get("position", [])
    return raw if isinstance(raw, list) else [raw]

def get_orders(limit=100):
    """Returns list of recent orders."""
    status, data = _get(f"/accounts/{ACCOUNT}/orders", {"limit": limit})
    if status != 200:
        raise RuntimeError(f"get_orders failed HTTP {status}: {data}")
    orders = data.get("orders")
    if not orders or orders == "null":
        return []
    raw = orders.get("order", [])
    return raw if isinstance(raw, list) else [raw]

def get_order(order_id):
    """Get status of a single order."""
    status, data = _get(f"/accounts/{ACCOUNT}/orders/{order_id}")
    if status == 200:
        return data.get("order", {})
    raise RuntimeError(f"get_order failed HTTP {status}: {data}")

# ── Market Data ───────────────────────────────────────────────────────────────
def get_quote(symbol="SPY"):
    """Returns quote dict: last, bid, ask, volume, change_percentage etc."""
    status, data = _get("/markets/quotes", {"symbols": symbol})
    if status == 200:
        return data.get("quotes", {}).get("quote", {})
    raise RuntimeError(f"get_quote failed HTTP {status}: {data}")

def get_option_expirations(symbol="SPY"):
    """Returns list of expiration date strings like ['2026-06-01', ...]."""
    status, data = _get("/markets/options/expirations", {"symbol": symbol})
    if status == 200:
        exps = data.get("expirations", {}).get("date", [])
        return exps if isinstance(exps, list) else [exps]
    raise RuntimeError(f"get_option_expirations failed HTTP {status}: {data}")

def get_options_chain(symbol="SPY", expiration=None, greeks=True):
    """Returns list of option contracts for a given expiry."""
    if not expiration:
        exps = get_option_expirations(symbol)
        # Pick expiry ~1-2 weeks out for better liquidity
        expiration = exps[3] if len(exps) > 3 else exps[0]
    status, data = _get("/markets/options/chains", {
        "symbol": symbol,
        "expiration": expiration,
        "greeks": "true" if greeks else "false"
    })
    if status == 200:
        options = data.get("options", {}).get("option", [])
        return options if isinstance(options, list) else [options]
    raise RuntimeError(f"get_options_chain failed HTTP {status}: {data}")

def get_atm_options(symbol="SPY", expiration=None):
    """Returns the 5 calls and 5 puts nearest to current price (ATM)."""
    quote = get_quote(symbol)
    current_price = quote.get("last", 0)
    chain = get_options_chain(symbol, expiration)

    calls = sorted(
        [o for o in chain if o.get("option_type") == "call"],
        key=lambda o: abs(o.get("strike", 0) - current_price)
    )[:5]
    puts = sorted(
        [o for o in chain if o.get("option_type") == "put"],
        key=lambda o: abs(o.get("strike", 0) - current_price)
    )[:5]

    return {
        "current_price": current_price,
        "expiration": expiration or (calls[0].get("expiration_date") if calls else None),
        "atm_calls": calls,
        "atm_puts": puts
    }

# ── OCC Symbol Builder ────────────────────────────────────────────────────────
def build_occ_symbol(symbol, expiry, option_type, strike):
    """
    Builds OCC option symbol from human-readable parts.
    Example: build_occ_symbol("SPY", "2026-06-06", "call", 760)
             → "SPY260606C00760000"
    """
    year  = expiry[2:4]   # "26"
    month = expiry[5:7]   # "06"
    day   = expiry[8:10]  # "06"
    cp    = "C" if option_type.lower() == "call" else "P"
    strike_str = str(int(float(strike) * 1000)).zfill(8)
    return f"{symbol.upper()}{year}{month}{day}{cp}{strike_str}"

# ── Trade Execution ───────────────────────────────────────────────────────────
def place_option_order(
    symbol,
    option_symbol,
    side,           # buy_to_open | sell_to_close | buy_to_close | sell_to_open
    quantity,
    limit_price,
    duration="day",
    preview=False
):
    """
    Places an options order via Tradier.
    Returns dict with order id and status.
    """
    data = {
        "class":         "option",
        "symbol":        symbol,
        "option_symbol": option_symbol,
        "side":          side,
        "quantity":      str(quantity),
        "type":          "limit",
        "duration":      duration,
        "price":         str(round(limit_price, 2)),
        "preview":       "true" if preview else "false",
    }
    status, resp = _post(f"/accounts/{ACCOUNT}/orders", data)
    if status in (200, 201):
        return resp.get("order", resp)
    raise RuntimeError(f"place_option_order failed HTTP {status}: {resp}")

def place_stock_order(
    symbol,
    side,           # buy | sell
    quantity,
    order_type="market",  # market | limit | stop
    limit_price=None,
    stop_price=None,
    duration="day",
    preview=False
):
    """
    Places a stock/equity order via Tradier.
    Returns dict with order id and status.
    """
    data = {
        "class":    "equity",
        "symbol":   symbol.upper(),
        "side":     side,
        "quantity": str(quantity),
        "type":     order_type,
        "duration": duration,
        "preview":  "true" if preview else "false",
    }
    if limit_price is not None:
        data["price"] = str(round(limit_price, 2))
    if stop_price is not None:
        data["stop"] = str(round(stop_price, 2))

    status, resp = _post(f"/accounts/{ACCOUNT}/orders", data)
    if status in (200, 201):
        return resp.get("order", resp)
    raise RuntimeError(f"place_stock_order failed HTTP {status}: {resp}")


def cancel_order(order_id):
    """Cancels an open order."""
    url = f"{BASE_URL}/accounts/{ACCOUNT}/orders/{order_id}"
    req = urllib.request.Request(url, headers=HEADERS, method="DELETE")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"cancel_order failed HTTP {e.code}: {body}")

# ── Portfolio Sync ────────────────────────────────────────────────────────────
def sync_portfolio():
    """
    Pulls live data from Tradier and returns a clean portfolio dict.
    This is what gets written to portfolio.json each run.
    """
    balances  = get_balances()
    positions = get_positions()
    orders    = get_orders(50)

    # Summarise positions with live data
    open_positions = []
    for p in positions:
        sym = p.get("symbol", "")
        qty = p.get("quantity", 0)
        cost_basis = p.get("cost_basis", 0)
        # Try to get current price
        try:
            q = get_quote(sym)
            current_price = q.get("last", 0)
            market_value  = current_price * abs(qty) * 100  # options: 100x
            unrealized_pnl = market_value - cost_basis
        except Exception:
            current_price  = 0
            market_value   = cost_basis
            unrealized_pnl = 0

        open_positions.append({
            "symbol":         sym,
            "quantity":       qty,
            "cost_basis":     cost_basis,
            "current_price":  current_price,
            "market_value":   round(market_value, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "date_acquired":  p.get("date_acquired", ""),
        })

    portfolio = {
        "account":              ACCOUNT,
        "mode":                 MODE,
        "total_equity":         balances.get("total_equity", 0),
        "total_cash":           balances.get("total_cash", 0),
        "option_buying_power":  balances.get("margin", {}).get("option_buying_power", 0),
        "open_pl":              balances.get("open_pl", 0),
        "close_pl":             balances.get("close_pl", 0),
        "open_positions":       open_positions,
        "open_positions_count": len(open_positions),
        "pending_orders":       len([o for o in orders if o.get("status") == "pending"]),
        "last_updated":         datetime.utcnow().isoformat() + "Z",
    }
    return portfolio


# ── Quick self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Mode: {MODE}  |  Account: {ACCOUNT}")
    print(f"Token: {TOKEN[:8]}...")
    print()

    print("=== Balances ===")
    b = get_balances()
    print(f"  Cash:            ${b.get('total_cash', 0):,.2f}")
    print(f"  Option BP:       ${b.get('margin', {}).get('option_buying_power', 0):,.2f}")
    print(f"  Open P&L:        ${b.get('open_pl', 0):,.2f}")

    print("\n=== Positions ===")
    pos = get_positions()
    if pos:
        for p in pos:
            print(f"  {p.get('symbol')}  qty={p.get('quantity')}  cost=${p.get('cost_basis')}")
    else:
        print("  No open positions")

    print("\n=== SPY Quote ===")
    q = get_quote("SPY")
    print(f"  Last: ${q.get('last')}  Bid: ${q.get('bid')}  Ask: ${q.get('ask')}")
    print(f"  Change: {q.get('change_percentage', 0):.2f}%  Volume: {q.get('volume', 0):,}")

    print("\n=== ATM Options (next expiry) ===")
    atm = get_atm_options("SPY")
    print(f"  SPY @ ${atm['current_price']}  Expiry: {atm['expiration']}")
    print(f"  ATM Calls:")
    for c in atm["atm_calls"][:3]:
        print(f"    Strike ${c.get('strike')}  Bid {c.get('bid')}  Ask {c.get('ask')}  Delta {c.get('greeks', {}).get('delta', 'n/a')}")
    print(f"  ATM Puts:")
    for p in atm["atm_puts"][:3]:
        print(f"    Strike ${p.get('strike')}  Bid {p.get('bid')}  Ask {p.get('ask')}  Delta {p.get('greeks', {}).get('delta', 'n/a')}")

    print("\n=== OCC Symbol Builder Test ===")
    sym = build_occ_symbol("SPY", "2026-06-06", "call", 760)
    print(f"  SPY Jun-06 $760 Call → {sym}")
    sym2 = build_occ_symbol("SPY", "2026-06-06", "put", 740)
    print(f"  SPY Jun-06 $740 Put  → {sym2}")

    print("\n=== Portfolio Sync ===")
    port = sync_portfolio()
    print(f"  Equity: ${port['total_equity']:,.2f}")
    print(f"  Cash:   ${port['total_cash']:,.2f}")
    print(f"  Open positions: {port['open_positions_count']}")

    print("\n=== ALL TESTS PASSED ===")
