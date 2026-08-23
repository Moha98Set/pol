"""
Tests for the NO-side basket — the mirror trade.

The arithmetic, stated once so the tests can be read against it:

    N mutually exclusive, exhaustive outcomes.
    Buy one NO share on every leg.
    Exactly one outcome wins, so exactly one NO expires worthless
    and the other N-1 pay $1 each.

        payout  = N - 1
        cost    = sum of NO asks
        arb if    sum(NO asks) < N - 1

Because a NO ask at p is a YES bid at 1-p, that is the same condition as
"sum of YES bids > 1" — the basket can be SOLD for more than a dollar. The
YES-side scan only ever asks whether it can be BOUGHT for less than one.

The trap this file exists to guard is the capital one: a NO basket's edge
looks enormous next to a YES basket's because the basket itself is N-1
times bigger. Ranking or thresholding on edge-per-basket would make the
scanner prefer wide events for no reason but their width.
"""

import json

import pytest

import arbmath
import config
import db as dblib
import fees
import scanner
import validate
from tests import fakeapi


@pytest.fixture
def database(tmp_path):
    db = dblib.connect(tmp_path / "no_side.db")
    yield db
    db.close()


def bids(*levels):
    """Raw YES bid levels, API-shaped."""
    return [{"price": str(p), "size": str(s)} for p, s in levels]


# =====================================================================
# The mirror: NO asks are YES bids, reflected
# =====================================================================


def test_a_no_ask_is_one_minus_the_yes_bid():
    """
    Measured, not assumed: across 1234 live legs the best NO ask equalled
    (1 - best YES bid) exactly, every time.
    """
    yes_bids = arbmath.normalize_asks(bids((0.30, 100)))
    assert arbmath.no_asks_from_yes_bids(yes_bids) == [(0.70, 100.0)]


def test_the_best_no_ask_comes_from_the_highest_yes_bid():
    """
    Reflection reverses the order: the best (highest) bid to sell into
    becomes the cheapest NO to buy. Getting this backwards would price
    every NO basket off the worst level in the book.
    """
    yes_bids = arbmath.normalize_asks(bids((0.30, 100), (0.20, 50), (0.10, 25)))
    no_asks = arbmath.no_asks_from_yes_bids(yes_bids)

    assert no_asks == [(0.70, 100.0), (0.80, 50.0), (0.90, 25.0)]
    assert arbmath.best_ask(no_asks) == 0.70


def test_sizes_carry_over_unchanged():
    """One share of YES sold is one share of NO bought."""
    no_asks = arbmath.no_asks_from_yes_bids(
        arbmath.normalize_asks(bids((0.25, 42))))
    assert no_asks[0][1] == 42.0


def test_degenerate_bids_are_dropped():
    yes_bids = arbmath.normalize_asks(
        bids((0.0, 100), (1.0, 100), (0.5, 0), (0.4, 10)))
    assert arbmath.no_asks_from_yes_bids(yes_bids) == [(0.60, 10.0)]


def test_an_empty_bid_side_gives_an_empty_no_book():
    assert arbmath.no_asks_from_yes_bids([]) == []


# =====================================================================
# Payout arithmetic
# =====================================================================


THREE_LEG_NO = [
    ("A", [(0.60, 100.0)]),
    ("B", [(0.70, 100.0)]),
    ("C", [(0.65, 100.0)]),
]   # sum 1.95, payout 2 -> $0.05 per basket


def test_a_no_basket_pays_n_minus_one():
    result = arbmath.basket_profit(THREE_LEG_NO, 100, 0.0, payout_per_basket=2)
    assert result["real_cost"] == pytest.approx(195.0)
    assert result["profit"] == pytest.approx(200.0 - 195.0)


def test_the_default_payout_is_still_one():
    """Every existing YES-side caller must be untouched by this change."""
    result = arbmath.basket_profit(THREE_LEG_NO, 100, 0.0)
    assert result["profit"] == pytest.approx(100.0 - 195.0)


