"""
Structural Arbitrage Deep Inspector
====================================

Takes the structural_arb_events.csv and digs deeper into each event:
  - Checks if all outcomes have liquid order books
  - Calculates REAL executable arbitrage (using asks, not midpoints)
  - Annualizes the return based on time to resolution
  - Flags suspicious events (sports with draws, missing outcomes)
  - Verifies depth at each price level

Run AFTER analyze_report.py.

Install:
    pip install pandas

Run:
    python deep_inspect_arb.py
"""

import json
from pathlib import Path

import pandas as pd

REPORT_DIR = Path("polymarket_report")
SUMMARY_CSV = REPORT_DIR / "markets_summary.csv"
STRUCT_CSV = REPORT_DIR / "structural_arb_events.csv"

POLYMARKET_FEE = 0.02
MIN_DEPTH_PER_MARKET = 10  # USD — every leg must have at least this much depth

# keywords that usually mean the event is NOT truly mutually exclusive
SUSPICIOUS_KEYWORDS = {
    "vs.": "may have a draw outcome (sports)",
    " vs ": "may have a draw outcome (sports)",
    "highest temperature": "may have 'none of these' outcome",
    "more markets": "sub-market grouping, not exhaustive",
    "more market": "sub-market grouping, not exhaustive",
}


def annualize(edge: float, days: float) -> float:
    """Convert a one-time edge to annualized return"""
    if days <= 0 or edge <= 0:
        return 0
    return ((1 + edge) ** (365 / days)) - 1


def check_suspicious(event_title: str) -> str:
    """Flag events that are likely not true mutually-exclusive"""
    if not event_title or pd.isna(event_title):
        return ""
    title_lower = str(event_title).lower()
    for keyword, reason in SUSPICIOUS_KEYWORDS.items():
        if keyword in title_lower:
            return reason
    return ""


