#!/usr/bin/env python3
"""
run_trading_cycle.py — Master script. Runs one full hourly cycle.

Flow:
  1. Check market hours (skip if closed)
  2. Fetch fresh market data + save snapshot
  3. Fetch latest news
  4. Build the full LLM prompt
  5. Send prompt to LLM (Hermes / Claude Sonnet)
  6. Parse LLM response, execute trade via executor
  7. Log the full run

Usage:
  python3 scripts/run_trading_cycle.py           # live run
  python3 scripts/run_trading_cycle.py --dry-run # no real orders placed
  python3 scripts/run_trading_cycle.py --force   # skip market hours check
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, time as dtime
from pathlib import Path
import urllib.request
import urllib.error

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent))

from fetch_market_data import fetch_all_market_data, save_snapshot
from fetch_news        import fetch_top_news, format_news_for_prompt
from build_prompt      import build_prompt
from executor          import execute

STATE_DIR = Path.home() / "trading-bot" / "state"
LOGS_DIR  = Path.home() / "trading-bot" / "logs"

# ── Market Hours Check ────────────────────────────────────────────────────────
def is_market_open() -> tuple[bool, str]:
    """
    Returns (is_open, reason_string).
    US market: Mon-Fri 9:30am - 4:00pm Eastern.
    """
    try:
        # Get current Eastern time via world time API
        req = urllib.request.Request(
            "http://worldtimeapi.org/api/timezone/America/New_York",
            headers={"User-Agent": "trading-bot/1.0"}
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        dt_str = data.get("datetime", "")
        # Parse: "2026-05-31T10:30:00.123456-04:00"
        dt = datetime.fromisoformat(dt_str)
    except Exception:
        # Fallback: use local time + assume EST offset
        dt = datetime.now(timezone.utc)

    weekday    = dt.weekday()  # 0=Mon, 6=Sun
    market_open  = dtime(9, 30)
    market_close = dtime(16, 0)
    current_time = dt.time().replace(tzinfo=None)

    if weekday >= 5:
        return False, f"Weekend ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][weekday]})"
    if current_time < market_open:
        return False, f"Pre-market (market opens at 9:30am ET, now {current_time.strftime('%H:%M')} ET)"
    if current_time >= market_close:
        return False, f"After-hours (market closed at 4:00pm ET, now {current_time.strftime('%H:%M')} ET)"

    return True, f"Market open ({current_time.strftime('%H:%M')} ET)"

# ── Call LLM ──────────────────────────────────────────────────────────────────
def call_llm(prompt: str) -> str:
    """
    Sends prompt to Claude Sonnet via Hermes and returns the response text.
    Uses hermes CLI in single-query mode.
    """
    print("  Sending prompt to LLM (Claude Sonnet 4)...")

    # Save prompt to temp file for hermes to read
    prompt_file = STATE_DIR / "latest_prompt.txt"
    prompt_file.write_text(prompt)

    try:
        result = subprocess.run(
            ["hermes", "chat", "-q", prompt, "--provider", "openrouter", "-m", "anthropic/claude-sonnet-4"],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max
        )
        if result.returncode != 0:
            print(f"  [WARN] Hermes returned code {result.returncode}")
            print(f"  stderr: {result.stderr[:500]}")

        response = result.stdout.strip()
        if not response:
            raise RuntimeError("Empty response from LLM")

        return response

    except subprocess.TimeoutExpired:
        raise RuntimeError("LLM call timed out after 5 minutes")
    except FileNotFoundError:
        raise RuntimeError("hermes CLI not found — is Hermes installed?")

# ── Log Run ───────────────────────────────────────────────────────────────────
def log_run(run_data: dict):
    """Appends the full run record to logs/runs.jsonl."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "runs.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(run_data) + "\n")

    # Also save latest run as pretty JSON for easy inspection
    latest_path = LOGS_DIR / "latest_run.json"
    latest_path.write_text(json.dumps(run_data, indent=2))

