"""
WebSocket listener for real-time price updates from Kalshi.

Kalshi WebSocket endpoint (v2):
    wss://demo-api.kalshi.co/trade-api/ws/v2   (demo)
    wss://api.elections.kalshi.com/trade-api/ws/v2  (prod)

Authentication: same RSA signing, but the token is sent as part of the
initial subscribe/auth message, NOT as HTTP headers.

Protocol:
  1. Open the WebSocket connection
  2. Send an "subscribe" command with channel="orderbook_delta" and market IDs
  3. Receive a "subscribed" acknowledgment
  4. Receive "orderbook_delta" messages as the book updates

This module runs the WebSocket in a background thread and provides a
thread-safe cache of latest orderbook snapshots so the main trading loop
can consume them without making REST calls.
"""

import json
import threading
import time
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

try:
    import websocket  # websocket-client library
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

import config
from auth import get_auth_headers
from logger import get_logger
from market_discovery import OrderbookSnapshot

log = get_logger(__name__)


def _ws_base_url() -> str:
    """Derive the WebSocket base URL from the REST base URL."""
    rest = config.KALSHI_API_BASE
    parsed = urlparse(rest)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    # Kalshi WS path convention: replace /trade-api/v2 with /trade-api/ws/v2
    ws_path = parsed.path.replace("/v2", "/ws/v2")
    return f"{scheme}://{parsed.netloc}{ws_path}"


