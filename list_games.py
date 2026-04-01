"""
list_games.py — fetch and print every open NBA, WNBA, and NCAA basketball
game-winner market currently listed on Kalshi.

No credentials required (market data is public).

Usage:
    python list_games.py
"""

import time
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Exact Kalshi series tickers for full-game winners only
GAME_SERIES = {
    "NBA":        "KXNBAGAME",
    "WNBA":       "KXWNBAGAME",
    "NCAA Men":   "KXNCAAMBGAME",
    "NCAA Women": "KXNCAAWBGAME",
}

REQUEST_DELAY = 0.15

session = requests.Session()
session.headers.update({"Accept": "application/json"})


def get(endpoint: str, params: dict = {}) -> dict:
    backoff = 2.0
    for _ in range(6):
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


def fetch_game_markets(series_ticker: str) -> list[dict]:
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


def main():
    results: dict[str, list[dict]] = {}

    for league, series_ticker in GAME_SERIES.items():
        markets = fetch_game_markets(series_ticker)
        results[league] = markets
        print(f"{league}: {len(markets)} markets")

    print()
    total = sum(len(v) for v in results.values())
    print(f"{'='*72}")
    print(f"  OPEN BASKETBALL GAME WINNERS ON KALSHI  ({total} total)")
    print(f"{'='*72}\n")

    for league, markets in results.items():
        if not markets:
            print(f"── {league}: no open games\n")
            continue

        # Group markets by game (strip the trailing team code)
        # Ticker format: KXNBAGAME-26APR03CHINYK-NYK  →  game = CHINYK
        games: dict[str, list[dict]] = {}
        for m in markets:
            parts = m.get("ticker", "").split("-")
            game_key = "-".join(parts[:-1])  # everything except the team suffix
            games.setdefault(game_key, []).append(m)

        print(f"── {league} ({len(games)} games, {len(markets)} markets)")

        for game_key in sorted(games):
            sides = games[game_key]
            title = sides[0].get("title", "")
            # Strip the team-specific part to get the matchup title
            # e.g. "Chicago at New York Winner?" (same for all sides)
            print(f"\n  {title}")
            print(f"  {'TICKER':<45} {'LAST':>6}")
            for m in sorted(sides, key=lambda x: x.get("ticker", "")):
                ticker = m.get("ticker", "")
                last = m.get("last_price")
                price_s = f"${float(last):.2f}" if last else "—"
                print(f"  {ticker:<45} {price_s:>6}")

        print()


if __name__ == "__main__":
    main()
