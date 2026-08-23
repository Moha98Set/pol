"""
Polymarket Slippage Analyzer v2
================================

Major fixes from v1:
  1. Filters out expired events before processing
  2. Properly counts legs with vs without liquidity
  3. Shows partial arb potential (e.g. 20/22 legs liquid)
  4. Better progress messages
  5. ACTUALLY prints the slippage table (v1 had a bug)

What this tells you:
  - For each event, the LIVE order book situation
  - Which legs have no sellers (you can't buy = arb impossible)
  - For executable events, the slippage curve at $10 to $10k capital
  - Optimal capital to deploy per event

Install:
    pip install requests pandas

Run:
    python slippage_analyzer_v2.py
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_FEE = 0.02
REPORT_DIR = Path("polymarket_report")

TOP_N = 10
TEST_CAPITALS = [10, 50, 100, 500, 1000, 5000, 10000]


def fetch_order_book(token_id: str) -> Optional[dict]:
    """Fetch live order book"""
    try:
        r = requests.get(f"{CLOB_URL}/book",
                         params={"token_id": token_id}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def cost_to_buy_k_shares(asks: list, k: float) -> dict:
    """Walk asks lowest-to-highest, buy K shares, return total cost"""
    if not asks:
        return {"spent_usd": 0, "shares_filled": 0, "fully_filled": False,
                "avg_price": None, "fill_pct": 0}

    asks_sorted = sorted(asks, key=lambda x: float(x["price"]))

    spent_usd = 0.0
    shares_filled = 0.0

    for level in asks_sorted:
        price = float(level["price"])
        size = float(level["size"])
        if price <= 0 or size <= 0:
            continue

        remaining = k - shares_filled
        if remaining <= 0:
            break

        if size <= remaining:
            spent_usd += price * size
            shares_filled += size
        else:
            spent_usd += price * remaining
            shares_filled += remaining
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
    }


def is_event_active(event: dict) -> bool:
    """Check if event hasn't expired yet"""
    end_date = event.get("endDate")
    if not end_date:
        return True
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return end > now
    except Exception:
        return True


def analyze_slippage_curve(books: list) -> list:
    """
    For given list of (outcome, asks) tuples, compute the slippage curve
    across various capital levels.
    """
    n = len(books)
    if n == 0:
        return []

    # Best ask price for each leg (estimate K without slippage)
    best_asks = []
    for _, asks in books:
        sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
        valid_asks = [a for a in sorted_asks
                      if float(a["price"]) > 0 and float(a["size"]) > 0]
        if valid_asks:
            best_asks.append(float(valid_asks[0]["price"]))

    if len(best_asks) < n:
        return []

    sum_best_asks = sum(best_asks)
    if sum_best_asks <= 0 or sum_best_asks >= 1:
        return []

    results = []
    for target_capital in TEST_CAPITALS:
        max_k_no_slip = target_capital / sum_best_asks

        # binary-search-like: try decreasing K until cost <= target
        best_k = 0
        best_result = None

        for shrink in [1.0, 0.98, 0.95, 0.92, 0.9, 0.85, 0.8, 0.7, 0.6, 0.5, 0.3, 0.1]:
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

            if not all_filled or total_cost > target_capital:
                continue

            payout = k
            fee = payout * POLYMARKET_FEE
            profit = payout - total_cost - fee

            if k > best_k and profit > 0:
                best_k = k
                best_result = {
                    "target_capital": target_capital,
                    "shares_per_outcome": k,
                    "real_cost": total_cost,
                    "payout": payout,
                    "fee": fee,
                    "profit": profit,
                    "roi_pct": (profit / total_cost * 100),
                    "leg_details": leg_details,
                }

        if best_result:
            results.append(best_result)

    return results


