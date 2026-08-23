"""
Pure arbitrage math — no network, no database, no globals.

Everything in this module is a deterministic function of its arguments.
That is deliberate: this is the part of the system that decides whether
money is made or lost, so it must be testable without touching the
Polymarket API. scanner.py, findmarket.py, live_engine.py and executor.py
all route their edge math through here so they can never disagree.

Vocabulary
----------
leg        one outcome you have to buy (YES of one market)
basket     one share of every leg — pays exactly $1 at resolution when the
           legs are mutually exclusive AND exhaustive
K          number of complete baskets (= equal shares per leg)
gross_edge 1 - sum(best asks)          — before fees, before slippage
net_edge   gross_edge - fee_per_share  — before slippage
profit     K - real_cost - real_fee    — after everything (dollars)
"""

from typing import List, Optional, Sequence, Tuple

import fees

# A normalized ask level: (price, size). Always sorted cheapest-first.
Level = Tuple[float, float]
# A normalized leg: (outcome_name, levels)
Leg = Tuple[str, List[Level]]

DEFAULT_TEST_CAPITALS = (10, 50, 100, 500, 1000, 5000)

# K is a share count; resolving it finer than this is meaningless
K_TOLERANCE = 1e-4


# =====================================================================
# Normalization — the API returns strings, everything downstream wants floats
# =====================================================================


