"""
Polymarket Slippage Analyzer
=============================

For each top arbitrage candidate event:
  1. Fetch LIVE order books for every leg
  2. Calculate REAL cost to buy K shares of each outcome
  3. Find the optimal K that maximizes profit
  4. Show recommended share allocation per leg

Math:
  For K shares of each outcome with order books B_1, B_2, ..., B_N:
    cost(K) = sum_i cost_to_buy_K_shares(B_i)
    payout = K * $1  (guaranteed if exhaustive)
    fee = K * 0.02
    profit(K) = K - cost(K) - fee

  Optimal K is the LARGEST K where profit(K) > 0
  (after that, slippage eats the edge)

Install:
    pip install requests pandas tqdm

Run:
    python slippage_analyzer.py
"""

import json
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from tqdm import tqdm

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_FEE = 0.02
REPORT_DIR = Path("polymarket_report")

# How many top candidates to analyze
TOP_N = 10

# Capital sizes to test for slippage curve
TEST_CAPITALS = [10, 50, 100, 500, 1000, 5000, 10000]


# =====================================================================
# Step 1: Fetch live order book
# =====================================================================

def fetch_order_book(token_id: str) -> Optional[dict]:
    """Fetch live order book for a token"""
    try:
        r = requests.get(f"{CLOB_URL}/book",
                         params={"token_id": token_id}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  Error: {e}")
    return None


def cost_to_buy_k_shares(asks: list, k: float) -> dict:
    """
    Walk through asks from lowest to highest price, buying until we have K shares.
    Returns total cost, avg price, fill percentage, and whether we ran out of liquidity.
    """
    # asks come from API. Sort by price ascending (best ask first)
    asks_sorted = sorted(asks, key=lambda x: float(x["price"]))

    spent_usd = 0.0
    shares_filled = 0.0
    levels_used = 0

    for level in asks_sorted:
        price = float(level["price"])
        size = float(level["size"])
        if price <= 0 or size <= 0:
            continue

        remaining = k - shares_filled
        if remaining <= 0:
            break

        if size <= remaining:
            # take entire level
            spent_usd += price * size
            shares_filled += size
            levels_used += 1
        else:
            # partial fill of this level
            spent_usd += price * remaining
            shares_filled += remaining
            levels_used += 1
            break

    avg_price = spent_usd / shares_filled if shares_filled > 0 else None
    fill_pct = (shares_filled / k * 100) if k > 0 else 0

    return {
        "spent_usd": spent_usd,
        "shares_filled": shares_filled,
        "shares_wanted": k,
        "avg_price": avg_price,
        "fill_pct": fill_pct,
        "fully_filled": fill_pct >= 99.99,
        "levels_used": levels_used,
    }


# =====================================================================
# Step 2: Find optimal K for an event
# =====================================================================

def analyze_event_slippage(event_title: str, event_slug: str,
                           books: list) -> dict:
    """
    For an event with N outcomes (each with order book),
    find the optimal K that maximizes profit.

    books is a list of (outcome_name, asks) tuples.
    """
    n = len(books)

    # Compute cost at each capital level
    results_by_capital = []

    for target_capital in TEST_CAPITALS:
        # Binary search for the largest K such that
        # total_cost(K) <= target_capital and all legs fully filled

        # Upper bound: assume best ask price, no slippage
        sum_best_asks = sum(
            float(sorted(asks, key=lambda x: float(x["price"]))[0]["price"])
            for _, asks in books
            if asks
        )
        if sum_best_asks <= 0:
            continue

        # Max K (no slippage) is target / sum_best_asks
        max_k_no_slip = target_capital / sum_best_asks

        # Now find actual K with slippage by trying decreasing values
        # We accept the largest K where total cost <= target
        k_test = max_k_no_slip

        best_k = 0
        best_result = None

        # Start with no-slip estimate, then walk down until cost fits
        for shrink in [1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5, 0.3, 0.1]:
            k = max_k_no_slip * shrink

            total_cost = 0
            all_filled = True
            leg_details = []

            for outcome, asks in books:
                sim = cost_to_buy_k_shares(asks, k)
                total_cost += sim["spent_usd"]
                if not sim["fully_filled"]:
                    all_filled = False
                leg_details.append({
                    "outcome": outcome,
                    "shares": sim["shares_filled"],
                    "cost": sim["spent_usd"],
                    "avg_price": sim["avg_price"],
                    "fill_pct": sim["fill_pct"],
                })

            if not all_filled:
                continue

            if total_cost > target_capital:
                continue

            # all good — this K works within budget
            payout = k  # exactly one outcome wins = $1 per share
            fee = payout * POLYMARKET_FEE
            profit = payout - total_cost - fee

            if k > best_k and profit > 0:
                best_k = k
                best_result = {
                    "capital_used": total_cost,
                    "target_capital": target_capital,
                    "shares_per_outcome": k,
                    "payout": payout,
                    "fee": fee,
                    "profit": profit,
                    "roi_pct": (profit / total_cost * 100) if total_cost > 0 else 0,
                    "all_legs_filled": all_filled,
                    "leg_details": leg_details,
                }

        if best_result:
            results_by_capital.append(best_result)

    return {
        "event_title": event_title,
        "event_slug": event_slug,
        "n_outcomes": n,
        "results_by_capital": results_by_capital,
    }


# =====================================================================
# Step 3: Main flow
# =====================================================================

def main():
    print("=" * 80)
    print("Polymarket Slippage Analyzer")
    print("=" * 80)

    # Load strict candidates from previous run
    csv_path = REPORT_DIR / "strict_arb_candidates.csv"
    if not csv_path.exists():
        print(f"\nERROR: {csv_path} not found.")
        print("Run polymarket_report.py and strict_inspect_arb.py first.")
        return

    df = pd.read_csv(csv_path, encoding="utf-8-sig")

    # Filter to events that are most likely to work:
    # - Mutually exclusive (sum < 1)
    # - Have sufficient depth
    # - Negative-risk preferred
    # - Sort by APR
    df = df[df["buy_all_yes_net_edge"] > 0].copy()
    df = df.sort_values("simple_apr_pct", ascending=False)
    top = df.head(TOP_N)

    print(f"\nAnalyzing top {len(top)} candidates with LIVE order books...")
    print(f"Each event has multiple outcomes (legs), fetching all books...\n")

    all_analyses = []

    for idx, cand in top.iterrows():
        event_slug = cand["event_slug"]
        title = str(cand["event_title"])[:60]
        n_markets = int(cand["n_markets"])

        print(f"\n[{idx + 1}/{len(top)}] {title}")
        print(f"  Slug: {event_slug}")
        print(f"  N legs: {n_markets}, snapshot edge: "
              f"{cand['buy_all_yes_net_edge'] * 100:.1f}%")
        print(f"  Fetching {n_markets} live order books...")

        # Fetch event details to get current markets
        try:
            r = requests.get(f"{GAMMA_URL}/events",
                             params={"slug": event_slug}, timeout=15)
            r.raise_for_status()
            event_data = r.json()
            event = event_data[0] if isinstance(event_data, list) else event_data
        except Exception as e:
            print(f"  Failed to fetch event: {e}")
            continue

        markets = [m for m in event.get("markets", []) if not m.get("closed")]

        # Fetch books for all legs
        books = []
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
            time.sleep(0.05)

            if book and book.get("asks"):
                books.append((outcome, book["asks"]))

        print(f"  Got order books for {len(books)} / {len(markets)} legs")

        if len(books) < len(markets):
            missing = len(markets) - len(books)
            print(f"  WARNING: {missing} legs have no asks. "
                  f"Arb requires ALL legs filled — will fail in execution.")
            continue

        # Run slippage analysis
        analysis = analyze_event_slippage(title, event_slug, books)
        all_analyses.append(analysis)

        # Print result for this event
        print(f"\n  Slippage curve for this event:")
        print(f"  {'Capital':>10} {'Shares/leg':>11} {'Real Cost':>12} "
              f"{'Profit':>10} {'ROI':>8}")
        print(f"  {'-' * 60}")

        for r in analysis["results_by_capital"]:
            print(f"  ${r['target_capital']:>8,.0f} "
                  f"{r['shares_per_outcome']:>11,.1f} "
                  f"${r['capital_used']:>10,.2f} "
                  f"${r['profit']:>8,.2f} "
                  f"{r['roi_pct']:>6.2f}%")

    # ================================================================
    # Save and summary
    # ================================================================
    print("\n" + "=" * 80)
    print("SUMMARY: Best capital allocation per event")
    print("=" * 80)

    summary_rows = []
    for a in all_analyses:
        # find the capital level with highest absolute profit
        if not a["results_by_capital"]:
            continue
        best = max(a["results_by_capital"], key=lambda x: x["profit"])
        summary_rows.append({
            "event": a["event_title"][:50],
            "slug": a["event_slug"],
            "n_legs": a["n_outcomes"],
            "best_capital": best["target_capital"],
            "real_cost": best["capital_used"],
            "shares_per_leg": best["shares_per_outcome"],
            "profit": best["profit"],
            "roi_pct": best["roi_pct"],
        })

    if summary_rows:
        sdf = pd.DataFrame(summary_rows).sort_values("profit", ascending=False)
        print()
        print(sdf.to_string(index=False))

        # save full details
        out_path = REPORT_DIR / "slippage_analysis.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(all_analyses, f, indent=2, default=str)
        print(f"\nSaved detailed leg-by-leg allocation: {out_path}")

        # save summary as CSV
        sdf.to_csv(REPORT_DIR / "slippage_summary.csv",
                   index=False, encoding="utf-8-sig")
        print(f"Saved summary: {REPORT_DIR / 'slippage_summary.csv'}")

    # ================================================================
    # Print detailed allocation for the best opportunity
    # ================================================================
    if all_analyses and summary_rows:
        # find the best one
        best_analysis = max(
            [a for a in all_analyses if a["results_by_capital"]],
            key=lambda a: max(r["profit"] for r in a["results_by_capital"])
        )
        best_capital_result = max(
            best_analysis["results_by_capital"],
            key=lambda x: x["profit"]
        )

        print("\n" + "=" * 80)
        print("BEST OPPORTUNITY - Detailed allocation per leg")
        print("=" * 80)
        print(f"\nEvent: {best_analysis['event_title']}")
        print(f"Capital: ${best_capital_result['target_capital']:,.0f}")
        print(f"Shares per leg: {best_capital_result['shares_per_outcome']:,.1f}")
        print(f"Expected profit: ${best_capital_result['profit']:,.2f}")
        print(f"\n{'Outcome':<35} {'Shares':>10} {'Cost':>10} {'AvgPrice':>10}")
        print("-" * 70)
        for leg in sorted(best_capital_result["leg_details"],
                          key=lambda x: -x["cost"]):
            outcome = str(leg["outcome"])[:33]
            print(f"  {outcome:<33} {leg['shares']:>10,.1f} "
                  f"${leg['cost']:>8,.2f} ${leg['avg_price']:>8,.4f}")

    print("\n" + "=" * 80)
    print("KEY INSIGHTS")
    print("=" * 80)
    print("""
1. SLIPPAGE COMPOUNDS: For each capital level, the script tested if all
   legs can be filled. If a leg's best-ask depth is thin, doubling capital
   doesn't double profit — it eats your edge through slippage.

2. OPTIMAL K: For each event, profit grows linearly with K up to a point,
   then plateaus or drops. The optimal K is the largest where every leg
   still gets fully filled at acceptable prices.

3. ALL OR NOTHING: If even ONE leg can't be filled, arb fails. The script
   skips events where any leg has missing liquidity.

4. CAPITAL ALLOCATION: Within an event, capital splits by PRICE, not by
   leg count. Expensive favorites get most of the capital.
""")


if __name__ == "__main__":
    main()