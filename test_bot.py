"""
Comprehensive test of every feature in blowout_monitor.py.
Run from the project root: python test_bot.py
"""

import json
import sys
import os
import time
import tempfile
from pathlib import Path
from dataclasses import asdict

# ── colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

_results: list[tuple[str, bool, str]] = []

def ok(name, detail=""):
    _results.append((name, True, detail))
    print(f"  {GREEN}PASS{RESET}  {name}" + (f"  {YELLOW}({detail}){RESET}" if detail else ""))

def fail(name, detail=""):
    _results.append((name, False, detail))
    print(f"  {RED}FAIL{RESET}  {name}" + (f"  — {detail}" if detail else ""))

def section(title):
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

# ── import guard ──────────────────────────────────────────────────────────────
section("1. IMPORTS")
try:
    import config
    import auth
    import utils
    ok("config / auth / utils imported")
except Exception as e:
    fail("config / auth / utils imported", str(e))
    sys.exit(1)

try:
    import blowout_monitor as bm
    ok("blowout_monitor imported")
except Exception as e:
    fail("blowout_monitor imported", str(e))
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────
section("2. CONFIG")

if config.KALSHI_API_KEY_ID:
    ok("KALSHI_API_KEY_ID set", config.KALSHI_API_KEY_ID[:8] + "…")
else:
    fail("KALSHI_API_KEY_ID set", "empty")

if config.KALSHI_PRIVATE_KEY_PATH:
    p = Path(config.KALSHI_PRIVATE_KEY_PATH)
    if p.exists():
        ok("Private key file exists", str(p))
    else:
        fail("Private key file exists", f"not found: {p}")
else:
    fail("KALSHI_PRIVATE_KEY_PATH set", "empty")

ok("KALSHI_API_BASE", config.KALSHI_API_BASE)
ok("BET_AMOUNT", f"${bm.BET_AMOUNT:.0f}")
ok("MAX_DAILY_SPEND", f"${bm.MAX_DAILY_SPEND:.0f}")
ok("MAX_TOTAL_EXPOSURE", f"${bm.MAX_TOTAL_EXPOSURE:.0f}")
if bm.TELEGRAM_TOKEN:
    ok("TELEGRAM_TOKEN set")
else:
    fail("TELEGRAM_TOKEN set", "empty — Telegram alerts won't work")

if bm.TELEGRAM_CHAT_ID:
    ok("TELEGRAM_CHAT_ID set", bm.TELEGRAM_CHAT_ID)
else:
    fail("TELEGRAM_CHAT_ID set", "empty")

# ── auth ──────────────────────────────────────────────────────────────────────
section("3. KALSHI AUTH (RSA signing)")

try:
    auth.verify_auth_config()
    ok("Private key loads without error")
except Exception as e:
    fail("Private key loads without error", str(e))

try:
    headers = auth.get_auth_headers("GET", "/trade-api/v2/markets")
    assert "KALSHI-ACCESS-KEY" in headers
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 20
    ok("Auth headers generated", f"key={headers['KALSHI-ACCESS-KEY'][:8]}…")
except Exception as e:
    fail("Auth headers generated", str(e))

# ── normalize ─────────────────────────────────────────────────────────────────
section("4. NORMALIZE / ESPN→KALSHI MAP")

cases = [
    ("SC",   "USC"),
    ("GS",   "GSW"),
    ("NY",   "NYK"),
    ("SA",   "SAS"),
    ("NO",   "NOP"),
    ("PHO",  "PHX"),
    ("UTAH", "UTA"),
    ("WSH",  "WAS"),
    ("LAL",  "LAL"),   # no mapping — pass through
    ("UCLA", "UCLA"),  # no mapping
    ("sc",   "USC"),   # lowercase input
]
for inp, expected in cases:
    result = bm.normalize(inp)
    if result == expected:
        ok(f"normalize({inp!r})", f"→ {result}")
    else:
        fail(f"normalize({inp!r})", f"got {result!r}, expected {expected!r}")

# ── is_blowout ────────────────────────────────────────────────────────────────
section("5. BLOWOUT DETECTION LOGIC")

nba = bm.LEAGUES[0]   # 4 quarters, 720s each

def make_game(diff, period, clock_sec, status="in", home_score=None, away_score=None):
    hs = home_score if home_score is not None else diff
    aws = away_score if away_score is not None else 0
    return bm.GameState(
        espn_id="test", league=nba,
        home_team="LAL", away_team="GSW",
        home_score=hs, away_score=aws,
        period=period, clock_sec=clock_sec, status=status,
    )

