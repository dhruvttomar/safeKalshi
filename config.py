import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
KALSHI_API_BASE = os.getenv(
    "KALSHI_API_BASE", "https://demo-api.kalshi.co/trade-api/v2"
)  # DEFAULT: demo — flip to prod after testing
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# ---------------------------------------------------------------------------
# Trading thresholds
# ---------------------------------------------------------------------------
MIN_IMPLIED_PROB = 0.90         # Only trade favorites at 90%+ implied probability
MAX_BUY_PRICE = 0.95            # Never pay more than 95 cents per contract
MIN_LIQUIDITY_CONTRACTS = 10    # Minimum contracts available on the ask side
MAX_SPREAD = 0.05               # Max allowable bid-ask spread (dollars)

# ---------------------------------------------------------------------------
# Risk limits
# ---------------------------------------------------------------------------
MAX_EXPOSURE_PER_MARKET = 50    # Max dollars risked in a single market
MAX_TOTAL_EXPOSURE = 500        # Max total dollars exposed across all markets
MAX_DAILY_LOSS = 25             # Stop trading for the day if we lose this much
MAX_CONTRACTS_PER_ORDER = 50    # Hard cap on contracts per single order
MAX_OPEN_ORDERS = 10            # Don't open a new order if we already have this many

STALE_ORDER_TIMEOUT_SEC = 300   # Cancel unfilled orders after 5 minutes

# ---------------------------------------------------------------------------
# Kelly Criterion
# ---------------------------------------------------------------------------
KELLY_MULTIPLIER = 0.25                 # Quarter Kelly for safety
ESTIMATED_EDGE_OVER_MARKET = 0.02       # Conservative 2% edge assumption when market says 90%+

# ---------------------------------------------------------------------------
# Polling intervals
# ---------------------------------------------------------------------------
SCAN_INTERVAL_SEC = 60          # How often to scan for new opportunities
ORDER_CHECK_INTERVAL_SEC = 15   # How often to check order fill status
SERIES_CACHE_TTL_SEC = 3600     # Cache the series list for 1 hour

# ---------------------------------------------------------------------------
# Basketball series prefixes (used as fallback / validation)
# ---------------------------------------------------------------------------
BASKETBALL_SERIES_PREFIXES = ["KXNBA", "KXWNBA", "KXNCAAMB", "KXNCAAWB"]

# Keywords in a series/market title that indicate it's a game-winner market
GAME_WINNER_KEYWORDS = ["win", "winner"]

# Keywords that disqualify a series from being a game-winner market
DISQUALIFY_KEYWORDS = [
    "total", "mvp", "roy", "wins", "championship", "finals", "conference",
    "points", "assists", "rebounds", "props", "season", "award", "scoring",
    "most valuable", "rookie",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = "bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
