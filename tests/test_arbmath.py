"""
Tests for the arbitrage math.

Every book here is hand-built, so the expected answer is arithmetic anyone
can check by hand. That is the point: this module decides whether money is
made, and it must be verifiable without the Polymarket API being up, a VPN
being connected, or a market happening to be mispriced today.

The recurring fixture:

    LEG_A: 100 shares @ 0.40, then 100 @ 0.45
    LEG_B: 100 shares @ 0.55, then 100 @ 0.60

    sum of best asks = 0.95  ->  5% gross edge
    K=100 costs 40 + 55 =  95  ->  profit 5.00   <-- the peak
    K=150 costs 62.5 + 85 = 147.5 -> profit 2.50
    K=200 costs 85 + 115 = 200 -> profit 0.00

Note that the LARGEST fillable size is the WORST one. That is the whole
reason optimal_k does a peak search instead of maximizing size.
"""

import pytest

import arbmath
import fees

LEG_A = [(0.40, 100.0), (0.45, 100.0)]
LEG_B = [(0.55, 100.0), (0.60, 100.0)]
BASKET = [("A", LEG_A), ("B", LEG_B)]

NO_FEE = 0.0


# =====================================================================
# normalize_asks — the boundary where API strings become numbers
# =====================================================================


def test_normalize_sorts_cheapest_first():
    raw = [{"price": "0.7", "size": "5"}, {"price": "0.3", "size": "9"}]
    assert arbmath.normalize_asks(raw) == [(0.3, 9.0), (0.7, 5.0)]


def test_normalize_accepts_pairs_as_well_as_dicts():
    assert arbmath.normalize_asks([("0.25", "4")]) == [(0.25, 4.0)]


def test_normalize_drops_zero_and_negative_levels():
    raw = [
        {"price": "0", "size": "10"},        # worthless price
        {"price": "0.5", "size": "0"},       # phantom level, no size
        {"price": "-0.1", "size": "10"},     # garbage
        {"price": "0.5", "size": "10"},      # the only real one
    ]
    assert arbmath.normalize_asks(raw) == [(0.5, 10.0)]


def test_normalize_skips_malformed_entries_instead_of_raising():
    """A single bad level must not take down a whole scan."""
    raw = [{"price": "abc", "size": "1"}, {"size": "1"}, None,
           {"price": "0.5", "size": "2"}]
    assert arbmath.normalize_asks(raw) == [(0.5, 2.0)]


def test_normalize_handles_empty_and_none():
    assert arbmath.normalize_asks(None) == []
    assert arbmath.normalize_asks([]) == []


def test_best_ask_and_depth():
    assert arbmath.best_ask(LEG_A) == 0.40
    assert arbmath.best_ask([]) is None
    # 0.40*100 + 0.45*100
    assert arbmath.depth_usd(LEG_A) == pytest.approx(85.0)


# =====================================================================
# Walking the book
# =====================================================================


def test_fill_within_the_first_level():
    fill = arbmath.cost_to_buy_k_shares(LEG_A, 50)
    assert fill.complete
    assert fill.cost == pytest.approx(20.0)
    assert fill.avg_price == pytest.approx(0.40)
    assert fill.levels_used == 1


def test_fill_consuming_exactly_one_level():
    fill = arbmath.cost_to_buy_k_shares(LEG_A, 100)
    assert fill.complete
    assert fill.cost == pytest.approx(40.0)
    assert fill.avg_price == pytest.approx(0.40)


def test_fill_spanning_two_levels_pays_the_blended_price():
    """The best ask is a lie about what you pay — this is that lie, priced."""
    fill = arbmath.cost_to_buy_k_shares(LEG_A, 150)
    assert fill.complete
    assert fill.cost == pytest.approx(40.0 + 0.45 * 50)   # 62.5
    assert fill.avg_price == pytest.approx(62.5 / 150)    # ~0.4167 > 0.40
    assert fill.avg_price > 0.40
    assert fill.levels_used == 2


