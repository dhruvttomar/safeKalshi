"""
blowout_monitor.py — Blowout detector + auto-trader with full risk management.

Strategy: when a live NBA/WNBA/NCAA game has a ≥22 point lead with ≤16 min
remaining in regulation, buy YES on the leading team's game-winner market.

Features
--------
- Risk gates: daily spend cap, total exposure cap, min liquidity, max price
- Persistence: state survives restarts (blowout_state.json)
- Order tracking: detects fills, cancels stale orders after 5 min
- Telegram bot: push alerts + /status /orders /stop commands
- Graceful shutdown: SIGINT cancels open orders, saves state, notifies Telegram
- Adaptive polling: 10 s in final period, 30 s otherwise

Setup
-----
    export KALSHI_API_KEY_ID="your-key-id"
    export KALSHI_PRIVATE_KEY_PATH="/path/to/kalshi_private_key.pem"
    export KALSHI_API_BASE="https://api.elections.kalshi.com/trade-api/v2"
    export TELEGRAM_BOT_TOKEN="123456:ABC..."   # optional
    export TELEGRAM_CHAT_ID="your-chat-id"      # optional
    python blowout_monitor.py

Risk env overrides (all optional):
    MAX_DAILY_SPEND      default 50    stop placing new bets after $N spent today
    MAX_TOTAL_EXPOSURE   default 100   max $ in unfilled open orders at once
    NUM_CONTRACTS        default 1     contracts per bet
"""

import collections
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify

import config
import utils

# ---------------------------------------------------------------------------
# Configuration — all tunable via env vars
# ---------------------------------------------------------------------------

MAX_DAILY_SPEND       = float(os.getenv("MAX_DAILY_SPEND",     "100000"))
MAX_TOTAL_EXPOSURE    = float(os.getenv("MAX_TOTAL_EXPOSURE",  "100000"))
BET_AMOUNT            = float(os.getenv("BET_AMOUNT",          "1000"))
NUM_CONTRACTS         = int(os.getenv("NUM_CONTRACTS",         "1"))  # unused when BET_AMOUNT set

BLOWOUT_DIFF          = 22      # minimum point differential
BLOWOUT_TIME_SEC      = 960     # 16 min = 960 s remaining in regulation
POLL_INTERVAL_SEC     = 30      # normal poll cadence
POLL_INTERVAL_FINAL   = 10      # poll cadence when any game is in its last period
STALE_ORDER_SEC       = 300     # cancel unfilled orders after 5 min
MAX_YES_ASK           = 0.98    # don't buy if market is already at 98 c — no edge
MIN_LIQUIDITY         = 3       # minimum contracts available on the ask side

STATE_FILE            = Path("blowout_state.json")
DASHBOARD_PORT        = int(os.getenv("DASHBOARD_PORT", "5001"))

TELEGRAM_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("blowout")

# In-memory ring buffer — dashboard reads from this
_log_buffer: collections.deque = collections.deque(maxlen=200)

class _BufHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.appendleft(self.format(record))

_bh = _BufHandler()
_bh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_bh)

_start_time = time.time()

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

_tg_session = requests.Session()


