"""
Binary Arbitrage Live Scanner
==============================

Final tool based on lessons learned:
  - Only scans binary (2-outcome) events: Yes vs No
  - Fetches LIVE order books at runtime (not from snapshot)
  - Calculates REAL slippage using full order book walk
  - Tests multiple capital sizes to find best ROI
  - No "Other" outcome trap

Strategy: For binary events, the formula is simple:
  yes_token has yes_ask + no_token has no_ask
  if yes_ask + no_ask < 1 → buy both, guaranteed profit

This is the simplest, most reliable Polymarket arb.

Install:
    pip install requests pandas

Run:
    python binary_arb_scanner.py
"""

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

# Scan parameters
MIN_VOLUME_24H = 5000     # only liquid markets
MIN_NET_EDGE = 0.005      # 0.5% minimum after fees
TEST_CAPITALS = [10, 50, 100, 500, 1000, 5000]
MAX_MARKETS_TO_SCAN = 200  # cap to keep runtime reasonable


def fetch_order_book(token_id: str) -> Optional[dict]:
    try:
        r = requests.get(f"{CLOB_URL}/book",
                        params={"token_id": token_id}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def cost_to_buy_k_shares(asks: list, k: float) -> dict:
    """Walk asks from lowest price, buy until we have K shares"""
    if not asks:
        return {"cost": 0, "filled": 0, "ok": False, "avg": None}
    
    asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
    cost = 0.0
    filled = 0.0
    
    for level in asks_sorted:
        p = float(level["price"])
        s = float(level["size"])
        if p <= 0 or s <= 0:
            continue
        need = k - filled
        if need <= 0:
            break
        if s <= need:
            cost += p * s
            filled += s
        else:
            cost += p * need
            filled += need
            break
    
    return {
        "cost": cost,
        "filled": filled,
        "ok": filled >= k * 0.9999,
        "avg": cost / filled if filled > 0 else None,
    }


def analyze_binary_market(yes_asks: list, no_asks: list) -> dict:
    """Compute slippage curve for a binary market"""
    if not yes_asks or not no_asks:
        return {"ok": False, "reason": "missing asks"}
    
    # best asks
    yes_best = sorted(yes_asks, key=lambda x: float(x["price"]))
    no_best = sorted(no_asks, key=lambda x: float(x["price"]))
    
    valid_yes = [a for a in yes_best 
                 if float(a["price"]) > 0 and float(a["size"]) > 0]
    valid_no = [a for a in no_best 
                if float(a["price"]) > 0 and float(a["size"]) > 0]
    
    if not valid_yes or not valid_no:
        return {"ok": False, "reason": "no valid asks"}
    
    yes_ask = float(valid_yes[0]["price"])
    no_ask = float(valid_no[0]["price"])
    sum_asks = yes_ask + no_ask
    
    gross_edge = 1 - sum_asks
    net_edge = gross_edge - POLYMARKET_FEE
    
    if net_edge <= 0:
        return {
            "ok": False,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "sum_asks": sum_asks,
            "gross_edge": gross_edge,
            "net_edge": net_edge,
            "reason": f"no edge (sum={sum_asks:.4f})",
        }
    
    # compute slippage curve
    curve = []
    for cap in TEST_CAPITALS:
        max_k = cap / sum_asks
        best = None
        for shrink in [1.0, 0.99, 0.97, 0.95, 0.9, 0.85, 0.8, 0.7, 0.5]:
            k = max_k * shrink
            yes_sim = cost_to_buy_k_shares(valid_yes, k)
            no_sim = cost_to_buy_k_shares(valid_no, k)
            if not yes_sim["ok"] or not no_sim["ok"]:
                continue
            total_cost = yes_sim["cost"] + no_sim["cost"]
            if total_cost > cap:
                continue
            payout = k
            fee = payout * POLYMARKET_FEE
            profit = payout - total_cost - fee
            if best is None or k > best["shares"]:
                best = {
                    "capital": cap,
                    "shares": k,
                    "real_cost": total_cost,
                    "profit": profit,
                    "roi": (profit / total_cost * 100) if total_cost > 0 else 0,
                    "yes_cost": yes_sim["cost"],
                    "no_cost": no_sim["cost"],
                    "yes_avg": yes_sim["avg"],
                    "no_avg": no_sim["avg"],
                }
        if best and best["profit"] > 0:
            curve.append(best)
    
    return {
        "ok": len(curve) > 0,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "sum_asks": sum_asks,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "curve": curve,
    }


def main():
    print("=" * 80)
    print("Binary Arbitrage Live Scanner")
    print("=" * 80)
    
    # Load summary CSV
    csv_path = REPORT_DIR / "markets_summary.csv"
    if not csv_path.exists():
        print(f"\nERROR: {csv_path} not found.")
        print("Run polymarket_report.py first.")
        return
    
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    
    # Filter to BINARY events only (single-market events with order book)
    # group by event to identify single-market events
    event_counts = df.groupby("event_id").size()
    single_market_events = event_counts[event_counts == 1].index
    
    binary = df[
        df["event_id"].isin(single_market_events) &
        df["yes_token_id"].notna() &
        df["no_token_id"].notna() &
        (df["volume_24hr"].fillna(0) >= MIN_VOLUME_24H) &
        df["enable_order_book"]
    ].copy()
    
    # Sort by snapshot edge (largest first)
    binary["snapshot_edge"] = 1 - binary["yes_ask"].fillna(1) - binary["no_ask"].fillna(1)
    binary = binary.sort_values("snapshot_edge", ascending=False)
    
    # Cap the scan
    if len(binary) > MAX_MARKETS_TO_SCAN:
        print(f"\nFound {len(binary)} binary markets matching criteria")
        print(f"Limiting to top {MAX_MARKETS_TO_SCAN} by snapshot edge")
        binary = binary.head(MAX_MARKETS_TO_SCAN)
    
    print(f"\nScanning {len(binary)} binary markets with LIVE order books...")
    print(f"Volume threshold: ${MIN_VOLUME_24H:,}")
    print(f"Min net edge: {MIN_NET_EDGE*100:.1f}%")
    print()
    
    opportunities = []
    no_edge = 0
    missing_data = 0
    
    for idx, (_, m) in enumerate(binary.iterrows(), 1):
        if idx % 20 == 0:
            print(f"  Scanned {idx}/{len(binary)}... "
                  f"({len(opportunities)} opportunities found)")
        
        yes_id = m["yes_token_id"]
        no_id = m["no_token_id"]
        
        yes_book = fetch_order_book(yes_id)
        time.sleep(0.03)
        no_book = fetch_order_book(no_id)
        time.sleep(0.03)
        
        if not yes_book or not no_book:
            missing_data += 1
            continue
        
        result = analyze_binary_market(
            yes_book.get("asks", []),
            no_book.get("asks", []),
        )
        
        if not result["ok"]:
            no_edge += 1
            continue
        
        if result["net_edge"] < MIN_NET_EDGE:
            no_edge += 1
            continue
        
        # found opportunity!
        opportunities.append({
            "question": m["question"],
            "slug": m["slug"],
            "category": m.get("category"),
            "volume_24h": m["volume_24hr"],
            "snapshot_edge": m["snapshot_edge"] * 100,
            "live_yes_ask": result["yes_ask"],
            "live_no_ask": result["no_ask"],
            "live_sum": result["sum_asks"],
            "live_gross_edge": result["gross_edge"] * 100,
            "live_net_edge": result["net_edge"] * 100,
            "curve": result["curve"],
            "best": max(result["curve"], key=lambda x: x["profit"]),
        })
    
    # =================================================================
    # RESULTS
    # =================================================================
    print("\n" + "=" * 80)
    print("FINAL RESULTS")
    print("=" * 80)
    print(f"\nMarkets scanned: {len(binary)}")
    print(f"Markets with no usable edge: {no_edge}")
    print(f"Markets with data fetch errors: {missing_data}")
    print(f"OPPORTUNITIES FOUND: {len(opportunities)}")
    
    if opportunities:
        # sort by best profit at $100
        opportunities.sort(
            key=lambda x: x["best"]["profit"],
            reverse=True,
        )
        
        print(f"\n{'='*80}")
        print(f"TOP OPPORTUNITIES (sorted by max profit)")
        print(f"{'='*80}\n")
        
        for o in opportunities[:10]:
            print(f"Question: {o['question'][:70]}")
            print(f"  Slug: {o['slug']}")
            print(f"  Volume 24h: ${o['volume_24h']:,.0f}")
            print(f"  Live: yes_ask={o['live_yes_ask']:.4f}, "
                  f"no_ask={o['live_no_ask']:.4f}, "
                  f"sum={o['live_sum']:.4f}")
            print(f"  Net edge: {o['live_net_edge']:.2f}%")
            print(f"  Slippage curve:")
            print(f"    {'Cap':>8} {'Shares':>10} {'Cost':>10} "
                  f"{'Profit':>10} {'ROI':>7}")
            for c in o["curve"]:
                print(f"    ${c['capital']:>6,.0f} "
                      f"{c['shares']:>10,.1f} "
                      f"${c['real_cost']:>8,.2f} "
                      f"${c['profit']:>8,.2f} "
                      f"{c['roi']:>5.2f}%")
            best = o["best"]
            print(f"  >>> BEST: deploy ${best['capital']} for ${best['profit']:.2f} profit")
            print()
        
        # save
        save_data = []
        for o in opportunities:
            save_data.append({
                "question": o["question"],
                "slug": o["slug"],
                "volume_24h": o["volume_24h"],
                "live_yes_ask": o["live_yes_ask"],
                "live_no_ask": o["live_no_ask"],
                "live_net_edge_pct": o["live_net_edge"],
                "best_capital": o["best"]["capital"],
                "best_profit": o["best"]["profit"],
                "best_roi_pct": o["best"]["roi"],
                "url": f"https://polymarket.com/event/{o['slug']}",
            })
        
        pd.DataFrame(save_data).to_csv(
            REPORT_DIR / "binary_opportunities_live.csv",
            index=False, encoding="utf-8-sig"
        )
        print(f"\nSaved: {REPORT_DIR / 'binary_opportunities_live.csv'}")
    else:
        print(f"\n{'='*80}")
        print("NO BINARY ARBITRAGE OPPORTUNITIES RIGHT NOW")
        print(f"{'='*80}")
        print("""
This is normal and expected. Polymarket's binary markets are very efficient.
Direct arbitrage (yes_ask + no_ask < 1) requires:
  - A sudden price movement that bots haven't caught yet
  - A market that's been temporarily dislocated
  - A pricing error

Recommendations:
  1. Run this script periodically (every few minutes)
  2. Set up WebSocket monitoring to catch opportunities sub-second
  3. Focus on markets with news catalysts (when spreads widen briefly)
  4. Consider cross-platform arb against Kalshi or sportsbooks instead
""")


if __name__ == "__main__":
    main()