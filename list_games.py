"""
list_games.py — fetch and print every open NBA, WNBA, and NCAA basketball
game currently listed on Kalshi.

No credentials required (market data is public).

Usage:
    python list_games.py
"""

from typing import Optional
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

LEAGUE_PREFIXES = {
    "NBA":        "KXNBA",
    "WNBA":       "KXWNBA",
    "NCAA Men":   "KXNCAAMB",
    "NCAA Women": "KXNCAAWB",
}

DISQUALIFY = {
    "total", "mvp", "roy", "wins", "championship", "finals", "conference",
    "points", "assists", "rebounds", "props", "season", "award", "scoring",
    "most valuable", "rookie",
}

import time

session = requests.Session()
session.headers.update({"Accept": "application/json"})

REQUEST_DELAY = 0.15  # seconds between requests to avoid rate-limiting


def get(endpoint: str, params: dict = {}) -> dict:
    backoff = 2.0
    for attempt in range(6):
        time.sleep(REQUEST_DELAY)
        r = session.get(f"{BASE}{endpoint}", params=params, timeout=15)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", backoff))
            print(f"  [rate limited — waiting {wait:.0f}s]")
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Exhausted retries for {endpoint}")


def fetch_all_series() -> list[dict]:
    series, cursor = [], None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get("/series", params)
        series.extend(data.get("series") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return series


def fetch_markets_for_series(series_ticker: str) -> list[dict]:
    markets, cursor = [], None
    while True:
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get("/markets", params)
        markets.extend(data.get("markets") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return markets


def is_game_market(market: dict) -> bool:
    combined = ((market.get("title") or "") + " " + (market.get("subtitle") or "")).lower()
    return not any(kw in combined for kw in DISQUALIFY)


def main():
    print("Fetching all series from Kalshi…")
    all_series = fetch_all_series()
    print(f"  {len(all_series)} total series\n")

    results: dict[str, list[dict]] = {league: [] for league in LEAGUE_PREFIXES}

    for league, prefix in LEAGUE_PREFIXES.items():
        matched = [s for s in all_series if s.get("ticker", "").upper().startswith(prefix)]
        print(f"{league}: {len(matched)} series found")

        for s in matched:
            markets = fetch_markets_for_series(s["ticker"])
            game_markets = [m for m in markets if is_game_market(m)]
            results[league].extend(game_markets)

    print()
    total = sum(len(v) for v in results.values())
    print(f"{'='*72}")
    print(f"  OPEN BASKETBALL GAMES ON KALSHI  ({total} total)")
    print(f"{'='*72}\n")

    for league, markets in results.items():
        if not markets:
            print(f"── {league}: no open games\n")
            continue

        print(f"── {league} ({len(markets)} games)")
        print(f"  {'TICKER':<38} {'YES BID':>8} {'YES ASK':>8} {'SPREAD':>7}")
        print(f"  {'-'*38} {'-'*8} {'-'*8} {'-'*7}")

        for m in sorted(markets, key=lambda x: x.get("ticker", "")):
            ticker = m.get("ticker", "")
            title  = m.get("title", "")
            yes_bid = m.get("yes_bid") or m.get("last_price") or "—"
            yes_ask = m.get("yes_ask") or "—"

            try:
                bid_f = float(yes_bid)
                ask_f = float(yes_ask)
                spread = f"{ask_f - bid_f:.2f}"
                bid_s  = f"${bid_f:.2f}"
                ask_s  = f"${ask_f:.2f}"
            except (TypeError, ValueError):
                bid_s = ask_s = spread = "—"

            print(f"  {ticker:<38} {bid_s:>8} {ask_s:>8} {spread:>7}")
            print(f"    {title[:70]}")

        print()


if __name__ == "__main__":
    main()