class KalshiWebSocketListener:
    """
    Background WebSocket listener that maintains a live orderbook cache.

    Usage:
        listener = KalshiWebSocketListener()
        listener.subscribe(["KXNBA-25-0401-T1", "KXNBA-25-0401-T2"])
        listener.start()
        ...
        snap = listener.get_snapshot("KXNBA-25-0401-T1")
        ...
        listener.stop()
    """

    def __init__(self, on_snapshot: Optional[Callable[[OrderbookSnapshot], None]] = None):
        """
        Args:
            on_snapshot: Optional callback invoked whenever a snapshot is updated.
                         Runs in the WebSocket thread — keep it fast and non-blocking.
        """
        self._last_close_was_auth_error = False
        if not WS_AVAILABLE:
            log.warning(
                "websocket-client not installed — WebSocket listener disabled. "
                "The bot will fall back to REST polling."
            )

        self._on_snapshot = on_snapshot
        self._snapshots: Dict[str, OrderbookSnapshot] = {}
        self._lock = threading.Lock()
        self._subscribed_tickers: set = set()
        self._ws: Optional[object] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # Track the full orderbook state for each ticker so we can reconstruct
        # a snapshot from deltas
        self._book_yes: Dict[str, Dict[int, int]] = {}   # ticker → {price_cents: size}
        self._book_no: Dict[str, Dict[int, int]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, tickers: list):
        """Add tickers to the subscription list. Safe to call before or after start()."""
        with self._lock:
            self._subscribed_tickers.update(tickers)
            log.debug("WebSocket: queued %d tickers for subscription", len(tickers))

    def start(self):
        """Start the background WebSocket thread. No-op if already running."""
        if not WS_AVAILABLE:
            return

        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_forever,
            name="kalshi-ws",
            daemon=True,
        )
        self._thread.start()
        log.info("WebSocket listener started (thread=%s)", self._thread.name)

    def stop(self):
        """Signal the listener to stop and wait for the thread to exit."""
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("WebSocket listener stopped")

    def get_snapshot(self, ticker: str) -> Optional[OrderbookSnapshot]:
        """Return the latest cached orderbook snapshot, or None if not yet received."""
        with self._lock:
            return self._snapshots.get(ticker)

    # ------------------------------------------------------------------
    # Internal WebSocket loop
    # ------------------------------------------------------------------

    def _run_forever(self):
        """Main loop: connect, subscribe, reconnect on disconnect."""
        backoff = 1.0
        while self._running:
            try:
                self._last_close_was_auth_error = False
                self._connect_and_run()
                if self._last_close_was_auth_error:
                    log.error(
                        "WebSocket auth failed (INCORRECT_API_KEY_SIGNATURE). "
                        "Check KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH. "
                        "Bot will continue using REST polling. Retrying WS in 120s."
                    )
                    time.sleep(120)
                    continue
                backoff = 1.0  # reset backoff on clean exit
            except Exception as exc:
                log.error("WebSocket error: %s — reconnecting in %.1fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _connect_and_run(self):
        if not WS_AVAILABLE:
            return

        ws_url = _ws_base_url()
        log.info("Connecting to Kalshi WebSocket: %s", ws_url)

        # Build auth headers for the HTTP upgrade request
        path = urlparse(ws_url).path
        auth_headers = get_auth_headers("GET", path)

        ws = websocket.WebSocketApp(
            ws_url,
            header=[f"{k}: {v}" for k, v in auth_headers.items()],
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws

        # Blocks until connection is closed
        ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws):
        log.info("WebSocket connection established")
        self._send_subscriptions(ws)

    def _send_subscriptions(self, ws):
        """Send channel subscription message for all queued tickers."""
        with self._lock:
            tickers = list(self._subscribed_tickers)

        if not tickers:
            log.debug("No tickers to subscribe to yet")
            return

        msg = {
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }
        ws.send(json.dumps(msg))
        log.info("WebSocket: subscribed to %d tickers", len(tickers))

    def _on_message(self, ws, raw_message: str):
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            log.warning("WebSocket: received non-JSON message: %s", raw_message[:200])
            return

        msg_type = msg.get("type") or msg.get("cmd", "")

        if msg_type == "subscribed":
            log.debug("WebSocket: subscription confirmed")
        elif msg_type == "orderbook_snapshot":
            self._handle_snapshot(msg)
        elif msg_type == "orderbook_delta":
            self._handle_delta(msg)
        elif msg_type == "error":
            log.error("WebSocket server error: %s", msg)

    def _handle_snapshot(self, msg: dict):
        """Full orderbook snapshot — replace the cached book entirely."""
        data = msg.get("msg") or msg
        ticker = data.get("market_ticker", "")
        if not ticker:
            return

        yes_levels = data.get("yes") or []
        no_levels = data.get("no") or []

        with self._lock:
            self._book_yes[ticker] = {int(p): int(s) for p, s in yes_levels}
            self._book_no[ticker] = {int(p): int(s) for p, s in no_levels}
            snap = self._build_snapshot(ticker)
            if snap:
                self._snapshots[ticker] = snap

        if snap and self._on_snapshot:
            self._on_snapshot(snap)

    def _handle_delta(self, msg: dict):
        """Incremental orderbook update — apply the delta to the cached book."""
        data = msg.get("msg") or msg
        ticker = data.get("market_ticker", "")
        if not ticker:
            return

        price = int(data.get("price", 0))
        delta = int(data.get("delta", 0))
        side = data.get("side", "yes")

        with self._lock:
            book = self._book_yes if side == "yes" else self._book_no
            if ticker not in book:
                book[ticker] = {}

            current = book[ticker].get(price, 0)
            new_size = current + delta
            if new_size <= 0:
                book[ticker].pop(price, None)
            else:
                book[ticker][price] = new_size

            snap = self._build_snapshot(ticker)
            if snap:
                self._snapshots[ticker] = snap

        if snap and self._on_snapshot:
            self._on_snapshot(snap)

    def _build_snapshot(self, ticker: str) -> Optional[OrderbookSnapshot]:
        """Reconstruct an OrderbookSnapshot from the internal book dicts."""
        yes_book = self._book_yes.get(ticker, {})
        no_book = self._book_no.get(ticker, {})

        if not yes_book and not no_book:
            return None

        # Best YES bid: highest price in yes_book
        if yes_book:
            best_yes_bid_cents = max(yes_book.keys())
            yes_bid = best_yes_bid_cents / 100.0
            yes_bid_size = yes_book[best_yes_bid_cents]
        else:
            yes_bid = 0.0
            yes_bid_size = 0

        # Best YES ask = 100 - best NO bid
        if no_book:
            best_no_bid_cents = max(no_book.keys())
            yes_ask = (100 - best_no_bid_cents) / 100.0
            yes_ask_size = no_book[best_no_bid_cents]
        else:
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

    def _on_error(self, ws, error):
        error_str = str(error)
        if "401" in error_str or "authentication_error" in error_str.lower():
            self._last_close_was_auth_error = True
        else:
            log.error("WebSocket error event: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        log.warning(
            "WebSocket closed: code=%s msg=%s", close_status_code, close_msg
        )
        self._ws = None
