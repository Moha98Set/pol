"""
Validation — refuse to trust data that does not make sense.
===========================================================

The dangerous failure mode of this system is not a crash. It is a plausible
number produced from bad input: a book with a $0.001 ask that was never
real, an event whose legs are not actually mutually exclusive, a market
already resolved but still flagged active. Each of those produces a
"profitable" basket that loses money the moment you touch it.

Every rejection here carries a machine-readable `code`, so the funnel can
be counted (metrics.py) and the reasons queried later:

    SELECT code, COUNT(*) FROM rejections GROUP BY code

Two levels of severity:

  * REJECT   — do not analyse this event at all
  * SUSPECT  — analyse it, but mark the result; the signal is real often
               enough to be worth recording, and wrong often enough that
               it should never be executed unreviewed

The distinction matters because throwing away everything suspicious means
never learning which suspicions were justified.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import config

# =====================================================================
# Rejection codes — stable strings, safe to store and group by
# =====================================================================

# structural
NO_OPEN_MARKETS = "no_open_markets"
NO_TOKENS = "no_tokens"
MALFORMED_TOKENS = "malformed_tokens"
DUPLICATE_TOKENS = "duplicate_tokens"
MARKET_COUNT_MISMATCH = "market_count_mismatch"

# eligibility
LOW_VOLUME = "low_volume"
RESOLVES_TOO_SOON = "resolves_too_soon"
RESOLVES_TOO_LATE = "resolves_too_late"
ALREADY_RESOLVED = "already_resolved"
NO_END_DATE = "no_end_date"

# exclusivity
NOT_NEG_RISK = "not_neg_risk"
NON_EXHAUSTIVE_PATTERN = "non_exhaustive_pattern"

# book quality
DRY_LEG = "dry_leg"
SUM_TOO_LOW = "sum_asks_too_low"
NO_EDGE = "no_edge"
IMPLAUSIBLE_PRICE = "implausible_price"
CROSSED_BOOK = "crossed_book"
THIN_BOOK = "thin_book"
STALE_BOOK = "stale_book"

# edge — the market is fine, the price just isn't there.
# Kept apart from the codes above: these mean "correctly priced today",
# not "something is wrong". Lumping them together would make an efficient
# market look like a broken pipeline.
BELOW_MIN_EDGE = "below_min_edge"        # some edge, under the threshold
FAR_BELOW_EDGE = "far_below_edge"        # not even close; no near-miss row
NO_FILLABLE_SIZE = "no_fillable_size"    # edge exists, book too thin to take it

OK = "ok"


@dataclass
class Verdict:
    """The outcome of validating one thing."""
    ok: bool
    code: str = OK
    detail: str = ""
    suspicions: List[str] = field(default_factory=list)

    @property
    def suspect(self) -> bool:
        return bool(self.suspicions)

    def __bool__(self) -> bool:
        return self.ok


ACCEPT = Verdict(True)


def reject(code: str, detail: str = "") -> Verdict:
    return Verdict(False, code, detail)


def accept(suspicions: List[str] = None) -> Verdict:
    return Verdict(True, OK, "", suspicions or [])


# =====================================================================
# Event-level validation
# =====================================================================


def parse_end_date(event: dict) -> Optional[datetime]:
    raw = event.get("endDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def validate_event(event: dict, markets: list = None) -> Verdict:
    """
    Is this event worth spending API calls on?

    Runs before any book is fetched, so it is deliberately cheap. Order is
    by cost: structural checks before date parsing before anything else.
    """
    markets = markets if markets is not None else [
        m for m in (event.get("markets") or [])
        if not m.get("closed") and m.get("enableOrderBook")
    ]

    if not markets:
        return reject(NO_OPEN_MARKETS)

    volume = sum(_as_float(m.get("volume24hr") or m.get("volumeNum")
                           or m.get("volume")) or 0.0 for m in markets)
    if volume < config.MIN_VOLUME_24H:
        return reject(LOW_VOLUME, f"${volume:,.0f}")

    end = parse_end_date(event)
    if end is None:
        # No end date is not fatal — plenty of real events omit it — but it
        # means the capital-lock filter cannot be applied, so flag it.
        return accept([NO_END_DATE])

    days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
    if days < 0:
        return reject(ALREADY_RESOLVED, f"ended {-days:.1f}d ago")
    if days < config.MIN_DAYS_TO_RES:
        return reject(RESOLVES_TOO_SOON, f"{days*24:.1f}h")
    if days > config.MAX_DAYS_TO_RES:
        return reject(RESOLVES_TOO_LATE, f"{days:.0f}d")

    return ACCEPT


def validate_exclusivity(event: dict, markets: list,
                         non_exhaustive_patterns: list = None) -> Verdict:
    """
    Are these legs really mutually exclusive and exhaustive?

    This is the check that separates arbitrage from gambling. If exactly one
    leg does not pay out, buying every leg is a directional bet with extra
    steps. Binary events are exclusive by construction; everything else has
    to prove it.

    `non_exhaustive_patterns` defaults to the shared list in patterns.py.
    It stays overridable so a caller can experiment with a stricter or
    looser list without editing the module every tool reads.
    """
    if len(markets) < 2:
        return ACCEPT                      # binary Yes/No, exclusive by design

    if not event.get("negRisk"):
        return reject(NOT_NEG_RISK)

    title = (event.get("title") or "").lower()

    if non_exhaustive_patterns is None:
        import patterns
        pattern, reason = patterns.matches(title)
        if pattern:
            # the reason travels with the rejection: a filter whose
            # decisions cannot be explained is one nobody dares to change
            return reject(NON_EXHAUSTIVE_PATTERN, f"{pattern} ({reason})")
        return ACCEPT

    for pattern in non_exhaustive_patterns:
        if pattern in title:
            return reject(NON_EXHAUSTIVE_PATTERN, pattern)

    return ACCEPT


def validate_tokens(markets: list, is_binary: bool) -> Verdict:
    """
    Token IDs must be present, parseable, and distinct.

    Duplicate token IDs across legs is the subtle one: it means two legs
    share an order book, so the same liquidity gets counted twice and the
    basket looks cheaper than it is.
    """
    seen = {}
    for market in markets:
        tokens = _parse_tokens(market.get("clobTokenIds"))
        if tokens is None:
            return reject(MALFORMED_TOKENS, str(market.get("slug")))
        if not tokens:
            return reject(NO_TOKENS, str(market.get("slug")))
        if is_binary and len(tokens) < 2:
            return reject(MARKET_COUNT_MISMATCH,
                          f"binary market with {len(tokens)} token(s)")

        token = tokens[0]
        if token in seen:
            return reject(DUPLICATE_TOKENS,
                          f"{market.get('slug')} shares a token with {seen[token]}")
        seen[token] = market.get("slug")

    return ACCEPT


# =====================================================================
# Book-level validation
# =====================================================================

# A leg priced below this is almost always a stale resting order nobody
# has cleaned up, not a real chance to buy a claim on $1.
MIN_PLAUSIBLE_PRICE = 0.001
MAX_PLAUSIBLE_PRICE = 0.999


def validate_book(name: str, asks: list, bids: list = None) -> Verdict:
    """
    Validate one leg's order book.

    `asks` is expected to be normalized [(price, size), ...] from arbmath.
    """
    if not asks:
        return reject(DRY_LEG, name)

    best = asks[0][0]
    suspicions = []

    if best < MIN_PLAUSIBLE_PRICE or best > MAX_PLAUSIBLE_PRICE:
        return reject(IMPLAUSIBLE_PRICE, f"{name} @ {best}")

    if bids:
        best_bid = max(price for price, _size in bids)
        if best_bid > best:
            # Someone is bidding more than the cheapest ask. On a real
            # exchange these would have matched, so one side is stale.
            return reject(CROSSED_BOOK, f"{name}: bid {best_bid} > ask {best}")

    depth = sum(price * size for price, size in asks)
    if depth < 1.0:
        suspicions.append(THIN_BOOK)

    return accept(suspicions)


def validate_no_basket(legs: list, sum_no_asks: float) -> Verdict:
    """
    Sanity-check a NO-side basket, where the payout is N-1 rather than 1.

    The bounds mirror the YES side but around a different centre. A NO
    basket summing far below N-1 is not a windfall either: with N legs the
    fair sum is N minus the sum of the YES prices, so anything much under
    N-1 means at least one leg's bid side is stale.
    """
    n = len(legs)
    if n < 2:
        return reject(MARKET_COUNT_MISMATCH, f"{n} leg(s)")

    payout = n - 1

    # Each NO leg is worth (1 - its YES price), so the sum cannot honestly
    # fall far below N-1 unless a leg is mispriced or stale.
    if sum_no_asks < payout - config.SUM_ASKS_MAX + config.SUM_ASKS_MIN:
        return reject(SUM_TOO_LOW, f"{sum_no_asks:.4f} vs payout {payout}")

    if sum_no_asks >= payout:
        return reject(NO_EDGE, f"{sum_no_asks:.4f} vs payout {payout}")

    return ACCEPT


def validate_basket(legs: list, sum_best_asks: float) -> Verdict:
    """
    Sanity-check the assembled basket before believing its edge.

    A sum far below $1 is not a windfall, it is evidence that the legs are
    not what we think they are — one is stale, or they are not actually
    exclusive. The scanner has always had this floor; here it becomes a
    named, countable reason rather than an anonymous `return None`.
    """
    if len(legs) < 2:
        return reject(MARKET_COUNT_MISMATCH, f"{len(legs)} leg(s)")

    if sum_best_asks < config.SUM_ASKS_MIN:
        return reject(SUM_TOO_LOW, f"{sum_best_asks:.4f}")

    if sum_best_asks >= config.SUM_ASKS_MAX:
        return reject(NO_EDGE, f"{sum_best_asks:.4f}")

    suspicions = []
    # Just inside the floor is still unusual enough to be worth marking.
    if sum_best_asks < config.SUM_ASKS_MIN * 1.2:
        suspicions.append(SUM_TOO_LOW)

    return accept(suspicions)


# =====================================================================
# Helpers
# =====================================================================


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_tokens(raw) -> Optional[list]:
    """Returns the token list, [] if absent, or None if malformed."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None
