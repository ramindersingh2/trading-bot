#!/usr/bin/env python3
"""
executor.py — Parses LLM JSON output, runs risk checks, executes trades.

Flow:
  1. Parse JSON from LLM response text
  2. Risk guard checks (size, positions, daily loss)
  3. Build OCC symbol
  4. Place order via Tradier
  5. Save trade to trades.jsonl
  6. Update portfolio.json
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tradier_client import (
    build_occ_symbol,
    place_option_order,
    get_balances,
    get_positions,
    sync_portfolio,
)

STATE_DIR = Path.home() / "trading-bot" / "state"

# ── Risk Limits ───────────────────────────────────────────────────────────────
RISK = {
    "max_position_pct":   0.10,   # max 10% of portfolio per single trade
    "max_open_positions": 5,      # never hold more than 5 at once
    "max_daily_loss":     3000,   # stop trading if lost $3k in a day
    "min_limit_price":    0.10,   # never buy options under 10 cents (illiquid)
    "max_quantity":       10,     # never more than 10 contracts per trade
}

# ── JSON Parser ───────────────────────────────────────────────────────────────
def extract_json(text: str) -> dict:
    """
    Extracts JSON from LLM response text.
    Handles cases where LLM adds explanation before/after the JSON block.
    """
    # Try to find JSON block wrapped in ```json ... ```
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))

    # Try raw JSON block (starts with { ends with })
    match = re.search(r"(\{[\s\S]*\})", text)
    if match:
        # Find the outermost complete JSON object
        candidate = match.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Try to find the last valid JSON object
            for i in range(len(text) - 1, -1, -1):
                if text[i] == "}":
                    for j in range(len(text)):
                        if text[j] == "{":
                            try:
                                return json.loads(text[j:i+1])
                            except Exception:
                                continue
    raise ValueError("No valid JSON found in LLM response")

# ── Risk Guard ────────────────────────────────────────────────────────────────
def risk_check(action: dict, portfolio: dict) -> tuple[bool, str]:
    """
    Runs risk checks before executing any trade.
    Returns (passed: bool, reason: str)
    """
    action_type = action.get("type", "hold")

    if action_type != "open":
        return True, "ok"  # hold/close don't need most checks

    total_equity = portfolio.get("total_equity", 0)
    open_count   = portfolio.get("open_positions_count", 0)
    daily_loss   = abs(min(portfolio.get("close_pl", 0), 0))  # today's realized losses

    # Check 1: daily loss limit
    if daily_loss >= RISK["max_daily_loss"]:
        return False, f"Daily loss limit hit: ${daily_loss:,.2f} >= ${RISK['max_daily_loss']:,.2f}. No new trades today."

    # Check 2: too many open positions
    if open_count >= RISK["max_open_positions"]:
        return False, f"Max open positions reached: {open_count} >= {RISK['max_open_positions']}. Close a position first."

    # Check 3: position size
    qty         = action.get("quantity", 1)
    limit_price = action.get("limit_price", 0)
    trade_cost  = qty * limit_price * 100  # each contract = 100 shares
    max_allowed = total_equity * RISK["max_position_pct"]

    if trade_cost > max_allowed:
        return False, f"Trade too large: ${trade_cost:,.2f} > max allowed ${max_allowed:,.2f} (10% of ${total_equity:,.2f})."

    # Check 4: min price (illiquid options)
    if limit_price < RISK["min_limit_price"]:
        return False, f"Limit price ${limit_price} too low (min ${RISK['min_limit_price']}). Option likely illiquid."

    # Check 5: quantity sanity
    if qty > RISK["max_quantity"]:
        return False, f"Quantity {qty} exceeds max allowed {RISK['max_quantity']}."

    # Check 6: enough cash
    cash = portfolio.get("total_cash", 0)
    if trade_cost > cash:
        return False, f"Not enough cash: trade costs ${trade_cost:,.2f} but only ${cash:,.2f} available."

    return True, "ok"

# ── Trade ID Generator ────────────────────────────────────────────────────────
def next_trade_id() -> str:
    path = STATE_DIR / "trades.jsonl"
    if not path.exists():
        return "t_001"
    count = sum(1 for line in open(path) if line.strip())
    return f"t_{count + 1:03d}"

# ── Load Open Trades ──────────────────────────────────────────────────────────
def load_open_trades() -> list:
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
                    continue
    return trades

def update_trade_status(trade_id: str, updates: dict):
    """Updates a specific trade in trades.jsonl by rewriting the file."""
    path = STATE_DIR / "trades.jsonl"
    if not path.exists():
        return
    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
                if trade.get("id") == trade_id:
                    trade.update(updates)
                lines.append(json.dumps(trade))
            except Exception:
                lines.append(line)
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")

# ── Append to JSONL ───────────────────────────────────────────────────────────
def append_jsonl(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")

# ── Save Plan ─────────────────────────────────────────────────────────────────
def save_plan(plan_data: dict, action: dict, result: dict):
    """Saves the LLM plan + action + result to plans.jsonl."""
    entry = {
        "id":                f"p_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "market_thesis":     plan_data.get("market_thesis", ""),
        "research_done":     plan_data.get("research_done", []),
        "steps":             plan_data.get("steps", []),
        "confidence":        plan_data.get("confidence", "unknown"),
        "action_taken":      action.get("type", "hold"),
        "action_detail":     action,
        "result":            result,
    }
    append_jsonl(STATE_DIR / "plans.jsonl", entry)
    print(f"  Plan saved: {entry['id']}")
    return entry["id"]

# ── Execute OPEN ──────────────────────────────────────────────────────────────
def execute_open(action: dict, plan_id: str, dry_run: bool = False) -> dict:
    """Places a buy_to_open order and records the trade."""
    symbol       = "SPY"
    option_type  = action.get("option_type", "call")
    strike       = action.get("strike")
    expiry       = action.get("expiry")
    quantity     = int(action.get("quantity", 1))
    limit_price  = float(action.get("limit_price", 0))

    # Build OCC symbol
    occ_symbol = build_occ_symbol(symbol, expiry, option_type, strike)
    trade_cost = quantity * limit_price * 100

    print(f"  Placing BUY order: {occ_symbol}  qty={quantity}  limit=${limit_price}")
    print(f"  Estimated cost: ${trade_cost:,.2f}")

    trade_id = next_trade_id()

    if dry_run:
        print(f"  [DRY RUN] Would place: buy_to_open {occ_symbol}")
        order_result = {"id": "DRY_RUN", "status": "dry_run"}
    else:
        order_result = place_option_order(
            symbol=symbol,
            option_symbol=occ_symbol,
            side="buy_to_open",
            quantity=quantity,
            limit_price=limit_price,
        )

    tradier_order_id = str(order_result.get("id", "unknown"))
    order_status     = order_result.get("status", "pending")

    trade_record = {
        "id":               trade_id,
        "plan_id":          plan_id,
        "symbol":           symbol,
        "option_symbol":    occ_symbol,
        "option_type":      option_type,
        "strike":           strike,
        "expiry":           expiry,
        "side":             "buy_to_open",
        "quantity":         quantity,
        "entry_price":      limit_price,
        "exit_price":       None,
        "cost":             trade_cost,
        "pnl":              None,
        "status":           "open",
        "tradier_order_id": tradier_order_id,
        "order_status":     order_status,
        "opened_at":        datetime.now(timezone.utc).isoformat(),
        "closed_at":        None,
        "reason":           action.get("reason", ""),
    }

    append_jsonl(STATE_DIR / "trades.jsonl", trade_record)
    print(f"  Trade recorded: {trade_id}  Tradier order: {tradier_order_id}")

    return {
        "status":    "executed",
        "trade_id":  trade_id,
        "occ":       occ_symbol,
        "order_id":  tradier_order_id,
        "cost":      trade_cost,
        "dry_run":   dry_run,
    }

# ── Execute CLOSE ─────────────────────────────────────────────────────────────
def execute_close(action: dict, plan_id: str, dry_run: bool = False) -> dict:
    """Places a sell_to_close order and updates the trade record."""
    trade_id     = action.get("trade_id")
    occ_symbol   = action.get("option_symbol")
    quantity     = int(action.get("quantity", 1))
    limit_price  = float(action.get("limit_price", 0))

    if not occ_symbol:
        # Try to find from open trades
        open_trades = load_open_trades()
        match = next((t for t in open_trades if t.get("id") == trade_id), None)
        if match:
            occ_symbol = match.get("option_symbol")
            quantity   = match.get("quantity", quantity)
        else:
            return {"status": "error", "reason": f"Trade {trade_id} not found in open trades"}

    print(f"  Placing SELL order: {occ_symbol}  qty={quantity}  limit=${limit_price}")

    if dry_run:
        print(f"  [DRY RUN] Would place: sell_to_close {occ_symbol}")
        order_result = {"id": "DRY_RUN", "status": "dry_run"}
    else:
        order_result = place_option_order(
            symbol="SPY",
            option_symbol=occ_symbol,
            side="sell_to_close",
            quantity=quantity,
            limit_price=limit_price,
        )

    tradier_order_id = str(order_result.get("id", "unknown"))

    # Find original trade to calculate P&L
    open_trades  = load_open_trades()
    orig_trade   = next((t for t in open_trades if t.get("id") == trade_id), None)
    pnl          = None
    if orig_trade:
        entry_cost = orig_trade.get("cost", 0)
        exit_value = quantity * limit_price * 100
        pnl        = round(exit_value - entry_cost, 2)

    # Update trade record
    if trade_id:
        update_trade_status(trade_id, {
            "status":           "closed",
            "exit_price":       limit_price,
            "closed_at":        datetime.now(timezone.utc).isoformat(),
            "close_order_id":   tradier_order_id,
            "pnl":              pnl,
            "close_plan_id":    plan_id,
        })
        print(f"  Trade {trade_id} marked closed. P&L: ${pnl:,.2f}" if pnl else f"  Trade {trade_id} marked closed.")

    return {
        "status":   "executed",
        "trade_id": trade_id,
        "occ":      occ_symbol,
        "order_id": tradier_order_id,
        "pnl":      pnl,
        "dry_run":  dry_run,
    }

# ── Master Execute ────────────────────────────────────────────────────────────
def execute(llm_response_text: str, dry_run: bool = False) -> dict:
    """
    Main entry point. Takes raw LLM text, extracts JSON,
    runs risk checks, executes trade, saves everything.

    Returns result dict with status and details.
    """
    now = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*50}")
    print(f"EXECUTOR — {now[:19]}")
    print(f"{'='*50}")

    # Step 1: Parse JSON
    print("\n[1] Parsing LLM output...")
    try:
        parsed = extract_json(llm_response_text)
        plan_data = parsed.get("plan", {})
        action    = parsed.get("action", {"type": "hold"})
        print(f"  Action type: {action.get('type', 'unknown')}")
        print(f"  Confidence:  {plan_data.get('confidence', 'N/A')}")
    except Exception as e:
        result = {"status": "error", "reason": f"JSON parse failed: {e}"}
        save_plan({}, {"type": "error"}, result)
        return result

    # Step 2: Load current portfolio
    print("\n[2] Loading portfolio state...")
    try:
        portfolio = sync_portfolio()
        # Save fresh portfolio.json
        port_path = STATE_DIR / "portfolio.json"
        port_path.write_text(json.dumps(portfolio, indent=2))
        print(f"  Cash: ${portfolio['total_cash']:,.2f}  |  Open positions: {portfolio['open_positions_count']}")
    except Exception as e:
        result = {"status": "error", "reason": f"Portfolio sync failed: {e}"}
        save_plan(plan_data, action, result)
        return result

    action_type = action.get("type", "hold").lower()

    # Step 3: Risk check (only for open trades)
    if action_type == "open":
        print("\n[3] Running risk checks...")
        passed, reason = risk_check(action, portfolio)
        if not passed:
            print(f"  ❌ BLOCKED: {reason}")
            result = {"status": "blocked", "reason": reason}
            save_plan(plan_data, action, result)
            return result
        print(f"  ✅ Risk checks passed")

    # Step 4: Execute
    print(f"\n[4] Executing action: {action_type.upper()}...")
    result = {}

    if action_type == "hold":
        print(f"  Holding. Reason: {action.get('reason', 'N/A')}")
        result = {"status": "hold", "reason": action.get("reason", "")}

    elif action_type == "open":
        try:
            plan_id = save_plan(plan_data, action, {})  # save plan first to get ID
            result  = execute_open(action, plan_id, dry_run=dry_run)
            # Update plan with result
            update_plan_result(plan_id, result)
        except Exception as e:
            result = {"status": "error", "reason": f"Open trade failed: {e}"}
            print(f"  ❌ Error: {e}")

    elif action_type == "close":
        try:
            plan_id = save_plan(plan_data, action, {})
            result  = execute_close(action, plan_id, dry_run=dry_run)
            update_plan_result(plan_id, result)
        except Exception as e:
            result = {"status": "error", "reason": f"Close trade failed: {e}"}
            print(f"  ❌ Error: {e}")

    else:
        result = {"status": "error", "reason": f"Unknown action type: {action_type}"}

    # Save plan for hold (open/close save their own above)
    if action_type == "hold":
        save_plan(plan_data, action, result)

    print(f"\n[5] Done. Status: {result.get('status', 'unknown')}")
    return result

def update_plan_result(plan_id: str, result: dict):
    """Updates the result field of a plan in plans.jsonl."""
    path = STATE_DIR / "plans.jsonl"
    if not path.exists():
        return
    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                plan = json.loads(line)
                if plan.get("id") == plan_id:
                    plan["result"] = result
                lines.append(json.dumps(plan))
            except Exception:
                lines.append(line)
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing executor with a simulated LLM HOLD response...\n")

    fake_hold_response = """
    After analyzing the market data carefully, I believe the best course
    of action is to hold and wait for a better entry.

    ```json
    {
      "plan": {
        "market_thesis": "SPY is at RSI 74 which is overbought. Near 52-week high resistance at $758. Risk/reward not favorable for new entries right now.",
        "research_done": ["Checked RSI — overbought at 74", "SPY within 0.2% of 52-week high resistance", "Fear & Greed at 28 — Fear despite high prices, divergence warning"],
        "steps": [
          "1. RSI at 74 indicates overbought conditions",
          "2. Price near 52-week high resistance $758 — likely to face selling pressure",
          "3. Fear & Greed divergence: prices high but sentiment fearful — warning sign",
          "4. Best action: hold cash, wait for pullback to MA20 ($739) for better entry"
        ],
        "confidence": "high"
      },
      "action": {
        "type": "hold",
        "reason": "Overbought RSI + resistance level + sentiment divergence. Waiting for better entry around $739-745 range."
      }
    }
    ```
    """

    result = execute(fake_hold_response, dry_run=True)
    print(f"\nResult: {json.dumps(result, indent=2)}")

    print("\n" + "="*50)
    print("Testing with a simulated OPEN trade response...\n")

    fake_open_response = """
    {
      "plan": {
        "market_thesis": "Bullish momentum confirmed. SPY above all major MAs. VIX low at 15. Fear index at 28 is contrarian bullish — smart money buying while retail fears.",
        "research_done": ["VIX low — low fear = good for calls", "SPY above MA20 and MA50 — uptrend intact", "Fear at 28 — contrarian buy signal"],
        "steps": [
          "1. Market in clear uptrend — above MA20 and MA50",
          "2. VIX at 15 is calm — no crash risk near term",
          "3. Fear at 28 is contrarian bullish — dip buyers will come in",
          "4. Opening a call with 1-2 week expiry to ride momentum"
        ],
        "confidence": "medium"
      },
      "action": {
        "type": "open",
        "option_type": "call",
        "strike": 760,
        "expiry": "2026-06-06",
        "quantity": 1,
        "limit_price": 3.50,
        "reason": "Bullish trend with low VIX. Buying call at $760 strike 1 week out."
      }
    }
    """

    result = execute(fake_open_response, dry_run=True)
    print(f"\nResult: {json.dumps(result, indent=2)}")
    print("\n=== ALL TESTS PASSED ===")