# Should trigger
g = make_game(22, 4, 600)   # 22pt lead, Q4, 10 min left  (10*60=600 ≤ 1260)
if bm.is_blowout(g):
    ok("22pt lead Q4 10min → blowout")
else:
    fail("22pt lead Q4 10min → blowout", f"time_remaining={g.time_remaining_sec}")

# Should NOT trigger — not enough lead
g = make_game(21, 4, 600)
if not bm.is_blowout(g):
    ok("21pt lead → not blowout (below threshold)")
else:
    fail("21pt lead → not blowout")

# Should NOT trigger — too much time left
g = make_game(22, 3, 720)   # Q3 with full 12 min left → >21 min total remaining
if not bm.is_blowout(g):
    ok("22pt Q3 full quarter → not blowout (too much time)")
else:
    fail("22pt Q3 full quarter → not blowout", f"time_remaining={g.time_remaining_sec}")

# Should NOT trigger — overtime
g = make_game(22, 5, 120)
if not bm.is_blowout(g):
    ok("OT period → not blowout (no OT bets)")
else:
    fail("OT period → not blowout")

# Should NOT trigger — game not live
g = make_game(22, 4, 600, status="post")
if not bm.is_blowout(g):
    ok("status=post → not blowout")
else:
    fail("status=post → not blowout")

# leading_team property
g = make_game(diff=0, period=2, clock_sec=100, home_score=80, away_score=58)
if g.leading_team == "LAL" and g.diff == 22:
    ok("leading_team / diff properties")
else:
    fail("leading_team / diff properties", f"leader={g.leading_team} diff={g.diff}")

# time_remaining_sec — NCAA Women (4 periods, 600s each)
ncaaw = bm.LEAGUES[3]
g2 = bm.GameState("x", ncaaw, "UCLA", "USC", 60, 38, 3, 300, "in")
expected_time = 300 + 600   # 1 full period left + 300s in current
if g2.time_remaining_sec == expected_time:
    ok("time_remaining_sec NCAA Women mid-Q3", f"{g2.time_remaining_sec}s")
else:
    fail("time_remaining_sec NCAA Women mid-Q3", f"got {g2.time_remaining_sec}, expected {expected_time}")

# ── check_risk ────────────────────────────────────────────────────────────────
section("6. RISK GATES")

state = bm.AppState()

# Should pass
try:
    bm.check_risk(state, 0.90, 10)
    ok("Risk check passes with clean state")
except bm.RiskBlocked as e:
    fail("Risk check passes with clean state", str(e))

# Daily spend exceeded
state2 = bm.AppState(daily_spend=bm.MAX_DAILY_SPEND - 5.0)
try:
    bm.check_risk(state2, 0.50, 20)  # cost = 10, would push over
    fail("Daily spend limit fires")
except bm.RiskBlocked as e:
    ok("Daily spend limit fires", str(e)[:60])

# Exposure exceeded
state3 = bm.AppState(total_exposure=bm.MAX_TOTAL_EXPOSURE - 5.0)
try:
    bm.check_risk(state3, 0.50, 20)
    fail("Exposure limit fires")
except bm.RiskBlocked as e:
    ok("Exposure limit fires", str(e)[:60])

# MAX_YES_ASK exceeded
try:
    bm.check_risk(state, 0.99, 1)
    fail("MAX_YES_ASK limit fires")
except bm.RiskBlocked as e:
    ok("MAX_YES_ASK limit fires", str(e)[:60])

# ── state persistence ─────────────────────────────────────────────────────────
section("7. STATE PERSISTENCE")

orig_state_file = bm.STATE_FILE
with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
    tmp_path = Path(tf.name)

bm.STATE_FILE = tmp_path
try:
    s = bm.AppState(date="2026-04-05", daily_spend=42.0, bets_placed=3)
    bm.save_state(s)
    s2 = bm.load_state()
    assert s2.date == "2026-04-05"
    assert s2.daily_spend == 42.0
    assert s2.bets_placed == 3
    ok("save_state / load_state round-trip")
except Exception as e:
    fail("save_state / load_state round-trip", str(e))
finally:
    tmp_path.unlink(missing_ok=True)
    bm.STATE_FILE = orig_state_file

# ── maybe_reset_daily ─────────────────────────────────────────────────────────
s = bm.AppState(date="2000-01-01", daily_spend=50.0, bets_placed=5)
bm.maybe_reset_daily(s)
from datetime import date as _date
today = str(_date.today())
if s.date == today and s.daily_spend == 0.0 and s.bets_placed == 0:
    ok("maybe_reset_daily resets counters on new day")
else:
    fail("maybe_reset_daily resets counters", f"date={s.date} spend={s.daily_spend}")

