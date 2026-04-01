"""
Market discovery: find all open basketball game-winner markets on Kalshi
and return those where the YES side (favorite) has >= 90% implied probability.

Basketball series monitored:
    KXNBA*    — NBA games
    KXWNBA*   — WNBA games
    KXNCAAMB* — NCAA Men's Basketball (March Madness)
    KXNCAAWB* — NCAA Women's Basketball

Only single-game winner markets are returned. Totals, futures, props, and
season-long markets are filtered out.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config
import utils
from logger import get_logger

log = get_logger(__name__)

BASE = config.KALSHI_API_BASE


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OrderbookSnapshot:
    ticker: str
    yes_bid: float          # Best bid for YES (dollars)
    yes_bid_size: int       # Contracts at best bid
    yes_ask: float          # Best ask for YES (dollars)
    yes_ask_size: int       # Contracts at best ask
    spread: float           # yes_ask - yes_bid

    @property
    def implied_prob(self) -> float:
        """Mid-market implied probability."""
        if self.yes_bid > 0 and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 2
        return self.yes_bid or self.yes_ask


@dataclass
class MarketOpportunity:
    ticker: str
    title: str
    series_ticker: str
    yes_bid: float
    yes_ask: float
    yes_ask_size: int       # Liquidity available to buy
    spread: float
    implied_prob: float


# ---------------------------------------------------------------------------
# Series discovery
# ---------------------------------------------------------------------------

_series_cache: Dict[str, float] = {}          # series_ticker → timestamp cached
_cached_basketball_series: List[dict] = []     # raw series objects


def _is_basketball_series(series: dict) -> bool:
    """
    Return True if this series is a basketball series we care about.
    Matches by ticker prefix OR by category/sport tags.
    """
    ticker: str = series.get("ticker", "")
    title: str = (series.get("title") or "").lower()
    tags: list = series.get("tags") or []
    tags_lower = [t.lower() for t in tags]
    category: str = (series.get("category") or "").lower()

    # Check ticker prefix first (most reliable)
    for prefix in config.BASKETBALL_SERIES_PREFIXES:
        if ticker.upper().startswith(prefix):
            return True

    # Fallback: check tags and category
    basketball_keywords = {"nba", "wnba", "ncaa", "basketball", "march madness"}
    if any(kw in tag for kw in basketball_keywords for tag in tags_lower):
        return True
    if any(kw in category for kw in basketball_keywords):
        return True
    if any(kw in title for kw in basketball_keywords):
        return True

    return False


def _is_game_winner_series(series: dict) -> bool:
    """
    Return True if this series represents single-game winner/moneyline markets.
    Filters out totals, futures, props, season awards, etc.
    """
    ticker: str = series.get("ticker", "").upper()
    title: str = (series.get("title") or "").lower()
    tags_lower = [t.lower() for t in (series.get("tags") or [])]
    combined = title + " " + " ".join(tags_lower)

    # Hard disqualifiers — reject immediately
    for kw in config.DISQUALIFY_KEYWORDS:
        if kw in combined:
            log.debug("Disqualifying series %s (%s): matched keyword '%s'", ticker, title, kw)
            return False

    # Some disqualifying ticker suffixes (season-level markets)
    disqualify_suffixes = [
        "MVP", "ROY", "WINS", "TOTAL", "FINALS", "CHAMP",
        "CONF", "AWARD", "SCORING", "ASSIST", "REBOUND",
    ]
    for suffix in disqualify_suffixes:
        if ticker.endswith(suffix) or f"{suffix}-" in ticker or f"-{suffix}" in ticker:
            log.debug("Disqualifying series %s: ticker suffix matches '%s'", ticker, suffix)
            return False

    # For KXNCAAMB / KXNCAAWB we want game-level markets, which Kalshi typically
    # names like "KXNCAAMB-25-{DATE}-{GAME_ID}". Single-game tickers usually have
    # a date segment in the form YYMMDD or YYYY-MM-DD.
    return True


def fetch_basketball_series(force_refresh: bool = False) -> List[dict]:
    """
    Fetch and cache all basketball series from the Kalshi /series endpoint.

    Uses a 1-hour TTL cache to avoid hammering the endpoint.
    Returns a list of raw series dicts.
    """
    global _cached_basketball_series

    now = time.time()
    last_cached = _series_cache.get("__last__", 0)

    if not force_refresh and (now - last_cached) < config.SERIES_CACHE_TTL_SEC:
        log.debug("Using cached basketball series (%d items)", len(_cached_basketball_series))
        return _cached_basketball_series

    log.info("Fetching series list from Kalshi…")
    all_series: List[dict] = []
    cursor: Optional[str] = None

    while True:
        params: dict = {"limit": 200}
        if cursor:
            params["cursor"] = cursor

        try:
            data = utils.api_request("GET", f"{BASE}/series", params=params)
        except Exception as exc:
            log.error("Failed to fetch series: %s", exc)
            break

        page = data.get("series") or []
        all_series.extend(page)

        cursor = data.get("cursor")
        if not cursor:
            break

    log.info("Fetched %d total series from Kalshi", len(all_series))

    basketball = [s for s in all_series if _is_basketball_series(s)]
    game_winner = [s for s in basketball if _is_game_winner_series(s)]

    log.info(
        "Filtered to %d basketball series, %d game-winner series",
        len(basketball),
        len(game_winner),
    )

    _cached_basketball_series = game_winner
    _series_cache["__last__"] = now
    return game_winner


# ---------------------------------------------------------------------------
# Market fetching
# ---------------------------------------------------------------------------

def _is_game_winner_market(market: dict) -> bool:
    """
    Return True if this specific market is a game-winner/moneyline market.
    Called on each market within an already-filtered series.
    """
    title: str = (market.get("title") or "").lower()
    subtitle: str = (market.get("subtitle") or "").lower()
    combined = title + " " + subtitle

    # Must contain a winner keyword
    if not any(kw in combined for kw in config.GAME_WINNER_KEYWORDS):
        # Some Kalshi game markets don't use "winner" but instead phrase the
        # question as "Will the {Team} beat..." — allow those too
        if "beat" not in combined and "defeat" not in combined:
            log.debug(
                "Skipping market %s: no winner/beat keyword in '%s'",
                market.get("ticker"),
                combined[:80],
            )
            return False

    # Reject if any disqualifier is present
    for kw in config.DISQUALIFY_KEYWORDS:
        if kw in combined:
            log.debug(
                "Skipping market %s: disqualifier '%s' in '%s'",
                market.get("ticker"),
                kw,
                combined[:80],
            )
            return False

    return True


def fetch_markets_for_series(series_ticker: str) -> List[dict]:
    """
    Fetch all OPEN markets for a given series (paginated).
    Returns raw market dicts filtered to game-winner markets only.
    """
    markets: List[dict] = []
    cursor: Optional[str] = None

    while True:
        params: dict = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            data = utils.api_request("GET", f"{BASE}/markets", params=params)
        except Exception as exc:
            log.error("Failed to fetch markets for series %s: %s", series_ticker, exc)
            break

        page = data.get("markets") or []
        markets.extend(page)

        cursor = data.get("cursor")
        if not cursor:
            break

    game_winner_markets = [m for m in markets if _is_game_winner_market(m)]
    log.debug(
        "Series %s: %d open markets, %d game-winner",
        series_ticker,
        len(markets),
        len(game_winner_markets),
    )
    return game_winner_markets


# ---------------------------------------------------------------------------
# Orderbook
# ---------------------------------------------------------------------------

def fetch_orderbook(ticker: str) -> Optional[OrderbookSnapshot]:
    """
    Fetch the current orderbook for a market and return the best bid/ask.

    The Kalshi v2 orderbook endpoint returns:
        {
          "orderbook": {
            "yes": [[price_cents, size], ...],  // sorted desc
            "no":  [[price_cents, size], ...]   // sorted desc
          }
        }
    Prices here are in CENTS (integers). We convert to dollars.
    """
    try:
        data = utils.api_request("GET", f"{BASE}/markets/{ticker}/orderbook")
    except Exception as exc:
        log.error("Failed to fetch orderbook for %s: %s", ticker, exc)
        return None

    orderbook = data.get("orderbook") or {}
    yes_levels = orderbook.get("yes") or []   # [[price_cents, size], ...]
    no_levels  = orderbook.get("no") or []

    if not yes_levels and not no_levels:
        log.debug("Empty orderbook for %s", ticker)
        return None

    # YES bids: people bidding to buy YES contracts
    # The YES bids are stored sorted descending (best bid first)
    if yes_levels:
        best_yes_bid_cents, best_yes_bid_size = yes_levels[0]
        yes_bid = best_yes_bid_cents / 100.0
        yes_bid_size = best_yes_bid_size
    else:
        yes_bid = 0.0
        yes_bid_size = 0

    # YES ask = 100 - best NO bid (Kalshi complementary pricing)
    # NO bids sorted descending; best NO bid gives us the YES ask
    if no_levels:
        best_no_bid_cents, best_no_bid_size = no_levels[0]
        yes_ask = (100 - best_no_bid_cents) / 100.0
        yes_ask_size = best_no_bid_size
    else:
        # Fallback: no NO bids means wide market
        yes_ask = 1.0
        yes_ask_size = 0

    spread = round(yes_ask - yes_bid, 4)

    return OrderbookSnapshot(
        ticker=ticker,
        yes_bid=yes_bid,
        yes_bid_size=yes_bid_size,
        yes_ask=yes_ask,
        yes_ask_size=yes_ask_size,
        spread=spread,
    )


# ---------------------------------------------------------------------------
# Opportunity scanning
# ---------------------------------------------------------------------------

def scan_for_opportunities(
    exclude_tickers: Optional[set] = None,
) -> List[MarketOpportunity]:
    """
    Full scan: discover all basketball game-winner markets and return those
    that meet the 90%+ implied-probability entry criteria.

    Args:
        exclude_tickers: Set of tickers to skip (already have position/order).

    Returns:
        List of MarketOpportunity objects sorted by implied_prob descending.
    """
    if exclude_tickers is None:
        exclude_tickers = set()

    opportunities: List[MarketOpportunity] = []
    series_list = fetch_basketball_series()

    if not series_list:
        log.warning("No basketball series found — is the API reachable?")
        return []

    log.info("Scanning %d basketball series for opportunities…", len(series_list))

    for series in series_list:
        series_ticker = series.get("ticker", "")
        markets = fetch_markets_for_series(series_ticker)

        for market in markets:
            ticker = market.get("ticker", "")

            if ticker in exclude_tickers:
                log.debug("Skipping %s — already in exclude list", ticker)
                continue

            ob = fetch_orderbook(ticker)
            if ob is None:
                continue

            # ----------------------------------------------------------------
            # Entry criteria checks
            # ----------------------------------------------------------------

            # 1. YES bid must be at or above MIN_IMPLIED_PROB
            if ob.yes_bid < config.MIN_IMPLIED_PROB:
                log.debug(
                    "%s: yes_bid=%.2f below MIN_IMPLIED_PROB=%.2f — skip",
                    ticker, ob.yes_bid, config.MIN_IMPLIED_PROB,
                )
                continue

            # 2. YES ask must not exceed MAX_BUY_PRICE
            if ob.yes_ask > config.MAX_BUY_PRICE:
                log.debug(
                    "%s: yes_ask=%.2f above MAX_BUY_PRICE=%.2f — skip",
                    ticker, ob.yes_ask, config.MAX_BUY_PRICE,
                )
                continue

            # 3. Must have enough liquidity on the ask side to buy into
            if ob.yes_ask_size < config.MIN_LIQUIDITY_CONTRACTS:
                log.debug(
                    "%s: ask_size=%d below MIN_LIQUIDITY=%d — skip",
                    ticker, ob.yes_ask_size, config.MIN_LIQUIDITY_CONTRACTS,
                )
                continue

            # 4. Spread must not be too wide (dead/illiquid market)
            if ob.spread > config.MAX_SPREAD:
                log.debug(
                    "%s: spread=%.2f above MAX_SPREAD=%.2f — skip",
                    ticker, ob.spread, config.MAX_SPREAD,
                )
                continue

            opp = MarketOpportunity(
                ticker=ticker,
                title=market.get("title", ""),
                series_ticker=series_ticker,
                yes_bid=ob.yes_bid,
                yes_ask=ob.yes_ask,
                yes_ask_size=ob.yes_ask_size,
                spread=ob.spread,
                implied_prob=ob.implied_prob,
            )
            opportunities.append(opp)

            log.info(
                "OPPORTUNITY — %s | %s | bid=%.2f ask=%.2f spread=%.2f size=%d",
                ticker,
                market.get("title", "")[:60],
                ob.yes_bid,
                ob.yes_ask,
                ob.spread,
                ob.yes_ask_size,
            )

    opportunities.sort(key=lambda o: o.implied_prob, reverse=True)
    log.info("Scan complete — %d opportunities found", len(opportunities))
    return opportunities
