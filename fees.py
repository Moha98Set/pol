"""
Polymarket taker fee model — single source of truth.

Every module that computes edge MUST import from here. Previously the fee
model was duplicated: scanner.py had the real per-leg model while
findmarket.py still used a flat 2%, so the two disagreed about which
events were profitable.

Real model (docs.polymarket.com/trading/fees):

    fee = shares × feeRate × p × (1 − p)     per leg, charged at match time

Makers pay zero. The rate depends on the market category, which we infer
from the event's tags.

Why p × (1 − p): the fee is proportional to the *uncertainty* of the leg.
A leg trading at 0.02 or 0.98 is nearly resolved and costs almost nothing;
a leg at 0.50 costs the full rate. This matters a lot for arbitrage —
a 20-leg event where every leg is cheap pays far less fee than a flat
percentage model would suggest, so the flat 2% model was rejecting real
opportunities.
"""

from typing import Iterable, Sequence, Tuple

# (tag keywords, rate) — first match wins, so order matters.
# geopolitics is listed first because it is fee-free even when an event is
# also tagged "politics".
FEE_RATES: Sequence[Tuple[Tuple[str, ...], float]] = (
    (("geopolitics",), 0.0),
    (("crypto", "bitcoin", "ethereum", "solana"), 0.07),
    (("sports", "esports"), 0.03),
    (("politics", "finance", "tech", "mentions", "mention markets"), 0.04),
)

# economics / culture / weather / anything untagged
DEFAULT_FEE_RATE = 0.05


def fee_rate_for_tags(tags_text: str) -> Tuple[float, str]:
    """
    Resolve a fee rate from a pre-joined, lowercased tag string.

    Returns (rate, matched_keyword). Split out from fee_rate_for_event so
    it can be unit-tested without constructing a whole event dict.
    """
    for keywords, rate in FEE_RATES:
        for kw in keywords:
            if kw in tags_text:
                return rate, kw
    return DEFAULT_FEE_RATE, "other"


def fee_rate_for_event(event: dict) -> Tuple[float, str]:
    """Resolve the taker fee rate for a Gamma event. Returns (rate, category)."""
    tags_text = " ".join(
        (t.get("label") or "").lower()
        for t in (event.get("tags") or [])
    )
    return fee_rate_for_tags(tags_text)


def fee_for_leg(price: float, shares: float, fee_rate: float) -> float:
    """Taker fee for buying `shares` of one leg at average price `price`."""
    return shares * fee_rate * price * (1.0 - price)


def fee_for_legs(leg_prices: Iterable[float], shares: float,
                 fee_rate: float) -> float:
    """
    Total taker fee for buying `shares` of EACH leg at the given prices.

    Pass slippage-adjusted average fill prices when you have them; passing
    best-ask prices gives an optimistic estimate suitable only for the
    cheap pre-filter stage.
    """
    return sum(fee_for_leg(p, shares, fee_rate) for p in leg_prices)


def fee_per_share(leg_prices: Iterable[float], fee_rate: float) -> float:
    """
    Fee expressed per share of the arbitrage basket — directly comparable
    to gross_edge, since both are in "dollars per share" units.

        net_edge = (1 - sum_asks) - fee_per_share(...)
    """
    return fee_for_legs(leg_prices, 1.0, fee_rate)