def send_telegram(msg: str) -> None:
    """Fire-and-forget Telegram message. Silently drops if not configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        _tg_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as exc:
        log.debug("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# League metadata
# ---------------------------------------------------------------------------

@dataclass
class LeagueConfig:
    name: str
    espn_url: str
    kalshi_series: str
    total_periods: int
    period_duration_sec: int


LEAGUES = [
    LeagueConfig("NBA",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        "KXNBAGAME", 4, 720),
    LeagueConfig("WNBA",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
        "KXWNBAGAME", 4, 600),
    LeagueConfig("NCAA Men",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
        "KXNCAAMBGAME", 2, 1200),
    LeagueConfig("NCAA Women",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball/scoreboard",
        "KXNCAAWBGAME", 4, 600),
]

# ---------------------------------------------------------------------------
# ESPN → Kalshi team code map
# ---------------------------------------------------------------------------

ESPN_TO_KALSHI: dict[str, str] = {
    "GS":   "GSW",
    "NY":   "NYK",
    "SA":   "SAS",
    "NO":   "NOP",
    "PHO":  "PHX",
    "UTAH": "UTA",
    "WSH":  "WAS",
}


def normalize(code: str) -> str:
    return ESPN_TO_KALSHI.get(code.upper(), code.upper())


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

@dataclass
class GameState:
    espn_id: str
    league: LeagueConfig
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    period: int
    clock_sec: float
    status: str        # "pre" | "in" | "post"

    @property
    def diff(self) -> int:
        return abs(self.home_score - self.away_score)

    @property
    def leading_team(self) -> str:
        return self.home_team if self.home_score >= self.away_score else self.away_team

    @property
    def trailing_team(self) -> str:
        return self.away_team if self.home_score >= self.away_score else self.home_team

    @property
    def time_remaining_sec(self) -> float:
        effective = min(self.period, self.league.total_periods)
        return self.clock_sec + (self.league.total_periods - effective) * self.league.period_duration_sec


# ---------------------------------------------------------------------------
# Persistent application state
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    date: str              = ""     # YYYY-MM-DD; daily fields reset when this changes
    daily_spend: float     = 0.0    # total $ committed to bets today
    total_exposure: float  = 0.0    # $ locked in unfilled open orders right now
    bets_placed: int       = 0      # bets placed today
    bets_filled: int       = 0      # bets confirmed filled today
    already_traded: list   = field(default_factory=list)   # ESPN game IDs
    open_orders: list      = field(default_factory=list)
    # open_orders entries: {order_id, ticker, espn_id, price, contracts, submitted_at}


_state_lock = threading.Lock()


def load_state() -> AppState:
    if not STATE_FILE.exists():
        return AppState()
    try:
        data = json.loads(STATE_FILE.read_text())
        return AppState(**{k: v for k, v in data.items() if k in AppState.__dataclass_fields__})
    except Exception as exc:
        log.warning("Could not load state file (%s) — starting fresh", exc)
        return AppState()


def save_state(state: AppState) -> None:
    try:
        STATE_FILE.write_text(json.dumps(asdict(state), indent=2))
    except Exception as exc:
        log.error("Failed to save state: %s", exc)


def maybe_reset_daily(state: AppState) -> None:
    today = str(date.today())
    if state.date == today:
        return
    log.info("New trading day — resetting daily counters")
    state.date         = today
    state.daily_spend  = 0.0
    state.bets_placed  = 0
    state.bets_filled  = 0
    # Preserve open_orders and already_traded across midnight (rare but correct)
    save_state(state)


# ---------------------------------------------------------------------------
# Risk gates
# ---------------------------------------------------------------------------

class RiskBlocked(Exception):
    pass


def check_risk(state: AppState, ask: float, contracts: int) -> None:
    """Raise RiskBlocked if any limit would be exceeded by this bet."""
    cost = ask * contracts
    if state.daily_spend + cost > MAX_DAILY_SPEND:
        raise RiskBlocked(
            f"Daily spend limit: ${state.daily_spend:.2f} + ${cost:.2f} > ${MAX_DAILY_SPEND:.2f}"
        )
    if state.total_exposure + cost > MAX_TOTAL_EXPOSURE:
        raise RiskBlocked(
            f"Exposure limit: ${state.total_exposure:.2f} + ${cost:.2f} > ${MAX_TOTAL_EXPOSURE:.2f}"
        )
    if ask > MAX_YES_ASK:
        raise RiskBlocked(f"Ask ${ask:.2f} exceeds MAX_YES_ASK ${MAX_YES_ASK:.2f} — no edge left")


# ---------------------------------------------------------------------------
# Order tracking — fills + stale cancellation
# ---------------------------------------------------------------------------

def poll_orders(state: AppState) -> None:
    """
    For every open order: check if stale (cancel) or filled (record).
    Mutates state in place; caller should save_state afterwards.
    """
    if not state.open_orders:
        return

    now = time.time()
    still_open = []

    for o in list(state.open_orders):
        order_id = o["order_id"]
        ticker   = o["ticker"]
        cost     = o["price"] * o["contracts"]
        age      = now - o["submitted_at"]

        # ── Stale: cancel ──
        if age >= STALE_ORDER_SEC:
            log.info("Cancelling stale order %s (%ds old)", order_id, int(age))
            try:
                utils.api_request(
                    "DELETE",
                    f"{config.KALSHI_API_BASE}/portfolio/orders/{order_id}",
                    authenticated=True,
                )
                state.total_exposure = max(0.0, state.total_exposure - cost)
                msg = (
                    f"Stale order cancelled\n"
                    f"Ticker: {ticker}\n"
                    f"Age: {int(age)}s | Exposure freed: ${cost:.2f}"
                )
                log.info(msg)
                send_telegram(msg)
            except Exception as exc:
                log.error("Could not cancel stale order %s: %s", order_id, exc)
                still_open.append(o)
            continue

        # ── Poll status ──
        try:
            resp = utils.api_request(
                "GET",
                f"{config.KALSHI_API_BASE}/portfolio/orders/{order_id}",
                authenticated=True,
            )
            raw    = resp.get("order") or resp
            status = (raw.get("status") or "").lower()
        except Exception as exc:
            log.debug("Could not poll order %s: %s", order_id, exc)
            still_open.append(o)
            continue

        if status in ("filled", "executed"):
            filled    = int(raw.get("contracts_filled") or raw.get("filled_count") or o["contracts"])
            avg_price = float(raw.get("avg_fill_price") or raw.get("average_fill_price") or o["price"])
            state.total_exposure = max(0.0, state.total_exposure - cost)
            state.bets_filled   += 1
            to_win = (1.0 - avg_price) * filled
            msg = (
                f"Order FILLED\n"
                f"Ticker: {ticker}\n"
                f"Fill: ${avg_price:.2f} x {filled} contract(s)\n"
                f"Max profit if YES wins: ${to_win:.2f}"
            )
            log.info(msg)
            send_telegram(msg)

        elif status in ("cancelled", "canceled", "expired"):
            state.total_exposure = max(0.0, state.total_exposure - cost)
            log.info("Order externally cancelled: %s", order_id)

        else:
            still_open.append(o)   # still resting on the book

    state.open_orders = still_open


# ---------------------------------------------------------------------------
# Telegram command bot (background thread)
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()
_tg_offset      = 0


def _tg_command_loop(state: AppState) -> None:
    """Poll Telegram for incoming commands; runs in a daemon thread."""
    global _tg_offset
    if not TELEGRAM_TOKEN:
        return

    while not _shutdown_event.is_set():
        try:
            r = _tg_session.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 10},
                timeout=15,
            )
            for update in r.json().get("result") or []:
                _tg_offset = update["update_id"] + 1
                text = (update.get("message", {}).get("text") or "").strip().lower()

                with _state_lock:
                    if text.startswith("/status"):
                        send_telegram(_fmt_status(state))
                    elif text.startswith("/orders"):
                        send_telegram(_fmt_orders(state))
                    elif text.startswith("/log"):
                        # /log or /log 20 (optional line count)
                        parts = text.split()
                        try:
                            n = max(1, min(int(parts[1]), 50)) if len(parts) > 1 else 20
                        except ValueError:
                            n = 20
                        send_telegram(_fmt_log(n))
                    elif text.startswith("/stop"):
                        send_telegram("Stop command received — shutting down...")
                        _shutdown_event.set()
                    elif text.startswith("/help"):
                        send_telegram(
                            "Commands:\n"
                            "/status      — daily stats and exposure\n"
                            "/orders      — list open (unfilled) orders\n"
                            "/log [N]     — last N log lines (default 20, max 50)\n"
                            "/stop        — graceful shutdown\n"
                            "/help        — this message"
                        )
        except Exception:
            pass
        time.sleep(2)


def _fmt_status(state: AppState) -> str:
    return (
        f"[STATUS] {state.date}\n"
        f"Daily spend:   ${state.daily_spend:.2f} / ${MAX_DAILY_SPEND:.2f}\n"
        f"Open exposure: ${state.total_exposure:.2f} / ${MAX_TOTAL_EXPOSURE:.2f}\n"
        f"Bets placed:   {state.bets_placed}\n"
        f"Bets filled:   {state.bets_filled}\n"
        f"Games tracked: {len(state.already_traded)}\n"
        f"Open orders:   {len(state.open_orders)}"
    )


def _fmt_log(n: int = 20) -> str:
    lines = list(_log_buffer)[:n]
    if not lines:
        return "No log lines yet."
    return "[LOG]\n" + "\n".join(lines)


def _fmt_orders(state: AppState) -> str:
    if not state.open_orders:
        return "No open orders."
    now   = time.time()
    lines = ["[OPEN ORDERS]"]
    for o in state.open_orders:
        age = int(now - o["submitted_at"])
        lines.append(
            f"{o['ticker']}\n"
            f"  ${o['price']:.2f} x {o['contracts']} | {age}s old"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ESPN scoreboard
# ---------------------------------------------------------------------------

_espn = requests.Session()
_espn.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})


def fetch_espn_games(league: LeagueConfig) -> list[GameState]:
    try:
        r    = _espn.get(league.espn_url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("ESPN fetch failed for %s: %s", league.name, exc)
        return []

    states = []
    for event in data.get("events") or []:
        try:
            comp    = event["competitions"][0]
            status  = comp["status"]
            state_s = status["type"]["state"]
            clock   = float(status.get("clock") or 0)
            period  = int(status.get("period") or 0)
            home    = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away    = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            states.append(GameState(
                espn_id    = event["id"],
                league     = league,
                home_team  = normalize(home["team"]["abbreviation"]),
                away_team  = normalize(away["team"]["abbreviation"]),
                home_score = int(home.get("score") or 0),
                away_score = int(away.get("score") or 0),
                period     = period,
                clock_sec  = clock,
                status     = state_s,
            ))
        except Exception as exc:
            log.debug("ESPN parse error for event %s: %s", event.get("id"), exc)
    return states


# ---------------------------------------------------------------------------
# Blowout detection
# ---------------------------------------------------------------------------

def is_blowout(g: GameState) -> bool:
    return (
        g.status == "in"
        and g.diff >= BLOWOUT_DIFF
        and g.period <= g.league.total_periods          # no overtime
        and g.time_remaining_sec <= BLOWOUT_TIME_SEC
    )


# ---------------------------------------------------------------------------
# Kalshi market helpers
# ---------------------------------------------------------------------------

_kalshi = requests.Session()
_kalshi.headers.update({"Accept": "application/json"})
_KALSHI_PUB = "https://api.elections.kalshi.com/trade-api/v2"


def _kget(endpoint: str, params: dict = {}) -> dict:
    backoff = 2.0
    for _ in range(5):
        time.sleep(0.15)
        r = _kalshi.get(f"{_KALSHI_PUB}{endpoint}", params=params, timeout=15)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", backoff))
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Exhausted retries for {endpoint}")


def fetch_kalshi_markets(series: str) -> list[dict]:
    markets, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _kget("/markets", params)
        markets.extend(data.get("markets") or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return markets


def fetch_orderbook_ask(ticker: str) -> tuple[Optional[float], int]:
    """Return (yes_ask_dollars, ask_size) or (None, 0) if no book."""
    try:
        data = _kget(f"/markets/{ticker}/orderbook")
    except Exception as exc:
        log.warning("Orderbook fetch failed for %s: %s", ticker, exc)
        return None, 0

    ob       = data.get("orderbook") or {}
    no_lvls  = ob.get("no") or []
    yes_lvls = ob.get("yes") or []

    if not no_lvls:
        return None, 0

    best_no_bid_cents = no_lvls[0][0]
    yes_ask           = (100 - best_no_bid_cents) / 100.0
    # ask_size comes from the NO bid size (complementary)
    ask_size          = no_lvls[0][1] if len(no_lvls[0]) > 1 else 0

    return yes_ask, ask_size


def find_winning_ticker(g: GameState, markets: list[dict]) -> Optional[str]:
    """Return the Kalshi ticker for the leading team, or None if not found."""
    games: dict[str, dict[str, str]] = {}
    for m in markets:
        parts = m.get("ticker", "").split("-")
        if len(parts) < 3:
            continue
        team_code  = parts[-1].upper()
        event_tick = "-".join(parts[:-1])
        games.setdefault(event_tick, {})[team_code] = m["ticker"]

    home, away = g.home_team.upper(), g.away_team.upper()
    for event_tick, team_map in games.items():
        seg = event_tick.split("-")[-1].upper()
        if home in seg and away in seg:
            return team_map.get(g.leading_team.upper())
    return None


# ---------------------------------------------------------------------------
# Order placement (with risk checks)
# ---------------------------------------------------------------------------

def place_bet(ticker: str, game: GameState, state: AppState) -> bool:
    """
    Fetch ask price, run risk checks, submit limit order, update state.
    Returns True on success.
    """
    ask, ask_size = fetch_orderbook_ask(ticker)

    if ask is None:
        log.warning("No orderbook for %s — skipping", ticker)
        return False

    if ask_size < MIN_LIQUIDITY:
        log.info("SKIP %s: ask_size=%d < MIN_LIQUIDITY=%d", ticker, ask_size, MIN_LIQUIDITY)
        return False

    num_contracts = max(1, int(BET_AMOUNT / ask))

    try:
        check_risk(state, ask, num_contracts)
    except RiskBlocked as exc:
        log.info("RISK BLOCK — %s", exc)
        send_telegram(f"Risk block: {exc}")
        return False

    body = {
        "ticker":    ticker,
        "action":    "buy",
        "side":      "yes",
        "type":      "limit",
        "count":     num_contracts,
        "yes_price": utils.dollars_to_cents(ask),
    }

    try:
        resp = utils.api_request(
            "POST",
            f"{config.KALSHI_API_BASE}/portfolio/orders",
            authenticated=True,
            json_body=body,
        )
    except Exception as exc:
        log.error("Order submission failed for %s: %s", ticker, exc)
        send_telegram(f"Order FAILED: {ticker}\n{exc}")
        return False

    raw      = resp.get("order") or resp
    order_id = raw.get("order_id") or raw.get("id", "unknown")
    cost     = ask * num_contracts

    state.open_orders.append({
        "order_id":     order_id,
        "ticker":       ticker,
        "espn_id":      game.espn_id,
        "price":        ask,
        "contracts":    num_contracts,
        "submitted_at": time.time(),
    })
    state.daily_spend    += cost
    state.total_exposure += cost
    state.bets_placed    += 1
    state.already_traded.append(game.espn_id)

    msg = (
        f"BET PLACED\n"
        f"{game.away_team} @ {game.home_team} — {game.league.name}\n"
        f"Leader: {game.leading_team} +{game.diff} pts | "
        f"{game.time_remaining_sec/60:.1f} min left\n"
        f"Ticker: {ticker}\n"
        f"Ask: ${ask:.2f} x {num_contracts} contract(s) = ${cost:.2f}\n"
        f"Order ID: {order_id}\n"
        f"Daily spend: ${state.daily_spend:.2f} / ${MAX_DAILY_SPEND:.2f}"
    )
    log.info(msg)
    send_telegram(msg)
    return True


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _handle_shutdown(state: AppState, *_) -> None:
    log.info("Shutdown — cancelling open orders and saving state...")
    _shutdown_event.set()

    with _state_lock:
        for o in state.open_orders:
            try:
                utils.api_request(
                    "DELETE",
                    f"{config.KALSHI_API_BASE}/portfolio/orders/{o['order_id']}",
                    authenticated=True,
                )
                log.info("Cancelled %s on shutdown", o["order_id"])
            except Exception as exc:
                log.error("Could not cancel %s: %s", o["order_id"], exc)
        save_state(state)

    send_telegram(
        f"Bot stopped\n"
        f"Daily spend: ${state.daily_spend:.2f}\n"
        f"Bets placed: {state.bets_placed} | Filled: {state.bets_filled}"
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

_app_state_ref: Optional[AppState] = None
_dash = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blowout Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font-family:'SF Mono','Fira Code',monospace;font-size:13px;padding:20px}
h1{color:#58a6ff;font-size:20px;font-weight:700}
.sub{color:#8b949e;font-size:12px;margin-top:2px;margin-bottom:14px}
.badges{display:flex;gap:8px;margin-bottom:16px}
.badge{padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700}
.prod{background:#da3633;color:#fff}.demo{background:#1f3a6e;color:#58a6ff;border:1px solid #388bfd}
.live{background:#1a4731;color:#3fb950;border:1px solid #238636}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:14px}
.card-label{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.card-value{font-size:24px;font-weight:700;color:#e6edf3}
.card-sub{font-size:11px;color:#8b949e;margin-top:3px}
.bar-bg{background:#21262d;border-radius:3px;height:3px;margin-top:8px}
.bar{border-radius:3px;height:3px;transition:width .4s}
.ok{background:#3fb950}.warn{background:#e3b341}.danger{background:#f85149}
.box{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:14px;margin-bottom:12px}
.box-title{color:#58a6ff;font-size:11px;text-transform:uppercase;font-weight:700;margin-bottom:10px}
table{width:100%;border-collapse:collapse}
th{color:#8b949e;text-align:left;padding:4px 8px;font-size:11px;border-bottom:1px solid #30363d;font-weight:400}
td{padding:5px 8px;border-bottom:1px solid #21262d;font-size:12px}
.log{background:#010409;border:1px solid #21262d;border-radius:4px;padding:10px;height:340px;overflow-y:auto}
.ll{white-space:pre-wrap;line-height:1.7;font-size:12px}
.INFO{color:#c9d1d9}.WARN{color:#e3b341}.ERROR{color:#f85149}.DEBUG{color:#484f58}
.empty{color:#484f58;font-style:italic}
.footer{color:#484f58;font-size:11px;margin-top:10px}
</style></head>
<body>
<h1>Blowout Monitor</h1>
<div class="sub" id="sub">Loading\u2026</div>
<div class="badges" id="badges"></div>
<div class="grid" id="stats"></div>
<div class="box"><div class="box-title">Open Orders</div><div id="orders"></div></div>
<div class="box"><div class="box-title">Recent Logs</div><div class="log" id="logs"></div></div>
<div class="footer" id="footer"></div>
<script>
function fmt(s){s=Math.floor(s);var h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;return h?h+'h '+m+'m':m?m+'m '+r+'s':r+'s'}
function bar(v,mx){var p=Math.min(100,v/mx*100),c=p>90?'danger':p>70?'warn':'ok';return'<div class="bar-bg"><div class="bar '+c+'" style="width:'+p.toFixed(1)+'%"></div></div>'}
async function refresh(){
  try{
    var d=await(await fetch('/api/state')).json(),lim=d.limits,now=Date.now()/1000;
    document.getElementById('sub').textContent=d.date+' \u2022 Uptime: '+fmt(d.uptime_sec);
    document.getElementById('badges').innerHTML='<span class="badge live">\u25cf LIVE</span>'
      +'<span class="badge '+(d.env==='PRODUCTION'?'prod':'demo')+'">'+d.env+'</span>';
    document.getElementById('stats').innerHTML=
      card('Daily Spend','$'+d.daily_spend.toFixed(2),'limit $'+lim.max_daily_spend.toFixed(0),bar(d.daily_spend,lim.max_daily_spend))+
      card('Open Exposure','$'+d.total_exposure.toFixed(2),'limit $'+lim.max_total_exposure.toFixed(0),bar(d.total_exposure,lim.max_total_exposure))+
      card('Bets Placed',d.bets_placed,d.bets_filled+' confirmed filled','')+
      card('Games Tracked',d.already_traded.length,d.open_orders.length+' open orders','');
    document.getElementById('orders').innerHTML=d.open_orders.length?
      '<table><thead><tr><th>Ticker</th><th>Price \u00d7 Qty</th><th>Cost</th><th>Age</th></tr></thead><tbody>'+
      d.open_orders.map(function(o){var age=fmt(now-o.submitted_at);return'<tr><td>'+o.ticker+'</td><td>$'+o.price.toFixed(2)+' \u00d7 '+o.contracts+'</td><td>$'+(o.price*o.contracts).toFixed(2)+'</td><td>'+age+' old</td></tr>'}).join('')+
      '</tbody></table>':'<span class="empty">No open orders</span>';
    document.getElementById('logs').innerHTML=d.logs.length?d.logs.map(function(l){
      var lv=(l.match(/\\[(INFO|WARNING|ERROR|DEBUG)\\]/)||['','INFO'])[1],c=lv==='WARNING'?'WARN':lv;
      return'<div class="ll '+c+'">'+l.replace(/</g,'&lt;')+'</div>'}).join(''):'<span class="empty">No log lines yet</span>';
    document.getElementById('footer').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('footer').textContent='Connection lost \u2014 '+new Date().toLocaleTimeString()}
}
function card(label,val,sub,extra){return'<div class="card"><div class="card-label">'+label+'</div><div class="card-value">'+val+'</div><div class="card-sub">'+sub+'</div>'+extra+'</div>'}
setInterval(refresh,3000);refresh();
</script></body></html>"""


