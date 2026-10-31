#!/usr/bin/env python3
"""
Trading Bot Dashboard — serves a live dashboard at http://localhost:8765
Reads state files and cron outputs. Auto-refreshes every 30 seconds.

Usage: python3 ~/trading-bot/dashboard.py
"""

import json
import re
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATE_DIR  = Path.home() / "trading-bot" / "state"
CRON_DIR   = Path.home() / ".hermes" / "cron" / "output" / "ad77e559cad4"
PORT       = 8765

# ── Data loaders ──────────────────────────────────────────────────────────────

def load_portfolio():
    p = STATE_DIR / "portfolio.json"
    return json.loads(p.read_text()) if p.exists() else {}

def load_trades():
    p = STATE_DIR / "trades.jsonl"
    if not p.exists(): return []
    trades = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try: trades.append(json.loads(line))
            except: pass
    return trades

def load_plans():
    p = STATE_DIR / "plans.jsonl"
    if not p.exists(): return []
    plans = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try: plans.append(json.loads(line))
            except: pass
    return sorted(plans, key=lambda x: x.get("timestamp",""), reverse=True)

def load_market_snapshot():
    p = STATE_DIR / "market_snapshots.jsonl"
    if not p.exists(): return {}
    last = ""
    for line in p.read_text().splitlines():
        if line.strip(): last = line.strip()
    return json.loads(last) if last else {}

def load_cron_outputs():
    """Load all cron run output files, newest first."""
    if not CRON_DIR.exists(): return []
    files = sorted(CRON_DIR.glob("*.md"), reverse=True)
    results = []
    for f in files:
        content = f.read_text()
        # Parse run time from filename: 2026-05-31_18-37-37.md
        name = f.stem  # 2026-05-31_18-37-37
        try:
            dt = datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
            run_time = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            run_time = name

        # Extract response section
        response = ""
        if "## Response" in content:
            response = content.split("## Response", 1)[1].strip()

        # Check if script errored
        script_error = ""
        if "## Script Error" in content:
            m = re.search(r"```\n(.*?)\n```", content, re.DOTALL)
            if m: script_error = m.group(1).strip()

        results.append({
            "filename": f.name,
            "run_time": run_time,
            "response": response,
            "script_error": script_error,
            "size": f.stat().st_size,
        })
    return results

