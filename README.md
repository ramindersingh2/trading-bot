<div align="center">

# Trading Bot

### An autonomous SPY options trading bot powered by LLM-driven analysis and the Tradier brokerage API

[![Python](https://img.shields.io/badge/Python-3.9+-blue?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Tradier](https://img.shields.io/badge/Broker-Tradier_Sandbox-00b4d8?style=for-the-badge)](https://tradier.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

</div>

---

## Overview

This is an autonomous trading bot that uses a large language model (LLM) to analyze live market data, decide trade actions, and execute real options orders through the [Tradier](https://tradier.com) brokerage API.

It runs on an hourly cron schedule during market hours, fetches fresh market data (SPY price, VIX, Fear & Greed, RSI, moving averages, treasury yields), reads the latest financial news, builds a structured prompt, sends it to an LLM for analysis, and then executes the resulting trade decision — all with built-in risk guards.

The bot was built to run on [Hermes Agent](https://hermes-agent.nousresearch.com) but can work with any LLM provider (OpenRouter, Anthropic, OpenAI, local models, etc.).

---

## How It Works

```
                   ┌──────────────────────────────────────────────┐
                   │              CRON SCHEDULER                   │
                   │     (hourly, Mon–Fri, 6:30am–1:30pm PT)       │
                   └─────────────────┬──────────────────────────────┘
                                     │
                                     ▼
                   ┌──────────────────────────────────────────────┐
                   │           1. MARKET HOURS CHECK               │
                   │     Skip if market is closed (weekends,       │
                   │     pre/post-market)                          │
                   └─────────────────┬──────────────────────────────┘
                                     │
                                     ▼
                   ┌──────────────────────────────────────────────┐
                   │         2. FETCH MARKET DATA                  │
                   │   • SPY quote + historical bars               │
                   │   • VIX                                        │
                   │   • Fear & Greed Index (CNN/alternative.me)    │
                   │   • Treasury yields (10Y, 2Y)                  │
                   │   • RSI, MA20/50/200, support/resistance       │
                   │   • 52-week high/low                           │
                   └─────────────────┬──────────────────────────────┘
                                     │
                                     ▼
                   ┌──────────────────────────────────────────────┐
                   │           3. FETCH FINANCIAL NEWS             │
                   │   • Yahoo Finance, CNBC, Reuters,              │
                   │     MarketWatch, Seeking Alpha (RSS)           │
                   └─────────────────┬──────────────────────────────┘
                                     │
                                     ▼
                   ┌──────────────────────────────────────────────┐
                   │          4. BUILD LLM PROMPT                  │
                   │   • Expert persona (expert prompting)         │
                   │   • Chain-of-thought trigger                   │
                   │   • Portfolio + open positions context         │
                   │   • Recent trade history + P&L summary        │
                   │   • Structured JSON output format              │
                   └─────────────────┬──────────────────────────────┘
                                     │
                                     ▼
                   ┌──────────────────────────────────────────────┐
                   │          5. LLM ANALYSIS + DECISION            │
                   │   • LLM analyzes market + portfolio           │
                   │   • Returns JSON: { plan, action }            │
                   │   • Action: hold | open | close                │
                   └─────────────────┬──────────────────────────────┘
                                     │
                                     ▼
                   ┌──────────────────────────────────────────────┐
                   │          6. RISK GUARDS                        │
                   │   • Max 10% portfolio per trade                │
                   │   • Max 5 open positions                       │
                   │   • $3,000 daily loss limit                    │
                   │   • Min $0.10 limit price (illiquid guard)    │
                   │   • Max 10 contracts per trade                 │
                   └─────────────────┬──────────────────────────────┘
                                     │
                                     ▼
                   ┌──────────────────────────────────────────────┐
                   │          7. EXECUTE TRADE                      │
                   │   • Build OCC option symbol                   │
                   │   • Place limit order via Tradier API           │
                   │   • Record trade in trades.jsonl                │
                   │   • Update portfolio.json                     │
                   │   • Log full plan + action + result            │
                   └──────────────────────────────────────────────┘
```

---

## Project Structure

```
trading-bot/
├── .env                    # Your credentials (gitignored — never committed)
├── .env.example            # Template — copy to .env and fill in
├── .gitignore
├── requirements.txt
├── README.md
├── dashboard.py            # Web dashboard (http://localhost:8765)
├── data-flow.html          # Architecture diagram
├── design.html             # Design overview
└── scripts/
    ├── tradier_client.py   # Tradier API client (balances, orders, quotes, options)
    ├── executor.py         # Trade execution + risk guards + state management
    ├── fetch_market_data.py# Live market data (SPY, VIX, Fear&Greed, yields, RSI)
    ├── fetch_news.py       # Financial news from RSS feeds (Yahoo, CNBC, Reuters)
    ├── build_prompt.py     # LLM prompt assembly with prompt engineering techniques
    ├── collect_data.py     # Pre-run context script for cron jobs
    ├── run_trading_cycle.py# Master script — runs one full hourly cycle
    └── test_api.py         # Tradier API connectivity test
```

---

## Getting Started

### Prerequisites

- **Python 3.9+**
- A **Tradier account** (free [sandbox account](https://tradier.com/products/sandbox-brokerage-api) with $100k virtual money)
- An **LLM API key** (OpenRouter, Anthropic, OpenAI, or any compatible provider)

### Installation

1. **Clone the repo**

   ```bash
   git clone https://github.com/ramindersingh2/trading-bot.git
   cd trading-bot
   ```

2. **Create a virtual environment**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**

   The core bot uses only Python stdlib (no pip installs needed). If you want the dashboard with rich formatting:

   ```bash
   pip install rich
   ```

4. **Configure your credentials**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and fill in your values:

   ```env
   # Tradier Sandbox (free virtual trading account)
   TRADIER_SANDBOX_TOKEN=your_tradier_api_token_here
   TRADIER_SANDBOX_ACCOUNT=your_account_id_here
   TRADING_MODE=sandbox

   # Tradier Production (ONLY when ready for live trading)
   # TRADIER_PROD_TOKEN=your_prod_token
   # TRADIER_PROD_ACCOUNT=your_prod_account_id
   # TRADING_MODE=production
   ```

5. **Test the Tradier connection**

   ```bash
   python scripts/test_api.py
   ```

   You should see your account balance, positions, and a live SPY quote.

---

## Usage

### Run a single trading cycle (dry run)

Test the full pipeline without placing real orders:

```bash
python scripts/run_trading_cycle.py --dry-run
```

### Run a live trading cycle

```bash
python scripts/run_trading_cycle.py
```

This will:
1. Check if the market is open
2. Fetch market data + news
3. Build the LLM prompt
4. Send to your LLM for analysis
5. Parse the response and execute the trade (with risk guards)

### Run on a schedule (cron)

```bash
# Every hour during market hours (Mon–Fri, 9:30am–4pm ET)
*/30 6-13 * * 1-5 cd ~/trading-bot && source venv/bin/activate && python scripts/run_trading_cycle.py
```

Or if using [Hermes Agent](https://hermes-agent.nousresearch.com):

```bash
hermes cron create "30 6-13 * * 1-5" \
  --name "trading-bot-hourly" \
  --script scripts/collect_data.py \
  --prompt "Execute your trading workflow..."
```

### Launch the dashboard

```bash
python dashboard.py
```

Then open http://localhost:8765 to see your portfolio, open positions, trade history, and LLM decision logs.

### Test individual components

```bash
# Test Tradier API connectivity
python scripts/test_api.py

# Fetch market data and print to stdout
python scripts/collect_data.py

# Test the Tradier client directly
python scripts/tradier_client.py
```

---

## Risk Guards

The executor includes built-in safety checks before any trade is placed:

| Guard | Limit | Description |
|-------|-------|-------------|
| Max position size | 10% of portfolio | No single trade exceeds 10% of total equity |
| Max open positions | 5 | Never hold more than 5 positions at once |
| Daily loss limit | $3,000 | Stop trading if $3k+ lost in a day |
| Min limit price | $0.10 | Skip illiquid options (under 10 cents) |
| Max quantity | 10 contracts | Hard cap on contracts per trade |

These limits are defined in `executor.py` and can be adjusted:

```python
RISK = {
    "max_position_pct":   0.10,
    "max_open_positions": 5,
    "max_daily_loss":     3000,
    "min_limit_price":    0.10,
    "max_quantity":       10,
}
```

---

## State Files

The bot maintains local state files (gitignored) in `state/`:

| File | Purpose |
|------|---------|
| `portfolio.json` | Current portfolio snapshot (cash, equity, positions) |
| `trades.jsonl` | All trades (open and closed) with entry/exit prices, P&L |
| `plans.jsonl` | LLM decision logs (market thesis, action taken, result) |
| `market_snapshots.jsonl` | Historical market data snapshots |
| `news_cache.json` | Cached news articles |

---

## LLM Integration

The bot sends a structured prompt to your LLM with:
- Expert trader persona ("You are a senior SPY options trader...")
- Chain-of-thought reasoning trigger
- Live market data context
- Portfolio state and open positions
- Recent trade history and win rate
- Required JSON output format

The LLM responds with:

```json
{
  "plan": {
    "market_thesis": "SPY is overbought at RSI 74...",
    "confidence": "high",
    "steps": ["1. RSI overbought...", "2. Near resistance..."]
  },
  "action": {
    "type": "open",
    "option_type": "call",
    "strike": 760,
    "expiry": "2026-06-06",
    "quantity": 1,
    "limit_price": 3.50,
    "reason": "Bullish trend with low VIX..."
  }
}
```

The executor parses this, runs risk checks, and places the order.

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.9+ (stdlib only — no heavy deps) |
| Brokerage API | [Tradier](https://tradier.com) Sandbox API |
| LLM Provider | Any (OpenRouter, Anthropic, OpenAI, local) |
| Market Data | Tradier + alternative.me (Fear & Greed) + FRED (yields) |
| News | RSS feeds (Yahoo Finance, CNBC, Reuters, MarketWatch) |
| Dashboard | Built-in HTTP server (Python `http.server`) |
| Scheduling | Cron / Hermes Agent cron jobs |

---

## Disclaimer

This bot trades on a **Tradier sandbox account** with $100k virtual money. It does not place real trades unless you switch to production mode.

Trading options involves significant risk of loss. This software is for educational purposes only and is not financial advice. Always test in sandbox mode first. The authors are not responsible for any financial losses.

---

## License

Distributed under the MIT License. See `LICENSE` for more information.