def normalize_asks(raw_asks) -> List[Level]:
    """
    Convert raw CLOB ask levels into sorted (price, size) tuples.

    Drops zero/negative/garbage levels. Sorting here once means no other
    function has to re-sort or trust the API's ordering — the REST /book
    and the WebSocket book snapshot do NOT use the same order.
    """
    if not raw_asks:
        return []

    levels: List[Level] = []
    for a in raw_asks:
        try:
            if isinstance(a, dict):
                price = float(a["price"])
                size = float(a["size"])
            else:
                price, size = float(a[0]), float(a[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            levels.append((price, size))

    levels.sort(key=lambda lv: lv[0])
    return levels


def no_asks_from_yes_bids(yes_bids: Sequence[Level]) -> List[Level]:
    """
    Turn a leg's YES *bid* side into the equivalent NO *ask* side.

    Buying NO at price p is the same trade as selling YES at (1 - p), and
    Polymarket implements it that way: a NO ask at p is a YES bid at 1-p,
    the same resting order seen from the other side.

    That was measured, not assumed — across 1234 live legs, the best NO
    ask equalled (1 - best YES bid) to the last decimal in every single
    case. So the NO book needs no extra API call: it is already in the
    YES book response, on the bid side the scanner used to discard.

    Sizes carry over unchanged — one share of YES sold is one share of NO
    bought.
    """
    converted = [(1.0 - price, size) for price, size in yes_bids
                 if 0.0 < price < 1.0 and size > 0]
    converted.sort(key=lambda level: level[0])
    return converted


def best_ask(levels: Sequence[Level]) -> Optional[float]:
    """Cheapest ask price, or None if the leg is dry."""
    return levels[0][0] if levels else None


def depth_usd(levels: Sequence[Level]) -> float:
    """Total dollars resting on the ask side."""
    return sum(p * s for p, s in levels)


# =====================================================================
# Filling
# =====================================================================


class Fill:
    """Result of walking a book to buy K shares."""

    __slots__ = ("cost", "filled", "requested", "avg_price", "levels_used")

    def __init__(self, cost: float, filled: float, requested: float,
                 levels_used: int):
        self.cost = cost
        self.filled = filled
        self.requested = requested
        self.levels_used = levels_used
        self.avg_price = (cost / filled) if filled > 0 else None

    @property
    def complete(self) -> bool:
        """True if the book had enough depth to fill the whole request."""
        return self.filled >= self.requested * 0.9999

    def __repr__(self):
        return (f"Fill(filled={self.filled:.2f}/{self.requested:.2f}, "
                f"cost={self.cost:.4f}, avg={self.avg_price})")


def cost_to_buy_k_shares(levels: Sequence[Level], k: float) -> Fill:
    """
    Walk the asks cheapest-first until K shares are bought.

    This is the whole point of the project: the best ask is a lie about
    what you actually pay. Buying $500 of a leg whose best ask has $20
    resting on it means eating four more levels at worse prices.
    """
    if k <= 0 or not levels:
        return Fill(0.0, 0.0, max(k, 0.0), 0)

    cost = 0.0
    filled = 0.0
    used = 0

    for price, size in levels:
        need = k - filled
        if need <= 0:
            break
        take = size if size <= need else need
        cost += price * take
        filled += take
        used += 1

    return Fill(cost, filled, k, used)


def fill_all_legs(legs: Sequence[Leg], k: float) -> Optional[List[Fill]]:
    """
    Fill K shares of every leg. Returns None if ANY leg cannot fill.

    All-or-nothing is not pedantry: a partially filled basket is not an
    arbitrage, it is a directional bet on whichever legs did fill.
    """
    out: List[Fill] = []
    for _name, levels in legs:
        fill = cost_to_buy_k_shares(levels, k)
        if not fill.complete:
            return None
        out.append(fill)
    return out


# =====================================================================
# Sizing — how many baskets should we actually buy?
# =====================================================================


def basket_profit(legs: Sequence[Leg], k: float, fee_rate: float,
                  payout_per_basket: float = 1.0) -> Optional[dict]:
    """
    Profit from buying K complete baskets, or None if K is not fillable.

    `payout_per_basket` is what one basket is guaranteed to be worth at
    resolution:

      * buying YES on every leg  -> 1.0
            exactly one leg wins and pays $1; the rest expire worthless
      * buying NO on every leg   -> N - 1
            exactly one leg loses, so N-1 of the NOs pay $1 each

    Fees use each leg's *average fill price*, not its best ask, because
    that is the price the exchange actually matched.
    """
    fills = fill_all_legs(legs, k)
    if fills is None:
        return None

    cost = sum(f.cost for f in fills)
    fee = fees.fee_for_legs([f.avg_price for f in fills], k, fee_rate)
    profit = k * payout_per_basket - cost - fee

    return {
        "shares": k,
        "real_cost": cost,
        "fee": fee,
        "total_outlay": cost + fee,
        "profit": profit,
        "roi": (profit / cost * 100.0) if cost > 0 else 0.0,
        "avg_prices": [f.avg_price for f in fills],
        "levels_used": [f.levels_used for f in fills],
    }


def max_fillable_k(legs: Sequence[Leg], budget: float) -> float:
    """
    Largest K where every leg fills completely and total cost <= budget.

    Both constraints are monotone in K (deeper fills cost strictly more),
    so a binary search finds the exact boundary. The old code sampled ten
    hard-coded shrink factors instead, which both missed the true maximum
    and wasted work re-simulating sizes it had no reason to try.
    """
    if budget <= 0 or not legs:
        return 0.0

    # Upper bound: no leg can supply more shares than its total resting size.
    depth_cap = min(sum(s for _p, s in levels) for _n, levels in legs)
    if depth_cap <= 0:
        return 0.0

    def feasible(k: float) -> bool:
        fills = fill_all_legs(legs, k)
        return fills is not None and sum(f.cost for f in fills) <= budget

    hi = depth_cap
    if feasible(hi):
        return hi

    lo = 0.0
    while hi - lo > K_TOLERANCE:
        mid = (lo + hi) / 2.0
        if feasible(mid):
            lo = mid
        else:
            hi = mid
    return lo


def optimal_k(legs: Sequence[Leg], budget: float, fee_rate: float,
              payout_per_basket: float = 1.0) -> Optional[dict]:
    """
    Find the K that MAXIMIZES profit within the budget — not the largest K.

    Profit is concave in K: each extra basket is bought at a worse average
    price than the last, so marginal profit falls monotonically and turns
    negative once the marginal basket costs more than it pays out. Buying
    up to the budget limit past that point destroys profit. A ternary
    search over the feasible range lands on the peak.
    """
    k_max = max_fillable_k(legs, budget)
    if k_max <= 0:
        return None

    def profit_at(k: float) -> float:
        r = basket_profit(legs, k, fee_rate, payout_per_basket)
        return r["profit"] if r else float("-inf")

    lo, hi = 0.0, k_max
    for _ in range(80):
        if hi - lo <= K_TOLERANCE:
            break
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if profit_at(m1) < profit_at(m2):
            lo = m1
        else:
            hi = m2

    k = (lo + hi) / 2.0
    result = basket_profit(legs, k, fee_rate, payout_per_basket)
    if result is None or result["profit"] <= 0:
        return None

    result["budget"] = budget
    result["k_max_fillable"] = k_max
    return result


def compute_slippage_curve(legs: Sequence[Leg], fee_rate: float,
                           capitals: Sequence[float] = DEFAULT_TEST_CAPITALS,
                           payout_per_basket: float = 1.0) -> List[dict]:
    """
    Best achievable profit at each test capital.

    The curve is the honest answer to "how much money can this actually
    absorb" — an edge that only survives at $10 is noise, an edge that
    still pays at $1000 is a real dislocation.
    """
    curve = []
    for cap in capitals:
        best = optimal_k(legs, float(cap), fee_rate, payout_per_basket)
        if not best:
            continue
        curve.append({
            "capital": float(cap),
            "shares": round(best["shares"], 2),
            "real_cost": round(best["real_cost"], 4),
            "fee": round(best["fee"], 4),
            "profit": round(best["profit"], 4),
            "roi": round(best["roi"], 3),
        })
    return curve


# =====================================================================
# Top-level evaluation
# =====================================================================


def evaluate_basket(legs: Sequence[Leg], fee_rate: float,
                    capitals: Sequence[float] = DEFAULT_TEST_CAPITALS,
                    payout_per_basket: float = 1.0) -> dict:
    """
    Full edge evaluation for one set of legs. The single entry point used
    by the periodic scanner, the live engine and the executor alike.

    `payout_per_basket` is 1.0 for a YES basket and N-1 for a NO basket
    (see basket_profit). `net_edge` stays per *basket*, so a YES edge and
    a NO edge are the same units — but note they are NOT the same return:
    a NO basket costs about N-1 dollars to earn that same edge, so its ROI
    is roughly (N-1) times worse. `net_edge_per_dollar` is the number to
    compare across basket types.

    Returns a dict that is always well-formed; check `dry_legs` and
    `net_edge` rather than expecting None on failure.
    """
    leg_prices = []
    dry = []
    for name, levels in legs:
        ba = best_ask(levels)
        if ba is None:
            dry.append(name)
        else:
            leg_prices.append(ba)

    if dry:
        return {
            "dry_legs": dry,
            "num_legs": len(legs),
            "payout_per_basket": payout_per_basket,
            "sum_best_asks": None,
            "gross_edge": None,
            "net_edge": None,
            "net_edge_per_dollar": None,
            "fee_rate": fee_rate,
            "curve": [],
            "best": None,
        }

    sum_asks = sum(leg_prices)
    gross_edge = payout_per_basket - sum_asks
    fee_ps = fees.fee_per_share(leg_prices, fee_rate)
    net_edge = gross_edge - fee_ps

    # The comparable number across basket types. A $0.01 edge on a basket
    # that costs $1 is not the same trade as a $0.01 edge on one costing
    # $20, and only this ratio says so.
    net_edge_per_dollar = net_edge / sum_asks if sum_asks > 0 else None

    curve = (compute_slippage_curve(legs, fee_rate, capitals, payout_per_basket)
             if net_edge > 0 else [])
    best = max(curve, key=lambda c: c["profit"]) if curve else None

    return {
        "dry_legs": [],
        "num_legs": len(legs),
        "payout_per_basket": payout_per_basket,
        "leg_best_asks": leg_prices,
        "sum_best_asks": sum_asks,
        "gross_edge": gross_edge,
        "fee_per_share": fee_ps,
        "net_edge": net_edge,
        "net_edge_per_dollar": net_edge_per_dollar,
        "fee_rate": fee_rate,
        "curve": curve,
        "best": best,
    }
