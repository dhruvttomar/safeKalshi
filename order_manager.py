"""
Order lifecycle management: place, track, cancel, and reconcile orders.

All order state is held in memory. On startup the bot refreshes from the API
so that the in-memory state stays consistent with reality.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import config
import utils
from auth import get_auth_headers
from logger import get_logger
from risk_manager import RiskManager

log = get_logger(__name__)

BASE = config.KALSHI_API_BASE


class OrderStatus(str, Enum):
    PENDING   = "pending"     # submitted, not yet confirmed by API
    OPEN      = "open"        # resting on the book
    FILLED    = "filled"      # fully filled
    CANCELLED = "cancelled"   # cancelled by us or expired
    REJECTED  = "rejected"    # rejected by exchange


@dataclass
class Order:
    order_id: str
    ticker: str
    side: str               # "yes" or "no"
    action: str             # "buy" or "sell"
    price: float            # dollars
    num_contracts: int
    status: OrderStatus
    submitted_at: float     # Unix timestamp
    filled_contracts: int = 0
    avg_fill_price: float = 0.0
    exposure: float = 0.0   # price * num_contracts (max loss)


class OrderManager:
    def __init__(self, risk_manager: RiskManager):
        self.risk = risk_manager
        self._orders: Dict[str, Order] = {}   # order_id → Order

    # ------------------------------------------------------------------
    # Placing orders
    # ------------------------------------------------------------------

    def place_buy_yes(
        self,
        ticker: str,
        price: float,
        num_contracts: int,
    ) -> Optional[Order]:
        """
        Place a limit BUY YES order on Kalshi.

        Args:
            ticker:        Market ticker
            price:         Limit price in dollars (e.g. 0.92)
            num_contracts: Number of contracts to buy

        Returns:
            Order object if placement was accepted, None otherwise.
        """
        if num_contracts <= 0:
            log.warning("place_buy_yes called with num_contracts=%d — skipping", num_contracts)
            return None

        price_cents = utils.dollars_to_cents(price)
        exposure = price * num_contracts

        # Final risk gate (should already have been checked by caller, but belt-and-suspenders)
        try:
            self.risk.check_all(ticker, exposure)
        except Exception as exc:
            log.warning("Risk check blocked order for %s: %s", ticker, exc)
            return None

        body = {
            "ticker": ticker,
            "action": "buy",
            "side": "yes",
            "type": "limit",
            "count": num_contracts,
            "yes_price": price_cents,
        }

        log.info(
            "Placing BUY YES: %s — %d contracts @ $%.2f (exposure $%.2f)",
            ticker, num_contracts, price, exposure,
        )

        try:
            resp = utils.api_request(
                "POST",
                f"{BASE}/portfolio/orders",
                authenticated=True,
                json_body=body,
            )
        except Exception as exc:
            log.error("Failed to place order for %s: %s", ticker, exc)
            return None

        raw_order = resp.get("order") or resp
        order_id = raw_order.get("order_id") or raw_order.get("id", "")
        if not order_id:
            log.error("API returned no order_id for %s: %s", ticker, resp)
            return None

        order = Order(
            order_id=order_id,
            ticker=ticker,
            side="yes",
            action="buy",
            price=price,
            num_contracts=num_contracts,
            status=OrderStatus.OPEN,
            submitted_at=time.time(),
            exposure=exposure,
        )
        self._orders[order_id] = order

        self.risk.record_order_placed(ticker, exposure)

        log.info(
            "Order placed: id=%s ticker=%s contracts=%d price=$%.2f",
            order_id, ticker, num_contracts, price,
        )
        return order

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order by ID.
        Returns True if the cancellation request succeeded.
        """
        order = self._orders.get(order_id)
        if order is None:
            log.warning("cancel_order: unknown order_id %s", order_id)
            return False

        if order.status not in (OrderStatus.OPEN, OrderStatus.PENDING):
            log.debug("cancel_order: order %s already in status %s", order_id, order.status)
            return False

        try:
            utils.api_request(
                "DELETE",
                f"{BASE}/portfolio/orders/{order_id}",
                authenticated=True,
            )
        except Exception as exc:
            log.error("Failed to cancel order %s: %s", order_id, exc)
            return False

        order.status = OrderStatus.CANCELLED
        unfilled_exposure = order.price * (order.num_contracts - order.filled_contracts)
        self.risk.record_order_cancelled(order.ticker, unfilled_exposure)

        log.info(
            "Order cancelled: id=%s ticker=%s unfilled_contracts=%d",
            order_id, order.ticker, order.num_contracts - order.filled_contracts,
        )
        return True

    def cancel_stale_orders(self) -> int:
        """
        Cancel all open orders older than STALE_ORDER_TIMEOUT_SEC.
        Returns the number of orders cancelled.
        """
        now = time.time()
        cancelled = 0
        for order_id, order in list(self._orders.items()):
            if order.status != OrderStatus.OPEN:
                continue
            age = now - order.submitted_at
            if age >= config.STALE_ORDER_TIMEOUT_SEC:
                log.info(
                    "Stale order detected: id=%s ticker=%s age=%.0fs",
                    order_id, order.ticker, age,
                )
                if self.cancel_order(order_id):
                    cancelled += 1
        return cancelled

    # ------------------------------------------------------------------
    # Reconciliation (refresh from API)
    # ------------------------------------------------------------------

    def refresh_orders(self):
        """
        Pull current order status from the API and update in-memory state.
        Call this periodically to detect fills and external cancellations.
        """
        try:
            resp = utils.api_request(
                "GET",
                f"{BASE}/portfolio/orders",
                authenticated=True,
                params={"status": "open", "limit": 200},
            )
        except Exception as exc:
            log.error("Failed to refresh orders: %s", exc)
            return

        api_orders = {
            o["order_id"]: o
            for o in (resp.get("orders") or [])
            if "order_id" in o
        }

        for order_id, order in list(self._orders.items()):
            if order.status not in (OrderStatus.OPEN, OrderStatus.PENDING):
                continue

            if order_id not in api_orders:
                # No longer open on the exchange
                self._handle_no_longer_open(order)
            else:
                self._sync_from_api(order, api_orders[order_id])

    def _handle_no_longer_open(self, order: Order):
        """
        Order disappeared from the open-orders list — either filled or cancelled externally.
        Fetch the order detail to find out which.
        """
        try:
            resp = utils.api_request(
                "GET",
                f"{BASE}/portfolio/orders/{order.order_id}",
                authenticated=True,
            )
        except Exception as exc:
            log.error("Could not fetch order detail for %s: %s", order.order_id, exc)
            return

        raw = resp.get("order") or resp
        status_str = (raw.get("status") or "").lower()
        filled = int(raw.get("contracts_filled") or raw.get("filled_count") or 0)
        avg_price = utils.parse_price(raw.get("avg_fill_price") or raw.get("average_fill_price"))

        if status_str in ("filled", "executed"):
            order.status = OrderStatus.FILLED
            order.filled_contracts = filled
            order.avg_fill_price = avg_price
            self.risk.record_order_filled(order.ticker, avg_price, filled)
            log.info(
                "Order FILLED: id=%s ticker=%s contracts=%d avg_price=$%.2f",
                order.order_id, order.ticker, filled, avg_price,
            )
        elif status_str in ("cancelled", "canceled", "expired"):
            order.status = OrderStatus.CANCELLED
            unfilled = order.num_contracts - filled
            self.risk.record_order_cancelled(order.ticker, order.price * unfilled)
            log.info(
                "Order externally CANCELLED: id=%s ticker=%s unfilled=%d",
                order.order_id, order.ticker, unfilled,
            )
        else:
            log.warning(
                "Order %s has unexpected status '%s'", order.order_id, status_str
            )

    def _sync_from_api(self, order: Order, raw: dict):
        """Update an open order from live API data (partial fill detection)."""
        filled = int(raw.get("contracts_filled") or raw.get("filled_count") or 0)
        if filled > order.filled_contracts:
            log.info(
                "Partial fill: order %s ticker=%s filled %d→%d of %d contracts",
                order.order_id, order.ticker,
                order.filled_contracts, filled, order.num_contracts,
            )
            order.filled_contracts = filled

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_open_tickers(self) -> set:
        return {
            o.ticker
            for o in self._orders.values()
            if o.status in (OrderStatus.OPEN, OrderStatus.PENDING)
        }

    def get_filled_tickers(self) -> set:
        return {
            o.ticker
            for o in self._orders.values()
            if o.status == OrderStatus.FILLED
        }

    def open_order_count(self) -> int:
        return sum(
            1 for o in self._orders.values()
            if o.status in (OrderStatus.OPEN, OrderStatus.PENDING)
        )

    def all_orders(self) -> List[Order]:
        return list(self._orders.values())
