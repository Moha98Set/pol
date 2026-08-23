"""
Tests for the shared non-exhaustive pattern list.

The list decides which events are even eligible for arbitrage, and it used
to exist twice with two different contents. The most important test in this
file is the one asserting there is now exactly one list.

The rest pin the two failure directions, which cost differently:

  * too loose  -> a "riskless" basket that is actually a bet, and loses
  * too strict -> real, tradeable inefficiencies never get looked at

Bucketed count markets sit right on that line, so they get their own tests.
"""

import pytest

import findmarket
import patterns
import scanner
import validate


# =====================================================================
# One list, shared
# =====================================================================


def test_scanner_and_findmarket_share_one_list():
    """
    The regression this module exists for. The two lists had drifted 16
    patterns apart, so on a real scan two of three reported opportunities
    would have been rejected outright by the other tool.
    """
    assert scanner.NON_EXHAUSTIVE_PATTERNS is patterns.PATTERN_STRINGS
    assert findmarket.NON_EXHAUSTIVE_PATTERNS is patterns.PATTERN_STRINGS


def test_every_pattern_carries_a_reason():
    """
    Without a documented reason nobody can safely delete a pattern, so the
    list only ever grows and slowly strangles the scan.
    """
    for pattern, reason in patterns.NON_EXHAUSTIVE_PATTERNS:
        assert pattern, "empty pattern"
        assert reason and len(reason) > 10, f"{pattern!r} has no real reason"


def test_patterns_are_lowercase():
    """Matching lowercases the title, so an uppercase pattern never fires."""
    for pattern in patterns.PATTERN_STRINGS:
        assert pattern == pattern.lower()


def test_no_duplicate_patterns():
    assert len(patterns.PATTERN_STRINGS) == len(set(patterns.PATTERN_STRINGS))


def test_deliberately_allowed_patterns_are_not_in_the_live_list():
    """
    The rejected candidates are documented in the same module. If one is
    ever added for real, this catches the contradiction.
    """
    for pattern, _reason in patterns.DELIBERATELY_ALLOWED:
        assert pattern not in patterns.PATTERN_STRINGS


# =====================================================================
# Matching
# =====================================================================


@pytest.mark.parametrize("title", [
    "Chiefs vs. Eagles",
    "Lakers vs Celtics",
    "Will Bitcoin go above $100,000?",
    "Will ETH hit $5000 this year?",
    "Will SOL reach $500?",
    "Highest temperature in NYC?",
    "Will X happen by March?",
    "Will Y happen by 2027?",
    "Where will BTC close above 90k?",
])
def test_non_exclusive_titles_are_caught(title):
    pattern, reason = patterns.matches(title)
    assert pattern, f"{title!r} was not caught"
    assert reason


def test_matching_is_case_insensitive():
    assert patterns.matches("CHIEFS VS. EAGLES")[0]


def test_matching_returns_the_reason():
    pattern, reason = patterns.matches("Chiefs vs. Eagles")
    assert pattern == "vs."
    assert "draw" in reason


def test_a_clean_title_matches_nothing():
    assert patterns.matches("Who wins the 2028 election?") == (None, None)


def test_empty_and_missing_titles_are_safe():
    assert patterns.matches("") == (None, None)
    assert patterns.matches(None) == (None, None)
    assert patterns.matches_event({}) == (None, None)


# =====================================================================
# Bucketed counts — the deliberate resolution of the divergence
# =====================================================================


@pytest.mark.parametrize("title", [
    "NZ Election: Labour Party # of seats?",
    "Donald Trump # Truth Social posts July 28 - August 4, 2026?",
    "How many Fed rate cuts in 2026?",
    "How many cases in California?",
])
def test_bucketed_count_markets_are_allowed(title):
    """
    These read like they overlap and do not: the legs partition the whole
    number line ("<20", "20-39", ..., "200+"), exactly one bucket wins.
    They are also among the few genuinely inefficient markets on the
    platform — wide, thin, and priced leg by leg at different times.

    findmarket used to drop them via "# of" and "posts ". Two of the three
    opportunities found on a real scan were exactly this shape.
    """
    assert patterns.matches(title) == (None, None)


def test_a_price_bucket_market_is_still_caught():
    """
    The other half of the judgement: ranged *price* thresholds under one
    event really do overlap, so they stay filtered.
    """
    assert patterns.matches("Will BTC be above $120k in July?")[0]


def test_the_allowed_list_does_not_swallow_ordinary_prose():
    """
    'above ' and ' below ' were dropped for being too broad — they match
    'above average' and plain English. Assert they no longer fire.
    """
    assert patterns.matches("Will turnout be above average?") == (None, None)
    assert patterns.matches("Who finishes below the line?") == (None, None)


# =====================================================================
# Wired into validation
# =====================================================================


def _multi_event(title, neg_risk=True):
    markets = [{"slug": "a"}, {"slug": "b"}]
    return {"title": title, "negRisk": neg_risk}, markets


def test_validate_exclusivity_uses_the_shared_list_by_default():
    event, markets = _multi_event("Chiefs vs. Eagles")
    verdict = validate.validate_exclusivity(event, markets)
    assert verdict.code == validate.NON_EXHAUSTIVE_PATTERN


def test_the_rejection_explains_itself():
    """A filter whose decisions cannot be explained is one nobody changes."""
    event, markets = _multi_event("Chiefs vs. Eagles")
    verdict = validate.validate_exclusivity(event, markets)
    assert "vs." in verdict.detail
    assert "draw" in verdict.detail


def test_a_bucketed_count_event_now_passes_validation():
    event, markets = _multi_event("NZ Election: Labour Party # of seats?")
    assert validate.validate_exclusivity(event, markets)


def test_an_explicit_list_still_overrides_the_default():
    """Kept overridable so a stricter list can be tried without editing
    the module every tool reads."""
    event, markets = _multi_event("NZ Election: Labour Party # of seats?")
    verdict = validate.validate_exclusivity(event, markets, ["# of"])
    assert verdict.code == validate.NON_EXHAUSTIVE_PATTERN


def test_negrisk_is_still_required_regardless_of_the_title():
    event, markets = _multi_event("Who wins the election?", neg_risk=False)
    assert validate.validate_exclusivity(event, markets).code == \
           validate.NOT_NEG_RISK