def build_runs_table(plans, trades, cron_outputs):
    """
    Build a unified runs table by correlating:
    - cron output files (one per run, gives us run time + full response)
    - plans.jsonl entries (give us action, thesis, confidence)
    - trades.jsonl (give us which trades were affected per run)
    """
    runs = []

    # Build trade lookup by plan_id
    trades_by_plan = {}
    for t in trades:
        pid = t.get("plan_id")
        if pid:
            trades_by_plan.setdefault(pid, []).append(t)

    # Build plans lookup by approximate timestamp (match to cron run time)
    # Plans within the same cron run share close timestamps
    # Group plans by minute window to correlate with cron outputs
    plans_by_minute = {}
    for p in plans:
        ts = p.get("timestamp","")[:16]  # "2026-05-31T18:37"
        plans_by_minute.setdefault(ts, []).append(p)

    # Process each cron output
    for i, run in enumerate(cron_outputs):
        run_id = f"RUN-{len(cron_outputs) - i:03d}"
        run_dt_str = run["run_time"]  # "2026-05-31 18:37:37"
        # Convert to ISO minute key
        try:
            dt = datetime.strptime(run_dt_str, "%Y-%m-%d %H:%M:%S")
            minute_key = dt.strftime("%Y-%m-%dT%H:%M")
            # Also check ±2 minutes for plans
            candidate_minutes = [
                (dt.replace(minute=max(0,dt.minute-m))).strftime("%Y-%m-%dT%H:%M")
                for m in range(0,5)
            ]
        except:
            minute_key = ""
            candidate_minutes = []

        # Find matching plans
        matched_plans = []
        for cmin in candidate_minutes:
            if cmin in plans_by_minute:
                matched_plans.extend(plans_by_minute[cmin])
        # Deduplicate
        seen_ids = set()
        deduped = []
        for p in matched_plans:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                deduped.append(p)
        matched_plans = deduped

        # Collect affected trades from matched plans
        affected_trades = []
        for p in matched_plans:
            for t in trades_by_plan.get(p["id"], []):
                if t["id"] not in [x["id"] for x in affected_trades]:
                    affected_trades.append(t)

        # Also catch trades with no plan_id that opened/closed around this run time
        # (from the older executor.py-based runs)
        if not affected_trades and not matched_plans:
            # Try matching trades by opened_at or closed_at timestamp
            for t in trades:
                t_ts = t.get("opened_at","") or t.get("closed_at","")
                if t_ts[:16].replace("T"," ") == run_dt_str[:16]:
                    affected_trades.append(t)

        # Determine overall run action + status
        actions = [p.get("action_taken","") for p in matched_plans]
        action  = ", ".join(set(a for a in actions if a)) or _infer_action_from_response(run["response"])

        # Compute net P&L impact this run
        run_pnl = sum(t.get("pnl") or 0 for t in affected_trades if t.get("status") != "open")

        # Build thesis summary
        thesis = ""
        if matched_plans:
            thesis = matched_plans[0].get("market_thesis","")
        elif run["response"]:
            # Extract first meaningful sentence from response
            lines = [l.strip() for l in run["response"].splitlines() if l.strip() and not l.startswith("#")]
            thesis = lines[0][:200] if lines else ""

        # Confidence
        confidences = [p.get("confidence","") for p in matched_plans if p.get("confidence")]
        confidence = confidences[0] if confidences else ""

        # Research done
        research = []
        for p in matched_plans:
            rd = p.get("research_done",[])
            if isinstance(rd, list): research.extend(rd)
            elif rd: research.append(str(rd))
        research = research[:4]  # cap at 4 items

        # Steps from the 4-step plan
        steps = []
        for p in matched_plans:
            st = p.get("steps", [])
            if isinstance(st, list): steps.extend(st)
            elif st: steps.append(str(st))
        steps = steps[:6]  # cap at 6

        runs.append({
            "run_id":          run_id,
            "run_time":        run_dt_str,
            "action":          action,
            "confidence":      confidence,
            "thesis":          thesis[:300],
            "research":        research,
            "affected_trades": [_summarize_trade(t) for t in affected_trades],
            "run_pnl":         run_pnl if affected_trades else None,
            "script_error":    bool(run["script_error"]),
            "plan_count":      len(matched_plans),
            "steps":           steps,
            "full_response":   run["response"][:2000],
        })

    return runs

def _infer_action_from_response(response):
    """Guess the action from the response text if no plan was matched."""
    r = response.lower()
    if "position opened" in r or "trade executed" in r or "buy" in r[:200]:
        return "open"
    if "position closed" in r or "taking profit" in r or "cutting loss" in r:
        return "close"
    if "hold" in r[:300] or "holding" in r[:300]:
        return "hold"
    return "—"

def _summarize_trade(t):
    pnl = t.get("pnl")
    return {
        "id":          t.get("id",""),
        "symbol":      t.get("option_symbol","")[-12:] if t.get("option_symbol") else "",
        "type":        t.get("option_type",""),
        "strike":      t.get("strike",""),
        "expiry":      t.get("expiry",""),
        "qty":         t.get("quantity",""),
        "status":      t.get("status",""),
        "pnl":         pnl,
        "cost":        t.get("cost"),
    }

