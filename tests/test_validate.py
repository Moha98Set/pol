"""
Tests for the validation layer.

The cases that matter here are the ones where bad data produces a
*plausible* number rather than an error — a basket that looks cheap because
one leg is stale, or because two legs secretly share an order book. Those
are the trades that lose money, and none of them raise an exception on
their own.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import validate


def future(days=30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def past(days=1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def market(volume=50_000, tokens=("a", "b"), **kw):
    m = {
        "closed": False,
        "enableOrderBook": True,
        "volume24hr": volume,
        "clobTokenIds": json.dumps(list(tokens)),
        "slug": kw.pop("slug", "some-market"),
    }
    m.update(kw)
    return m


def event(markets=None, **kw):
    e = {
        "title": "Who wins?",
        "slug": "who-wins",
        "endDate": future(),
        "markets": markets if markets is not None else [market()],
    }
    e.update(kw)
    return e


# =====================================================================
# Verdict behaviour
# =====================================================================


def test_verdict_is_truthy_when_ok():
    assert validate.ACCEPT
    assert not validate.reject(validate.DRY_LEG)


def test_a_suspect_verdict_still_passes():
    """
    Suspicion is not rejection. Discarding everything odd means never
    learning which suspicions were justified.
    """
    verdict = validate.accept([validate.THIN_BOOK])
    assert verdict.ok
    assert verdict.suspect
    assert verdict.suspicions == [validate.THIN_BOOK]


# =====================================================================
# Event eligibility
# =====================================================================


def test_a_normal_event_is_accepted():
    assert validate.validate_event(event())


def test_event_with_no_open_markets_is_rejected():
    verdict = validate.validate_event(event(markets=[
        market(closed=True), market(enableOrderBook=False)]))
    assert verdict.code == validate.NO_OPEN_MARKETS


def test_low_volume_is_rejected_with_the_amount_in_the_detail():
    verdict = validate.validate_event(event(markets=[market(volume=10)]))
    assert verdict.code == validate.LOW_VOLUME
    assert "10" in verdict.detail


def test_volume_is_summed_across_legs():
    """
    A 20-leg event with $100 on each leg is a $2000 event, not twenty
    illiquid ones. Judging legs individually would drop real markets.
    """
    legs = [market(volume=100, slug=f"m{i}", tokens=(f"t{i}", f"n{i}"))
            for i in range(20)]
    assert validate.validate_event(event(markets=legs))


def test_an_already_resolved_event_is_rejected():
    verdict = validate.validate_event(event(endDate=past(2)))
    assert verdict.code == validate.ALREADY_RESOLVED
    assert "ago" in verdict.detail


def test_an_event_resolving_in_minutes_is_rejected():
    """No time to fill both legs before the market stops trading."""
    verdict = validate.validate_event(
        event(endDate=(datetime.now(timezone.utc)
                       + timedelta(minutes=5)).isoformat()))
    assert verdict.code == validate.RESOLVES_TOO_SOON


def test_an_event_years_away_is_rejected():
    verdict = validate.validate_event(event(endDate=future(900)))
    assert verdict.code == validate.RESOLVES_TOO_LATE


def test_a_missing_end_date_is_suspect_not_fatal():
    """
    Plenty of real events omit endDate. It only means the capital-lock
    filter cannot be applied, which is worth knowing, not worth dropping.
    """
    verdict = validate.validate_event(event(endDate=None))
    assert verdict.ok
    assert validate.NO_END_DATE in verdict.suspicions


def test_an_unparseable_end_date_is_treated_as_missing():
    verdict = validate.validate_event(event(endDate="not a date"))
    assert verdict.ok
    assert validate.NO_END_DATE in verdict.suspicions


# =====================================================================
# Exclusivity — the check that separates arbitrage from gambling
# =====================================================================


def test_a_binary_event_is_exclusive_by_construction():
    assert validate.validate_exclusivity(event(), [market()], [])


def test_a_multi_event_without_negrisk_is_rejected():
    legs = [market(slug="a"), market(slug="b", tokens=("c", "d"))]
    verdict = validate.validate_exclusivity(event(markets=legs), legs, [])
    assert verdict.code == validate.NOT_NEG_RISK


def test_negrisk_multi_event_is_accepted():
    legs = [market(slug="a"), market(slug="b", tokens=("c", "d"))]
    assert validate.validate_exclusivity(
        event(markets=legs, negRisk=True), legs, [])


def test_a_blacklisted_title_pattern_is_rejected_even_with_negrisk():
    """
    negRisk is Polymarket's flag, and it is occasionally set on events
    whose legs overlap. The pattern list is the second layer, and it has to
    be able to override the first.
    """
    legs = [market(slug="a"), market(slug="b", tokens=("c", "d"))]
    verdict = validate.validate_exclusivity(
        event(markets=legs, negRisk=True,
              title="Bitcoin above $100k in March?"), legs, ["above $"])
    assert verdict.code == validate.NON_EXHAUSTIVE_PATTERN
    assert verdict.detail == "above $"


# =====================================================================
# Tokens
# =====================================================================


def test_valid_tokens_pass():
    assert validate.validate_tokens([market()], is_binary=True)


def test_missing_tokens_are_rejected():
    verdict = validate.validate_tokens(
        [market(clobTokenIds=None)], is_binary=True)
    assert verdict.code == validate.NO_TOKENS


def test_malformed_token_json_is_rejected():
    verdict = validate.validate_tokens(
        [market(clobTokenIds="[not json")], is_binary=True)
    assert verdict.code == validate.MALFORMED_TOKENS


def test_a_binary_market_with_one_token_is_rejected():
    verdict = validate.validate_tokens(
        [market(clobTokenIds=json.dumps(["only-one"]))], is_binary=True)
    assert verdict.code == validate.MARKET_COUNT_MISMATCH


def test_duplicate_tokens_across_legs_are_rejected():
    """
    The subtle one. Two legs sharing an order book means the same
    liquidity is counted twice, and the basket looks cheaper than it is.
    """
    legs = [market(slug="a", tokens=("shared", "x")),
            market(slug="b", tokens=("shared", "y"))]
    verdict = validate.validate_tokens(legs, is_binary=False)
    assert verdict.code == validate.DUPLICATE_TOKENS
    assert "shares a token" in verdict.detail


def test_tokens_given_as_a_real_list_are_accepted():
    """The API returns this field as a JSON string, sometimes as a list."""
    assert validate.validate_tokens(
        [market(clobTokenIds=["a", "b"])], is_binary=True)


# =====================================================================
# Book quality
# =====================================================================


def test_a_healthy_book_passes():
    assert validate.validate_book("Yes", [(0.40, 100.0), (0.45, 50.0)])


def test_a_dry_leg_is_rejected_by_name():
    verdict = validate.validate_book("Other", [])
    assert verdict.code == validate.DRY_LEG
    assert verdict.detail == "Other"


def test_an_implausibly_cheap_ask_is_rejected():
    """
    A leg at $0.0005 is a stale resting order nobody cleaned up, not a
    chance to buy a claim on $1 for half a tenth of a cent. Trusting it
    makes any basket containing it look free.
    """
    verdict = validate.validate_book("Yes", [(0.0005, 10_000.0)])
    assert verdict.code == validate.IMPLAUSIBLE_PRICE


def test_an_ask_at_the_top_of_the_range_is_rejected():
    assert validate.validate_book(
        "Yes", [(0.9995, 100.0)]).code == validate.IMPLAUSIBLE_PRICE


def test_a_crossed_book_is_rejected():
    """
    A bid above the cheapest ask would have matched on a live exchange.
    Seeing one means at least one side is stale, so neither can be trusted.
    """
    verdict = validate.validate_book(
        "Yes", asks=[(0.40, 100.0)], bids=[(0.55, 100.0)])
    assert verdict.code == validate.CROSSED_BOOK


def test_a_normal_spread_is_not_crossed():
    assert validate.validate_book(
        "Yes", asks=[(0.40, 100.0)], bids=[(0.38, 100.0)])


def test_a_book_worth_under_a_dollar_is_suspect_not_rejected():
    verdict = validate.validate_book("Yes", [(0.40, 1.0)])
    assert verdict.ok
    assert validate.THIN_BOOK in verdict.suspicions


# =====================================================================
# Basket sanity
# =====================================================================


def test_a_normal_basket_passes():
    legs = [("A", []), ("B", [])]
    assert validate.validate_basket(legs, sum_best_asks=0.95)


def test_a_single_leg_basket_is_rejected():
    assert validate.validate_basket(
        [("A", [])], 0.9).code == validate.MARKET_COUNT_MISMATCH


def test_a_suspiciously_cheap_basket_is_rejected():
    """
    Sum of $0.20 across the legs is not a windfall. It is evidence the
    legs are not what we think they are — the most expensive possible
    misreading of the data, so it gets its own code.
    """
    verdict = validate.validate_basket([("A", []), ("B", [])], 0.20)
    assert verdict.code == validate.SUM_TOO_LOW
    assert "0.2" in verdict.detail


def test_a_basket_at_or_above_a_dollar_has_no_edge():
    assert validate.validate_basket(
        [("A", []), ("B", [])], 1.02).code == validate.NO_EDGE
    assert validate.validate_basket(
        [("A", []), ("B", [])], 1.00).code == validate.NO_EDGE


def test_a_basket_just_inside_the_floor_is_flagged():
    verdict = validate.validate_basket([("A", []), ("B", [])], 0.55)
    assert verdict.ok
    assert validate.SUM_TOO_LOW in verdict.suspicions


# =====================================================================
# Codes are stable — they end up in the database and in queries
# =====================================================================


def test_every_code_is_a_lowercase_identifier():
    codes = [v for k, v in vars(validate).items()
             if k.isupper() and isinstance(v, str) and k != "OK"]
    for code in codes:
        assert code == code.lower()
        assert " " not in code


def test_codes_are_unique():
    codes = [v for k, v in vars(validate).items()
             if k.isupper() and isinstance(v, str)]
    assert len(codes) == len(set(codes))