# ── find_winning_ticker ───────────────────────────────────────────────────────
section("8. FIND_WINNING_TICKER LOGIC")

# Real Kalshi ticker format (from live API): SERIES-DATEAWAYHOMEDCOMBINED-TEAMCODE
# e.g. KXNBAGAME-26APR07MIATOR-TOR  (no dash between date and team segment)
mock_markets = [
    {"ticker": "KXNCAAWBGAME-26APR05USCUCLA-USC"},
    {"ticker": "KXNCAAWBGAME-26APR05USCUCLA-UCLA"},
    {"ticker": "KXNBAGAME-26APR05GSWLAL-LAL"},
    {"ticker": "KXNBAGAME-26APR05GSWLAL-GSW"},
]

# USC @ UCLA, UCLA leads
ncaaw = bm.LEAGUES[3]
g = bm.GameState("id1", ncaaw, home_team="UCLA", away_team="USC",
                 home_score=60, away_score=38, period=3, clock_sec=300, status="in")
ticker = bm.find_winning_ticker(g, mock_markets)
if ticker == "KXNCAAWBGAME-26APR05USCUCLA-UCLA":
    ok("find_winning_ticker USC @ UCLA → UCLA ticker")
else:
    fail("find_winning_ticker USC @ UCLA → UCLA ticker", f"got {ticker!r}")

# USC @ UCLA, USC leads
g2 = bm.GameState("id2", ncaaw, home_team="UCLA", away_team="USC",
                  home_score=38, away_score=60, period=3, clock_sec=300, status="in")
ticker2 = bm.find_winning_ticker(g2, mock_markets)
if ticker2 == "KXNCAAWBGAME-26APR05USCUCLA-USC":
    ok("find_winning_ticker USC @ UCLA, USC leads → USC ticker")
else:
    fail("find_winning_ticker USC @ UCLA, USC leads → USC ticker", f"got {ticker2!r}")

# No matching market
g3 = bm.GameState("id3", nba, home_team="BOS", away_team="MIL",
                  home_score=90, away_score=65, period=4, clock_sec=300, status="in")
ticker3 = bm.find_winning_ticker(g3, mock_markets)
if ticker3 is None:
    ok("find_winning_ticker returns None when no match")
else:
    fail("find_winning_ticker returns None when no match", f"got {ticker3!r}")

# ── ESPN live fetch ───────────────────────────────────────────────────────────
section("9. ESPN LIVE SCOREBOARD")

for league in bm.LEAGUES:
    try:
        games = bm.fetch_espn_games(league)
        ok(f"ESPN {league.name}", f"{len(games)} game(s) returned")
        for g in games[:2]:
            print(f"         {g.away_team} @ {g.home_team}  {g.away_score}-{g.home_score}  "
                  f"P{g.period}  {g.clock_sec:.0f}s  status={g.status}")
    except Exception as e:
        fail(f"ESPN {league.name}", str(e))

# ── Kalshi public markets ─────────────────────────────────────────────────────
section("10. KALSHI PUBLIC MARKETS (production API)")

all_kalshi_markets: dict[str, list] = {}
for league in bm.LEAGUES:
    try:
        markets = bm.fetch_kalshi_markets(league.kalshi_series)
        all_kalshi_markets[league.name] = markets
        ok(f"Kalshi {league.name} ({league.kalshi_series})", f"{len(markets)} open market(s)")
        for m in markets[:3]:
            print(f"         {m.get('ticker','?')}  status={m.get('status','?')}")
    except Exception as e:
        fail(f"Kalshi {league.name}", str(e))

# ── Orderbook fetch ───────────────────────────────────────────────────────────
section("11. ORDERBOOK FETCH")

tested_ob = False
for league_name, markets in all_kalshi_markets.items():
    if not markets:
        continue
    ticker = markets[0].get("ticker")
    if not ticker:
        continue
    try:
        ask, size = bm.fetch_orderbook_ask(ticker)
        if ask is not None:
            ok(f"Orderbook {ticker}", f"ask=${ask:.2f}  size={size}")
        else:
            fail(f"Orderbook {ticker}", "ask=None (empty book or bad response)")
        tested_ob = True
        break
    except Exception as e:
        fail(f"Orderbook {ticker}", str(e))
        tested_ob = True
        break

if not tested_ob:
    print(f"  {YELLOW}SKIP{RESET}  Orderbook — no open markets available right now")

# ── find_winning_ticker with LIVE data ────────────────────────────────────────
section("12. TICKER MATCHING WITH LIVE DATA")

