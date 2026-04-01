"""
Risk management: enforces all position limits before any order is placed.

Checks performed (all must pass):
  1. Daily loss limit not exceeded
  2. Total portfolio exposure under the cap
  3. Per-market exposure under the cap
  4. Open order count under the cap
  5. Not already in a position on this ticker
  6. Not already have an open order on this ticker (no double entry)
"""

from dataclasses import dataclass, field
from typing import Dict, Set

import config
from logger import get_logger

log = get_logger(__name__)


@dataclass
class RiskState:
    """Mutable runtime state tracked by the RiskManager."""
    daily_pnl: float = 0.0                  # Realized P&L today (negative = loss)
    total_exposure: float = 0.0             # Sum of dollars currently at risk
    exposure_by_ticker: Dict[str, float] = field(default_factory=dict)
    open_order_tickers: Set[str] = field(default_factory=set)
    position_tickers: Set[str] = field(default_factory=set)
    open_order_count: int = 0


class RiskCheckFailed(Exception):
    """Raised when a risk check blocks order placement."""


class RiskManager:
    def __init__(self):
        self.state = RiskState()

    # ------------------------------------------------------------------
    # State update methods (called by OrderManager / bot main loop)
    # ------------------------------------------------------------------

    def record_order_placed(self, ticker: str, exposure: float):
        """Call when a new order is submitted."""
        self.state.open_order_tickers.add(ticker)
        self.state.open_order_count += 1
        self.state.total_exposure += exposure
        self.state.exposure_by_ticker[ticker] = (
            self.state.exposure_by_ticker.get(ticker, 0.0) + exposure
        )
        log.info(
            "Risk: order placed on %s — exposure $%.2f | total_exposure $%.2f | open_orders=%d",
            ticker, exposure, self.state.total_exposure, self.state.open_order_count,
        )

    def record_order_filled(self, ticker: str, fill_price: float, num_contracts: int):
        """Call when an order is fully/partially filled."""
        self.state.open_order_tickers.discard(ticker)
        self.state.open_order_count = max(0, self.state.open_order_count - 1)
        self.state.position_tickers.add(ticker)
        log.info(
            "Risk: order filled on %s — price=%.2f contracts=%d",
            ticker, fill_price, num_contracts,
        )

    def record_order_cancelled(self, ticker: str, exposure_returned: float):
        """Call when an order is cancelled (exposure freed)."""
        self.state.open_order_tickers.discard(ticker)
        self.state.open_order_count = max(0, self.state.open_order_count - 1)
        self.state.total_exposure = max(
            0.0, self.state.total_exposure - exposure_returned
        )
        ticker_exp = self.state.exposure_by_ticker.get(ticker, 0.0)
        self.state.exposure_by_ticker[ticker] = max(
            0.0, ticker_exp - exposure_returned
        )
        log.info(
            "Risk: order cancelled on %s — freed $%.2f | total_exposure $%.2f",
            ticker, exposure_returned, self.state.total_exposure,
        )

    def record_position_closed(self, ticker: str, pnl: float):
        """Call when a position resolves (market settles)."""
        self.state.position_tickers.discard(ticker)
        exposure = self.state.exposure_by_ticker.pop(ticker, 0.0)
        self.state.total_exposure = max(0.0, self.state.total_exposure - exposure)
        self.state.daily_pnl += pnl
        log.info(
            "Risk: position closed on %s — pnl=$%.2f | daily_pnl=$%.2f",
            ticker, pnl, self.state.daily_pnl,
        )

    def reset_daily_state(self):
        """Reset daily P&L counter at start of each trading day."""
        log.info("Risk: resetting daily P&L (was $%.2f)", self.state.daily_pnl)
        self.state.daily_pnl = 0.0

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def check_all(self, ticker: str, order_exposure: float):
        """
        Run all risk checks. Raises RiskCheckFailed with a descriptive message
        if any check fails. Call this before placing any order.

        Args:
            ticker:         Market ticker
            order_exposure: Max dollars at risk for this order (price * contracts)
        """
        self._check_daily_loss()
        self._check_no_existing_position(ticker)
        self._check_no_open_order(ticker)
        self._check_open_order_count()
        self._check_per_market_exposure(ticker, order_exposure)
        self._check_total_exposure(order_exposure)

    def _check_daily_loss(self):
        if self.state.daily_pnl <= -config.MAX_DAILY_LOSS:
            raise RiskCheckFailed(
                f"Daily loss limit hit (daily_pnl=${self.state.daily_pnl:.2f}, "
                f"limit=${config.MAX_DAILY_LOSS:.2f}) — no new orders today"
            )

    def _check_no_existing_position(self, ticker: str):
        if ticker in self.state.position_tickers:
            raise RiskCheckFailed(
                f"Already have a position in {ticker} — not adding to existing position"
            )

    def _check_no_open_order(self, ticker: str):
        if ticker in self.state.open_order_tickers:
            raise RiskCheckFailed(
                f"Already have an open order for {ticker} — skipping duplicate entry"
            )

    def _check_open_order_count(self):
        if self.state.open_order_count >= config.MAX_OPEN_ORDERS:
            raise RiskCheckFailed(
                f"Max open orders reached ({self.state.open_order_count}/{config.MAX_OPEN_ORDERS})"
            )

    def _check_per_market_exposure(self, ticker: str, order_exposure: float):
        current = self.state.exposure_by_ticker.get(ticker, 0.0)
        if current + order_exposure > config.MAX_EXPOSURE_PER_MARKET:
            raise RiskCheckFailed(
                f"Per-market exposure limit: {ticker} would have "
                f"${current + order_exposure:.2f} > ${config.MAX_EXPOSURE_PER_MARKET:.2f}"
            )

    def _check_total_exposure(self, order_exposure: float):
        if self.state.total_exposure + order_exposure > config.MAX_TOTAL_EXPOSURE:
            raise RiskCheckFailed(
                f"Total portfolio exposure limit: "
                f"${self.state.total_exposure + order_exposure:.2f} > "
                f"${config.MAX_TOTAL_EXPOSURE:.2f}"
            )

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def daily_loss_limit_hit(self) -> bool:
        return self.state.daily_pnl <= -config.MAX_DAILY_LOSS

    def summary(self) -> str:
        s = self.state
        return (
            f"daily_pnl=${s.daily_pnl:.2f} | "
            f"total_exposure=${s.total_exposure:.2f} | "
            f"open_orders={s.open_order_count} | "
            f"positions={len(s.position_tickers)}"
        )