def main():
    print("=" * 80)
    print("Polymarket Slippage Analyzer v2")
    print("=" * 80)

    csv_path = REPORT_DIR / "strict_arb_candidates.csv"
    if not csv_path.exists():
        print(f"\nERROR: {csv_path} not found.")
        return

    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = df[df["buy_all_yes_net_edge"] > 0].copy()
    df = df.sort_values("simple_apr_pct", ascending=False)
    top = df.head(TOP_N)

    print(f"\nAnalyzing top {len(top)} candidates from your saved CSV...")
    print(f"Some may be expired since snapshot was taken.\n")

    successful = []
    expired = []
    partial = []

    for idx, (_, cand) in enumerate(top.iterrows(), 1):
        event_slug = cand["event_slug"]
        title = str(cand["event_title"])[:55]
        snapshot_edge = cand["buy_all_yes_net_edge"] * 100

        print(f"\n[{idx}/{len(top)}] {title}")
        print(f"  Slug: {event_slug}")
        print(f"  Snapshot edge was: {snapshot_edge:.1f}%")

        # Fetch live event
        try:
            r = requests.get(f"{GAMMA_URL}/events",
                             params={"slug": event_slug}, timeout=15)
            r.raise_for_status()
            event_data = r.json()
            if not event_data:
                print(f"  Event no longer exists on Polymarket")
                expired.append(title)
                continue
            event = event_data[0] if isinstance(event_data, list) else event_data
        except Exception as e:
            print(f"  Failed to fetch event: {e}")
            continue

        # Check if expired
        if not is_event_active(event):
            print(f"  Event has EXPIRED (endDate passed). Skipping.")
            expired.append(title)
            continue

        # Get markets
        all_markets = event.get("markets", [])
        open_markets = [m for m in all_markets if not m.get("closed")]

        print(f"  Total markets in event: {len(all_markets)}")
        print(f"  Open markets: {len(open_markets)}")

        if len(open_markets) < 2:
            print(f"  Less than 2 outcomes — not multi-outcome arb")
            continue

        # Fetch order books
        print(f"  Fetching {len(open_markets)} order books...")
        books_with_asks = []
        legs_no_asks = []

        for m in open_markets:
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
            time.sleep(0.04)

            if book and book.get("asks"):
                # check actual usable asks
                valid_asks = [a for a in book["asks"]
                              if float(a["price"]) > 0 and float(a["size"]) > 0]
                if valid_asks:
                    books_with_asks.append((outcome, valid_asks))
                else:
                    legs_no_asks.append(outcome)
            else:
                legs_no_asks.append(outcome)

        n_have = len(books_with_asks)
        n_missing = len(legs_no_asks)
        total = n_have + n_missing

        print(f"  Legs WITH liquidity: {n_have} / {total}")
        if n_missing > 0:
            print(f"  Legs MISSING (no sellers): {n_missing}")
            print(f"  WARNING: Arb impossible — every leg must be buyable")
            if n_missing < 5:
                print(f"  Missing: {', '.join(legs_no_asks[:5])}")
            partial.append({
                "title": title,
                "have": n_have,
                "missing": n_missing,
                "missing_names": legs_no_asks,
            })
            continue

        # All legs have liquidity! Run slippage analysis
        print(f"  All legs liquid! Running slippage analysis...")

        curve = analyze_slippage_curve(books_with_asks)

        if not curve:
            print(f"  No profitable size found. Edge may have closed since snapshot.")
            continue

        print(f"\n  Slippage curve (LIVE data):")
        print(f"  {'Capital':>10} {'Shares/leg':>11} {'Real Cost':>12} "
              f"{'Profit':>10} {'ROI':>8}")
        print(f"  " + "-" * 60)

        for r in curve:
            print(f"  ${r['target_capital']:>8,.0f} "
                  f"{r['shares_per_outcome']:>11,.1f} "
                  f"${r['real_cost']:>10,.2f} "
                  f"${r['profit']:>8,.2f} "
                  f"{r['roi_pct']:>6.2f}%")

        # find best (highest profit)
        best = max(curve, key=lambda x: x["profit"])
        successful.append({
            "title": title,
            "slug": event_slug,
            "n_legs": n_have,
            "best_capital": best["target_capital"],
            "real_cost": best["real_cost"],
            "shares": best["shares_per_outcome"],
            "profit": best["profit"],
            "roi": best["roi_pct"],
            "leg_details": best["leg_details"],
        })

    # ================================================================
    # FINAL SUMMARY
    # ================================================================
    print("\n" + "=" * 80)
    print("FINAL RESULTS")
    print("=" * 80)

    print(f"\nSuccessful (all legs liquid): {len(successful)}")
    print(f"Partial (some legs missing):  {len(partial)}")
    print(f"Expired:                      {len(expired)}")

    if successful:
        print(f"\n{'=' * 80}")
        print("EXECUTABLE OPPORTUNITIES (sorted by profit):")
        print(f"{'=' * 80}")

        successful.sort(key=lambda x: x["profit"], reverse=True)

        for s in successful:
            print(f"\n  {s['title']}")
            print(f"    Legs: {s['n_legs']}")
            print(f"    Best capital: ${s['best_capital']:,.0f}")
            print(f"    Real cost (with slippage): ${s['real_cost']:,.2f}")
            print(f"    Shares per leg: {s['shares']:,.1f}")
            print(f"    PROFIT: ${s['profit']:,.2f} (ROI: {s['roi']:.2f}%)")

        # show detailed allocation for the best one
        best = successful[0]
        print(f"\n{'=' * 80}")
        print(f"DETAILED ALLOCATION FOR BEST OPPORTUNITY")
        print(f"{'=' * 80}")
        print(f"\nEvent: {best['title']}")
        print(f"Capital target: ${best['best_capital']:,.0f}")
        print(f"Total real cost: ${best['real_cost']:,.2f}")
        print(f"Profit: ${best['profit']:,.2f}")
        print(f"\n{'Outcome':<35} {'Shares':>10} {'Cost':>10} {'AvgPrice':>10}")
        print("-" * 70)
        for leg in sorted(best['leg_details'], key=lambda x: -x['cost']):
            outcome = str(leg['outcome'])[:33]
            print(f"  {outcome:<33} {leg['shares']:>10,.1f} "
                  f"${leg['cost']:>8,.2f} ${leg['avg_price']:>8,.4f}")

        # save
        with open(REPORT_DIR / "slippage_results.json", "w") as f:
            json.dump(successful, f, indent=2, default=str)
        print(f"\nSaved: {REPORT_DIR / 'slippage_results.json'}")

    if partial:
        print(f"\n{'=' * 80}")
        print(f"PARTIAL EVENTS (some legs missing — arb impossible):")
        print(f"{'=' * 80}")
        for p in partial:
            print(f"  {p['title']}: {p['have']} have liquidity, {p['missing']} missing")

    if expired:
        print(f"\nExpired events skipped: {len(expired)}")
        for e in expired:
            print(f"  - {e}")

    print(f"\n{'=' * 80}")
    print("LESSON FROM THIS RUN:")
    print(f"{'=' * 80}")
    print("""
The snapshot CSV is from a previous moment. Many candidates may have:
  - Expired since snapshot
  - Gained more outcomes (markets without sellers)
  - Had their edge closed by other traders

If most events show 'partial' or 'expired', the lesson is:
  → Snapshot-based arb scanning becomes stale fast
  → You need LIVE monitoring via WebSocket, not periodic snapshots
  → Or, run polymarket_report.py FRESH right before each analysis
""")


if __name__ == "__main__":
    main()