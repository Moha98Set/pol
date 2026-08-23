"""
findMarket.py — Arbitrage Market Finder
========================================

Implements the full specification from findMarket.md.

Finds events where the sum of best asks across all legs, minus the 2% fee,
yields a positive edge — AND passes all 8 safety conditions so you don't get
false positives.

The 8 conditions (see findMarket.md):
  1. Multi-outcome (2+ markets) or binary (1 market)
  2. volume24hr >= MIN_VOLUME
  3. Title has no non-exhaustive pattern (vs., price thresholds, etc)
  4. Every leg has sell-side liquidity (no dry legs)
  5. SUM_MIN <= sum_asks <= SUM_MAX
  6. net_edge = 1 - sum_asks - fee > MIN_NET_EDGE
  7. MIN_DAYS <= time_to_resolution <= MAX_DAYS
  8. Slippage allows at least $MIN_FILLABLE to fill with positive profit

Install:
    pip install requests

Run:
    python findMarket.py
    python findMarket.py --min-edge 0.01 --min-volume 5000
    python findMarket.py --binary-only
    python findMarket.py --save results.csv
"""

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import arbmath
import config
import fees
import patterns

GAMMA_URL = config.GAMMA_URL
CLOB_URL = config.CLOB_URL

# -------------------- Tunable parameters (now in config.py) -----------------
# NOTE: this file used to assume a flat 2% fee. That was wrong in both
# directions — it over-charged cheap legs and under-charged legs near 0.50 —
# so it disagreed with scanner.py about which events were profitable.
# The real per-leg model now lives in fees.py and is shared by both, and
# every threshold below comes from config.py so the two tools cannot be
# run with different settings by accident.
MIN_VOLUME_24H = config.MIN_VOLUME_24H
MIN_NET_EDGE = config.MIN_NET_EDGE
SUM_MIN = config.SUM_ASKS_MIN
SUM_MAX = config.SUM_ASKS_MAX
MIN_DAYS = config.MIN_DAYS_TO_RES
MAX_DAYS = config.MAX_DAYS_TO_RES
TEST_CAPITALS = config.TEST_CAPITALS
MIN_FILLABLE = config.MIN_FILLABLE
EVENTS_PAGE_SIZE = config.EVENTS_PAGE_SIZE
MAX_EVENTS = config.MAX_EVENTS

# Patterns that mean the event is NOT mutually-exclusive + exhaustive
# (§4 cond 6). This file used to keep its own copy, which had drifted 16
# patterns from scanner's — so the two tools disagreed about which events
# were even eligible. Now both read patterns.py.
NON_EXHAUSTIVE_PATTERNS = patterns.PATTERN_STRINGS


# =====================================================================
# API fetching
# =====================================================================

def fetch_active_events(max_events: int) -> list:
    """Page through active events, sorted by volume (biggest first)"""
    print("Fetching active events from Gamma API...")
    events = []
    offset = 0
    while len(events) < max_events:
        try:
            r = requests.get(f"{GAMMA_URL}/events", params={
                "closed": "false", "active": "true",
                "order": "volume24hr", "ascending": "false",
                "limit": EVENTS_PAGE_SIZE, "offset": offset,
            }, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"  Error at offset {offset}: {e}")
            break
        if not batch:
            break
        events.extend(batch)
        offset += EVENTS_PAGE_SIZE
        if len(batch) < EVENTS_PAGE_SIZE:
            break
        time.sleep(0.1)
    print(f"  Got {len(events)} events\n")
    return events[:max_events]


