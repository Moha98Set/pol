"""
Polymarket Slippage Analyzer v3 - Debug Mode
=============================================

Key improvements over v2:
  1. Always shows the CURRENT edge (even if negative)
  2. Compares snapshot edge vs live edge
  3. Shows what the sum of best asks is RIGHT NOW
  4. Tells you WHY no profitable size was found

This is the most honest version — it tells you what's happening, not just
gives empty tables.

Install:
    pip install requests pandas

Run:
    python slippage_analyzer_v3.py
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

TOP_N = 15  # increased to see more
TEST_CAPITALS = [10, 50, 100, 500, 1000, 5000, 10000]


def fetch_order_book(token_id: str) -> Optional[dict]:
    try:
        r = requests.get(f"{CLOB_URL}/book", 
                        params={"token_id": token_id}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def cost_to_buy_k_shares(asks: list, k: float) -> dict:
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
        "avg_price": avg_price,
        "fill_pct": fill_pct,
        "fully_filled": fill_pct >= 99.99,
    }


def is_event_active(event: dict) -> bool:
    end_date = event.get("endDate")
    if not end_date:
        return True
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return end > datetime.now(timezone.utc)
    except Exception:
        return True


def analyze_event_full(event_slug: str, event_title: str, 
                       snapshot_edge: float) -> dict:
    """
    Complete analysis with full diagnostics.
    """
    result = {
        "title": event_title,
        "slug": event_slug,
        "snapshot_edge_pct": snapshot_edge * 100,
        "status": "unknown",
        "details": "",
    }
    
    # fetch event
    try:
        r = requests.get(f"{GAMMA_URL}/events",
                        params={"slug": event_slug}, timeout=15)
        r.raise_for_status()
        event_data = r.json()
        if not event_data:
            result["status"] = "not_found"
            return result
        event = event_data[0] if isinstance(event_data, list) else event_data
    except Exception as e:
        result["status"] = "fetch_error"
        result["details"] = str(e)
        return result
    
    if not is_event_active(event):
        result["status"] = "expired"
        return result
    
    all_markets = event.get("markets", [])
    open_markets = [m for m in all_markets if not m.get("closed")]
    
    if len(open_markets) < 2:
        result["status"] = "not_multi_outcome"
        return result
    
    # fetch all order books
    legs_with_asks = []
    legs_missing = []
    
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
            valid_asks = [a for a in book["asks"] 
                          if float(a["price"]) > 0 and float(a["size"]) > 0]
            if valid_asks:
                legs_with_asks.append((outcome, valid_asks))
            else:
                legs_missing.append(outcome)
        else:
            legs_missing.append(outcome)
    
    n_have = len(legs_with_asks)
    n_missing = len(legs_missing)
    result["legs_total"] = n_have + n_missing
    result["legs_with_asks"] = n_have
    result["legs_missing"] = n_missing
    result["missing_names"] = legs_missing[:10]
    
    if n_missing > 0:
        result["status"] = "missing_legs"
        return result
    
    if n_have < 2:
        result["status"] = "insufficient_legs"
        return result
    
    # === Compute current sum of best asks ===
    best_asks = []
    for outcome, asks in legs_with_asks:
        sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
        best_asks.append(float(sorted_asks[0]["price"]))
    
    sum_best_asks = sum(best_asks)
    result["sum_best_asks_live"] = sum_best_asks
    result["gross_edge_live_pct"] = (1 - sum_best_asks) * 100
    result["net_edge_live_pct"] = (1 - sum_best_asks - POLYMARKET_FEE) * 100
    
    # === Detect if edge has closed ===
    if sum_best_asks >= 1.0:
        result["status"] = "no_edge"
        result["details"] = f"Sum of best asks = {sum_best_asks:.4f} ≥ 1.0"
        return result
    
    if (1 - sum_best_asks - POLYMARKET_FEE) <= 0:
        result["status"] = "edge_below_fee"
        result["details"] = f"Gross edge {(1-sum_best_asks)*100:.2f}% < 2% fee"
        return result
    
    # === Run slippage analysis ===
    curve = []
    for target_capital in TEST_CAPITALS:
        max_k_no_slip = target_capital / sum_best_asks
        
        best_for_this_capital = None
        for shrink in [1.0, 0.99, 0.98, 0.97, 0.95, 0.92, 0.9, 0.85, 0.8, 0.7, 0.5, 0.3]:
            k = max_k_no_slip * shrink
            total_cost = 0
            all_filled = True
            leg_details = []
            for outcome, asks in legs_with_asks:
                sim = cost_to_buy_k_shares(asks, k)
                total_cost += sim["spent_usd"]
                if not sim["fully_filled"]:
                    all_filled = False
                leg_details.append({
                    "outcome": outcome,
                    "shares": sim["shares_filled"],
                    "cost": sim["spent_usd"],
                    "avg_price": sim["avg_price"],
                })
            if not all_filled or total_cost > target_capital:
                continue
            payout = k
            fee = payout * POLYMARKET_FEE
            profit = payout - total_cost - fee
            if best_for_this_capital is None or k > best_for_this_capital["shares"]:
                best_for_this_capital = {
                    "target_capital": target_capital,
                    "shares": k,
                    "real_cost": total_cost,
                    "payout": payout,
                    "fee": fee,
                    "profit": profit,
                    "roi_pct": (profit / total_cost * 100) if total_cost > 0 else 0,
                    "leg_details": leg_details,
                }
        if best_for_this_capital and best_for_this_capital["profit"] > 0:
            curve.append(best_for_this_capital)
    
    if not curve:
        result["status"] = "depth_too_thin"
        result["details"] = (f"Gross edge is {(1-sum_best_asks)*100:.2f}% but "
                            f"best ask depth is too thin to execute profitably")
        return result
    
    result["status"] = "executable"
    result["slippage_curve"] = curve
    result["best"] = max(curve, key=lambda x: x["profit"])
    return result


def main():
    print("=" * 80)
    print("Polymarket Slippage Analyzer v3 - Debug Mode")
    print("=" * 80)
    
    csv_path = REPORT_DIR / "strict_arb_candidates.csv"
    if not csv_path.exists():
        print(f"\nERROR: {csv_path} not found.")
        return
    
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = df[df["buy_all_yes_net_edge"] > 0].copy()
    df = df.sort_values("buy_all_yes_net_edge", ascending=False)  # sort by edge, not APR
    top = df.head(TOP_N)
    
    print(f"\nAnalyzing top {len(top)} candidates from CSV...")
    print(f"Sorted by snapshot edge (highest first)\n")
    
    all_results = []
    
    for idx, (_, cand) in enumerate(top.iterrows(), 1):
        title = str(cand["event_title"])[:55]
        slug = cand["event_slug"]
        snap_edge = cand["buy_all_yes_net_edge"]
        
        print(f"\n[{idx}/{len(top)}] {title}")
        print(f"  Snapshot edge: {snap_edge*100:.1f}%   |   Slug: {slug}")
        
        r = analyze_event_full(slug, title, snap_edge)
        all_results.append(r)
        
        # print result
        status = r["status"]
        if status == "executable":
            best = r["best"]
            print(f"  ✓ EXECUTABLE")
            print(f"  Live sum of asks: {r['sum_best_asks_live']:.4f}")
            print(f"  Live net edge: {r['net_edge_live_pct']:.2f}%")
            print(f"  Best: ${best['target_capital']:,.0f} → profit ${best['profit']:.2f}")
        elif status == "no_edge":
            print(f"  ✗ NO EDGE (edge closed)")
            print(f"  Live sum of asks: {r['sum_best_asks_live']:.4f} (≥ 1.0)")
            print(f"  Live gross edge: {r['gross_edge_live_pct']:.2f}%")
        elif status == "edge_below_fee":
            print(f"  ✗ EDGE BELOW FEE")
            print(f"  Live sum of asks: {r['sum_best_asks_live']:.4f}")
            print(f"  Live gross edge: {r['gross_edge_live_pct']:.2f}% (< 2% fee)")
        elif status == "depth_too_thin":
            print(f"  ✗ DEPTH TOO THIN")
            print(f"  Live sum of asks: {r['sum_best_asks_live']:.4f}")
            print(f"  Live gross edge: {r['gross_edge_live_pct']:.2f}%")
            print(f"  But cannot fill even smallest size due to slippage")
        elif status == "missing_legs":
            print(f"  ✗ MISSING LEGS: {r['legs_missing']}/{r['legs_total']}")
            if r['missing_names']:
                names = ", ".join(r['missing_names'][:3])
                print(f"  Examples: {names}")
        elif status == "expired":
            print(f"  ✗ EXPIRED")
        elif status == "not_found":
            print(f"  ✗ NOT FOUND on Polymarket")
        else:
            print(f"  ✗ {status}: {r.get('details', '')}")
    
    # ================================================================
    # SUMMARY
    # ================================================================
    print("\n" + "=" * 80)
    print("DETAILED SUMMARY")
    print("=" * 80)
    
    # group by status
    by_status = {}
    for r in all_results:
        by_status.setdefault(r["status"], []).append(r)
    
    print(f"\nStatus breakdown:")
    for status, rs in by_status.items():
        print(f"  {status:<25}: {len(rs)}")
    
    # show edge erosion table
    print(f"\n{'='*80}")
    print("EDGE EROSION: Snapshot edge vs Live edge")
    print(f"{'='*80}")
    print(f"\n{'Event':<45} {'Snap':>8} {'Live':>8} {'Diff':>8} Status")
    print("-" * 90)
    
    for r in all_results:
        title = r["title"][:43]
        snap = r["snapshot_edge_pct"]
        live = r.get("net_edge_live_pct")
        if live is not None:
            diff = live - snap
            live_str = f"{live:>6.2f}%"
            diff_str = f"{diff:>+6.2f}%"
        else:
            live_str = "  N/A"
            diff_str = "  N/A"
        print(f"  {title:<43} {snap:>6.2f}% {live_str} {diff_str} {r['status']}")
    
    # ================================================================
    # SHOW EXECUTABLE EVENTS IN DETAIL
    # ================================================================
    executable = [r for r in all_results if r["status"] == "executable"]
    
    if executable:
        print(f"\n{'='*80}")
        print(f"EXECUTABLE EVENTS - DETAILED SLIPPAGE CURVES")
        print(f"{'='*80}")
        
        for r in executable:
            print(f"\n{r['title']}")
            print(f"  Sum asks: {r['sum_best_asks_live']:.4f}, "
                  f"Net edge: {r['net_edge_live_pct']:.2f}%")
            print(f"  {'Capital':>10} {'Shares/leg':>11} {'Real Cost':>12} "
                  f"{'Profit':>10} {'ROI':>8}")
            print(f"  " + "-" * 60)
            for c in r["slippage_curve"]:
                print(f"  ${c['target_capital']:>8,.0f} "
                      f"{c['shares']:>11,.1f} "
                      f"${c['real_cost']:>10,.2f} "
                      f"${c['profit']:>8,.2f} "
                      f"{c['roi_pct']:>6.2f}%")
    else:
        print(f"\n{'='*80}")
        print("NO EXECUTABLE OPPORTUNITIES")
        print(f"{'='*80}")
        print("""
None of the top candidates from the snapshot are currently executable.
This is the harsh reality of arbitrage:
  - Snapshot edges close fast (other bots get there first)
  - Multi-outcome events often have "Other" or thin-liquidity legs
  - Even with all legs liquid, depth at best ask may be too thin

Strategies that work better:
  1. Live WebSocket monitoring (sub-second reaction)
  2. Focus on simple 2-leg events (Fed decisions, binary politics)
  3. Watch for sudden spread widening events (news, weekends)
""")
    
    # save full results
    with open(REPORT_DIR / "slippage_v3_results.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull diagnostic data saved: {REPORT_DIR / 'slippage_v3_results.json'}")


if __name__ == "__main__":
    main()