"""
NegRisk Arbitrage Verifier
===========================

For a SPECIFIC event, fetch live order books and calculate:
  - REAL executable depth at each price level (not just total book depth)
  - True arbitrage cost if you size up to $X
  - Per-leg slippage analysis
  - Which legs have zero/thin liquidity (the deal-breakers)

Usage:
    python verify_arb.py <event_slug>

Example:
    python verify_arb.py f1-drivers-champion-2026

Or just run it and it shows the top candidates and asks you to pick.

Install:
    pip install requests pandas
"""

import sys
import json
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_FEE = 0.02
REPORT_DIR = Path("polymarket_report")


def fetch_event_by_slug(slug: str) -> Optional[dict]:
    """Get event details by slug"""
    try:
        r = requests.get(f"{GAMMA_URL}/events",
                         params={"slug": slug}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data:
            return data[0] if isinstance(data, list) else data
    except Exception as e:
        print(f"Error fetching event: {e}")
    return None


def fetch_order_book(token_id: str) -> Optional[dict]:
    """Get live order book for a token"""
    try:
        r = requests.get(f"{CLOB_URL}/book",
                         params={"token_id": token_id}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  Error fetching book for {token_id[:20]}: {e}")
    return None


def buy_through_book(asks: list, target_usd: float) -> dict:
    """
    Simulate buying shares from the ask side of the order book until we
    have spent target_usd. Returns the average price paid and how much
    of the target was filled.

    asks comes from Polymarket sorted descending by price, with the best
    (lowest) ask LAST in the list.
    """
    # sort by price ascending (best first)
    asks_sorted = sorted(asks, key=lambda x: float(x["price"]))

    spent_usd = 0
    shares_bought = 0
    fills = []

    for level in asks_sorted:
        price = float(level["price"])
        size = float(level["size"])
        if price == 0 or size == 0:
            continue

        # how much could we buy at this level?
        level_cost = price * size
        remaining_need = target_usd - spent_usd

        if level_cost <= remaining_need:
            # buy entire level
            spent_usd += level_cost
            shares_bought += size
            fills.append({"price": price, "size": size, "cost": level_cost})
        else:
            # partial fill
            partial_shares = remaining_need / price
            spent_usd += remaining_need
            shares_bought += partial_shares
            fills.append({"price": price, "size": partial_shares,
                          "cost": remaining_need})
            break

    avg_price = (spent_usd / shares_bought) if shares_bought > 0 else None
    fill_pct = (spent_usd / target_usd * 100) if target_usd > 0 else 0

    return {
        "spent_usd": spent_usd,
        "shares_bought": shares_bought,
        "avg_price": avg_price,
        "fill_pct": fill_pct,
        "fills": fills,
        "n_levels_used": len(fills),
    }


def buy_one_share(asks: list) -> dict:
    """Get the cost of buying exactly 1 share at best ask"""
    asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
    for level in asks_sorted:
        price = float(level["price"])
        size = float(level["size"])
        if price == 0 or size == 0:
            continue
        return {
            "price": price,
            "size_available": size,
            "max_at_this_price": price * size,
        }
    return {"price": None, "size_available": 0, "max_at_this_price": 0}


def analyze_event(event_slug: str, target_sizes: list = None):
    """Deep analysis of arbitrage potential for a single event"""

    if target_sizes is None:
        target_sizes = [10, 100, 1000, 10000]

    print(f"\nFetching event: {event_slug}")
    event = fetch_event_by_slug(event_slug)

    if not event:
        print("Event not found")
        return

    print(f"\n{'=' * 78}")
    print(f"EVENT: {event.get('title')}")
    print(f"{'=' * 78}")
    print(f"URL: https://polymarket.com/event/{event_slug}")
    print(f"End date: {event.get('endDate')}")
    print(f"NegRisk: {event.get('negRisk')}")
    print(f"Description: {(event.get('description') or '')[:200]}...")
    print()

    markets = event.get("markets", [])
    if not markets:
        print("No markets in this event")
        return

    print(f"Found {len(markets)} markets. Fetching live order books...\n")

    # fetch order books for all markets
    market_data = []
    for i, m in enumerate(markets):
        if not m.get("active") or m.get("closed"):
            continue

        token_ids = m.get("clobTokenIds")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except:
                continue
        if not token_ids or len(token_ids) < 2:
            continue

        yes_id = token_ids[0]
        print(f"  [{i + 1}/{len(markets)}] {m.get('groupItemTitle') or m.get('question', '')[:40]}")

        yes_book = fetch_order_book(yes_id)
        time.sleep(0.1)

        if yes_book is None:
            print(f"      No order book")
            continue

        asks = yes_book.get("asks", [])
        bids = yes_book.get("bids", [])

        # cost of buying 1 share at best ask
        one_share = buy_one_share(asks)

        market_data.append({
            "question": m.get("question", ""),
            "outcome": m.get("groupItemTitle") or m.get("question", ""),
            "yes_token_id": yes_id,
            "yes_asks": asks,
            "yes_bids": bids,
            "best_ask": one_share["price"],
            "size_at_best_ask": one_share["size_available"],
            "usd_at_best_ask": one_share["max_at_this_price"],
        })

    if not market_data:
        print("\nNo valid market data")
        return

    # ================================================================
    # Snapshot: best asks across all outcomes
    # ================================================================
    print(f"\n{'=' * 78}")
    print("CURRENT BEST ASKS PER OUTCOME")
    print(f"{'=' * 78}")
    print(f"{'Outcome':<35} {'Best Ask':>10} {'Size':>10} {'USD':>10}")
    print("-" * 78)

    sum_best_ask = 0
    sum_at_best_ask_usd = 0
    legs_without_liquidity = 0

    for md in market_data:
        outcome = str(md["outcome"])[:33]
        ba = md["best_ask"]
        size = md["size_at_best_ask"]
        usd = md["usd_at_best_ask"]

        if ba is None:
            print(f"  {outcome:<33} {'NO ASK':>10} {'-':>10} {'-':>10}")
            legs_without_liquidity += 1
        else:
            print(f"  {outcome:<33} {ba:>10.4f} {size:>10.1f} ${usd:>9.2f}")
            sum_best_ask += ba
            sum_at_best_ask_usd += usd

    print("-" * 78)
    print(f"  {'TOTAL':<33} {sum_best_ask:>10.4f}")
    print()

    if legs_without_liquidity > 0:
        print(f"⚠️  {legs_without_liquidity} outcome(s) have NO sell-side liquidity!")
        print(f"   You cannot complete a 'buy all Yes' arbitrage without these legs.")
        print()

    if sum_best_ask >= 1.0:
        print(f"Sum of best asks = {sum_best_ask:.4f} >= 1.0 — no arb opportunity at best ask")
    else:
        gross_edge = (1.0 - sum_best_ask) * 100
        net_edge = (1.0 - sum_best_ask - POLYMARKET_FEE) * 100
        print(f"Sum of best asks = {sum_best_ask:.4f}")
        print(f"  Gross edge: {gross_edge:.2f}%  |  Net edge (after 2% fee): {net_edge:.2f}%")

    # ================================================================
    # Slippage analysis: what if you try to size up?
    # ================================================================
    print(f"\n{'=' * 78}")
    print("SLIPPAGE ANALYSIS: Cost to buy 1 share of each outcome at size $X")
    print(f"{'=' * 78}")
    print("This simulates buying every outcome to lock in arb, scaling up dollar amounts.\n")

    n_outcomes = len(market_data)

    print(f"{'Total Size':>12} {'Sum Cost':>12} {'Fee':>10} {'Net Profit':>12} "
          f"{'Edge %':>9} {'Filled':>9}")
    print("-" * 78)

    for target_total in target_sizes:
        # split equally across outcomes
        per_outcome = target_total / n_outcomes

        total_cost = 0
        total_shares_bought = []
        full_fill = True

        for md in market_data:
            sim = buy_through_book(md["yes_asks"], per_outcome)
            total_cost += sim["spent_usd"]
            total_shares_bought.append(sim["shares_bought"])
            if sim["fill_pct"] < 99:
                full_fill = False

        # arbitrage payout: exactly 1 outcome resolves YES = $1
        # so we get back: min(shares we hold across outcomes) * $1
        # because exactly one of those positions is worth $1
        guaranteed_payout = min(total_shares_bought) if total_shares_bought else 0

        fee = guaranteed_payout * POLYMARKET_FEE
        net_profit = guaranteed_payout - total_cost - fee

        edge_pct = (net_profit / total_cost * 100) if total_cost > 0 else 0
        fill_marker = "OK" if full_fill else "PARTIAL"

        print(f"  ${target_total:>9,.0f} ${total_cost:>10,.2f} ${fee:>8,.2f} "
              f"${net_profit:>10,.2f} {edge_pct:>7.2f}% {fill_marker:>9}")

    print(f"\nNote: 'Guaranteed payout' assumes EXACTLY ONE outcome resolves YES = $1.")
    print(f"For sport/political events with possible 'no winner', this assumption fails.")


def show_candidates():
    """Show candidates from the strict report so user can pick one"""
    csv_path = REPORT_DIR / "strict_arb_candidates.csv"
    if not csv_path.exists():
        print(f"{csv_path} not found. Run strict_inspect_arb.py first.")
        return None

    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    print("\nTop candidates from strict_arb_candidates.csv:")
    print("-" * 78)
    for i, r in df.head(15).iterrows():
        slug = r.get("event_slug", "")
        title = str(r.get("event_title", ""))[:42]
        edge = r.get("buy_all_yes_net_edge", 0) * 100
        depth = r.get("min_depth_usd", 0)
        days = r.get("time_to_res_days", 0)
        print(f"  [{i + 1:>2}] {edge:>5.1f}% edge | {days:>4.0f}d | "
              f"${depth:>8.0f} depth | {title}")
        print(f"       slug: {slug}")

    return df


if __name__ == "__main__":
    if len(sys.argv) > 1:
        slug = sys.argv[1]
        analyze_event(slug)
    else:
        print("No event slug provided. Showing top candidates...")
        df = show_candidates()
        if df is not None and not df.empty:
            print("\nUsage: python verify_arb.py <event_slug>")
            print("Example: python verify_arb.py", df.iloc[0]["event_slug"])