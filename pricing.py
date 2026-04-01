"""
Position sizing via Kelly Criterion and expected value calculations.

For Kalshi YES contracts:
  - Contract pays $1.00 if YES wins
  - You pay `price` dollars per contract
  - Net profit per contract = (1 - price) if wins, -price if loses

Kelly formula (binary bet):
  f* = (p * b - (1 - p)) / b
  where:
    p = estimated true probability of winning
    b = net odds (profit per dollar risked) = (1 - price) / price

Quarter-Kelly is used for safety: bet_fraction = KELLY_MULTIPLIER * f*
"""

import math
from dataclasses import dataclass
from typing import Optional

import config
from logger import get_logger

log = get_logger(__name__)


@dataclass
class SizingResult:
    ticker: str
    price_paid: float           # yes_ask price we plan to buy at
    true_prob: float            # our estimated true probability
    edge: float                 # true_prob - price_paid
    kelly_fraction: float       # raw Kelly fraction
    scaled_fraction: float      # after KELLY_MULTIPLIER
    dollar_bet: float           # scaled_fraction * bankroll
    num_contracts: int          # floor(dollar_bet / price_paid)
    expected_value: float       # edge * num_contracts * 1.00 (per contract EV)
    max_loss: float             # price_paid * num_contracts


def estimate_true_prob(market_implied_prob: float) -> float:
    """
    Estimate the true probability of winning given the market's implied probability.

    We assume a conservative edge of ESTIMATED_EDGE_OVER_MARKET percentage points
    on top of what the market already implies (i.e. the market underestimates the
    heavy favorite slightly due to bettors seeking underdogs).

    Caps at 0.99 to avoid infinite Kelly.
    """
    estimated = market_implied_prob + config.ESTIMATED_EDGE_OVER_MARKET
    return min(estimated, 0.99)


def kelly_size(
    ticker: str,
    price: float,
    bankroll: float,
    true_prob: Optional[float] = None,
) -> SizingResult:
    """
    Compute the quarter-Kelly position size for a YES contract.

    Args:
        ticker:     Market ticker (for logging)
        price:      The ask price we plan to pay (dollars, e.g. 0.92)
        bankroll:   Available capital in dollars
        true_prob:  Override estimated true probability (defaults to market + edge)

    Returns:
        SizingResult with the recommended number of contracts.
    """
    if true_prob is None:
        # Use mid-market price as the market's implied prob for edge calculation
        true_prob = estimate_true_prob(price)

    edge = true_prob - price

    if edge <= 0:
        log.debug("%s: no edge (true_prob=%.3f, price=%.3f) — sizing to 0", ticker, true_prob, price)
        return SizingResult(
            ticker=ticker, price_paid=price, true_prob=true_prob, edge=edge,
            kelly_fraction=0.0, scaled_fraction=0.0, dollar_bet=0.0,
            num_contracts=0, expected_value=0.0, max_loss=0.0,
        )

    # Net odds: profit per dollar risked = (1 - price) / price
    net_odds = (1.0 - price) / price

    # Full Kelly fraction of bankroll
    kelly_fraction = (true_prob * net_odds - (1.0 - true_prob)) / net_odds
    kelly_fraction = max(kelly_fraction, 0.0)   # clamp negative values

    # Scale down by Kelly multiplier (quarter-Kelly)
    scaled_fraction = kelly_fraction * config.KELLY_MULTIPLIER

    # Dollar amount to bet
    dollar_bet = scaled_fraction * bankroll

    # Respect per-market exposure cap
    dollar_bet = min(dollar_bet, config.MAX_EXPOSURE_PER_MARKET)

    # Convert to number of whole contracts
    if price <= 0:
        num_contracts = 0
    else:
        num_contracts = math.floor(dollar_bet / price)

    # Enforce hard max contracts per order
    num_contracts = min(num_contracts, config.MAX_CONTRACTS_PER_ORDER)
    num_contracts = max(num_contracts, 0)

    expected_value = edge * num_contracts   # EV in dollars (each contract pays $1 if wins)
    max_loss = price * num_contracts

    result = SizingResult(
        ticker=ticker,
        price_paid=price,
        true_prob=true_prob,
        edge=edge,
        kelly_fraction=kelly_fraction,
        scaled_fraction=scaled_fraction,
        dollar_bet=dollar_bet,
        num_contracts=num_contracts,
        expected_value=expected_value,
        max_loss=max_loss,
    )

    log.debug(
        "%s: price=%.2f true_prob=%.3f edge=%.3f kelly=%.4f "
        "scaled=%.4f dollar_bet=$%.2f contracts=%d EV=$%.3f",
        ticker, price, true_prob, edge, kelly_fraction,
        scaled_fraction, dollar_bet, num_contracts, expected_value,
    )

    return result