def test_a_no_basket_with_no_edge_yields_nothing():
    losing = [("A", [(0.70, 100.0)]), ("B", [(0.70, 100.0)])]   # 1.40 vs 1
    assert arbmath.optimal_k(losing, 500.0, 0.0, payout_per_basket=1) is None


def test_evaluate_reports_the_edge_against_the_right_payout():
    result = arbmath.evaluate_basket(THREE_LEG_NO, 0.0, [500],
                                     payout_per_basket=2)
    assert result["payout_per_basket"] == 2
    assert result["sum_best_asks"] == pytest.approx(1.95)
    assert result["gross_edge"] == pytest.approx(0.05)
    assert result["best"]["profit"] > 0


def test_edge_per_dollar_is_the_comparable_number():
    """
    $0.05 per basket sounds like the YES side's 5%, and is not: the basket
    costs $1.95, so the real return is 2.6%.
    """
    result = arbmath.evaluate_basket(THREE_LEG_NO, 0.0, [500],
                                     payout_per_basket=2)
    assert result["net_edge"] == pytest.approx(0.05)
    assert result["net_edge_per_dollar"] == pytest.approx(0.05 / 1.95)
    assert result["net_edge_per_dollar"] < result["net_edge"]


def test_fees_are_identical_on_both_sides_of_the_same_leg():
    """
    A consequence of the p*(1-p) shape being symmetric: buying NO at 0.7
    costs exactly what buying YES at 0.3 costs. Worth pinning, because it
    means the NO side is never cheaper to trade — only bigger.
    """
    assert fees.fee_for_leg(0.7, 100, 0.04) == pytest.approx(
        fees.fee_for_leg(0.3, 100, 0.04))


# =====================================================================
# Validation of NO baskets
# =====================================================================


def test_a_normal_no_basket_passes():
    # three legs -> payout 2; 1.95 is under it, so there is edge
    assert validate.validate_no_basket([("A", []), ("B", []), ("C", [])], 1.95)


def test_a_no_basket_at_or_above_payout_has_no_edge():
    verdict = validate.validate_no_basket(
        [("A", []), ("B", []), ("C", [])], 2.0)
    assert verdict.code == validate.NO_EDGE


def test_the_payout_tracks_the_leg_count():
    """Two legs pay 1, three pay 2. Getting this off by one would price
    every NO basket against the wrong target."""
    assert validate.validate_no_basket([("A", []), ("B", [])], 0.95)
    assert validate.validate_no_basket(
        [("A", []), ("B", [])], 1.05).code == validate.NO_EDGE


def test_an_impossibly_cheap_no_basket_is_rejected():
    """
    Three legs summing to $0.10 against a $2 payout is not free money, it
    is a stale bid side. Same reasoning as the YES-side floor, mirrored.
    """
    verdict = validate.validate_no_basket(
        [("A", []), ("B", []), ("C", [])], 0.10)
    assert verdict.code == validate.SUM_TOO_LOW


def test_a_single_leg_no_basket_is_meaningless():
    assert validate.validate_no_basket([("A", [])], 0.5).code == \
           validate.MARKET_COUNT_MISMATCH


# =====================================================================
# The scanner, end to end
# =====================================================================


@pytest.fixture
def api(monkeypatch):
    fake = fakeapi.FakeAPI()
    monkeypatch.setattr(scanner, "SESSION", fake)
    monkeypatch.setattr(scanner, "API_SLEEP", 0)
    return fake


def event_with_bids(slug="mirror", yes_prices=(0.40, 0.40, 0.40),
                    bid_prices=None, size=500):
    """
    A negRisk event whose YES asks and YES bids are set independently, so
    the two sides can be given different edges.
    """
    event, books = fakeapi.multi_event(
        slug=slug, legs=[(p, size) for p in yes_prices])
    for i, token in enumerate(f"{slug}-{j}" for j in range(len(yes_prices))):
        if bid_prices:
            books[token]["bids"] = [
                {"price": str(bid_prices[i]), "size": str(size)}]
        else:
            books[token]["bids"] = []
    return event, books