def fetch_order_book(token_id: str) -> Optional[dict]:
    """Fetch live order book from CLOB (not Gamma!)"""
    try:
        r = requests.get(f"{CLOB_URL}/book",
                        params={"token_id": token_id}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def valid_asks(book: Optional[dict]) -> list:
    """Normalized, sorted (price, size) levels — see arbmath.normalize_asks."""
    if not book:
        return []
    return arbmath.normalize_asks(book.get("asks"))


# =====================================================================
# Core math (findMarket.md §5) — now shared with the scanner via arbmath
# =====================================================================

cost_to_buy_k_shares = arbmath.cost_to_buy_k_shares
best_ask_price = arbmath.best_ask


def has_non_exhaustive_pattern(title: str) -> Optional[str]:
    """Return the matching pattern if title looks non-exhaustive, else None"""
    if not title:
        return None
    t = str(title).lower()
    for pattern in NON_EXHAUSTIVE_PATTERNS:
        if pattern in t:
            return pattern
    return None


def days_to_resolution(end_date: str) -> Optional[float]:
    """Days from now until endDate"""
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (end - now).total_seconds() / 86400
    except Exception:
        return None


# =====================================================================
# Slippage curve (findMarket.md §6)
# =====================================================================

def compute_slippage_curve(leg_asks: list, fee_rate: float) -> list:
    """
    Profit-maximizing size at each test capital, using the real per-leg fee.

    `leg_asks` may be a plain list of ask-level lists; arbmath wants
    (name, levels) pairs, so unnamed legs get positional names.
    """
    legs = [
        leg if isinstance(leg, tuple) else (f"leg{i}", leg)
        for i, leg in enumerate(leg_asks)
    ]
    return arbmath.compute_slippage_curve(legs, fee_rate, TEST_CAPITALS)


# =====================================================================
# Analyze one event (findMarket.md §6 full algorithm)
# =====================================================================

def analyze_event(event: dict, min_volume: float = MIN_VOLUME_24H,
                  min_edge: float = MIN_NET_EDGE) -> dict:
    """
    Run all 8 conditions. Return a dict with 'status' and details.
    status == 'opportunity' means it passed everything.
    """
    title = event.get("title", "")
    result = {"title": title, "slug": event.get("slug"), "status": "unknown"}

    # ----- Condition 1: multi-outcome -----
    markets = [m for m in event.get("markets", []) if not m.get("closed")]
    n = len(markets)
    if n < 2:
        result["status"] = "not_multi"
        return result
    result["n_legs"] = n

    # ----- Condition 2: volume -----
    volume = float(event.get("volume24hr") or 0)
    result["volume_24h"] = volume
    if volume < min_volume:
        result["status"] = "low_volume"
        return result

    # ----- Condition 3: non-exhaustive pattern -----
    pattern = has_non_exhaustive_pattern(title)
    if pattern:
        result["status"] = "non_exhaustive"
        result["pattern"] = pattern
        return result

    # ----- Condition 7: time to resolution (check early, cheap) -----
    ttr = days_to_resolution(event.get("endDate"))
    result["time_to_res_days"] = ttr
    if ttr is None or ttr < MIN_DAYS or ttr > MAX_DAYS:
        result["status"] = "bad_timing"
        return result

    # ----- Fee rate for this event's category (geopolitics 0%..crypto 7%) ---
    fee_rate, fee_category = fees.fee_rate_for_event(event)
    result["fee_rate"] = fee_rate
    result["fee_category"] = fee_category

    # ----- Fetch order books for all legs -----
    leg_asks = []
    legs_info = []
    all_liquid = True
    for m in markets:
        token_ids = m.get("clobTokenIds")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except:
                continue
        if not token_ids or len(token_ids) < 2:
            continue
        outcome = m.get("groupItemTitle") or m.get("question", "")[:40]
        book = fetch_order_book(token_ids[0])
        time.sleep(0.03)
        asks = valid_asks(book)
        ba = best_ask_price(asks)
        has_liq = len(asks) > 0
        if not has_liq:
            all_liquid = False
        legs_info.append({
            "outcome": outcome,
            "token_id": token_ids[0],
            "best_ask": ba,
            "has_liquidity": has_liq,
            "depth_usd": arbmath.depth_usd(asks),
        })
        if has_liq:
            leg_asks.append((outcome, asks))

    result["legs"] = legs_info

    # ----- Condition 4: all legs liquid -----
    if not all_liquid:
        dry = [l["outcome"] for l in legs_info if not l["has_liquidity"]]
        result["status"] = "dry_legs"
        result["dry_legs"] = dry[:5]
        return result

    # ----- Compute sum of best asks -----
    sum_asks = sum(l["best_ask"] for l in legs_info if l["best_ask"] is not None)
    result["sum_asks"] = sum_asks

    # ----- Condition 5: sum in range -----
    if not (SUM_MIN <= sum_asks <= SUM_MAX):
        result["status"] = "sum_out_of_range"
        return result

    # ----- Condition 6: net edge (real per-leg fee, not a flat 2%) -----
    leg_prices = [l["best_ask"] for l in legs_info if l["best_ask"] is not None]
    fee_ps = fees.fee_per_share(leg_prices, fee_rate)
    net_edge = 1 - sum_asks - fee_ps
    result["gross_edge"] = 1 - sum_asks
    result["fee_per_share"] = fee_ps
    result["net_edge"] = net_edge
    if net_edge <= min_edge:
        result["status"] = "edge_too_small"
        return result

    # ----- Condition 8: slippage -----
    curve = compute_slippage_curve(leg_asks, fee_rate)
    if not curve:
        result["status"] = "depth_too_thin"
        return result

    # check we can fill at least MIN_FILLABLE
    fillable = [c for c in curve if c["real_cost"] >= MIN_FILLABLE]
    if not fillable:
        # can only fill tiny amounts
        result["status"] = "too_small_to_matter"
        result["curve"] = curve
        return result

    # PASSED EVERYTHING!
    best = max(curve, key=lambda x: x["profit"])
    result["status"] = "opportunity"
    result["curve"] = curve
    result["best_capital"] = best["capital"]
    result["best_profit"] = best["profit"]
    result["best_roi"] = best["roi"]
    result["best_shares"] = best["shares"]
    return result


# =====================================================================
# Print detailed allocation for an opportunity (findMarket.md §7)
# =====================================================================

def print_opportunity(opp: dict):
    """Print full leg-by-leg allocation"""
    print(f"\n{'='*72}")
    print(f"OPPORTUNITY: {opp['title'][:60]}")
    print(f"{'='*72}")
    print(f"  Slug: {opp['slug']}")
    print(f"  Legs: {opp['n_legs']}")
    print(f"  Sum of asks: {opp['sum_asks']:.4f}")
    print(f"  Gross edge: {opp['gross_edge']*100:.2f}%")
    print(f"  Fee: {opp['fee_rate']*100:.0f}% ({opp['fee_category']}) "
          f"= {opp['fee_per_share']*100:.2f}% per share")
    print(f"  Net edge: {opp['net_edge']*100:.2f}%")
    print(f"  Volume 24h: ${opp['volume_24h']:,.0f}")
    print(f"  Days to resolution: {opp['time_to_res_days']:.0f}")

    best_cap = opp["best_capital"]
    best_shares = opp["best_shares"]
    print(f"\n  Best deployment: ${best_cap:,.0f}")
    print(f"  Expected profit: ${opp['best_profit']:.2f} "
          f"(ROI {opp['best_roi']:.2f}%)")
    print(f"  Shares per leg: {best_shares:,.1f}")

    print(f"\n  Allocation (buy {best_shares:.1f} shares of each):")
    print(f"  {'Outcome':<35} {'Price':>8} {'Cost':>10} {'% of capital':>12}")
    print(f"  {'-'*67}")
    sum_asks = opp["sum_asks"]
    for leg in sorted(opp["legs"], key=lambda x: -(x["best_ask"] or 0)):
        price = leg["best_ask"]
        cost = best_shares * price
        pct = (price / sum_asks) * 100
        outcome = str(leg["outcome"])[:33]
        print(f"  {outcome:<35} {price:>8.4f} ${cost:>8.2f} {pct:>11.1f}%")

    # slippage curve
    print(f"\n  Slippage curve:")
    print(f"  {'Capital':>10} {'Shares':>10} {'Cost':>10} "
          f"{'Profit':>10} {'ROI':>7}")
    for c in opp["curve"]:
        print(f"  ${c['capital']:>8,.0f} {c['shares']:>10,.1f} "
              f"${c['real_cost']:>8,.2f} ${c['profit']:>8,.2f} "
              f"{c['roi']:>5.2f}%")


# =====================================================================
# Main
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="Find arbitrage markets")
    parser.add_argument("--min-edge", type=float, default=MIN_NET_EDGE)
    parser.add_argument("--min-volume", type=float, default=MIN_VOLUME_24H)
    parser.add_argument("--max-events", type=int, default=MAX_EVENTS)
    parser.add_argument("--save", type=str, help="Save opportunities to CSV")
    args = parser.parse_args()

    min_edge = args.min_edge
    min_volume = args.min_volume

    print("=" * 72)
    print("findMarket — Arbitrage Finder")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Params: min_edge={min_edge*100:.1f}%, "
          f"min_volume=${min_volume:,.0f}")
    print("=" * 72 + "\n")

    events = fetch_active_events(args.max_events)

    # counters for each rejection reason
    reasons = {}
    opportunities = []

    for i, event in enumerate(events, 1):
        if i % 20 == 0:
            print(f"  Analyzed {i}/{len(events)}... "
                  f"({len(opportunities)} opportunities)")

        try:
            result = analyze_event(event, min_volume=min_volume,
                                   min_edge=min_edge)
        except Exception as e:
            reasons["error"] = reasons.get("error", 0) + 1
            continue

        status = result["status"]
        reasons[status] = reasons.get(status, 0) + 1

        if status == "opportunity":
            opportunities.append(result)
            print(f"  ✓ FOUND: {result['title'][:45]} "
                  f"edge={result['net_edge']*100:.2f}% "
                  f"profit=${result['best_profit']:.2f}@${result['best_capital']:.0f}")

    # ================================================================
    # Summary
    # ================================================================
    print("\n" + "=" * 72)
    print("RESULTS")
    print("=" * 72)
    print(f"\nEvents analyzed: {len(events)}")
    print(f"\nRejection breakdown:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason:<22}: {count}")

    print(f"\n{'='*72}")
    print(f"OPPORTUNITIES FOUND: {len(opportunities)}")
    print(f"{'='*72}")

    if opportunities:
        opportunities.sort(key=lambda x: -x["best_profit"])
        for opp in opportunities:
            print_opportunity(opp)

        # save to CSV if requested
        if args.save:
            with open(args.save, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["title", "slug", "n_legs", "sum_asks", "net_edge_pct",
                           "volume_24h", "days_to_res", "best_capital",
                           "best_profit", "best_roi_pct", "url"])
                for o in opportunities:
                    w.writerow([
                        o["title"], o["slug"], o["n_legs"],
                        f"{o['sum_asks']:.4f}", f"{o['net_edge']*100:.2f}",
                        f"{o['volume_24h']:.0f}", f"{o['time_to_res_days']:.0f}",
                        o["best_capital"], f"{o['best_profit']:.2f}",
                        f"{o['best_roi']:.2f}",
                        f"https://polymarket.com/event/{o['slug']}",
                    ])
            print(f"\n\nSaved {len(opportunities)} opportunities to {args.save}")
    else:
        print("""
No opportunities found. This is normal — the market is efficient.

If you expected some, check:
  1. Is --min-edge too high? Try --min-edge 0.001
  2. Is --min-volume too high? Try --min-volume 500
  3. Are all candidates being rejected as 'dry_legs'? That means
     multi-outcome events have "Other" legs with no sellers — a real
     structural barrier, not a bug.

To find opportunities you likely need LIVE WebSocket monitoring
(sub-second reaction) rather than periodic scanning.
""")


if __name__ == "__main__":
    main()