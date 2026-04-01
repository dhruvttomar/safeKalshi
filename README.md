# Kalshi Basketball Bot

A Python trading bot that monitors Kalshi prediction markets for NBA, WNBA, and NCAA basketball games where one team has a 90%+ implied probability of winning, then places sized limit orders on the heavy favorite's YES side.

## Architecture

```
kalshi_basketball_bot/
├── config.py              # All tunable parameters — risk limits, thresholds, API base
├── auth.py                # RSA PKCS1v15 request signing
├── logger.py              # Rotating file + console logging
├── utils.py               # Shared HTTP client with 429 backoff
├── market_discovery.py    # Series/market filtering, orderbook fetching, opportunity scan
├── pricing.py             # Kelly Criterion position sizing + EV calculation
├── risk_manager.py        # Pre-trade risk gates + runtime exposure tracking
├── order_manager.py       # Place, track, cancel, reconcile orders
├── websocket_listener.py  # Real-time orderbook updates (background thread)
├── bot.py                 # Main loop
└── requirements.txt
```

## Setup

```bash
pip install -r requirements.txt
```

Generate an RSA key pair in Kalshi's dashboard, download the PEM private key, then:

```bash
export KALSHI_API_KEY_ID="your-key-id-here"
export KALSHI_PRIVATE_KEY_PATH="/path/to/private_key.pem"
# Leave KALSHI_API_BASE unset to default to the DEMO environment
```

## Running

```bash
cd kalshi_basketball_bot
python bot.py
```

Ctrl+C triggers a graceful shutdown: all open orders are cancelled before exit.

## Configuration

All parameters live in `config.py` and can be overridden via environment variables.

| Parameter | Default | Description |
|---|---|---|
| `KALSHI_API_BASE` | `https://demo-api.kalshi.co/trade-api/v2` | **Demo by default** |
| `MIN_IMPLIED_PROB` | `0.90` | Only trade favorites at 90%+ |
| `MAX_BUY_PRICE` | `0.95` | Never pay more than $0.95/contract |
| `MIN_LIQUIDITY_CONTRACTS` | `10` | Minimum ask-side liquidity |
| `MAX_SPREAD` | `0.05` | Max bid-ask spread |
| `MAX_EXPOSURE_PER_MARKET` | `$50` | Per-market risk cap |
| `MAX_TOTAL_EXPOSURE` | `$500` | Total portfolio risk cap |
| `MAX_DAILY_LOSS` | `$25` | Stop trading after this daily loss |
| `KELLY_MULTIPLIER` | `0.25` | Quarter-Kelly sizing |
| `SCAN_INTERVAL_SEC` | `60` | Seconds between market scans |

## Switching to Production

Only after thorough demo testing:

```bash
export KALSHI_API_BASE="https://api.elections.kalshi.com/trade-api/v2"
```

## Trading Logic

1. Fetch all open basketball game-winner markets (NBA / WNBA / NCAA Men's+Women's)
2. Filter out totals, props, futures, and season awards — **only single-game moneylines**
3. For each qualifying market, fetch the orderbook
4. Check: `yes_bid >= 0.90`, `yes_ask <= 0.95`, spread `<= 0.05`, liquidity `>= 10`
5. Size the order with Quarter-Kelly: `edge / (1 - price) * 0.25 * bankroll / price`
6. Run risk gates (per-market cap, total exposure cap, daily loss, no double-entry)
7. Place a LIMIT BUY YES order at the current best ask
8. Cancel unfilled orders after 5 minutes

## Risk Controls

- Per-market exposure cap (`MAX_EXPOSURE_PER_MARKET`)
- Total portfolio exposure cap (`MAX_TOTAL_EXPOSURE`)
- Daily loss limit — bot stops trading for the day if hit (`MAX_DAILY_LOSS`)
- Max simultaneous open orders (`MAX_OPEN_ORDERS`)
- No averaging down — skips any ticker already in positions
- Stale order cancellation after 5 minutes
- Quarter-Kelly sizing to limit variance

## Testing Checklist

- [ ] Bot starts and authenticates against demo environment
- [ ] Discovers basketball series correctly
- [ ] Filters only game-winner markets (not totals, props, futures)
- [ ] Correctly identifies 90%+ favorites from orderbook data
- [ ] Calculates Kelly sizing correctly
- [ ] Respects all risk limits
- [ ] Places limit orders and tracks fills
- [ ] Cancels stale orders
- [ ] Handles rate limits gracefully (429 backoff)
- [ ] Logs every decision with full context
- [ ] Graceful shutdown on Ctrl+C
- [ ] Does NOT trade in prod until explicitly configured