def test_the_no_side_finds_an_edge_the_yes_side_cannot_see(api):
    """
    The whole point. YES asks sum to 1.20 — no edge buying. YES bids sum to
    1.05 — so the basket can be SOLD for $1.05, which is arbitrage the
    YES-side scan is structurally blind to.
    """
    api.add(event_with_bids(yes_prices=(0.40, 0.40, 0.40),
                            bid_prices=(0.35, 0.35, 0.35)))

    events = scanner.fetch_all_events()
    result, _verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert result["kind"] == "opportunity"
    assert result["market_type"] == "multi_no"
    # NO asks are 0.65 each -> 1.95 against a payout of 2
    assert result["sum_best_asks"] == pytest.approx(1.95)
    assert result["payout_per_basket"] == 2
    assert result["best_profit"] > 0


def test_the_scanner_names_the_no_token_to_order(api):
    """
    The money bug, on the scanner path. The basket is priced off the YES
    book's bids but must be ordered against the NO token — a stored
    opportunity naming the YES token would have the executor buy the exact
    opposite position on every leg.
    """
    api.add(event_with_bids(bid_prices=(0.35, 0.35, 0.35)))

    events = scanner.fetch_all_events()
    result, _verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    legs = result["legs_detail"]
    assert [leg["token_id"] for leg in legs] == [
        "mirror-0-no", "mirror-1-no", "mirror-2-no"]
    assert [leg["yes_token_id"] for leg in legs] == [
        "mirror-0", "mirror-1", "mirror-2"]


def test_a_market_without_a_no_token_cannot_be_traded_this_way(api):
    event, books = event_with_bids(bid_prices=(0.35, 0.35, 0.35))
    # strip one market down to a single token
    event["markets"][1]["clobTokenIds"] = json.dumps(["mirror-1"])
    api.add((event, books))

    events = scanner.fetch_all_events()
    result, verdict = scanner.scan_multi_no_side(
        scanner.prefilter_event(events[0]))

    assert result is None
    assert verdict.code == validate.NO_TOKENS


def test_the_no_side_needs_no_extra_api_calls(api):
    """
    The NO book is the YES book's bid side, already in the response. If
    this ever regresses into a second fetch, the scan's API cost doubles
    for no new information.
    """
    api.add(event_with_bids(bid_prices=(0.35, 0.35, 0.35)))

    events = scanner.fetch_all_events()
    group = scanner.prefilter_event(events[0])
    api.post_calls.clear()

    scanner.scan_event_verbose(group)

    tokens = set(api.tokens_requested)
    assert tokens == {"mirror-0", "mirror-1", "mirror-2"}   # YES tokens only


def test_the_yes_side_is_reported_when_it_is_the_one_with_the_edge(api):
    api.add(event_with_bids(yes_prices=(0.30, 0.30, 0.30),   # sum 0.90, 10%
                            bid_prices=(0.29, 0.29, 0.29)))  # NO 2.13 > 2

    events = scanner.fetch_all_events()
    result, _verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert result["market_type"] == "multi"
    assert result["sum_best_asks"] == pytest.approx(0.90)


def test_the_two_arbs_can_never_both_exist():
    """
    A proof, pinned as a test, because it is the fact that makes the whole
    feature worth having and is easy to lose sight of:

        per leg      best bid <= best ask     (or the book is crossed)
        summing      sum(bid) <= sum(ask)
        YES arb      sum(ask) < 1
        NO  arb      sum(1-bid) < N-1   <=>   sum(bid) > 1

    Both at once would need sum(bid) > 1 > sum(ask) >= sum(bid).
    Contradiction.

    So the sides are mutually exclusive, and they are also complementary:
    together they cover strictly more of the price space than the YES side
    alone. That is exactly the value of adding the NO scan — not a bigger
    edge, but a second half of the space that was never being looked at.
    """
    for sum_ask, sum_bid, n in [(0.95, 0.90, 3), (1.05, 1.02, 3),
                                (1.20, 0.80, 5), (0.99, 0.99, 4)]:
        assert sum_bid <= sum_ask, "test data must not be crossed"
        yes_arb = sum_ask < 1
        no_arb = sum_bid > 1
        assert not (yes_arb and no_arb)


