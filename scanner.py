"""
Polymarket arbitrage scanner — fetching, filtering, and slippage analysis.

Implements the full filter pipeline from the project guide:

  all active events (ordered by volume24hr DESC — most liquid first)
    -> volume filter        (>= MIN_VOLUME_24H)
    -> type split           (binary = 1 market, multi = 2+ markets)
    -> pattern filter       (drop non-mutually-exclusive events)
    -> negRisk check        (multi events must be negRisk)
    -> time filter          (1..365 days to resolution)
    -> live order books     (CLOB /book, asks only — never midpoint)
    -> sum_asks sanity      (0.5 < sum_asks < 1.0)
    -> fee check            (net_edge = gross - 2%)
    -> slippage curve       (walking the book, equal shares per leg)
    -> opportunity
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import arbmath
import config
import fees
import patterns
import validate

log = logging.getLogger("arb_monitor")

# Set by arb_monitor (or replay) to a recorder.Recorder instance. When set,
# every scanned event's raw input is frozen to disk so the exact decision
# can be re-run later. None means recording is off, which is the default.
RECORDER = None

# Re-exported so the rejection codes read as plain names at the call sites
# below, where they sit next to the arithmetic that produces them.
BELOW_MIN_EDGE = validate.BELOW_MIN_EDGE
FAR_BELOW_EDGE = validate.FAR_BELOW_EDGE
NO_FILLABLE_SIZE = validate.NO_FILLABLE_SIZE

# persistent connection — avoids a new TLS handshake per request
SESSION = requests.Session()

# ====================================================================
# Config
# ====================================================================

# The fee model lives in fees.py, the edge math in arbmath.py, and every
# tunable number in config.py — so no two components can disagree the way
# the flat-2% copy in findmarket did.

GAMMA_URL = config.GAMMA_URL
CLOB_URL = config.CLOB_URL

MIN_VOLUME_24H = config.MIN_VOLUME_24H
MIN_NET_EDGE = config.MIN_NET_EDGE
NEAR_MISS_MIN_NET = config.NEAR_MISS_MIN_NET
SUM_ASKS_MIN = config.SUM_ASKS_MIN
SUM_ASKS_MAX = config.SUM_ASKS_MAX
MIN_DAYS_TO_RES = config.MIN_DAYS_TO_RES
MAX_DAYS_TO_RES = config.MAX_DAYS_TO_RES
TEST_CAPITALS = config.TEST_CAPITALS

EVENTS_PAGE_SIZE = config.EVENTS_PAGE_SIZE
REQUEST_TIMEOUT = config.REQUEST_TIMEOUT
API_SLEEP = config.API_SLEEP

# Patterns that indicate an event's outcomes are NOT mutually exclusive.
# Buying all legs of these is a bet, not an arbitrage. Defined once in
# patterns.py and shared with findmarket, which used to keep its own copy
# that had drifted 16 patterns away from this one.
NON_EXHAUSTIVE_PATTERNS = patterns.PATTERN_STRINGS


# ====================================================================
# API helpers
# ====================================================================


def api_get(url: str, params: dict = None, retries: int = None) -> Optional[list]:
    retries = config.API_RETRIES if retries is None else retries
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code in (404, 422):
                # 404: no book for this token; 422: pagination limit.
                # both expected — no retry, no warning
                return None
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ReadTimeout:
            if attempt == retries - 1:
                log.warning("Timeout %s", url)
                return None
            time.sleep(1)
        except Exception as e:
            if attempt == retries - 1:
                log.warning("API failed %s: %s", url, e)
                return None
            time.sleep(1)


def fetch_order_books(token_ids: list) -> dict:
    """
    Batch-fetch order books via POST /books — one HTTP call for up to
    ~100 tokens instead of one call per token. Tokens with no book are
    silently absent from the response (no 404s).

    Returns {token_id: book_dict}.
    """
    books = {}
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        payload = [{"token_id": t} for t in chunk]
        for attempt in range(2):
            try:
                r = SESSION.post(f"{CLOB_URL}/books", json=payload,
                                 timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                for b in r.json():
                    aid = b.get("asset_id")
                    if aid:
                        books[aid] = b
                break
            except Exception as e:
                if attempt == 1:
                    log.warning("Batch /books failed: %s", e)
                else:
                    time.sleep(1)
        time.sleep(API_SLEEP)
    return books


def fetch_all_events(max_events: int = None) -> list:
    """
    Page through active events ordered by 24h volume (highest first).

    Ordering by volume is critical: the API stops paginating after a few
    thousand rows, so ordering by id would give us the NEWEST events
    (zero-volume, just created) and miss every market that matters.

    `max_events` stops early. Because the ordering is by volume, a capped
    fetch is not an arbitrary sample — it is the most liquid slice, which
    is exactly the part worth looking at when the goal is a fast run
    rather than a complete one.
    """
    max_events = config.MAX_EVENTS_SCAN if max_events is None else max_events
    all_events = []
    offset = 0

    while True:
        data = api_get(f"{GAMMA_URL}/events", {
            "closed": "false",
            "active": "true",
            "order": "volume24hr",
            "ascending": "false",
            "limit": EVENTS_PAGE_SIZE,
            "offset": offset,
        })

        if not data:
            break

        all_events.extend(data)
        offset += EVENTS_PAGE_SIZE

        if max_events and len(all_events) >= max_events:
            return all_events[:max_events]

        if len(data) < EVENTS_PAGE_SIZE:
            break

        time.sleep(0.1)

    return all_events


# ====================================================================
# Field helpers
# ====================================================================


def get_volume(market: dict) -> float:
    for key in ("volume24hr", "volumeNum", "volume"):
        val = market.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return 0.0


def parse_token_ids(market: dict) -> list:
    token_ids = market.get("clobTokenIds")
    if not token_ids:
        return []
    if isinstance(token_ids, str):
        try:
            return json.loads(token_ids)
        except json.JSONDecodeError:
            return []
    return token_ids if isinstance(token_ids, list) else []


def get_valid_asks(book: Optional[dict]) -> list:
    """Normalized, sorted (price, size) levels for a raw CLOB book."""
    if not book:
        return []
    return arbmath.normalize_asks(book.get("asks"))


def days_to_resolution(event: dict) -> Optional[float]:
    end_date = event.get("endDate")
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return (end - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return None


# fee helpers re-exported so existing callers keep working
fee_rate_for_event = fees.fee_rate_for_event
fee_for_legs = fees.fee_for_legs


def matches_non_exhaustive_pattern(event: dict) -> bool:
    """True if the event title looks non-mutually-exclusive."""
    pattern, _reason = patterns.matches_event(event)
    return pattern is not None


# ====================================================================
# Event pre-filter (no API calls — cheap, runs on all events)
# ====================================================================


def prefilter_event_verbose(event: dict) -> tuple:
    """
    Apply all cheap filters. Returns (group, verdict).

    `group` is None when the event is rejected; `verdict` always says why,
    with a stable code from validate.py. The reason is the valuable half:
    an event silently vanishing is the single hardest thing to debug in
    this pipeline, and a counted, named rejection is the cure.
    """
    markets = [
        m for m in event.get("markets", [])
        if not m.get("closed") and m.get("enableOrderBook")
    ]

    verdict = validate.validate_event(event, markets)
    if not verdict:
        return None, verdict

    is_binary = len(markets) == 1

    exclusivity = validate.validate_exclusivity(event, markets)
    if not exclusivity:
        return None, exclusivity

    tokens = validate.validate_tokens(markets, is_binary)
    if not tokens:
        return None, tokens

    fee_rate, fee_category = fee_rate_for_event(event)

    group = {
        "event": event,
        "markets": markets,
        "is_binary": is_binary,
        "volume": sum(get_volume(m) for m in markets),
        "fee_rate": fee_rate,
        "fee_category": fee_category,
        "suspicions": verdict.suspicions + exclusivity.suspicions,
    }
    return group, verdict


def prefilter_event(event: dict) -> Optional[dict]:
    """Convenience wrapper: the group, or None. Reason discarded."""
    group, _verdict = prefilter_event_verbose(event)
    return group


# ====================================================================
# Slippage engine — walking the book
# ====================================================================


cost_to_buy_k_shares = arbmath.cost_to_buy_k_shares


def compute_slippage_curve(legs_asks: list, sum_best_asks: float,
                           fee_rate: float) -> list:
    """
    Slippage curve for these legs. Delegates to arbmath, which searches for
    the profit-maximizing K rather than the largest K that fits the budget.

    `sum_best_asks` is accepted for call-site compatibility and no longer
    used — arbmath derives sizing from the books themselves.
    """
    return arbmath.compute_slippage_curve(legs_asks, fee_rate, TEST_CAPITALS)


# ====================================================================
# Scan results
# ====================================================================
# Scan functions return one of:
#   None                          — nothing usable (dry leg, fetch error...)
#   {"kind": "near_miss", ...}    — edge exists conceptually but below threshold
#   {"kind": "opportunity", ...}  — executable, slippage-verified


def _near_miss(market_type: str, event: dict, num_outcomes: int,
               sum_asks: float, volume: float, net_edge: float,
               fee_rate: float, payout: float = 1.0) -> dict:
    """
    `payout` is what one basket pays: 1 for a YES basket, N-1 for a NO
    basket, where exactly one outcome wins and the other N-1 NOs pay out.
    It was previously hardcoded to 1, which made every multi_no row record
    a gross edge of roughly -(N-2) — a five-outcome basket costing 4.01 to
    return 4 was stored as -301% rather than -1%.
    """
    return {
        "kind": "near_miss",
        "market_type": market_type,
        "event_title": event.get("title"),
        "event_slug": event.get("slug"),
        "num_outcomes": num_outcomes,
        "sum_best_asks": sum_asks,
        "gross_edge": payout - sum_asks,
        "net_edge": net_edge,
        "fee_rate": fee_rate,
        "volume_24h": volume,
        "url": f"https://polymarket.com/event/{event.get('slug')}",
    }


def scan_binary_event(group: dict, books: dict = None) -> tuple:
    """
    Scan a single-market (binary Yes/No) event.

    Returns (result, verdict). `result` is an opportunity, a near miss, or
    None; `verdict` always carries a stable code saying why. The reason is
    what makes a scan explainable: a quarter of all events used to be
    rejected in here and land in the funnel as one anonymous bucket.

    `books` may be supplied to analyse a recorded snapshot instead of
    hitting the API — that is what makes this function replayable and
    testable offline.
    """
    event = group["event"]
    m = group["markets"][0]

    token_ids = parse_token_ids(m)
    if len(token_ids) < 2:
        return None, validate.reject(validate.MARKET_COUNT_MISMATCH,
                                     f"{len(token_ids)} token(s)")

    if books is None:
        books = fetch_order_books(token_ids[:2])
        if RECORDER is not None:
            RECORDER.record_event(event, books, fee_rate=group["fee_rate"],
                                  is_binary=True)

    yes_asks = get_valid_asks(books.get(token_ids[0]))
    no_asks = get_valid_asks(books.get(token_ids[1]))

    suspicions = list(group.get("suspicions", []))
    for name, asks, token in (("Yes", yes_asks, token_ids[0]),
                              ("No", no_asks, token_ids[1])):
        book_verdict = validate.validate_book(
            name, asks,
            arbmath.normalize_asks((books.get(token) or {}).get("bids")))
        if not book_verdict:
            return None, book_verdict
        suspicions.extend(book_verdict.suspicions)

    yes_best = arbmath.best_ask(yes_asks)
    no_best = arbmath.best_ask(no_asks)
    sum_asks = yes_best + no_best
    gross_edge = 1 - sum_asks
    fee_rate = group["fee_rate"]
    # estimated fee at best-ask prices, real per-leg formula
    fee_est = fee_for_legs([yes_best, no_best], 1.0, fee_rate)
    net_edge = gross_edge - fee_est

    if net_edge < MIN_NET_EDGE:
        if net_edge >= NEAR_MISS_MIN_NET:
            return (_near_miss("binary", event, 2, sum_asks,
                               group["volume"], net_edge, fee_rate),
                    validate.reject(BELOW_MIN_EDGE, f"{net_edge*100:.3f}%"))
        return None, validate.reject(FAR_BELOW_EDGE, f"{net_edge*100:.1f}%")

    legs = [("Yes", yes_asks), ("No", no_asks)]
    curve = compute_slippage_curve(legs, sum_asks, fee_rate)
    if not curve:
        # edge exists on paper but book is too thin to execute
        return (_near_miss("binary", event, 2, sum_asks,
                           group["volume"], net_edge, fee_rate),
                validate.reject(NO_FILLABLE_SIZE))

    best = max(curve, key=lambda x: x["profit"])
    top = arbmath.top_of_book(legs, 1.0, fee_rate)

    return {
        "kind": "opportunity",
        "market_type": "binary",
        "top_shares": top["shares"],
        "top_capital": top["capital"],
        "top_profit": top["profit"],
        "event_title": event.get("title"),
        "event_slug": event.get("slug"),
        "question": m.get("question"),
        "slug": m.get("slug"),
        "category": m.get("category"),
        "volume_24h": group["volume"],
        "num_outcomes": 2,
        "yes_ask": yes_best,
        "no_ask": no_best,
        "sum_best_asks": sum_asks,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "fee_rate": fee_rate,
        "best_capital": best["capital"],
        "best_shares": best["shares"],
        "best_real_cost": best["real_cost"],
        "best_profit": best["profit"],
        "best_roi_pct": best["roi"],
        "slippage_curve": curve,
        # token_id is carried through so the executor can act on a stored
        # opportunity without re-resolving the event
        "legs_detail": [
            {"outcome": "Yes", "best_ask": yes_best,
             "best_ask_size": arbmath.best_ask_size(yes_asks),
             "token_id": token_ids[0]},
            {"outcome": "No", "best_ask": no_best,
             "best_ask_size": arbmath.best_ask_size(no_asks),
             "token_id": token_ids[1]},
        ],
        # anything the validator flagged but did not reject — a signal that
        # is real often enough to record, wrong often enough not to trust
        "suspicions": sorted(set(suspicions)),
        "url": f"https://polymarket.com/event/{event.get('slug')}",
    }, validate.accept(suspicions)


def scan_multi_outcome_event(group: dict, books: dict = None) -> tuple:
    """
    Scan a negRisk multi-outcome event (e.g. "Who wins the election?").
    Buy K equal shares of every outcome; if sum of best asks < 1 there is edge.
    Requires EVERY leg to have sellers — one dry leg kills the whole arb.

    Returns (result, verdict), as scan_binary_event does.
    """
    event = group["event"]
    markets = group["markets"]

    # collect the YES token of every leg, then batch-fetch all books at once
    leg_tokens = []
    for m in markets:
        token_ids = parse_token_ids(m)
        if not token_ids:
            return None, validate.reject(validate.NO_TOKENS, m.get("slug"))
        leg_tokens.append((m, token_ids[0]))

    if books is None:
        books = fetch_order_books([t for _, t in leg_tokens])
        if RECORDER is not None:
            RECORDER.record_event(event, books, fee_rate=group["fee_rate"],
                                  is_binary=False)

    legs_asks = []
    legs_detail = []
    suspicions = list(group.get("suspicions", []))

    for m, token_id in leg_tokens:
        book = books.get(token_id)
        asks = get_valid_asks(book)
        outcome_name = m.get("groupItemTitle") or m.get("question", "")[:40]

        # a single bad leg invalidates the whole basket — one leg that
        # cannot be bought at the assumed price turns the arb into a bet
        book_verdict = validate.validate_book(
            outcome_name, asks, arbmath.normalize_asks((book or {}).get("bids")))
        if not book_verdict:
            return None, book_verdict
        suspicions.extend(book_verdict.suspicions)

        legs_asks.append((outcome_name, asks))
        legs_detail.append({"outcome": outcome_name,
                            "best_ask": arbmath.best_ask(asks),
                            "best_ask_size": arbmath.best_ask_size(asks),
                            "token_id": token_id})

    sum_asks = sum(leg["best_ask"] for leg in legs_detail)

    basket_verdict = validate.validate_basket(legs_asks, sum_asks)
    if not basket_verdict and basket_verdict.code != validate.NO_EDGE:
        # NO_EDGE still deserves a near-miss row; anything else is unusable
        return None, basket_verdict
    suspicions.extend(basket_verdict.suspicions)

    gross_edge = 1 - sum_asks
    fee_rate = group["fee_rate"]
    fee_est = fee_for_legs([l["best_ask"] for l in legs_detail], 1.0, fee_rate)
    net_edge = gross_edge - fee_est
    n = len(legs_asks)

    if net_edge < MIN_NET_EDGE:
        if net_edge >= NEAR_MISS_MIN_NET:
            return (_near_miss("multi", event, n, sum_asks,
                               group["volume"], net_edge, fee_rate),
                    validate.reject(BELOW_MIN_EDGE, f"{net_edge*100:.3f}%"))
        return None, validate.reject(FAR_BELOW_EDGE, f"{net_edge*100:.1f}%")

    curve = compute_slippage_curve(legs_asks, sum_asks, fee_rate)
    if not curve:
        return (_near_miss("multi", event, n, sum_asks,
                           group["volume"], net_edge, fee_rate),
                validate.reject(NO_FILLABLE_SIZE))

    best = max(curve, key=lambda x: x["profit"])
    top = arbmath.top_of_book(legs_asks, 1.0, fee_rate)

    return {
        "kind": "opportunity",
        "market_type": "multi",
        "top_shares": top["shares"],
        "top_capital": top["capital"],
        "top_profit": top["profit"],
        "event_title": event.get("title"),
        "event_slug": event.get("slug"),
        "question": None,
        "slug": None,
        "category": markets[0].get("category"),
        "volume_24h": group["volume"],
        "num_outcomes": n,
        "yes_ask": None,
        "no_ask": None,
        "sum_best_asks": sum_asks,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "fee_rate": fee_rate,
        "best_capital": best["capital"],
        "best_shares": best["shares"],
        "best_real_cost": best["real_cost"],
        "best_profit": best["profit"],
        "best_roi_pct": best["roi"],
        "slippage_curve": curve,
        "legs_detail": legs_detail,
        "suspicions": sorted(set(suspicions)),
        "url": f"https://polymarket.com/event/{event.get('slug')}",
    }, validate.accept(suspicions)


def scan_multi_no_side(group: dict, books: dict = None) -> tuple:
    """
    The mirror trade: buy NO on every leg instead of YES on every leg.

    In a negRisk event with N mutually exclusive, exhaustive outcomes,
    exactly one leg wins. So exactly one NO expires worthless and the other
    N-1 pay $1 each. A basket of one NO per leg is therefore worth N-1 at
    resolution, and it is arbitrage whenever

        sum of NO asks  <  N - 1

    Two facts make this worth having, and one makes it worth distrusting:

    * It is a genuinely different price condition. Since a NO ask at p is
      a YES bid at 1-p, the condition above is the same as
      "sum of YES bids > 1" — the basket can be SOLD for more than a
      dollar. The YES-side scan only ever asks whether it can be BOUGHT
      for less than one. Neither implies the other.

    * It costs no extra API calls. The NO book is the YES book's bid side,
      which is already in the response.

    * The capital is far worse. The same dollar of edge needs about N-1
      dollars of capital instead of one, so a 12-leg NO basket ties up $11
      to earn what a YES basket earns with $1. That is why the result
      carries net_edge_per_dollar, and why ROI — not edge — is the number
      to rank these by.
    """
    event = group["event"]
    markets = group["markets"]

    leg_tokens = []
    for m in markets:
        token_ids = parse_token_ids(m)
        if len(token_ids) < 2:
            # the NO basket has to name the NO token to buy, so a market
            # that only exposes its YES token cannot be traded this way
            return None, validate.reject(validate.NO_TOKENS, m.get("slug"))
        leg_tokens.append((m, token_ids[0], token_ids[1]))

    if books is None:
        books = fetch_order_books([t for _, t, _ in leg_tokens])

    legs_asks = []
    legs_detail = []
    suspicions = list(group.get("suspicions", []))

    for m, yes_token, no_token in leg_tokens:
        book = books.get(yes_token) or {}
        # the NO side, reconstructed from the YES bids already fetched
        no_asks = arbmath.no_asks_from_yes_bids(
            arbmath.normalize_asks(book.get("bids")))
        outcome_name = m.get("groupItemTitle") or m.get("question", "")[:40]

        book_verdict = validate.validate_book(f"NO {outcome_name}", no_asks)
        if not book_verdict:
            return None, book_verdict
        suspicions.extend(book_verdict.suspicions)

        legs_asks.append((outcome_name, no_asks))
        # token_id is what the executor will BUY, so it must be the NO
        # token — pricing off the YES bids but ordering the YES token
        # would buy the exact opposite of the intended position
        legs_detail.append({"outcome": f"NO {outcome_name}",
                            "best_ask": arbmath.best_ask(no_asks),
                            "token_id": no_token,
                            "yes_token_id": yes_token,
                            "side": "NO"})

    n = len(legs_asks)
    sum_no = sum(leg["best_ask"] for leg in legs_detail)

    basket_verdict = validate.validate_no_basket(legs_asks, sum_no)
    if not basket_verdict and basket_verdict.code != validate.NO_EDGE:
        return None, basket_verdict
    suspicions.extend(basket_verdict.suspicions)

    payout = n - 1
    fee_rate = group["fee_rate"]
    result = arbmath.evaluate_basket(legs_asks, fee_rate, TEST_CAPITALS,
                                     payout_per_basket=payout)

    net_edge = result["net_edge"]
    # ranked and thresholded per dollar of capital, not per basket: a NO
    # basket's edge looks big only because the basket itself is big
    edge_per_dollar = result["net_edge_per_dollar"]

    if edge_per_dollar is None or edge_per_dollar < MIN_NET_EDGE:
        if edge_per_dollar is not None and edge_per_dollar >= NEAR_MISS_MIN_NET:
            miss = _near_miss("multi_no", event, n, sum_no,
                              group["volume"], edge_per_dollar, fee_rate,
                              payout=payout)
            return miss, validate.reject(BELOW_MIN_EDGE,
                                         f"{edge_per_dollar*100:.3f}%/$")
        return None, validate.reject(FAR_BELOW_EDGE)

    if not result["curve"]:
        return (_near_miss("multi_no", event, n, sum_no, group["volume"],
                           edge_per_dollar, fee_rate, payout=payout),
                validate.reject(NO_FILLABLE_SIZE))

    best = max(result["curve"], key=lambda x: x["profit"])
    # payout is N-1 for a NO basket; passing 1 here would report every one
    # of them as a loss
    top = arbmath.top_of_book(legs_asks, payout, fee_rate)

    return {
        "kind": "opportunity",
        "market_type": "multi_no",
        "top_shares": top["shares"],
        "top_capital": top["capital"],
        "top_profit": top["profit"],
        "event_title": event.get("title"),
        "event_slug": event.get("slug"),
        "question": None,
        "slug": None,
        "category": markets[0].get("category"),
        "volume_24h": group["volume"],
        "num_outcomes": n,
        "yes_ask": None,
        "no_ask": None,
        "sum_best_asks": sum_no,
        "payout_per_basket": payout,
        "gross_edge": result["gross_edge"],
        # stored per dollar so it is directly comparable with a YES-side row
        "net_edge": edge_per_dollar,
        "net_edge_per_basket": net_edge,
        "fee_rate": fee_rate,
        "best_capital": best["capital"],
        "best_shares": best["shares"],
        "best_real_cost": best["real_cost"],
        "best_profit": best["profit"],
        "best_roi_pct": best["roi"],
        "slippage_curve": result["curve"],
        "legs_detail": legs_detail,
        "suspicions": sorted(set(suspicions)),
        "url": f"https://polymarket.com/event/{event.get('slug')}",
    }, validate.accept(suspicions)


def _rank(result: Optional[dict]) -> float:
    """Order two candidate trades on the same event. Opportunities first,
    then by edge per dollar — never by edge per basket, which would always
    prefer the NO side simply because its baskets are bigger."""
    if not result:
        return float("-inf")
    base = 1000.0 if result["kind"] == "opportunity" else 0.0
    return base + (result.get("net_edge") or 0.0)


def scan_event_verbose(group: dict, books: dict = None) -> tuple:
    """
    Scan an event, returning (result, verdict).

    Multi-outcome events are checked from both sides: buy the basket for
    less than it pays (YES), or sell it for more (NO). Both readings come
    off the same books, so the second side costs no extra API call.

    The two can never both be available:

        per leg      best bid <= best ask     (or the book is crossed)
        summing      sum(bid) <= sum(ask)
        YES arb      sum(ask) < 1
        NO  arb      sum(bid) > 1

    which together would require sum(bid) > 1 > sum(ask) >= sum(bid).

    So this is not a contest between two candidate trades — it is one
    trade or the other or neither, and the pair covers strictly more of
    the price space than the YES side did alone. The ranking below is kept
    anyway as a cheap guard: if both ever do come back non-None, the books
    were crossed and the higher *per-dollar* edge is the safer of two
    readings that should not have coexisted.
    """
    if group["is_binary"]:
        return scan_binary_event(group, books)

    if not config.SCAN_NO_SIDE:
        return scan_multi_outcome_event(group, books)

    yes_result, yes_verdict = scan_multi_outcome_event(group, books)
    no_result, no_verdict = scan_multi_no_side(group, books)

    if _rank(no_result) > _rank(yes_result):
        return no_result, no_verdict
    if yes_result is not None:
        return yes_result, yes_verdict
    # neither side produced anything: report the YES side's reason unless
    # it is the uninformative one, since that is the trade normally sought
    if yes_verdict.code in (validate.FAR_BELOW_EDGE, validate.NO_EDGE):
        return None, no_verdict if no_result is None else yes_verdict
    return None, yes_verdict


def scan_event(group: dict, books: dict = None) -> Optional[dict]:
    """Scan an event. Result only — the reason is discarded."""
    result, _verdict = scan_event_verbose(group, books)
    return result
