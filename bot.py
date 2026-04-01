"""
Main bot loop: orchestrates market discovery, risk checking, and order placement.

Run with:
    python bot.py

Environment variables required for live trading:
    KALSHI_API_KEY_ID        — your Kalshi API key ID
    KALSHI_PRIVATE_KEY_PATH  — path to your RSA private key PEM file

Optional overrides:
    KALSHI_API_BASE          — defaults to demo environment
    LOG_LEVEL                — INFO, DEBUG, WARNING (default: INFO)
"""

import signal
import sys
import time
from datetime import date
from typing import Optional

import config
from auth import verify_auth_config
from logger import get_logger
from market_discovery import scan_for_opportunities, MarketOpportunity
from order_manager import OrderManager
from pricing import kelly_size
from risk_manager import RiskManager, RiskCheckFailed
from websocket_listener import KalshiWebSocketListener
import utils

log = get_logger(__name__)


class KalshiBasketballBot:
    def __init__(self):
        self.risk = RiskManager()
        self.orders = OrderManager(self.risk)
        self.ws_listener = KalshiWebSocketListener(on_snapshot=self._on_ws_snapshot)
        self._running = False
        self._current_day: Optional[date] = None
        self._bankroll: float = 0.0

        # Register shutdown handlers (only works on the main thread)
        try:
            signal.signal(signal.SIGINT, self._handle_shutdown)
            signal.signal(signal.SIGTERM, self._handle_shutdown)
        except ValueError:
            pass  # Running in a background thread (e.g. dashboard)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def run(self):
        """Entry point: validate config, then start the main loop."""
        log.info("=" * 60)
        log.info("Kalshi Basketball Bot starting")
        log.info("API base: %s", config.KALSHI_API_BASE)
        log.info(
            "Risk limits: max_exposure_per_market=$%s | "
            "max_total=$%s | max_daily_loss=$%s",
            config.MAX_EXPOSURE_PER_MARKET,
            config.MAX_TOTAL_EXPOSURE,
            config.MAX_DAILY_LOSS,
        )

        if "demo" not in config.KALSHI_API_BASE.lower():
            log.warning(
                "WARNING: KALSHI_API_BASE appears to be PRODUCTION. "
                "Set KALSHI_API_BASE to demo URL for testing."
            )

        # Validate auth (will raise if keys are missing/invalid)
        try:
            verify_auth_config()
        except Exception as exc:
            log.error("Auth configuration error: %s", exc)
            sys.exit(1)

        # Fetch initial balance
        self._refresh_balance()

        # Start WebSocket listener (non-blocking background thread)
        self.ws_listener.start()

        self._running = True
        self._main_loop()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _main_loop(self):
        log.info("Entering main loop (scan_interval=%ds)", config.SCAN_INTERVAL_SEC)

        order_check_counter = 0

        while self._running:
            try:
                self._maybe_reset_daily_state()

                # --------------------------------------------------------
                # 1. Check daily loss limit
                # --------------------------------------------------------
                if self.risk.daily_loss_limit_hit:
                    log.warning(
                        "Daily loss limit hit — sleeping 60s before recheck. %s",
                        self.risk.summary(),
                    )
                    time.sleep(60)
                    continue

                # --------------------------------------------------------
                # 2. Refresh balance and positions
                # --------------------------------------------------------
                self._refresh_balance()

                # --------------------------------------------------------
                # 3. Cancel stale orders
                # --------------------------------------------------------
                stale_cancelled = self.orders.cancel_stale_orders()
                if stale_cancelled:
                    log.info("Cancelled %d stale orders", stale_cancelled)

                # --------------------------------------------------------
                # 4. Scan for opportunities
                # --------------------------------------------------------
                exclude = (
                    self.orders.get_open_tickers()
                    | self.orders.get_filled_tickers()
                    | self.risk.state.position_tickers
                )

                opportunities = scan_for_opportunities(exclude_tickers=exclude)

                # --------------------------------------------------------
                # 5. Evaluate and trade each opportunity
                # --------------------------------------------------------
                for opp in opportunities:
                    if not self._running:
                        break
                    self._evaluate_opportunity(opp)

                # --------------------------------------------------------
                # 6. Periodically refresh order statuses
                # --------------------------------------------------------
                order_check_counter += 1
                checks_per_scan = max(
                    1,
                    config.SCAN_INTERVAL_SEC // config.ORDER_CHECK_INTERVAL_SEC,
                )
                if order_check_counter % checks_per_scan == 0:
                    self.orders.refresh_orders()
                    log.info("Portfolio state: %s", self.risk.summary())

                # --------------------------------------------------------
                # 7. Sleep until next scan
                # --------------------------------------------------------
                log.info("Sleeping %ds until next scan…", config.SCAN_INTERVAL_SEC)
                self._interruptible_sleep(config.SCAN_INTERVAL_SEC)

            except Exception as exc:
                log.error("Unhandled error in main loop: %s", exc, exc_info=True)
                # Don't crash the loop — back off and retry
                time.sleep(10)

        log.info("Main loop exited")

    # ------------------------------------------------------------------
    # Opportunity evaluation
    # ------------------------------------------------------------------

    def _evaluate_opportunity(self, opp: MarketOpportunity):
        """
        Given a market that passed the discovery filters, decide whether to
        place an order. Logs the full reasoning regardless of outcome.
        """
        ticker = opp.ticker
        buy_price = opp.yes_ask   # We buy at the ask

        # Try to get a fresher quote from the WebSocket cache
        ws_snap = self.ws_listener.get_snapshot(ticker)
        if ws_snap is not None:
            log.debug(
                "%s: using WS snapshot (bid=%.2f ask=%.2f)",
                ticker, ws_snap.yes_bid, ws_snap.yes_ask,
            )
            buy_price = ws_snap.yes_ask
            liquidity = ws_snap.yes_ask_size
            spread = ws_snap.spread
        else:
            liquidity = opp.yes_ask_size
            spread = opp.spread
            # Subscribe this ticker for future WS updates
            self.ws_listener.subscribe([ticker])

        # Re-validate thresholds with potentially fresher data
        if buy_price > config.MAX_BUY_PRICE:
            log.info(
                "SKIP %s: buy_price=%.2f exceeds MAX_BUY_PRICE=%.2f",
                ticker, buy_price, config.MAX_BUY_PRICE,
            )
            return

        if spread > config.MAX_SPREAD:
            log.info("SKIP %s: spread=%.2f exceeds MAX_SPREAD=%.2f", ticker, spread, config.MAX_SPREAD)
            return

        # Position sizing
        sizing = kelly_size(ticker, buy_price, self._bankroll)

        if sizing.num_contracts == 0:
            log.info(
                "SKIP %s: Kelly sizing returned 0 contracts "
                "(edge=%.3f price=%.2f bankroll=$%.2f)",
                ticker, sizing.edge, buy_price, self._bankroll,
            )
            return

        # Risk checks
        try:
            self.risk.check_all(ticker, sizing.max_loss)
        except RiskCheckFailed as exc:
            log.info("SKIP %s: risk check failed — %s", ticker, exc)
            return

        # Validate liquidity once more
        if liquidity < sizing.num_contracts:
            actual_contracts = min(liquidity, sizing.num_contracts)
            if actual_contracts <= 0:
                log.info(
                    "SKIP %s: insufficient liquidity (ask_size=%d, want=%d)",
                    ticker, liquidity, sizing.num_contracts,
                )
                return
            log.info(
                "%s: reducing order from %d to %d contracts due to liquidity",
                ticker, sizing.num_contracts, actual_contracts,
            )
            sizing = kelly_size.__wrapped__(
                ticker, buy_price, self._bankroll
            ) if hasattr(kelly_size, "__wrapped__") else sizing
            # Just override num_contracts directly
            from dataclasses import replace
            sizing = replace(sizing, num_contracts=actual_contracts,
                             max_loss=buy_price * actual_contracts)

        # All checks passed — place the order
        log.info(
            "TRADE %s | title='%s' | bid=%.2f ask=%.2f spread=%.2f | "
            "contracts=%d price=%.2f max_loss=$%.2f EV=$%.3f | "
            "edge=%.3f kelly_raw=%.4f kelly_scaled=%.4f bankroll=$%.2f",
            ticker,
            opp.title[:50],
            opp.yes_bid,
            buy_price,
            spread,
            sizing.num_contracts,
            buy_price,
            sizing.max_loss,
            sizing.expected_value,
            sizing.edge,
            sizing.kelly_fraction,
            sizing.scaled_fraction,
            self._bankroll,
        )

        order = self.orders.place_buy_yes(ticker, buy_price, sizing.num_contracts)
        if order:
            log.info(
                "ORDER SUBMITTED: id=%s ticker=%s contracts=%d price=$%.2f",
                order.order_id, ticker, sizing.num_contracts, buy_price,
            )
        else:
            log.warning("Order submission failed for %s", ticker)

    # ------------------------------------------------------------------
    # Balance refresh
    # ------------------------------------------------------------------

    def _refresh_balance(self):
        try:
            resp = utils.api_request(
                "GET",
                f"{config.KALSHI_API_BASE}/portfolio/balance",
                authenticated=True,
            )
            # Balance may be in cents or dollars depending on the API version
            balance = resp.get("balance") or 0
            # Kalshi returns balance in cents; convert to dollars
            if isinstance(balance, int) and balance > 1000:
                self._bankroll = balance / 100.0
            else:
                self._bankroll = float(balance)

            log.info("Account balance: $%.2f", self._bankroll)
        except Exception as exc:
            log.error("Failed to refresh balance: %s", exc)

    # ------------------------------------------------------------------
    # WebSocket callback
    # ------------------------------------------------------------------

    def _on_ws_snapshot(self, snap):
        """Called from the WS thread whenever a snapshot updates."""
        log.debug(
            "WS update: %s bid=%.2f ask=%.2f spread=%.2f",
            snap.ticker, snap.yes_bid, snap.yes_ask, snap.spread,
        )

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def _maybe_reset_daily_state(self):
        today = date.today()
        if self._current_day is None:
            self._current_day = today
        elif today != self._current_day:
            log.info("New trading day detected — resetting daily P&L")
            self.risk.reset_daily_state()
            self._current_day = today

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _handle_shutdown(self, signum, frame):
        log.info("Shutdown signal received (%s) — cleaning up…", signum)
        self._running = False
        self._shutdown()

    def _shutdown(self):
        log.info("Cancelling all open orders…")
        for order in self.orders.all_orders():
            from order_manager import OrderStatus
            if order.status in (OrderStatus.OPEN, OrderStatus.PENDING):
                self.orders.cancel_order(order.order_id)

        self.ws_listener.stop()

        log.info("Final state: %s", self.risk.summary())
        log.info("Bot shut down cleanly")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, seconds: float):
        """Sleep in small increments so SIGINT is handled promptly."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot = KalshiBasketballBot()
    bot.run()
