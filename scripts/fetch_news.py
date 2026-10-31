#!/usr/bin/env python3
"""
Fetches top financial news for the trading bot prompt.
Sources (all free, no API key needed):
  - Yahoo Finance RSS
  - CNBC RSS
  - Reuters RSS
  - MarketWatch RSS
  - Seeking Alpha RSS (public)
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from html.parser import HTMLParser

# ── HTML Stripper ─────────────────────────────────────────────────────────────
class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
    def handle_data(self, data):
        self._parts.append(data)
    def get_text(self):
        return " ".join(self._parts).strip()

def _strip_html(text):
    if not text:
        return ""
    s = _HTMLStripper()
    try:
        s.feed(text)
        return s.get_text()[:300]
    except Exception:
        return re.sub(r"<[^>]+>", "", text)[:300]

# ── RSS Parser ────────────────────────────────────────────────────────────────
def _parse_rss(xml_bytes):
    """
    Minimal RSS/Atom parser — no dependencies.
    Returns list of { title, summary, url, published } dicts.
    """
    xml = xml_bytes.decode("utf-8", errors="replace")
    items = []

    # Find all <item> or <entry> blocks
    for tag in ("item", "entry"):
        pattern = re.compile(rf"<{tag}[^>]*>(.*?)</{tag}>", re.DOTALL)
        for m in pattern.finditer(xml):
            block = m.group(1)

            def get_tag(t, blk=block):
                patterns = [
                    rf"<{t}[^>]*><!\[CDATA\[(.*?)\]\]></{t}>",
                    rf"<{t}[^>]*>(.*?)</{t}>",
                ]
                for p in patterns:
                    r = re.search(p, blk, re.DOTALL)
                    if r:
                        return r.group(1).strip()
                return ""

            title   = _strip_html(get_tag("title"))
            summary = _strip_html(get_tag("description") or get_tag("summary"))
            url     = get_tag("link") or get_tag("id")
            if url.startswith("http") is False:
                # atom <link href="...">
                m2 = re.search(r'<link[^>]+href="([^"]+)"', block)
                if m2:
                    url = m2.group(1)

            pub_raw = get_tag("pubDate") or get_tag("published") or get_tag("updated")
            try:
                pub_dt = parsedate_to_datetime(pub_raw)
                pub_iso = pub_dt.isoformat()
            except Exception:
                try:
                    pub_dt  = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                    pub_iso = pub_dt.isoformat()
                except Exception:
                    pub_dt  = datetime.now(timezone.utc)
                    pub_iso = pub_dt.isoformat()

            if title:
                items.append({
                    "title":     title,
                    "summary":   summary,
                    "url":       url,
                    "published": pub_iso,
                    "published_dt": pub_dt,
                })

    return items

# ── RSS Feeds ─────────────────────────────────────────────────────────────────
FEEDS = [
    ("Yahoo Finance",  "https://finance.yahoo.com/rss/topstories"),
    ("CNBC Markets",   "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("WSJ Business",   "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"),
    ("MarketWatch",    "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("CNBC Economy",   "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
]

def _fetch_feed(name, url, timeout=8):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        return _parse_rss(resp.read())
    except Exception as e:
        print(f"  [WARN] {name} feed failed: {e}")
        return []

# ── Financial Keywords Filter ─────────────────────────────────────────────────
FINANCIAL_KEYWORDS = [
    # Market & indices
    "s&p", "s&p 500", "spy", "spx", "nasdaq", "dow jones", "russell",
    "stock market", "stocks", "equities", "bull market", "bear market",
    # Fed & rates
    "federal reserve", "fed", "fomc", "powell", "interest rate", "rate hike",
    "rate cut", "basis points", "monetary policy", "quantitative",
    # Economic data
    "inflation", "cpi", "ppi", "pce", "gdp", "jobs report", "non-farm",
    "payroll", "unemployment", "jobless", "retail sales", "housing",
    "consumer confidence", "manufacturing", "ism",
    # Bonds & yields
    "treasury", "yield", "bond", "10-year", "2-year", "yield curve",
    "inverted", "spread",
    # Market conditions
    "volatility", "vix", "rally", "selloff", "sell-off", "correction",
    "crash", "pullback", "breakout", "resistance", "support",
    # Macro
    "recession", "stagflation", "tariff", "trade war", "sanctions",
    "geopolitical", "oil price", "energy", "dollar", "currency",
    # Sectors & earnings
    "earnings", "revenue", "profit", "guidance", "quarterly results",
    "banking", "financials", "tech sector", "big tech",
]

# Words that disqualify an article even if it has a finance keyword
NOISE_KEYWORDS = [
    "streaming", "netflix", "hulu", "disney+", "tv show", "movie",
    "celebrity", "sports", "nfl", "nba", "soccer", "entertainment",
    "fashion", "food", "travel", "gaming", "video game",
]

def _is_financial(item):
    text = (item["title"] + " " + item["summary"]).lower()
    # Must NOT be noise
    if any(kw in text for kw in NOISE_KEYWORDS):
        return False
    # Must have at least one financial keyword
    return any(kw in text for kw in FINANCIAL_KEYWORDS)

# ── Master Fetch ──────────────────────────────────────────────────────────────
def fetch_top_news(days_back=3, max_items=5):
    """
    Fetches top financial news from last N days across all feeds.
    Returns list of { title, summary, url, published, source } dicts.
    Sorted by recency, filtered by financial relevance.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    all_items = []

    for name, url in FEEDS:
        print(f"  Fetching {name}...")
        items = _fetch_feed(name, url)
        for item in items:
            item["source"] = name
        all_items.extend(items)

    # Filter: only financial, only within cutoff, deduplicate by title
    seen_titles = set()
    filtered = []
    for item in all_items:
        title_key = item["title"].lower()[:60]
        if title_key in seen_titles:
            continue
        pub_dt = item.get("published_dt")
        # Make sure pub_dt is timezone-aware
        if pub_dt and pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        if pub_dt and pub_dt >= cutoff and _is_financial(item):
            seen_titles.add(title_key)
            filtered.append(item)

    # Sort by recency
    filtered.sort(key=lambda x: x.get("published_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # Return top N, cleaned up (remove the datetime object)
    result = []
    for item in filtered[:max_items]:
        result.append({
            "title":     item["title"],
            "summary":   item["summary"],
            "url":       item["url"],
            "published": item["published"],
            "source":    item["source"],
        })

    return result

def format_news_for_prompt(news_items):
    """
    Formats news list into readable text for the LLM prompt.
    """
    if not news_items:
        return "No recent financial news available."

    lines = []
    for i, item in enumerate(news_items, 1):
        lines.append(f"{i}. [{item['source']}] {item['title']}")
        if item["summary"]:
            lines.append(f"   {item['summary'][:200]}")
        lines.append(f"   Published: {item['published'][:19]}")
        lines.append("")

    return "\n".join(lines)

# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Fetching top financial news (last 3 days)...\n")
    news = fetch_top_news(days_back=3, max_items=5)

    print(f"\nFound {len(news)} relevant articles:\n")
    print(format_news_for_prompt(news))

    # Save to state dir for inspection
    out = Path.home() / "trading-bot" / "state" / "latest_news.json"
    with open(out, "w") as f:
        json.dump(news, f, indent=2)
    print(f"Saved to {out}")
    print("\n=== DONE ===")