# ── Main Cycle ────────────────────────────────────────────────────────────────
def run_cycle(dry_run: bool = False, force: bool = False):
    start_time = datetime.now(timezone.utc)
    run_id     = f"run_{start_time.strftime('%Y%m%d_%H%M%S')}"

    print(f"\n{'='*60}")
    print(f"TRADING BOT CYCLE — {run_id}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE PAPER TRADING'}")
    print(f"{'='*60}\n")

    run_log = {
        "run_id":    run_id,
        "started_at": start_time.isoformat(),
        "dry_run":   dry_run,
        "steps":     {},
        "result":    None,
        "error":     None,
    }

    # ── Step 1: Market Hours ──────────────────────────────────────────────────
    print("[STEP 1] Checking market hours...")
    is_open, market_status = is_market_open()
    print(f"  Status: {market_status}")
    run_log["steps"]["market_status"] = market_status

    if not is_open and not force:
        print(f"\n  Market is closed. Skipping cycle.")
        print(f"  Use --force to run anyway (for testing).\n")
        run_log["result"] = "skipped_market_closed"
        log_run(run_log)
        return run_log

    # ── Step 2: Fetch Market Data ─────────────────────────────────────────────
    print("\n[STEP 2] Fetching market data...")
    try:
        snapshot = fetch_all_market_data("SPY")
        save_snapshot(snapshot)
        run_log["steps"]["market_data"] = {
            "spy_price":        snapshot.get("price"),
            "vix":              snapshot.get("vix"),
            "fear_greed":       snapshot.get("fear_greed_score"),
            "rsi":              snapshot.get("rsi_14"),
        }
        print(f"  SPY: ${snapshot.get('price')}  VIX: {snapshot.get('vix')}  "
              f"F&G: {snapshot.get('fear_greed_score')}  RSI: {snapshot.get('rsi_14')}")
    except Exception as e:
        print(f"  [WARN] Market data fetch failed: {e}. Continuing with cached data.")
        run_log["steps"]["market_data"] = {"error": str(e)}

    # ── Step 3: Fetch News ────────────────────────────────────────────────────
    print("\n[STEP 3] Fetching financial news...")
    try:
        news = fetch_top_news(days_back=3, max_items=5)
        # Save for build_prompt to pick up
        news_path = STATE_DIR / "latest_news.json"
        news_path.write_text(json.dumps(news))
        run_log["steps"]["news_count"] = len(news)
        print(f"  Fetched {len(news)} relevant articles")
        for n in news[:3]:
            print(f"    - {n['title'][:80]}")
    except Exception as e:
        print(f"  [WARN] News fetch failed: {e}. Continuing without news.")
        run_log["steps"]["news"] = {"error": str(e)}

    # ── Step 4: Build Prompt ──────────────────────────────────────────────────
    print("\n[STEP 4] Building LLM prompt...")
    try:
        prompt = build_prompt()
        run_log["steps"]["prompt_chars"]  = len(prompt)
        run_log["steps"]["prompt_tokens"] = len(prompt) // 4
        print(f"  Prompt: {len(prompt)} chars (~{len(prompt)//4} tokens)")
    except Exception as e:
        err = f"Prompt build failed: {e}"
        print(f"  ❌ {err}")
        run_log["error"] = err
        run_log["result"] = "error"
        log_run(run_log)
        return run_log

    # ── Step 5: Call LLM ──────────────────────────────────────────────────────
    print("\n[STEP 5] Calling LLM...")
    try:
        llm_response = call_llm(prompt)
        run_log["steps"]["llm_response_chars"] = len(llm_response)
        print(f"  LLM responded: {len(llm_response)} chars")

        # Save raw response for debugging
        resp_path = STATE_DIR / "latest_llm_response.txt"
        resp_path.write_text(llm_response)
        print(f"  Response saved → {resp_path}")
    except Exception as e:
        err = f"LLM call failed: {e}"
        print(f"  ❌ {err}")
        run_log["error"] = err
        run_log["result"] = "error"
        log_run(run_log)
        return run_log

    # ── Step 6: Execute ───────────────────────────────────────────────────────
    print("\n[STEP 6] Executing trade decision...")
    try:
        result = execute(llm_response, dry_run=dry_run)
        run_log["result"]           = result.get("status")
        run_log["steps"]["execute"] = result
        print(f"  Execution result: {result.get('status')}")
    except Exception as e:
        err = f"Execution failed: {e}"
        print(f"  ❌ {err}")
        run_log["error"] = err
        run_log["result"] = "error"

    # ── Finish ────────────────────────────────────────────────────────────────
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()
    run_log["ended_at"]       = end_time.isoformat()
    run_log["duration_secs"]  = round(duration, 1)

    log_run(run_log)

    print(f"\n{'='*60}")
    print(f"CYCLE COMPLETE in {duration:.1f}s")
    print(f"Result: {run_log['result']}")
    print(f"Log: {LOGS_DIR}/latest_run.json")
    print(f"{'='*60}\n")

    return run_log


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one trading bot cycle")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and plan but do NOT place real orders")
    parser.add_argument("--force",   action="store_true",
                        help="Skip market hours check (for testing)")
    args = parser.parse_args()

    run_cycle(dry_run=args.dry_run, force=args.force)