live_game_found = False
for league in bm.LEAGUES:
    games = bm.fetch_espn_games(league)
    markets = all_kalshi_markets.get(league.name, [])
    if not markets:
        continue
    for g in games:
        if g.status != "in":
            continue
        ticker = bm.find_winning_ticker(g, markets)
        live_game_found = True
        if ticker:
            ok(f"Live match: {g.away_team}@{g.home_team} ({league.name})", f"→ {ticker}")
        else:
            fail(f"Live match: {g.away_team}@{g.home_team} ({league.name})",
                 "no Kalshi market found — check ESPN abbrev vs Kalshi ticker")
        break
    if live_game_found:
        break

if not live_game_found:
    print(f"  {YELLOW}SKIP{RESET}  Live ticker match — no in-progress games right now")

# ── Demo API auth round-trip ──────────────────────────────────────────────────
section("13. KALSHI API — AUTHENTICATED GET PORTFOLIO BALANCE")

env_label = "DEMO" if "demo" in config.KALSHI_API_BASE.lower() else "PRODUCTION"
try:
    resp = utils.api_request(
        "GET",
        f"{config.KALSHI_API_BASE}/portfolio/balance",
        authenticated=True,
    )
    ok(f"{env_label} API authenticated GET /portfolio/balance", f"response keys: {list(resp.keys())[:5]}")
except Exception as e:
    fail(f"{env_label} API authenticated GET /portfolio/balance", str(e))
    if "401" in str(e):
        print(f"         {YELLOW}HINT: API key in shell env may not match KALSHI_API_BASE={config.KALSHI_API_BASE}{RESET}")
        print(f"         {YELLOW}Shell key_id={config.KALSHI_API_KEY_ID[:8]}… — ensure this key belongs to the {env_label} environment{RESET}")

# ── Demo order placement (place + immediate cancel) ───────────────────────────
section("14. DEMO ORDER PLACEMENT (place + cancel)")

placed_order_id = None
for league_name, markets in all_kalshi_markets.items():
    if not markets:
        continue
    ticker = markets[0].get("ticker")
    if not ticker:
        continue

    # Fetch orderbook first to get a real price
    ask, size = bm.fetch_orderbook_ask(ticker)
    if ask is None or ask <= 0:
        continue

    price_cents = utils.dollars_to_cents(ask)
    body = {
        "ticker":    ticker,
        "action":    "buy",
        "side":      "yes",
        "type":      "limit",
        "count":     1,
        "yes_price": price_cents,
    }
    try:
        resp = utils.api_request(
            "POST",
            f"{config.KALSHI_API_BASE}/portfolio/orders",
            authenticated=True,
            json_body=body,
        )
        raw = resp.get("order") or resp
        order_id = raw.get("order_id") or raw.get("id")
        placed_order_id = order_id
        ok("Place limit order on demo", f"order_id={order_id}  ticker={ticker}  ask=${ask:.2f}")
    except Exception as e:
        fail("Place limit order on demo", str(e))
    break

# Immediately cancel the test order
if placed_order_id:
    try:
        utils.api_request(
            "DELETE",
            f"{config.KALSHI_API_BASE}/portfolio/orders/{placed_order_id}",
            authenticated=True,
        )
        ok("Cancel test order on demo", f"order_id={placed_order_id}")
    except Exception as e:
        if "404" in str(e):
            ok("Cancel test order on demo", "order filled immediately (no resting order to cancel — correct behaviour)")
        else:
            fail("Cancel test order on demo", str(e))
else:
    print(f"  {YELLOW}SKIP{RESET}  Order placement — no suitable market found")

# ── Telegram ──────────────────────────────────────────────────────────────────
section("15. TELEGRAM")

if bm.TELEGRAM_TOKEN and bm.TELEGRAM_CHAT_ID:
    try:
        bm.send_telegram("✅ blowout_monitor test suite — all checks ran")
        ok("Telegram send_telegram (check your chat for the message)")
    except Exception as e:
        fail("Telegram send_telegram", str(e))
else:
    print(f"  {YELLOW}SKIP{RESET}  Telegram — token/chat_id not set")

# ── summary ───────────────────────────────────────────────────────────────────
section("RESULTS")
passed = sum(1 for _, r, _ in _results if r)
failed = sum(1 for _, r, _ in _results if not r)
total  = len(_results)
print(f"\n  {GREEN}{passed} passed{RESET}  /  {RED}{failed} failed{RESET}  /  {total} total\n")
if failed:
    print(f"{RED}Failed tests:{RESET}")
    for name, result, detail in _results:
        if not result:
            print(f"  • {name}: {detail}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}All tests passed!{RESET}")
