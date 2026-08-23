"""
Tests for the fee model.

These are cheap to write and they protect the number that decides whether
a trade is profitable, so they are worth more than their size suggests.
"""

import pytest

import fees


# =====================================================================
# Rate resolution from tags
# =====================================================================


def test_geopolitics_is_free_even_when_also_tagged_politics():
    """
    Order in FEE_RATES is load-bearing. An event tagged both 'geopolitics'
    and 'politics' pays 0%, not 4% — if someone reorders the table this
    silently starts over-charging every geopolitical event.
    """
    rate, category = fees.fee_rate_for_tags("politics geopolitics elections")
    assert rate == 0.0
    assert category == "geopolitics"


@pytest.mark.parametrize("tags, expected", [
    ("crypto bitcoin", 0.07),
    ("ethereum", 0.07),
    ("sports nfl", 0.03),
    ("esports", 0.03),
    ("politics", 0.04),
    ("finance", 0.04),
    ("mention markets", 0.04),
])
def test_known_categories(tags, expected):
    rate, _category = fees.fee_rate_for_tags(tags)
    assert rate == expected


def test_unknown_tags_fall_back_to_default():
    rate, category = fees.fee_rate_for_tags("weather culture something-new")
    assert rate == fees.DEFAULT_FEE_RATE
    assert category == "other"


def test_empty_tags_fall_back_to_default():
    assert fees.fee_rate_for_tags("") == (fees.DEFAULT_FEE_RATE, "other")


def test_fee_rate_for_event_reads_tag_labels():
    event = {"tags": [{"label": "Crypto"}, {"label": "Bitcoin"}]}
    rate, category = fees.fee_rate_for_event(event)
    assert rate == 0.07
    assert category == "crypto"


def test_fee_rate_for_event_survives_malformed_tags():
    """The API sometimes returns tags with a null label. Must not raise."""
    event = {"tags": [{"label": None}, {}]}
    rate, _ = fees.fee_rate_for_event(event)
    assert rate == fees.DEFAULT_FEE_RATE


def test_fee_rate_for_event_with_no_tags_key():
    assert fees.fee_rate_for_event({})[0] == fees.DEFAULT_FEE_RATE


# =====================================================================
# The p * (1 - p) shape — this is what the old flat-2% model got wrong
# =====================================================================


def test_fee_is_zero_at_the_boundaries():
    """A leg at 0.00 or 1.00 is fully resolved and costs nothing to trade."""
    assert fees.fee_for_leg(0.0, 100, 0.04) == 0.0
    assert fees.fee_for_leg(1.0, 100, 0.04) == 0.0


def test_fee_peaks_at_fifty_cents():
    rate = 0.04
    at_half = fees.fee_for_leg(0.50, 100, rate)
    for p in (0.05, 0.2, 0.35, 0.65, 0.8, 0.95):
        assert fees.fee_for_leg(p, 100, rate) < at_half


def test_fee_is_symmetric_around_fifty_cents():
    assert fees.fee_for_leg(0.3, 100, 0.04) == pytest.approx(
        fees.fee_for_leg(0.7, 100, 0.04))


def test_fee_exact_value():
    # 100 shares * 4% * 0.5 * 0.5 = 1.00
    assert fees.fee_for_leg(0.5, 100, 0.04) == pytest.approx(1.0)


def test_fee_scales_linearly_with_shares():
    one = fees.fee_for_leg(0.4, 1, 0.05)
    assert fees.fee_for_leg(0.4, 250, 0.05) == pytest.approx(250 * one)


def test_zero_rate_means_zero_fee():
    assert fees.fee_for_legs([0.3, 0.4, 0.5], 1000, 0.0) == 0.0


# =====================================================================
# Multi-leg aggregation
# =====================================================================


def test_fee_for_legs_sums_each_leg():
    prices = [0.2, 0.5, 0.3]
    expected = sum(fees.fee_for_leg(p, 10, 0.04) for p in prices)
    assert fees.fee_for_legs(prices, 10, 0.04) == pytest.approx(expected)


def test_fee_per_share_equals_fee_for_one_share():
    prices = [0.11, 0.44, 0.45]
    assert fees.fee_per_share(prices, 0.07) == pytest.approx(
        fees.fee_for_legs(prices, 1.0, 0.07))


FLAT_OLD_MODEL = 0.02   # what findmarket.py used to assume


def test_flat_model_undercharged_wide_multi_outcome_events():
    """
    A 20-leg event at 0.05 each really pays 3.8% per share, not 2%.
    The old flat model would have called losing trades profitable.
    """
    prices = [0.05] * 20
    real = fees.fee_per_share(prices, 0.04)
    assert real == pytest.approx(20 * 0.04 * 0.05 * 0.95)
    assert real > FLAT_OLD_MODEL


def test_flat_model_overcharged_lopsided_binaries():
    """
    A binary at 0.02/0.97 really pays 0.2% per share. The old flat model
    charged 2% and threw away genuine edges.
    """
    real = fees.fee_per_share([0.02, 0.97], 0.04)
    assert real < FLAT_OLD_MODEL / 5


def test_binary_at_the_money_is_the_worst_case():
    """Two legs at 0.50 is the most expensive possible basket to trade."""
    worst = fees.fee_per_share([0.5, 0.5], 0.04)
    assert worst == pytest.approx(2 * 0.04 * 0.25)
    assert worst > fees.fee_per_share([0.9, 0.1], 0.04)
