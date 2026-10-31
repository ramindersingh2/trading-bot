#!/usr/bin/env python3
"""
Assembles the full LLM prompt from all data sources.
Uses proven prompt engineering techniques:
  - Detailed expert persona (ExpertPrompting, Xu et al. 2023)
  - Chain-of-Thought trigger (Wei et al. 2022)
  - Emotional stimulus for task importance (Li et al. 2023)
  - Structured output format enforcement
"""

import json
from datetime import datetime, timezone
from pathlib import Path


STATE_DIR = Path.home() / "trading-bot" / "state"

# ── Load State Files ──────────────────────────────────────────────────────────
def load_portfolio():
    path = STATE_DIR / "portfolio.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}

def load_recent_plans(days=15):
    """Loads plans from last N days from plans.jsonl."""
    path = STATE_DIR / "plans.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    plans = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                plan = json.loads(line)
                ts = plan.get("timestamp", "")
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.timestamp() >= cutoff:
                    plans.append(plan)
            except Exception:
                continue
    return sorted(plans, key=lambda p: p.get("timestamp", ""))

def load_trade_summary():
    """
    Returns:
      - open_trades: full detail list
      - closed_summary: { total, wins, losses, total_pnl }
    """
    path = STATE_DIR / "trades.jsonl"
    if not path.exists():
        return [], {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0, "win_rate": 0.0}

    open_trades   = []
    closed_trades = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
                if trade.get("status") == "open":
                    open_trades.append(trade)
                else:
                    closed_trades.append(trade)
            except Exception:
                continue

    wins   = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed_trades if (t.get("pnl") or 0) <= 0]
    total_pnl = sum(t.get("pnl") or 0 for t in closed_trades)

    return open_trades, {
        "total":     len(closed_trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "total_pnl": round(total_pnl, 2),
        "win_rate":  round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0.0,
    }

def load_latest_market_snapshot():
    path = STATE_DIR / "market_snapshots.jsonl"
    if not path.exists():
        return {}
    last_line = ""
    with open(path) as f:
        for line in f:
            if line.strip():
                last_line = line.strip()
    return json.loads(last_line) if last_line else {}

def load_latest_news():
    path = STATE_DIR / "latest_news.json"
    if path.exists():
        return json.loads(path.read_text())
    return []

# ── Prompt Sections ───────────────────────────────────────────────────────────
def section_role():
    return """
=== SECTION 1: YOUR ROLE & IDENTITY ===

You are an expert options trader with 20 years of experience specializing in SPY (S&P 500 ETF) options trading, volatility analysis, technical analysis, and risk management. You have a deep understanding of market microstructure, options greeks, macroeconomic indicators, and how news events move markets.

You are managing a REAL portfolio. Every decision you make has real financial consequences. This is very important to your track record and your portfolio's success.

Your SOLE objective: maximize risk-adjusted returns by trading SPY options (calls and puts).
You have full autonomy — you decide when to buy, sell, or hold. No restrictions on strategy.
You believe markets have hidden patterns, like nature, and your edge is recognizing them early.

Think step by step before every decision. Reason carefully through all available data.
""".strip()


def section_market_data(snapshot: dict):
    if not snapshot:
        return "=== SECTION 2: MARKET DATA ===\nMarket data unavailable this run.\n"

    def fmt(val, prefix="$", suffix="", decimals=2):
        if val is None:
            return "N/A"
        try:
            return f"{prefix}{float(val):.{decimals}f}{suffix}"
        except Exception:
            return str(val)

    # RSI interpretation
    rsi = snapshot.get("rsi_14")
    rsi_label = ""
    if rsi:
        if rsi >= 70:   rsi_label = " ⚠ OVERBOUGHT"
        elif rsi <= 30: rsi_label = " ⚠ OVERSOLD"
        else:           rsi_label = " (neutral)"

    # Fear & Greed interpretation
    fg = snapshot.get("fear_greed_score")
    fg_label = snapshot.get("fear_greed_label", "")
    fg_context = ""
    if fg is not None:
        if fg >= 75:   fg_context = " — Extreme Greed (contrarian: market may be overextended)"
        elif fg >= 55: fg_context = " — Greed (bullish sentiment)"
        elif fg >= 45: fg_context = " — Neutral"
        elif fg >= 25: fg_context = " — Fear (potential buying opportunity)"
        else:          fg_context = " — Extreme Fear (high risk, but contrarian opportunity)"

    # Price vs MAs
    p = snapshot.get("price", 0)
    ma20  = snapshot.get("ma_20")
    ma50  = snapshot.get("ma_50")
    ma200 = snapshot.get("ma_200")

    def vs_ma(price, ma):
        if not ma:
            return "N/A"
        diff = price - ma
        pct  = diff / ma * 100
        sign = "above" if diff > 0 else "below"
        return f"${ma} ({sign} by ${abs(diff):.2f} / {abs(pct):.1f}%)"

    return f"""
=== SECTION 2: LIVE MARKET DATA (as of {snapshot.get('timestamp', 'N/A')[:19]}) ===

PRICE ACTION:
  SPY Price:     ${p}
  Open / High / Low: ${snapshot.get('open')} / ${snapshot.get('high')} / ${snapshot.get('low')}
  Change Today:  {fmt(snapshot.get('change_pct'), prefix='', suffix='%')}
  Volume:        {snapshot.get('volume', 'N/A'):,} (avg: check vs normal)
  Prev Close:    ${snapshot.get('prev_close')}

VOLATILITY & SENTIMENT:
  VIX:           {snapshot.get('vix')} ({fmt(snapshot.get('vix_change_pct'), prefix='', suffix='% today')})
  Fear & Greed:  {fg} — {fg_label}{fg_context}

INTEREST RATES:
  10-Year Yield: {fmt(snapshot.get('treasury_10y'), prefix='', suffix='%')}
  2-Year Yield:  {fmt(snapshot.get('treasury_2y'), prefix='', suffix='%')}
  Yield Curve:   {"Inverted (recession signal)" if (snapshot.get('treasury_2y') or 0) > (snapshot.get('treasury_10y') or 999) else "Normal"}

TECHNICALS:
  RSI (14):      {rsi}{rsi_label}
  MA 20:         {vs_ma(p, ma20)}
  MA 50:         {vs_ma(p, ma50)}
  MA 200:        {vs_ma(p, ma200)}
  Support (20d): ${snapshot.get('support_20d')}
  Resistance:    ${snapshot.get('resistance_20d')}

52-WEEK RANGE:
  High: ${snapshot.get('week52_high')} ({fmt(snapshot.get('pct_from_52h'), prefix='', suffix='%')} from current)
  Low:  ${snapshot.get('week52_low')} ({fmt(snapshot.get('pct_from_52l'), prefix='', suffix='%')} from current)
""".strip()


def section_news(news_items: list):
    if not news_items:
        return "=== SECTION 3: RECENT NEWS ===\nNo news available.\n"

    lines = ["=== SECTION 3: TOP FINANCIAL NEWS (last 3 days) ===\n"]
    for i, item in enumerate(news_items[:5], 1):
        lines.append(f"{i}. [{item.get('source', 'Unknown')}] {item.get('title', '')}")
        if item.get("summary"):
            lines.append(f"   {item['summary'][:250]}")
        lines.append(f"   Published: {item.get('published', '')[:19]}\n")

    return "\n".join(lines).strip()


def section_history(portfolio: dict, open_trades: list, closed_summary: dict, recent_plans: list):
    lines = ["=== SECTION 4: YOUR TRADING HISTORY & CURRENT STATE ===\n"]

    # Portfolio overview
    lines.append("PORTFOLIO OVERVIEW:")
    lines.append(f"  Account Cash:        ${portfolio.get('total_cash', 0):,.2f}")
    lines.append(f"  Total Equity:        ${portfolio.get('total_equity', 0):,.2f}")
    lines.append(f"  Option Buying Power: ${portfolio.get('option_buying_power', 0):,.2f}")
    lines.append(f"  Open P&L (today):    ${portfolio.get('open_pl', 0):,.2f}")
    lines.append(f"  Closed P&L (today):  ${portfolio.get('close_pl', 0):,.2f}")
    lines.append(f"  Open Positions:      {portfolio.get('open_positions_count', 0)}")
    lines.append("")

    # All-time trade summary
    lines.append("ALL-TIME TRADE RECORD:")
    lines.append(f"  Total closed trades: {closed_summary['total']}")
    lines.append(f"  Wins / Losses:       {closed_summary['wins']} / {closed_summary['losses']}")
    lines.append(f"  Win Rate:            {closed_summary['win_rate']}%")
    lines.append(f"  Total Realized P&L:  ${closed_summary['total_pnl']:,.2f}")
    lines.append("")

    # Open positions — full detail
    if open_trades:
        lines.append("CURRENTLY OPEN POSITIONS:")
        for t in open_trades:
            unrealized = t.get("unrealized_pnl")
            upnl_str   = f"  Unrealized P&L: ${unrealized:,.2f}" if unrealized is not None else ""
            lines.append(f"  [{t.get('id')}] {t.get('option_symbol', t.get('symbol', 'N/A'))}")
            lines.append(f"    Type: {t.get('option_type','').upper()} | Strike: ${t.get('strike')} | Expiry: {t.get('expiry')}")
            lines.append(f"    Entry: ${t.get('entry_price')} | Qty: {t.get('quantity')} | Cost: ${t.get('cost', 0):,.2f}")
            lines.append(f"    Opened: {t.get('opened_at', '')[:19]}{upnl_str}")
            lines.append("")
    else:
        lines.append("CURRENTLY OPEN POSITIONS: None\n")

    # Last 15 days plans — full detail
    if recent_plans:
        lines.append(f"YOUR LAST {len(recent_plans)} PLANS (full detail, most recent last):\n")
        for plan in recent_plans[-20:]:  # cap at 20 to avoid token overload
            lines.append(f"  ── {plan.get('timestamp', '')[:19]} ──")
            lines.append(f"  Thesis:    {plan.get('market_thesis', 'N/A')[:200]}")
            lines.append(f"  Action:    {plan.get('action_taken', 'N/A')}")
            lines.append(f"  Confidence:{plan.get('confidence', 'N/A')}")
            if plan.get("research_done"):
                lines.append(f"  Research:  {str(plan.get('research_done', ''))[:200]}")
            if plan.get("result"):
                lines.append(f"  Result:    {plan.get('result')}")
            lines.append("")
    else:
        lines.append("PLAN HISTORY: No previous plans yet. This is your first run.\n")

    return "\n".join(lines).strip()


def section_task():
    return """
=== SECTION 5: YOUR TASK THIS HOUR ===

You have all the information above. Now do the following — THINK STEP BY STEP:

STEP 1 — STATE YOUR MARKET THESIS
  What is your read on the market RIGHT NOW?
  Is it bullish, bearish, or unclear? What is the dominant trend?
  What does the VIX and Fear & Greed tell you?

STEP 2 — IDENTIFY WHAT ELSE YOU NEED TO KNOW
  What additional research would increase your confidence?
  Examples: check a specific news event, verify a technical level,
  look at sector performance, check upcoming economic events.
  List the specific things you want to research, then reason through what they mean.

STEP 3 — REVIEW YOUR OPEN POSITIONS
  For each open position: should you hold, close for profit, or cut losses?
  Is the original thesis still valid?

STEP 4 — DECIDE YOUR ACTION
  Based on your analysis, what do you do?
  Options: HOLD everything | OPEN a new trade | CLOSE an existing trade | BOTH (close one, open another)

STEP 5 — OUTPUT YOUR DECISION AS JSON
  Your final output MUST end with a valid JSON block.
  Do NOT add any text after the JSON block.
  If uncertain, action type = "hold".

JSON FORMAT:

For HOLD:
{
  "plan": {
    "market_thesis": "your thesis in 1-3 sentences",
    "research_done": ["thing you checked 1", "thing you checked 2"],
    "steps": ["step 1 reasoning", "step 2 reasoning", ...],
    "confidence": "high|medium|low"
  },
  "action": {
    "type": "hold",
    "reason": "why holding is the right move"
  }
}

For OPEN a new trade:
{
  "plan": {
    "market_thesis": "your thesis",
    "research_done": ["what you checked"],
    "steps": ["reasoning steps"],
    "confidence": "high|medium|low"
  },
  "action": {
    "type": "open",
    "option_type": "call|put",
    "strike": 760,
    "expiry": "YYYY-MM-DD",
    "quantity": 1,
    "limit_price": 4.50,
    "reason": "why this specific trade"
  }
}

For CLOSE an existing trade:
{
  "plan": {
    "market_thesis": "your thesis",
    "research_done": ["what you checked"],
    "steps": ["reasoning steps"],
    "confidence": "high|medium|low"
  },
  "action": {
    "type": "close",
    "trade_id": "t_003",
    "option_symbol": "SPY260606C00760000",
    "quantity": 2,
    "limit_price": 7.40,
    "reason": "why closing now"
  }
}

Remember: This is your portfolio. Your decisions directly impact your performance.
Think carefully. Be precise. Output valid JSON.
""".strip()


# ── Master Assembler ──────────────────────────────────────────────────────────
def build_prompt():
    """
    Loads all state files, assembles and returns the full prompt string.
    """
    print("  Loading portfolio...")
    portfolio = load_portfolio()

    print("  Loading trade history...")
    open_trades, closed_summary = load_trade_summary()

    print("  Loading recent plans...")
    recent_plans = load_recent_plans(days=15)

    print("  Loading market snapshot...")
    snapshot = load_latest_market_snapshot()

    print("  Loading news...")
    news = load_latest_news()

    sections = [
        section_role(),
        "",
        section_market_data(snapshot),
        "",
        section_news(news),
        "",
        section_history(portfolio, open_trades, closed_summary, recent_plans),
        "",
        section_task(),
    ]

    prompt = "\n".join(sections)
    return prompt


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Building prompt...\n")
    prompt = build_prompt()

    # Save to file for inspection
    out = Path.home() / "trading-bot" / "state" / "latest_prompt.txt"
    out.write_text(prompt)
    print(f"\nPrompt saved → {out}")
    print(f"Prompt length: {len(prompt)} chars / ~{len(prompt)//4} tokens\n")
    print("=" * 60)
    print("PROMPT PREVIEW (first 2000 chars):")
    print("=" * 60)
    print(prompt[:2000])
    print("...")
    print("=" * 60)
    print("\n=== DONE ===")