@_dash.route("/")
def _dash_index():
    return _DASHBOARD_HTML


@_dash.route("/api/state")
def _dash_api():
    with _state_lock:
        if _app_state_ref is None:
            return jsonify({"error": "bot not started"})
        data = asdict(_app_state_ref)
    data["logs"]       = list(_log_buffer)
    data["uptime_sec"] = int(time.time() - _start_time)
    data["env"]        = "DEMO" if "demo" in config.KALSHI_API_BASE.lower() else "PRODUCTION"
    data["limits"]     = {
        "max_daily_spend":    MAX_DAILY_SPEND,
        "max_total_exposure": MAX_TOTAL_EXPOSURE,
        "num_contracts":      NUM_CONTRACTS,
    }
    return jsonify(data)


def _start_dashboard() -> None:
    _dash.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------

def monitor() -> None:
    global _app_state_ref
    state = load_state()
    _app_state_ref = state

    signal.signal(signal.SIGINT,  lambda s, f: _handle_shutdown(state, s, f))
    signal.signal(signal.SIGTERM, lambda s, f: _handle_shutdown(state, s, f))

    # Start Telegram command listener in background
    tg_thread = threading.Thread(
        target=_tg_command_loop, args=(state,), daemon=True, name="tg-cmd"
    )
    tg_thread.start()

    # Start web dashboard in background
    dash_thread = threading.Thread(target=_start_dashboard, daemon=True, name="dashboard")
    dash_thread.start()

    is_demo = "demo" in config.KALSHI_API_BASE.lower()

    log.info("=" * 60)
    log.info("Blowout Monitor v2 starting")
    log.info("Environment  : %s", "DEMO" if is_demo else "PRODUCTION")
    log.info("Blowout rule : diff >= %d pts, <= %ds remaining", BLOWOUT_DIFF, BLOWOUT_TIME_SEC)
    log.info("Risk limits  : daily_spend=$%.0f  exposure=$%.0f", MAX_DAILY_SPEND, MAX_TOTAL_EXPOSURE)
    log.info("Poll cadence : %ds normal / %ds final period", POLL_INTERVAL_SEC, POLL_INTERVAL_FINAL)
    log.info("Contracts/bet: %d", NUM_CONTRACTS)
    log.info("State file   : %s", STATE_FILE.resolve())
    log.info("Telegram     : %s", "configured" if TELEGRAM_TOKEN else "not configured")
    log.info("Dashboard    : http://127.0.0.1:%d", DASHBOARD_PORT)
    log.info("=" * 60)

    send_telegram(
        f"Blowout Monitor started\n"
        f"Env: {'DEMO' if is_demo else 'PRODUCTION'}\n"
        f"Rule: >{BLOWOUT_DIFF} pts, <={BLOWOUT_TIME_SEC//60} min\n"
        f"Limits: spend=${MAX_DAILY_SPEND:.0f}, exposure=${MAX_TOTAL_EXPOSURE:.0f}\n"
        f"Send /help for commands"
    )

    while not _shutdown_event.is_set():
        with _state_lock:
            maybe_reset_daily(state)

            # ── Track existing open orders (fills + stale cancellations) ──
            poll_orders(state)
            save_state(state)

            # ── Refresh Kalshi markets ──
            kalshi_markets: dict[str, list[dict]] = {}
            for league in LEAGUES:
                try:
                    kalshi_markets[league.name] = fetch_kalshi_markets(league.kalshi_series)
                except Exception as exc:
                    log.warning("Kalshi fetch failed for %s: %s", league.name, exc)
                    kalshi_markets[league.name] = []

            total_kalshi = sum(len(v) for v in kalshi_markets.values())
            log.info(
                "Kalshi: %d open markets | spend $%.2f/$%.2f | exposure $%.2f/$%.2f | orders %d",
                total_kalshi,
                state.daily_spend, MAX_DAILY_SPEND,
                state.total_exposure, MAX_TOTAL_EXPOSURE,
                len(state.open_orders),
            )

            # Check if daily spend limit is hit — no point scanning
            if state.daily_spend >= MAX_DAILY_SPEND:
                log.warning("Daily spend limit reached ($%.2f) — no new bets today", state.daily_spend)
                _shutdown_event.wait(POLL_INTERVAL_SEC)
                continue

            # ── Scan ESPN ──
            in_final = False

            for league in LEAGUES:
                games = fetch_espn_games(league)
                live  = [g for g in games if g.status == "in"]

                if any(g.period >= league.total_periods for g in live):
                    in_final = True

                if not live:
                    log.debug("%s: no live games", league.name)
                    continue

                log.info("%s: %d live game(s)", league.name, len(live))

                for g in live:
                    t_min = g.time_remaining_sec / 60
                    log.info(
                        "  %s @ %s  %d-%d (diff %d)  P%d %.0fs (%.1f min left)",
                        g.away_team, g.home_team,
                        g.away_score, g.home_score,
                        g.diff, g.period, g.clock_sec, t_min,
                    )

                    if g.espn_id in state.already_traded:
                        continue

                    if not is_blowout(g):
                        continue

                    log.info(
                        "BLOWOUT: %s leads %s by %d pts — %.1f min left",
                        g.leading_team, g.trailing_team, g.diff, t_min,
                    )
                    send_telegram(
                        f"BLOWOUT DETECTED\n"
                        f"{g.away_team} @ {g.home_team} — {league.name}\n"
                        f"{g.leading_team} leads by {g.diff} pts\n"
                        f"Period {g.period} | {t_min:.1f} min remaining"
                    )

                    ticker = find_winning_ticker(g, kalshi_markets.get(league.name, []))
                    if ticker is None:
                        log.warning(
                            "No Kalshi market found for %s @ %s",
                            g.away_team, g.home_team,
                        )
                        send_telegram(
                            f"No Kalshi market found\n"
                            f"{g.away_team} @ {g.home_team} ({league.name})"
                        )
                        # Still mark as "handled" so we don't spam this warning every poll
                        state.already_traded.append(g.espn_id)
                        continue

                    place_bet(ticker, g, state)
                    save_state(state)

        interval = POLL_INTERVAL_FINAL if in_final else POLL_INTERVAL_SEC
        log.info("Sleeping %ds…", interval)
        _shutdown_event.wait(interval)   # interruptible sleep — exits immediately on /stop

    log.info("Monitor loop exited cleanly")


if __name__ == "__main__":
    monitor()
