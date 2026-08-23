"""
Title patterns that mean an event's legs are NOT mutually exclusive.
====================================================================

Buying every leg of an event is arbitrage only if exactly one leg pays out.
Polymarket's `negRisk` flag is supposed to guarantee that, and mostly does,
but it is set on some events whose outcomes overlap or leave gaps. So the
title is used as a second, independent check.

This list used to exist twice — once in scanner.py, once in findmarket.py —
and the two had drifted 16 patterns apart. The consequence was concrete:
on a real scan, two of the three opportunities the monitor reported would
have been rejected outright by findmarket. Two tools disagreeing about
which trades are real is worse than either being wrong, because it hides
the disagreement behind whichever one you happen to run.

Each pattern below says why it is here. That matters more than it sounds:
without a reason, nobody can ever safely delete one, so the list only ever
grows and slowly strangles the scan.

Two kinds of pattern are deliberately NOT here
----------------------------------------------
Bucketed count markets — "# of seats?", "# Truth Social posts", "How many
rate cuts?" — read like they overlap but do not. Their legs partition the
whole number line ("<20", "20-39", ..., "200+"), exactly one bucket wins,
and they are among the few genuinely inefficient markets on the platform:
they are wide, thinly traded, and the legs are priced by different people
at different times. findmarket used to drop them via "# of" and "posts ";
that was a mistake, and dropping it is the deliberate resolution of the
divergence rather than an accident of merging.

Ranged threshold markets — "above $100k", "below $50" — genuinely DO
overlap when several are listed under one event, so those stay.
"""

# Each entry is (substring, reason). Matching is case-insensitive on the
# event title.
NON_EXHAUSTIVE_PATTERNS = [
    # --- head-to-head: both sides can lose (draw, void, postponement) ---
    ("vs.", "sports matchup — a draw or void resolves neither side"),
    (" vs ", "sports matchup — a draw or void resolves neither side"),

    # --- price thresholds: several can be true at once ---
    ("above $", "price threshold — 'above $50' and 'above $60' overlap"),
    ("below $", "price threshold — overlapping ranges"),
    ("hit $", "touch threshold — several targets can all be hit"),
    ("reach $", "touch threshold — several targets can all be reached"),
    ("dip to", "touch threshold — several levels can all be touched"),
    ("close above", "close threshold — overlapping ranges"),
    ("close below", "close threshold — overlapping ranges"),
    ("fdv above", "valuation threshold — overlapping ranges"),
    ("price will", "price prediction — legs rarely partition the outcome"),
    ("what price", "price prediction — legs rarely partition the outcome"),
    ("settle at", "settlement level — overlapping ranges"),

    # --- deadline markets: 'by March' implies 'by April' ---
    ("by january", "deadline — earlier deadlines imply later ones"),
    ("by february", "deadline — earlier deadlines imply later ones"),
    ("by march", "deadline — earlier deadlines imply later ones"),
    ("by april", "deadline — earlier deadlines imply later ones"),
    ("by may", "deadline — earlier deadlines imply later ones"),
    ("by june", "deadline — earlier deadlines imply later ones"),
    ("by july", "deadline — earlier deadlines imply later ones"),
    ("by august", "deadline — earlier deadlines imply later ones"),
    ("by september", "deadline — earlier deadlines imply later ones"),
    ("by october", "deadline — earlier deadlines imply later ones"),
    ("by november", "deadline — earlier deadlines imply later ones"),
    ("by december", "deadline — earlier deadlines imply later ones"),
    ("by 20", "deadline — 'by 2027' style, earlier implies later"),

    # --- structurally open-ended ---
    ("highest temperature", "may resolve to none of the listed bands"),
    ("more markets", "an open-ended bucket, not a real outcome"),
    ("more market", "an open-ended bucket, not a real outcome"),
    ("released by", "deadline phrasing — earlier implies later"),
]

# The bare substrings, which is what the hot loop wants.
PATTERN_STRINGS = [pattern for pattern, _reason in NON_EXHAUSTIVE_PATTERNS]

# Patterns considered and deliberately rejected. Kept in the code rather
# than in a commit message so the next person to wonder "shouldn't we
# filter '# of'?" finds the answer where they are already looking.
DELIBERATELY_ALLOWED = [
    ("# of", "bucketed counts partition the range; exactly one bucket wins"),
    ("posts ", "same — '80-99', '100-119', ... covers every possibility"),
    ("cases in", "same — bucketed counts"),
    ("how many", "same — bucketed counts"),
    ("above ", "too broad: matches 'above average', 'above the line'"),
    (" below ", "too broad: matches ordinary prose"),
]


def matches(title: str) -> tuple:
    """
    Returns (pattern, reason) for the first match, or (None, None).

    The reason is returned so a rejection can say *why* rather than only
    that a blacklist fired — a filter whose decisions cannot be explained
    is one nobody dares to change.
    """
    lowered = (title or "").lower()
    for pattern, reason in NON_EXHAUSTIVE_PATTERNS:
        if pattern in lowered:
            return pattern, reason
    return None, None


def matches_event(event: dict) -> tuple:
    return matches(event.get("title") or "")