def test_fill_beyond_available_depth_is_incomplete():
    fill = arbmath.cost_to_buy_k_shares(LEG_A, 500)
    assert not fill.complete
    assert fill.filled == pytest.approx(200.0)
    assert fill.cost == pytest.approx(85.0)


def test_fill_of_nothing_is_harmless():
    assert arbmath.cost_to_buy_k_shares(LEG_A, 0).cost == 0
    assert arbmath.cost_to_buy_k_shares([], 10).filled == 0
    assert arbmath.cost_to_buy_k_shares(LEG_A, -5).cost == 0


def test_fill_all_legs_returns_none_when_any_leg_is_short():
    """
    All-or-nothing is a safety property, not an optimization: a partially
    filled basket is a directional bet, not an arbitrage.
    """
    shallow = [("A", LEG_A), ("B", [(0.55, 10.0)])]
    assert arbmath.fill_all_legs(shallow, 50) is None
    assert arbmath.fill_all_legs(shallow, 10) is not None


# =====================================================================
# basket_profit
# =====================================================================


def test_basket_profit_at_the_top_of_the_book():
    result = arbmath.basket_profit(BASKET, 100, NO_FEE)
    assert result["real_cost"] == pytest.approx(95.0)
    assert result["profit"] == pytest.approx(5.0)
    assert result["roi"] == pytest.approx(5 / 95 * 100)


def test_basket_profit_shrinks_as_size_grows():
    p100 = arbmath.basket_profit(BASKET, 100, NO_FEE)["profit"]
    p150 = arbmath.basket_profit(BASKET, 150, NO_FEE)["profit"]
    p200 = arbmath.basket_profit(BASKET, 200, NO_FEE)["profit"]
    assert p100 == pytest.approx(5.0)
    assert p150 == pytest.approx(2.5)
    assert p200 == pytest.approx(0.0, abs=1e-9)
    assert p100 > p150 > p200


def test_basket_profit_uses_average_fill_price_for_fees():
    """
    Fees must be charged on what the exchange matched, not on the best ask.
    At K=150 leg A's average is 0.41667, not 0.40.
    """
    result = arbmath.basket_profit(BASKET, 150, 0.04)
    assert result["avg_prices"][0] == pytest.approx(62.5 / 150)
    expected_fee = fees.fee_for_legs(result["avg_prices"], 150, 0.04)
    assert result["fee"] == pytest.approx(expected_fee)


def test_basket_profit_is_none_when_unfillable():
    assert arbmath.basket_profit(BASKET, 5000, NO_FEE) is None


def test_fees_reduce_profit():
    free = arbmath.basket_profit(BASKET, 100, NO_FEE)["profit"]
    charged = arbmath.basket_profit(BASKET, 100, 0.07)["profit"]
    assert charged < free


# =====================================================================
# Sizing
# =====================================================================


def test_max_fillable_k_is_limited_by_budget():
    # $95 buys exactly 100 baskets at the top of the book
    assert arbmath.max_fillable_k(BASKET, 95.0) == pytest.approx(100.0, abs=0.01)


def test_max_fillable_k_is_limited_by_depth_not_budget():
    """With unlimited money you still cannot buy more than the book holds."""
    assert arbmath.max_fillable_k(BASKET, 1_000_000.0) == pytest.approx(200.0)


def test_max_fillable_k_of_a_dry_leg_is_zero():
    assert arbmath.max_fillable_k([("A", LEG_A), ("B", [])], 100.0) == 0.0
    assert arbmath.max_fillable_k(BASKET, 0.0) == 0.0


def test_optimal_k_finds_the_peak_not_the_maximum_size():
    """
    THE regression test for the sizing bug.

    With a $200 budget the old code bought 200 baskets — the largest size
    that fit — and made exactly $0. The peak is at 100 baskets for $5.
    """
    best = arbmath.optimal_k(BASKET, 200.0, NO_FEE)
    assert best is not None
    assert best["shares"] == pytest.approx(100.0, abs=0.5)
    assert best["profit"] == pytest.approx(5.0, abs=0.01)
    assert best["k_max_fillable"] == pytest.approx(200.0, abs=0.5)