def test_a_wide_no_basket_is_ranked_by_return_not_by_edge_size(api):
    """
    The capital trap. A NO basket's edge is measured against a payout of
    N-1, so a wide event's edge looks large in dollars while being small
    per dollar deployed. The stored net_edge must be the per-dollar
    figure, or every ranking in the system silently prefers wide events.
    """
    n = 12
    # NO asks ~0.912 each -> sum ~10.95 against a payout of 11
    api.add(event_with_bids(slug="wide",
                            yes_prices=[0.30] * n,
                            bid_prices=[0.0875] * n))

    events = scanner.fetch_all_events()
    result, _verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert result["market_type"] == "multi_no"
    assert result["payout_per_basket"] == 11
    # ~$0.05 of edge on a basket costing ~$10.95 is well under 1%
    assert result["net_edge_per_basket"] > result["net_edge"] * 5
    assert result["net_edge"] < 0.01


def test_a_dry_bid_side_rejects_the_no_basket_only(api):
    """
    No bids anywhere means no NO book. The YES side must still be scanned
    normally — one side failing cannot take the other down.
    """
    api.add(event_with_bids(yes_prices=(0.30, 0.30, 0.30), bid_prices=None))

    events = scanner.fetch_all_events()
    group = scanner.prefilter_event(events[0])

    no_result, no_verdict = scanner.scan_multi_no_side(group)
    assert no_result is None
    assert no_verdict.code == validate.DRY_LEG

    result, _ = scanner.scan_event_verbose(group)
    assert result["market_type"] == "multi"


def test_the_no_side_can_be_switched_off(api, monkeypatch):
    monkeypatch.setattr(config, "SCAN_NO_SIDE", False)
    api.add(event_with_bids(bid_prices=(0.35, 0.35, 0.35)))

    events = scanner.fetch_all_events()
    result, _verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert result is None or result["market_type"] != "multi_no"


def test_binary_events_are_untouched(api):
    """A binary event's NO leg is already its second token — the mirror
    logic would double-count it."""
    api.add(fakeapi.binary_event(yes=(0.40, 100), no=(0.55, 100)))

    events = scanner.fetch_all_events()
    result, _ = scanner.scan_event_verbose(scanner.prefilter_event(events[0]))
    assert result["market_type"] == "binary"


# =====================================================================
# Storage
# =====================================================================


def test_a_no_side_opportunity_round_trips_through_the_database(
        api, database):
    import arb_monitor

    api.add(event_with_bids(bid_prices=(0.35, 0.35, 0.35)))
    arb_monitor.run_scan(database)

    row = database.execute(
        "SELECT * FROM opportunities WHERE market_type = 'multi_no'"
    ).fetchone()

    assert row is not None
    assert row["payout_per_basket"] == 2
    assert row["num_outcomes"] == 3
    # net_edge is stored per dollar so it sorts against YES-side rows
    assert row["net_edge"] < row["net_edge_per_basket"]
    legs = json.loads(row["legs_detail"])
    assert all(leg["side"] == "NO" for leg in legs)


def test_existing_yes_rows_still_default_to_a_payout_of_one(api, database):
    import arb_monitor

    api.add(fakeapi.binary_event(yes=(0.40, 100), no=(0.55, 100)))
    arb_monitor.run_scan(database)

    row = database.execute(
        "SELECT * FROM opportunities WHERE market_type = 'binary'").fetchone()
    assert row["payout_per_basket"] == 1.0