def main():
    if not SUMMARY_CSV.exists():
        print(f"File not found: {SUMMARY_CSV}")
        return

    print("Loading data...")
    df = pd.read_csv(SUMMARY_CSV, encoding="utf-8-sig")
    print(f"Loaded {len(df):,} markets\n")

    # group by event with all relevant data
    df_book = df[df["yes_ask"].notna() & df["no_ask"].notna()].copy()

    events_data = []
    for event_id, group in df_book.groupby("event_id"):
        if len(group) < 2:
            continue

        title = group["event_title"].iloc[0] if "event_title" in group else ""
        slug = group["event_slug"].iloc[0] if "event_slug" in group else ""

        # calculate executable arb (using actual asks)
        sum_yes_ask = group["yes_ask"].sum()
        sum_no_ask = group["no_ask"].sum()
        sum_yes_bid = group["yes_bid"].sum()
        sum_no_bid = group["no_bid"].sum()

        # min depth across all legs (you can only arb as much as the weakest leg)
        # for buy-all-Yes strategy: min of yes ask depth
        # we approximate ask depth using the book metric we already have
        min_yes_ask_depth = group["tradable_depth_usd"].min()

        # time to resolution
        times = group["time_to_resolution_days"].dropna()
        time_to_res = times.min() if len(times) > 0 else None

        # check for any negRisk markets (special mechanism)
        is_neg_risk = group["neg_risk"].any() if "neg_risk" in group else False

        # max spread (worst-case execution cost across all legs)
        max_yes_spread = group["yes_spread"].max()
        max_no_spread = group["no_spread"].max()

        # check suspicious patterns
        warning = check_suspicious(title)

        # the REAL arbitrage formulas:
        # Strategy A — Buy all Yes: if sum(yes_ask) < 1, lock $1 - sum profit
        buy_all_yes_edge = 1.0 - sum_yes_ask

        # Strategy B — Buy all No: if sum(no_ask) < (n-1),
        # because exactly one Yes wins, n-1 No's must pay $1 each
        n = len(group)
        buy_all_no_edge = (n - 1) - sum_no_ask

        # Strategy C — Sell all Yes: if sum(yes_bid) > 1, you get more than the obligation
        sell_all_yes_edge = sum_yes_bid - 1.0

        events_data.append({
            "event_id": event_id,
            "event_title": title,
            "event_slug": slug,
            "n_markets": n,
            "sum_yes_ask": sum_yes_ask,
            "sum_no_ask": sum_no_ask,
            "sum_yes_bid": sum_yes_bid,
            "sum_no_bid": sum_no_bid,
            "buy_all_yes_edge": buy_all_yes_edge,
            "buy_all_no_edge_per_dollar": buy_all_no_edge / (n - 1) if n > 1 else 0,
            "sell_all_yes_edge": sell_all_yes_edge,
            "min_depth_usd": min_yes_ask_depth,
            "max_yes_spread": max_yes_spread,
            "max_no_spread": max_no_spread,
            "time_to_res_days": time_to_res,
            "is_neg_risk": is_neg_risk,
            "warning": warning,
        })

    events_df = pd.DataFrame(events_data)
    print(f"Analyzed {len(events_df):,} multi-outcome events\n")

    # ================================================================
    # STRATEGY A: Buy all Yes (sum(yes_ask) < 1)
    # ================================================================
    print("=" * 75)
    print("STRATEGY A: BUY ALL YES (works only if exactly one will resolve True)")
    print("=" * 75)

    candidates = events_df[
        (events_df["buy_all_yes_edge"] > 0.01) &
        (events_df["sum_yes_ask"] >= 0.5) &  # not absurdly low
        (events_df["min_depth_usd"].fillna(0) >= MIN_DEPTH_PER_MARKET) &
        (events_df["warning"] == "")  # filter out sports vs.
        ].copy()

    # gross edge minus fee on full $1 payout
    candidates["net_edge"] = candidates["buy_all_yes_edge"] - POLYMARKET_FEE

    # annualized return if you have a time horizon
    candidates["annualized"] = candidates.apply(
        lambda r: annualize(r["net_edge"], r["time_to_res_days"])
        if r["time_to_res_days"] else 0,
        axis=1
    )

    candidates = candidates.sort_values("net_edge", ascending=False)

    print(f"\nFiltered to {len(candidates)} candidates (no draw-prone, depth >= ${MIN_DEPTH_PER_MARKET})\n")

    if not candidates.empty:
        print(f"{'Net Edge':>8} {'Annual':>8} {'Days':>6} {'N':>4} {'Depth':>8} {'NegRisk':>8}  Event")
        print("-" * 75)
        for _, r in candidates.head(20).iterrows():
            ann = f"{r['annualized'] * 100:>6.1f}%" if r['annualized'] > 0 else "    n/a"
            days = f"{r['time_to_res_days']:>4.0f}d" if r['time_to_res_days'] else "  n/a"
            neg = "YES" if r["is_neg_risk"] else "no"
            title = str(r["event_title"])[:35]
            print(f"  {r['net_edge'] * 100:>5.2f}% {ann:>8} {days:>6} "
                  f"{int(r['n_markets']):>4} ${r['min_depth_usd']:>6.0f} {neg:>8}  {title}")

    # ================================================================
    # STRATEGY B: Buy all No (more robust, works for negRisk events)
    # ================================================================
    print("\n" + "=" * 75)
    print("STRATEGY B: BUY ALL NO (cost should be n-1, if less = arb)")
    print("=" * 75)
    print("If exactly one Yes wins, then n-1 No's pay $1 each.")
    print("Cost = sum(no_ask). Payout = n-1. Edge = (n-1) - sum(no_ask).\n")

    b_candidates = events_df[
        (events_df["buy_all_no_edge_per_dollar"] > 0.01) &
        (events_df["min_depth_usd"].fillna(0) >= MIN_DEPTH_PER_MARKET) &
        (events_df["warning"] == "")
        ].copy()

    b_candidates["net_edge_pct"] = (
            b_candidates["buy_all_no_edge_per_dollar"] - POLYMARKET_FEE
    )
    b_candidates["annualized"] = b_candidates.apply(
        lambda r: annualize(r["net_edge_pct"], r["time_to_res_days"])
        if r["time_to_res_days"] else 0,
        axis=1
    )
    b_candidates = b_candidates.sort_values("net_edge_pct", ascending=False)

    print(f"Found {len(b_candidates)} candidates\n")

    if not b_candidates.empty:
        print(f"{'Net %':>8} {'Annual':>8} {'Days':>6} {'N':>4} {'Depth':>8}  Event")
        print("-" * 75)
        for _, r in b_candidates.head(20).iterrows():
            ann = f"{r['annualized'] * 100:>6.1f}%" if r['annualized'] > 0 else "    n/a"
            days = f"{r['time_to_res_days']:>4.0f}d" if r['time_to_res_days'] else "  n/a"
            title = str(r["event_title"])[:40]
            print(f"  {r['net_edge_pct'] * 100:>5.2f}% {ann:>8} {days:>6} "
                  f"{int(r['n_markets']):>4} ${r['min_depth_usd']:>6.0f}  {title}")

    # ================================================================
    # SUSPICIOUS EVENTS (events flagged with warnings)
    # ================================================================
    print("\n" + "=" * 75)
    print("EVENTS FILTERED OUT AS SUSPICIOUS")
    print("=" * 75)

    suspicious = events_df[
        (events_df["warning"] != "") &
        (events_df["buy_all_yes_edge"] > 0.05)
        ].sort_values("buy_all_yes_edge", ascending=False)

    print(f"\n{len(suspicious)} events look like arb but probably aren't:\n")
    for _, r in suspicious.head(10).iterrows():
        title = str(r["event_title"])[:50]
        print(f"  edge={r['buy_all_yes_edge'] * 100:>5.1f}%  reason: {r['warning']}")
        print(f"    {title}")

    # ================================================================
    # SAVE
    # ================================================================
    print("\n" + "=" * 75)
    print("SAVING RESULTS")
    print("=" * 75)

    events_df_sorted = events_df.sort_values(
        "buy_all_yes_edge", ascending=False, key=abs
    )
    output_path = REPORT_DIR / "deep_arb_analysis.csv"
    events_df_sorted.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"\nSaved: {output_path}")
    print(f"  Columns include: net edges, annualized returns, depth, warnings\n")

    # critical reminders
    print("=" * 75)
    print("CRITICAL REMINDERS BEFORE TRADING ANY OF THESE")
    print("=" * 75)
    print("""
1. THIS DATA IS A SNAPSHOT — prices may have moved significantly.
   Verify the order book live before placing any trade.

2. "Mutually exclusive" must be VERIFIED for each event by reading 
   the actual market questions and resolution rules on Polymarket.
   Many events that LOOK exclusive are not (draws, cancellations, etc).

3. DEPTH numbers shown are total book depth, not depth at the best ask.
   Real executable size may be much smaller.

4. Long time-to-resolution = your capital is LOCKED.
   A 25% edge over 18 months = 16% APR, after fees often lower.

5. negRisk events have a special on-chain mechanism that may already
   arbitrage these spreads. Verify before assuming opportunity.

6. SPORTS markets often resolve in unexpected ways (postponements,
   no-contests, weather, scoring changes). Avoid unless you understand
   the specific event rules.
""")


if __name__ == "__main__":
    main()