def test_optimal_k_respects_a_smaller_budget():
    best = arbmath.optimal_k(BASKET, 50.0, NO_FEE)
    assert best["real_cost"] <= 50.0
    # $50 at 0.95/basket ~ 52.6 baskets, all inside the first levels
    assert best["shares"] == pytest.approx(50 / 0.95, abs=0.5)


def test_optimal_k_returns_none_when_there_is_no_profit():
    """Sum of asks above $1 — buying the basket is a guaranteed loss."""
    losing = [("A", [(0.60, 100.0)]), ("B", [(0.55, 100.0)])]
    assert arbmath.optimal_k(losing, 100.0, NO_FEE) is None


def test_optimal_k_returns_none_when_fees_eat_the_edge():
    """Same book, but a 7% crypto fee turns a real edge into a loss."""
    thin = [("A", [(0.49, 100.0)]), ("B", [(0.50, 100.0)])]
    assert arbmath.optimal_k(thin, 100.0, NO_FEE) is not None
    assert arbmath.optimal_k(thin, 100.0, 0.07) is None


def test_optimal_k_never_exceeds_the_budget():
    for budget in (1, 7.5, 33, 95, 150, 500):
        best = arbmath.optimal_k(BASKET, float(budget), 0.04)
        if best:
            assert best["real_cost"] <= budget + 1e-6


# =====================================================================
# Slippage curve
# =====================================================================


def test_curve_has_one_entry_per_affordable_capital():
    curve = arbmath.compute_slippage_curve(BASKET, NO_FEE, [10, 50, 100])
    assert [c["capital"] for c in curve] == [10, 50, 100]


def test_curve_profit_plateaus_once_the_book_is_exhausted():
    """
    Beyond $95 there is nothing left worth buying, so profit stops growing.
    An edge that does not scale is exactly what this curve exists to show.
    """
    curve = arbmath.compute_slippage_curve(BASKET, NO_FEE, [95, 500, 5000])
    profits = [c["profit"] for c in curve]
    assert profits[0] == pytest.approx(5.0, abs=0.01)
    assert all(p == pytest.approx(5.0, abs=0.01) for p in profits)


def test_curve_is_empty_when_there_is_no_edge():
    losing = [("A", [(0.60, 100.0)]), ("B", [(0.55, 100.0)])]
    assert arbmath.compute_slippage_curve(losing, NO_FEE) == []


# =====================================================================
# evaluate_basket — the single entry point every component uses
# =====================================================================


def test_evaluate_reports_the_edge():
    result = arbmath.evaluate_basket(BASKET, NO_FEE, [100])
    assert result["dry_legs"] == []
    assert result["sum_best_asks"] == pytest.approx(0.95)
    assert result["gross_edge"] == pytest.approx(0.05)
    assert result["net_edge"] == pytest.approx(0.05)
    assert result["best"]["profit"] == pytest.approx(5.0, abs=0.01)


def test_evaluate_subtracts_fees_from_the_edge():
    result = arbmath.evaluate_basket(BASKET, 0.04, [100])
    expected_fee = fees.fee_per_share([0.40, 0.55], 0.04)
    assert result["fee_per_share"] == pytest.approx(expected_fee)
    assert result["net_edge"] == pytest.approx(0.05 - expected_fee)
    assert result["net_edge"] < result["gross_edge"]


def test_evaluate_names_the_dry_legs():
    """
    A dry leg must be reported by name, not swallowed as a generic failure —
    when a 12-leg event never trades, you need to know it is always the
    'Other' leg that has no sellers.
    """
    result = arbmath.evaluate_basket(
        [("Trump", LEG_A), ("Other", [])], 0.04)
    assert result["dry_legs"] == ["Other"]
    assert result["net_edge"] is None
    assert result["curve"] == []


