#!/usr/bin/env python3
"""Test Tradier sandbox API connectivity."""

import json
import os
import urllib.request

# Load .env
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    config = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config

_cfg = _load_env()
TOKEN = _cfg.get("TRADIER_SANDBOX_TOKEN", "")
BASE = "https://sandbox.tradier.com/v1"
ACCT = _cfg.get("TRADIER_SANDBOX_ACCOUNT", "")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json"
}

def api_get(path):
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return resp.status, data
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return e.code, body

print()

# Test 1: Account balances
print("=== 1. Account Balances ===")
status, data = api_get(f"/accounts/{ACCT}/balances")
print(f"  HTTP {status}")
if status == 200:
    b = data.get("balances", {})
    print(f"  Total Equity: ${b.get('total_equity', 0):,.2f}")
    print(f"  Total Cash:   ${b.get('total_cash', 0):,.2f}")
    print(f"  Option BP:    ${b.get('margin', {}).get('option_buying_power', 0):,.2f}")
else:
    print(f"  Error: {data}")

# Test 2: SPY Quote
print("\n=== 2. SPY Quote ===")
status, data = api_get("/markets/quotes?symbols=SPY")
print(f"  HTTP {status}")
if status == 200:
    q = data.get("quotes", {}).get("quote", {})
    print(f"  SPY Last:   ${q.get('last', 0)}")
    print(f"  SPY Bid:    ${q.get('bid', 0)}")
    print(f"  SPY Ask:    ${q.get('ask', 0)}")
    print(f"  Volume:     {q.get('volume', 0):,}")
else:
    print(f"  Error: {data}")

# Test 3: Options Expirations
print("\n=== 3. Options Expirations for SPY ===")
status, data = api_get("/markets/options/expirations?symbol=SPY")
print(f"  HTTP {status}")
if status == 200:
    exps = data.get("expirations", {}).get("date", [])
    if isinstance(exps, list):
        print(f"  Available expirations: {len(exps)}")
        for d in exps[:5]:
            print(f"    {d}")
        if len(exps) > 5:
            print(f"    ... and {len(exps) - 5} more")
    else:
        print(f"  Data: {exps}")
else:
    print(f"  Error: {data}")

# Test 4: Options Chain (use first expiration if available)
if status == 200 and isinstance(exps, list) and len(exps) > 0:
    exp_date = exps[0]
    print(f"\n=== 4. Options Chain for SPY {exp_date} ===")
    status, data = api_get(f"/markets/options/chains?symbol=SPY&expiration={exp_date}&greeks=true")
    print(f"  HTTP {status}")
    if status == 200:
        options = data.get("options", {}).get("option", [])
        if isinstance(options, list):
            calls = [o for o in options if o.get("option_type") == "call"]
            puts = [o for o in options if o.get("option_type") == "put"]
            print(f"  Total options: {len(options)} ({len(calls)} calls, {len(puts)} puts)")
            # Show a few near-the-money
            if calls:
                print(f"  Sample call: {calls[len(calls)//2].get('symbol')} "
                      f"Strike: ${calls[len(calls)//2].get('strike')} "
                      f"Bid: {calls[len(calls)//2].get('bid')} "
                      f"Ask: {calls[len(calls)//2].get('ask')}")
        else:
            print(f"  Unexpected format: {str(options)[:200]}")
    else:
        print(f"  Error: {data}")

# Test 5: Positions
print("\n=== 5. Current Positions ===")
status, data = api_get(f"/accounts/{ACCT}/positions")
print(f"  HTTP {status}")
if status == 200:
    pos = data.get("positions")
    if pos == "null" or pos is None:
        print("  No positions (empty account)")
    else:
        print(f"  Positions: {pos}")
else:
    print(f"  Error: {data}")

# Test 6: Order History
print("\n=== 6. Order History ===")
status, data = api_get(f"/accounts/{ACCT}/orders")
print(f"  HTTP {status}")
if status == 200:
    orders = data.get("orders")
    if orders == "null" or orders is None:
        print("  No orders yet (fresh account)")
    else:
        print(f"  Orders: {orders}")
else:
    print(f"  Error: {data}")

print("\n=== ALL TESTS COMPLETE ===")