def build_api_data():
    trades  = load_trades()
    plans   = load_plans()
    cron_outputs = load_cron_outputs()

    open_trades   = [t for t in trades if t.get("status") == "open"]
    closed_trades = [t for t in trades if t.get("status") != "open"]
    wins   = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed_trades if (t.get("pnl") or 0) <= 0]

    pnl_timeline = []
    running = 0
    for t in sorted(closed_trades, key=lambda x: x.get("closed_at","") or ""):
        running += t.get("pnl") or 0
        pnl_timeline.append({
            "date":      (t.get("closed_at") or "")[:10],
            "pnl":       round(running, 2),
            "trade_pnl": t.get("pnl") or 0,
        })

    snap = load_market_snapshot()
    port = load_portfolio()
    runs = build_runs_table(plans, trades, cron_outputs)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": {
            "cash":      port.get("total_cash", 100000),
            "equity":    port.get("total_equity", 100000),
            "option_bp": port.get("option_buying_power", 100000),
            "open_pl":   port.get("open_pl", 0),
            "close_pl":  port.get("close_pl", 0),
        },
        "stats": {
            "total_trades": len(closed_trades),
            "open_trades":  len(open_trades),
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins)/len(closed_trades)*100,1) if closed_trades else 0,
            "total_pnl":    round(sum(t.get("pnl") or 0 for t in closed_trades), 2),
            "total_runs":   len(cron_outputs),
        },
        "market": {
            "price":            snap.get("price"),
            "change_pct":       snap.get("change_pct"),
            "vix":              snap.get("vix"),
            "fear_greed":       snap.get("fear_greed_score"),
            "fear_greed_label": snap.get("fear_greed_label"),
            "rsi":              snap.get("rsi_14"),
            "ma20":             snap.get("ma_20"),
            "ma50":             snap.get("ma_50"),
            "support":          snap.get("support_20d"),
            "resistance":       snap.get("resistance_20d"),
            "treasury_10y":     snap.get("treasury_10y"),
            "treasury_2y":      snap.get("treasury_2y"),
            "week52_high":      snap.get("week52_high"),
            "week52_low":       snap.get("week52_low"),
            "timestamp":        snap.get("timestamp","")[:19],
        },
        "open_positions":  open_trades,
        "closed_trades":   list(reversed(closed_trades))[:20],
        "runs":            runs,
        "pnl_timeline":    pnl_timeline,
        "latest_output":   runs[0]["full_response"] if runs else "No runs yet.",
    }

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Trading Bot — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'JetBrains Mono',monospace;background:#020617;color:#e2e8f0;min-height:100vh;padding:1.5rem}
.container{max-width:1500px;margin:0 auto}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.5rem;flex-wrap:wrap;gap:1rem}
.header-left{display:flex;align-items:center;gap:1rem}
.pulse{width:12px;height:12px;background:#34d399;border-radius:50%;animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(52,211,153,.4)}50%{opacity:.7;box-shadow:0 0 0 6px rgba(52,211,153,0)}}
h1{font-size:1.3rem;font-weight:700;color:#f8fafc}
.timestamp{font-size:.7rem;color:#64748b;margin-top:.2rem}
.refresh-btn{background:rgba(52,211,153,.15);border:1px solid #34d399;color:#34d399;padding:.4rem 1rem;border-radius:.5rem;cursor:pointer;font-family:inherit;font-size:.8rem}
.refresh-btn:hover{background:rgba(52,211,153,.25)}

.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:1rem}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem}
.grid-full{margin-bottom:1rem}
@media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.grid-4,.grid-2{grid-template-columns:1fr}}

.card{background:rgba(15,23,42,.7);border:1px solid #1e293b;border-radius:.75rem;padding:1.2rem}
.card-title{font-size:.7rem;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:.6rem}
.card-value{font-size:1.5rem;font-weight:700;color:#f8fafc}
.card-sub{font-size:.75rem;color:#94a3b8;margin-top:.2rem}
.card-green .card-value{color:#34d399}
.card-red .card-value{color:#fb7185}

.section-title{font-size:.75rem;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:.75rem;display:flex;align-items:center;gap:.5rem}
.section-title::after{content:'';flex:1;height:1px;background:#1e293b}

.market-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.6rem}
.market-item{background:rgba(15,23,42,.5);border:1px solid #1e293b;border-radius:.5rem;padding:.7rem}
.market-label{font-size:.65rem;color:#64748b;margin-bottom:.3rem}
.market-val{font-size:1rem;font-weight:700;color:#f8fafc}
.market-sub{font-size:.65rem;margin-top:.1rem;color:#64748b}

.chart-wrap{height:140px;width:100%;overflow:hidden}

.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.75rem}
th{text-align:left;color:#64748b;font-weight:600;padding:.5rem .75rem;border-bottom:1px solid #1e293b;white-space:nowrap}
td{padding:.5rem .75rem;border-bottom:1px solid #0f172a;color:#cbd5e1}
tr:hover td{background:rgba(255,255,255,.02)}

.badge{display:inline-block;padding:.15rem .5rem;border-radius:.3rem;font-size:.65rem;font-weight:600;white-space:nowrap}
.badge-green{background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.badge-red{background:rgba(251,113,133,.15);color:#fb7185;border:1px solid rgba(251,113,133,.3)}
.badge-amber{background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.3)}
.badge-blue{background:rgba(34,211,238,.15);color:#22d3ee;border:1px solid rgba(34,211,238,.3)}
.badge-gray{background:rgba(100,116,139,.15);color:#94a3b8;border:1px solid rgba(100,116,139,.3)}
.badge-violet{background:rgba(167,139,250,.15);color:#a78bfa;border:1px solid rgba(167,139,250,.3)}

.pos-val{color:#34d399}
.neg-val{color:#fb7185}
.neu-val{color:#94a3b8}

/* Run detail drawer */
.run-row{cursor:pointer;transition:background .15s}
.run-row:hover td{background:rgba(34,211,238,.04)!important}
.run-row.active td{background:rgba(34,211,238,.07)!important}
.run-detail-row{display:none}
.run-detail-row.open{display:table-row}
.run-detail-cell{padding:0!important;border-bottom:2px solid #22d3ee!important}
.run-detail-inner{padding:1rem 1.5rem;background:rgba(8,51,68,.3)}

.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem}
@media(max-width:700px){.detail-grid{grid-template-columns:1fr}}
.detail-box{background:rgba(15,23,42,.5);border:1px solid #1e293b;border-radius:.5rem;padding:.9rem}
.detail-box-title{font-size:.65rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:.6rem}
.detail-thesis{font-size:.8rem;color:#cbd5e1;line-height:1.6}
.detail-research li{font-size:.75rem;color:#94a3b8;margin-bottom:.25rem;line-height:1.4}
.detail-research{padding-left:1rem}
.detail-output{font-size:.75rem;color:#cbd5e1;line-height:1.6;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto}
.detail-output strong,.detail-output b{color:#fbbf24}

.fg-bar{height:8px;border-radius:4px;background:linear-gradient(to right,#fb7185,#fbbf24,#34d399);margin:4px 0;position:relative}
.fg-indicator{position:absolute;top:-3px;width:14px;height:14px;border-radius:50%;background:#f8fafc;border:2px solid #020617;transform:translateX(-50%);transition:left .5s}

.no-data{color:#475569;font-size:.8rem;text-align:center;padding:1.5rem}
.loading{text-align:center;padding:3rem;color:#475569;font-size:.85rem}

/* Tabs */
.tabs{display:flex;gap:.5rem;margin-bottom:1rem;border-bottom:1px solid #1e293b;padding-bottom:0}
.tab{padding:.5rem 1rem;font-size:.75rem;font-weight:600;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.active{color:#22d3ee;border-bottom-color:#22d3ee}
.tab:hover:not(.active){color:#94a3b8}
.tab-content{display:none}
.tab-content.active{display:block}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <div class="header-left">
      <div class="pulse"></div>
      <div>
        <h1>Hermes Trading Bot</h1>
        <div class="timestamp" id="ts">Loading...</div>
      </div>
    </div>
    <button class="refresh-btn" onclick="loadData()">⟳ Refresh</button>
  </div>

  <div id="content"><div class="loading">Loading trading data...</div></div>
</div>

<script>
async function loadData() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    render(d);
  } catch(e) {
    document.getElementById('content').innerHTML =
      `<div class="loading" style="color:#fb7185">Error: ${e.message}</div>`;
  }
}

function fmt$(n, always=false) {
  if(n==null && !always) return 'N/A';
  if(n==null) return '$0.00';
  const abs = Math.abs(n);
  const s = abs>=1000?'$'+abs.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}):'$'+abs.toFixed(2);
  return n<0?'-'+s:s;
}
function fmtPct(n){return n==null?'N/A':(n>0?'+':'')+n.toFixed(2)+'%'}
function cls(n){return n>0?'pos-val':n<0?'neg-val':'neu-val'}

function actionBadge(a){
  if(!a||a==='—') return `<span class="badge badge-gray">—</span>`;
  const l=a.toLowerCase();
  if(l.includes('open')||l.includes('buy')) return `<span class="badge badge-green">${a.toUpperCase()}</span>`;
  if(l.includes('close')||l.includes('sell')) return `<span class="badge badge-red">${a.toUpperCase()}</span>`;
  if(l.includes('hold')) return `<span class="badge badge-amber">HOLD</span>`;
  return `<span class="badge badge-gray">${a}</span>`;
}
function confBadge(c){
  if(!c) return '';
  if(c==='high') return `<span class="badge badge-green">${c}</span>`;
  if(c==='low')  return `<span class="badge badge-red">${c}</span>`;
  return `<span class="badge badge-amber">${c}</span>`;
}
function typeBadge(t){
  if(!t) return '';
  return t.toLowerCase()==='call'?`<span class="badge badge-blue">CALL</span>`:`<span class="badge badge-red">PUT</span>`;
}

function render(d) {
  document.getElementById('ts').textContent = 'Updated ' + new Date(d.generated_at).toLocaleString();
  const p=d.portfolio, s=d.stats, m=d.market;
  const totalPnl=s.total_pnl||0;
  const fgScore=m.fear_greed||50;

  document.getElementById('content').innerHTML = `
  <div class="grid-4">
    <div class="card">
      <div class="card-title">Cash Available</div>
      <div class="card-value">${fmt$(p.cash)}</div>
      <div class="card-sub">Option BP: ${fmt$(p.option_bp)}</div>
    </div>
    <div class="card">
      <div class="card-title">Total Equity</div>
      <div class="card-value">${fmt$(p.equity)}</div>
      <div class="card-sub">Started at $100,000.00</div>
    </div>
    <div class="card ${totalPnl>=0?'card-green':'card-red'}">
      <div class="card-title">Total Realized P&L</div>
      <div class="card-value">${fmt$(totalPnl)}</div>
      <div class="card-sub">${s.total_trades} closed &nbsp;·&nbsp; ${s.open_trades} open</div>
    </div>
    <div class="card ${s.win_rate>=50?'card-green':'card-red'}">
      <div class="card-title">Win Rate &nbsp;·&nbsp; ${s.total_runs||0} Runs</div>
      <div class="card-value">${s.win_rate}%</div>
      <div class="card-sub">${s.wins}W / ${s.losses}L</div>
    </div>
  </div>

  <div class="card grid-full">
    <div class="section-title">Live Market <span style="color:#64748b;font-size:.65rem;font-weight:400">${m.timestamp||''}</span></div>
    <div class="market-grid">
      <div class="market-item">
        <div class="market-label">SPY</div>
        <div class="market-val">$${m.price||'—'}</div>
        <div class="market-sub ${(m.change_pct||0)>=0?'pos-val':'neg-val'}">${fmtPct(m.change_pct)}</div>
      </div>
      <div class="market-item">
        <div class="market-label">VIX</div>
        <div class="market-val" style="color:${(m.vix||0)>20?'#fb7185':(m.vix||0)<15?'#34d399':'#fbbf24'}">${m.vix||'—'}</div>
        <div class="market-sub">${(m.vix||0)<15?'Calm':(m.vix||0)>25?'High fear':'Normal'}</div>
      </div>
      <div class="market-item">
        <div class="market-label">Fear & Greed ${fgScore}</div>
        <div class="market-val" style="color:${fgScore<30?'#fb7185':fgScore>70?'#34d399':'#fbbf24'}">${m.fear_greed_label||'—'}</div>
        <div class="fg-bar"><div class="fg-indicator" style="left:${fgScore}%"></div></div>
      </div>
      <div class="market-item">
        <div class="market-label">RSI (14)</div>
        <div class="market-val" style="color:${(m.rsi||50)>70?'#fb7185':(m.rsi||50)<30?'#34d399':'#f8fafc'}">${m.rsi||'—'}</div>
        <div class="market-sub">${(m.rsi||50)>70?'⚠ Overbought':(m.rsi||50)<30?'⚠ Oversold':'Neutral'}</div>
      </div>
      <div class="market-item">
        <div class="market-label">MA 20 / 50</div>
        <div class="market-val" style="font-size:.85rem">$${m.ma20||'—'} / $${m.ma50||'—'}</div>
        <div class="market-sub ${m.price>m.ma20?'pos-val':'neg-val'}">${m.price&&m.ma20?(m.price>m.ma20?'Above':'Below')+' MA20 by $'+Math.abs(m.price-m.ma20).toFixed(2):'—'}</div>
      </div>
      <div class="market-item">
        <div class="market-label">Support / Resist</div>
        <div class="market-val" style="font-size:.85rem">$${m.support||'—'} / $${m.resistance||'—'}</div>
        <div class="market-sub">20-day range</div>
      </div>
      <div class="market-item">
        <div class="market-label">10Y / 2Y Yield</div>
        <div class="market-val" style="font-size:.85rem">${m.treasury_10y||'—'}% / ${m.treasury_2y||'—'}%</div>
        <div class="market-sub">${m.treasury_2y&&m.treasury_10y?(m.treasury_2y>m.treasury_10y?'⚠ Inverted':'Normal'):'—'}</div>
      </div>
      <div class="market-item">
        <div class="market-label">52W High / Low</div>
        <div class="market-val" style="font-size:.85rem">$${m.week52_high||'—'}</div>
        <div class="market-sub">Low: $${m.week52_low||'—'}</div>
      </div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="section-title">Cumulative P&L</div>
      ${d.pnl_timeline.length>0
        ? `<div class="chart-wrap"><canvas id="pnlChart" height="120"></canvas></div>`
        : `<div class="no-data">No closed trades yet</div>`}
    </div>
    <div class="card">
      <div class="section-title">Open Positions (${d.open_positions.length})</div>
      ${d.open_positions.length===0
        ? `<div class="no-data">No open positions</div>`
        : `<div class="table-wrap"><table>
          <tr><th>ID</th><th>Symbol</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Qty</th><th>Entry</th><th>Cost</th></tr>
          ${d.open_positions.map(t=>`<tr>
            <td style="color:#64748b">${t.id||'—'}</td>
            <td style="color:#22d3ee;font-size:.7rem">${(t.option_symbol||'')}</td>
            <td>${typeBadge(t.option_type)}</td>
            <td>$${t.strike||'—'}</td>
            <td>${t.expiry||'—'}</td>
            <td>${t.quantity||'—'}</td>
            <td>$${t.entry_price||'—'}</td>
            <td>${fmt$(t.cost)}</td>
          </tr>`).join('')}
        </table></div>`}
    </div>
  </div>

  <!-- TABS for runs + trades -->
  <div class="card grid-full">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('runs')">🤖 Bot Runs (${d.runs.length})</div>
      <div class="tab" onclick="switchTab('trades')">📊 Trade History (${d.closed_trades.length})</div>
    </div>

    <div id="tab-runs" class="tab-content active">
      ${renderRunsTable(d.runs)}
    </div>

    <div id="tab-trades" class="tab-content">
      ${d.closed_trades.length===0
        ? `<div class="no-data">No closed trades yet</div>`
        : `<div class="table-wrap"><table>
          <tr><th>ID</th><th>Symbol</th><th>Type</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Opened</th><th>Closed</th><th>Reason</th></tr>
          ${d.closed_trades.map(t=>{const pnl=t.pnl||0; return `<tr>
            <td style="color:#64748b">${t.id||'—'}</td>
            <td style="color:#22d3ee;font-size:.68rem">${t.option_symbol||''}</td>
            <td>${typeBadge(t.option_type)}</td>
            <td>${t.quantity||'—'}</td>
            <td>$${t.entry_price||'—'}</td>
            <td>$${t.exit_price||'—'}</td>
            <td class="${cls(pnl)}" style="font-weight:600">${fmt$(pnl)}</td>
            <td>${(t.opened_at||'').slice(0,10)}</td>
            <td>${(t.closed_at||'').slice(0,10)}</td>
            <td style="color:#64748b;max-width:180px;overflow:hidden;text-overflow:ellipsis">${(t.close_reason||t.reason||'').slice(0,60)}</td>
          </tr>`}).join('')}
        </table></div>`}
    </div>
  </div>
  `;

  if(d.pnl_timeline.length>0) drawChart(d.pnl_timeline);
}

function renderRunsTable(runs) {
  if(!runs||runs.length===0) return `<div class="no-data">No runs yet — bot fires Monday 9:30am PST</div>`;

  const rows = runs.map((r,i) => {
    const tradeCount = r.affected_trades.length;
    const hasTrades  = tradeCount > 0;
    const pnlStr     = r.run_pnl != null ? `<span class="${r.run_pnl>=0?'pos-val':'neg-val'}" style="font-weight:600">${fmt$(r.run_pnl)}</span>` : `<span class="neu-val">—</span>`;

    // Trade impact summary (inline chips)
    const tradeChips = r.affected_trades.slice(0,3).map(t=>{
      const sym = t.symbol || (t.type?`${t.type.toUpperCase()} $${t.strike}`:'');
      const pnl = t.pnl!=null?` ${t.pnl>=0?'+':''}$${Math.abs(t.pnl).toFixed(0)}`:'';
      const color = t.status==='open'?'badge-blue':t.pnl>=0?'badge-green':'badge-red';
      return `<span class="badge ${color}">${sym}${pnl}</span>`;
    }).join(' ');

    const statusDot = r.script_error
      ? `<span title="Script error" style="color:#fb7185">⚠</span>`
      : `<span style="color:#34d399">✓</span>`;

    return `
    <tr class="run-row" onclick="toggleRunDetail(${i})" id="run-row-${i}">
      <td style="color:#64748b;font-weight:600">${r.run_id}</td>
      <td style="color:#94a3b8;white-space:nowrap">${r.run_time}</td>
      <td>${statusDot}</td>
      <td>${actionBadge(r.action)}</td>
      <td>${confBadge(r.confidence)}</td>
      <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#cbd5e1" title="${r.thesis.replace(/"/g,'')}">${r.thesis.slice(0,120)}${r.thesis.length>120?'…':''}</td>
      <td>${hasTrades?tradeChips:`<span class="neu-val">none</span>`}</td>
      <td>${pnlStr}</td>
      <td style="color:#475569;font-size:.65rem">▼</td>
    </tr>
    <tr id="run-detail-${i}" class="run-detail-row">
      <td colspan="9" class="run-detail-cell">
        <div class="run-detail-inner">
          <div class="detail-grid">
            <div class="detail-box">
              <div class="detail-box-title">Market Thesis</div>
              <div class="detail-thesis">${r.thesis||'—'}</div>
            </div>
            <div class="detail-box">
              <div class="detail-box-title">Research Done</div>
              ${r.research.length?`<ul class="detail-research">${r.research.map(x=>`<li>${x}</li>`).join('')}</ul>`:`<div style="color:#475569;font-size:.8rem">None recorded</div>`}
            </div>
          </div>
          ${r.steps.length?`
          <div class="detail-box" style="margin-bottom:1rem">
            <div class="detail-box-title">📋 Plan Steps</div>
            <div class="detail-thesis" style="white-space:pre-wrap">${r.steps.map((s,i)=>`<b>Step ${i+1}:</b> ${s}`).join('<br>')}</div>
          </div>`:''}
          <div class="detail-grid">
            <div class="detail-box">
              <div class="detail-box-title">Trades Impacted (${tradeCount})</div>
              ${tradeCount===0
                ? `<div style="color:#475569;font-size:.8rem">No trades this run</div>`
                : `<div class="table-wrap"><table>
                  <tr><th>ID</th><th>Symbol</th><th>Type</th><th>Strike</th><th>Qty</th><th>Status</th><th>P&L</th></tr>
                  ${r.affected_trades.map(t=>`<tr>
                    <td style="color:#64748b">${t.id}</td>
                    <td style="color:#22d3ee;font-size:.68rem">${t.symbol}</td>
                    <td>${typeBadge(t.type)}</td>
                    <td>$${t.strike||'—'}</td>
                    <td>${t.qty||'—'}</td>
                    <td><span class="badge ${t.status==='open'?'badge-blue':'badge-amber'}">${t.status}</span></td>
                    <td class="${t.pnl!=null?cls(t.pnl):'neu-val'}">${t.pnl!=null?fmt$(t.pnl):'—'}</td>
                  </tr>`).join('')}
                </table></div>`}
            </div>
          </div>
          <div class="detail-box" style="margin-top:0">
            <div class="detail-box-title">Full Bot Output</div>
            <div class="detail-output">${escHtml(r.full_response)}</div>
          </div>
        </div>
      </td>
    </tr>`;
  }).join('');

  return `<div class="table-wrap"><table>
    <tr>
      <th>Run ID</th>
      <th>Time</th>
      <th>Status</th>
      <th>Action</th>
      <th>Confidence</th>
      <th>Thesis</th>
      <th>Trades Impacted</th>
      <th>Run P&L</th>
      <th></th>
    </tr>
    ${rows}
  </table></div>`;
}

function toggleRunDetail(i) {
  const row     = document.getElementById(`run-row-${i}`);
  const detail  = document.getElementById(`run-detail-${i}`);
  const isOpen  = detail.classList.contains('open');
  // Close all others
  document.querySelectorAll('.run-detail-row.open').forEach(el=>el.classList.remove('open'));
  document.querySelectorAll('.run-row.active').forEach(el=>el.classList.remove('active'));
  if(!isOpen) {
    detail.classList.add('open');
    row.classList.add('active');
    row.querySelector('td:last-child').textContent = '▲';
  } else {
    row.querySelector('td:last-child').textContent = '▼';
  }
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i)=>{
    const names=['runs','trades'];
    t.classList.toggle('active', names[i]===name);
  });
  document.querySelectorAll('.tab-content').forEach(el=>{
    el.classList.toggle('active', el.id===`tab-${name}`);
  });
}

function escHtml(s){
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
    .replace(/^###\s+(.+)$/gm,'<b style="color:#22d3ee">$1</b>')
    .replace(/^##\s+(.+)$/gm,'<b style="color:#a78bfa">$1</b>')
    .replace(/✅/g,'✅ ').replace(/🔴/g,'🔴 ').replace(/⚠/g,'⚠ ');
}

function drawChart(timeline) {
  const canvas = document.getElementById('pnlChart');
  if(!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth||400, H=120;
  canvas.width=W; canvas.height=H;
  const values=timeline.map(t=>t.pnl);
  const min=Math.min(0,...values), max=Math.max(0,...values), range=max-min||1;
  const pad={t:10,r:10,b:20,l:50};
  const w=W-pad.l-pad.r, h=H-pad.t-pad.b;
  const toX=i=>pad.l+(i/(timeline.length-1||1))*w;
  const toY=v=>pad.t+h-((v-min)/range)*h;
  [min,(min+max)/2,max].forEach(v=>{
    const y=toY(v);
    ctx.strokeStyle='#1e293b';ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(W-pad.r,y);ctx.stroke();
    ctx.fillStyle='#475569';ctx.font='9px JetBrains Mono';
    ctx.fillText((v>=0?'+':'')+v.toFixed(0),2,y+3);
  });
  const zero=toY(0);
  ctx.strokeStyle='#334155';ctx.lineWidth=1;ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.moveTo(pad.l,zero);ctx.lineTo(W-pad.r,zero);ctx.stroke();
  ctx.setLineDash([]);
  if(timeline.length<2) return;
  const lastVal=values[values.length-1];
  const grad=ctx.createLinearGradient(0,pad.t,0,H-pad.b);
  if(lastVal>=0){grad.addColorStop(0,'rgba(52,211,153,.3)');grad.addColorStop(1,'rgba(52,211,153,.02)');}
  else{grad.addColorStop(0,'rgba(251,113,133,.02)');grad.addColorStop(1,'rgba(251,113,133,.3)');}
  ctx.beginPath();ctx.moveTo(toX(0),toY(0));
  timeline.forEach((t,i)=>ctx.lineTo(toX(i),toY(t.pnl)));
  ctx.lineTo(toX(timeline.length-1),toY(0));ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();ctx.strokeStyle=lastVal>=0?'#34d399':'#fb7185';ctx.lineWidth=2;
  timeline.forEach((t,i)=>i===0?ctx.moveTo(toX(i),toY(t.pnl)):ctx.lineTo(toX(i),toY(t.pnl)));
  ctx.stroke();
  timeline.forEach((t,i)=>{
    ctx.beginPath();ctx.arc(toX(i),toY(t.pnl),3,0,Math.PI*2);
    ctx.fillStyle=t.trade_pnl>=0?'#34d399':'#fb7185';ctx.fill();
  });
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>
"""

# ── HTTP Server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def send_json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(body))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/data":
            try: self.send_json(build_api_data())
            except Exception as e: self.send_json({"error": str(e)})
        elif self.path in ("/", "/dashboard"):
            self.send_html(HTML)
        else:
            self.send_response(404); self.end_headers()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser, threading

    # Kill any existing instance on this port
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("localhost", PORT)) == 0:
            print(f"  Port {PORT} already in use — killing existing instance...")
            os.system(f"lsof -ti:{PORT} | xargs kill -9 2>/dev/null")
            import time; time.sleep(1)

    server = HTTPServer(("localhost", PORT), Handler)
    print(f"\n  Hermes Trading Bot — Dashboard")
    print(f"  ─────────────────────────────")
    print(f"  URL:     http://localhost:{PORT}")
    print(f"  Data:    {STATE_DIR}")
    print(f"  Refresh: auto every 30 seconds")
    print(f"\n  Press Ctrl+C to stop\n")

    def open_browser():
        import time; time.sleep(0.8)
        webbrowser.open(f"http://localhost:{PORT}")
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