def test_evaluate_skips_the_expensive_curve_when_there_is_no_edge():
    losing = [("A", [(0.60, 100.0)]), ("B", [(0.55, 100.0)])]
    result = arbmath.evaluate_basket(losing, NO_FEE)
    assert result["net_edge"] < 0
    assert result["curve"] == []
    assert result["best"] is None


def test_evaluate_is_stable_on_a_many_legged_event():
    """20 legs at 0.045 each: sum 0.90, a real 10% gross edge."""
    legs = [(f"leg{i}", [(0.045, 1000.0)]) for i in range(20)]
    result = arbmath.evaluate_basket(legs, 0.04, [1000])
    assert result["num_legs"] == 20
    assert result["sum_best_asks"] == pytest.approx(0.90)
    assert result["best"]["profit"] > 0


# =====================================================================
# Top of book — what fits at the quoted price
# =====================================================================


def test_top_of_book_is_limited_by_the_thinnest_leg():
    """
    A basket needs the same number of shares of every leg, so the size
    available without slippage is the smallest top level among them — not
    the average, and not the largest.
    """
    legs = [("A", [(0.48, 500), (0.49, 9000)]),
            ("B", [(0.49, 120), (0.50, 9000)])]

    top = arbmath.top_of_book(legs, 1.0, 0.0)

    assert top["shares"] == 120
    assert top["sum_best_asks"] == pytest.approx(0.97)
    assert top["capital"] == pytest.approx(120 * 0.97)


def test_top_of_book_profit_uses_the_quoted_prices_only():
    legs = [("A", [(0.40, 100), (0.45, 900)]),
            ("B", [(0.55, 100), (0.60, 900)])]

    top = arbmath.top_of_book(legs, 1.0, 0.0)

    # 100 shares of a 0.95 basket paying 1.00, no fee
    assert top["profit"] == pytest.approx(100 * 0.05)


def test_top_of_book_charges_the_fee():
    legs = [("A", [(0.40, 100)]), ("B", [(0.55, 100)])]

    free = arbmath.top_of_book(legs, 1.0, 0.0)["profit"]
    charged = arbmath.top_of_book(legs, 1.0, 0.05)["profit"]

    assert charged < free


def test_top_of_book_uses_the_payout_it_is_given():
    """
    A NO basket over N outcomes pays N-1. Assuming 1 would report every
    one of them as a catastrophic loss.
    """
    # three legs at 0.60 — the basket costs 1.80
    legs = [("A", [(0.6, 50)]), ("B", [(0.6, 50)]), ("C", [(0.6, 50)])]

    as_yes = arbmath.top_of_book(legs, 1.0, 0.0)
    as_no = arbmath.top_of_book(legs, 2.0, 0.0)

    assert as_yes["profit"] < 0          # 1.80 to return 1.00 — a loss
    assert as_no["profit"] > 0           # 1.80 to return 2.00 — a trade
    assert as_no["profit"] == pytest.approx(50 * 0.2)


def test_a_dry_leg_makes_the_top_of_book_empty_rather_than_raising():
    legs = [("A", [(0.4, 100)]), ("B", [])]

    top = arbmath.top_of_book(legs, 1.0, 0.0)

    assert top["shares"] == 0
    assert top["capital"] == 0
    assert top["sum_best_asks"] is None


def test_top_of_book_never_exceeds_what_the_curve_deploys():
    """
    The curve is free to walk deeper and deploy more; the top of book is a
    floor under it, never above.
    """
    legs = [("A", [(0.40, 50), (0.44, 5000)]),
            ("B", [(0.55, 50), (0.56, 5000)])]

    top = arbmath.top_of_book(legs, 1.0, 0.02)
    best = arbmath.evaluate_basket(legs, 0.02, [10, 100, 1000, 5000])["best"]

    assert top["shares"] <= best["shares"]
