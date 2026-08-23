"""
Strict Arbitrage Inspector (v2)
================================

A much more conservative analyzer that:
  - Removes annualization (it's misleading for one-time edges)
  - Filters out expired markets (negative days to resolution)
  - Filters out non-exhaustive events (price thresholds, view counts, etc)
  - Only trusts negRisk events for "buy all No" strategy
  - Shows raw edge + days held + simple APR (not compounded)

Run:
    python strict_inspect_arb.py
"""

from pathlib import Path
import pandas as pd

REPORT_DIR = Path("polymarket_report")
SUMMARY_CSV = REPORT_DIR / "markets_summary.csv"

POLYMARKET_FEE = 0.02
MIN_DEPTH_PER_MARKET = 50
MIN_DAYS_TO_RESOLUTION = 1  # ignore expired/expiring-today markets
MAX_DAYS_TO_RESOLUTION = 365  # ignore long-locked capital

# patterns that almost always mean NOT mutually exclusive + exhaustive
NON_EXHAUSTIVE_PATTERNS = [
    "vs.", " vs ",  # sports with possible draws
    "highest temperature",  # may have 'none of these'
    "more markets", "more market",  # sub-grouping
    "by may", "by june", "by july",  # date threshold (may not happen)
    "by aug", "by sep", "by oct", "by nov", "by dec",
    "by january", "by february", "by march", "by april",
    "above $", "above ", "below $",  # price thresholds overlap
    "hit $", "hit ___", "hit billion", "hit million",
    "price will", "price hit",
    "what price",
    "fdv above",  # token FDV thresholds overlap
    "posts ",  # post-count thresholds overlap
    "cases in",  # case-count thresholds overlap
    "transit the strait",  # overlapping ship counts
    "released by",  # may not release at all
    "out by",  # may not happen
    "departs as",  # may not depart
    "by...",  # date thresholds usually overlap or skip
]


def is_non_exhaustive(title: str) -> str:
    """Return reason if event isn't truly mutually-exclusive+exhaustive, else empty"""
    if pd.isna(title) or not title:
        return "no title"
    t = str(title).lower()
    for pattern in NON_EXHAUSTIVE_PATTERNS:
        if pattern in t:
            return f"pattern: '{pattern}'"
    return ""


def main():
    print("Loading data...")
    df = pd.read_csv(SUMMARY_CSV, encoding="utf-8-sig")
    print(f"Loaded {len(df):,} markets\n")

    df_book = df[df["yes_ask"].notna() & df["no_ask"].notna()].copy()

    events_data = []
    for event_id, group in df_book.groupby("event_id"):
        if len(group) < 2:
            continue

        title = group["event_title"].iloc[0] if "event_title" in group else ""
        n = len(group)
        sum_yes_ask = group["yes_ask"].sum()
        sum_no_ask = group["no_ask"].sum()
        sum_yes_bid = group["yes_bid"].sum()

        # min depth across legs
        min_depth = group["tradable_depth_usd"].min()

        # time to resolution: use the EARLIEST resolution among all legs
        # (because once one resolves, the dynamics change)
        times = group["time_to_resolution_days"].dropna()
        time_to_res = times.min() if len(times) > 0 else None

        is_neg_risk = group["neg_risk"].any() if "neg_risk" in group else False

        non_exh_reason = is_non_exhaustive(title)

        # max yes volume (best case if you wanted to buy all yes)
        sum_volume_24h = group["volume_24hr"].fillna(0).sum()

        events_data.append({
            "event_id": event_id,
            "event_title": title,
            "event_slug": group["event_slug"].iloc[0] if "event_slug" in group else "",
            "n_markets": n,
            "sum_yes_ask": sum_yes_ask,
            "sum_no_ask": sum_no_ask,
            "sum_yes_bid": sum_yes_bid,
            "buy_all_yes_gross_edge": 1.0 - sum_yes_ask,
            "buy_all_yes_net_edge": (1.0 - sum_yes_ask) - POLYMARKET_FEE,
            "min_depth_usd": min_depth,
            "sum_volume_24h": sum_volume_24h,
            "time_to_res_days": time_to_res,
            "is_neg_risk": is_neg_risk,
            "non_exhaustive_reason": non_exh_reason,
        })

    events_df = pd.DataFrame(events_data)
    print(f"Analyzed {len(events_df):,} multi-outcome events\n")

    # ================================================================
    # STRICT FILTER: only events that pass ALL of these
    # ================================================================
    print("=" * 78)
    print("STRICT FILTERED OPPORTUNITIES")
    print("=" * 78)
    print(f"""
Filters applied:
  - Net edge > 1%               (after 2% fee)
  - Min depth >= ${MIN_DEPTH_PER_MARKET}             (executable size on every leg)
  - {MIN_DAYS_TO_RESOLUTION} <= time to resolution <= {MAX_DAYS_TO_RESOLUTION} days   (not expired, not too far out)
  - Pattern filter (no sports vs, no price thresholds, no date-thresholds)
  - sum(yes_ask) between 0.5 and 0.99   (suspicious if outside)
""")

    strict = events_df[
        (events_df["buy_all_yes_net_edge"] > 0.01) &
        (events_df["min_depth_usd"].fillna(0) >= MIN_DEPTH_PER_MARKET) &
        (events_df["time_to_res_days"] >= MIN_DAYS_TO_RESOLUTION) &
        (events_df["time_to_res_days"] <= MAX_DAYS_TO_RESOLUTION) &
        (events_df["non_exhaustive_reason"] == "") &
        (events_df["sum_yes_ask"] >= 0.5) &
        (events_df["sum_yes_ask"] <= 0.99)
        ].copy()

    # SIMPLE APR (no compounding) — much more honest than annualization
    strict["simple_apr_pct"] = (
            strict["buy_all_yes_net_edge"] / strict["time_to_res_days"] * 365 * 100
    )

    strict = strict.sort_values("simple_apr_pct", ascending=False)

    print(f"Found {len(strict)} candidates that pass ALL filters:\n")

    if not strict.empty:
        print(f"{'Net':>6} {'Simple APR':>11} {'Days':>5} {'N':>3} {'Depth':>10} {'NegRisk':>8}  Event")
        print("-" * 78)
        for _, r in strict.head(30).iterrows():
            neg = "YES" if r["is_neg_risk"] else " no"
            title = str(r["event_title"])[:42]
            depth = r['min_depth_usd'] or 0
            print(f"  {r['buy_all_yes_net_edge'] * 100:>4.1f}% "
                  f"  {r['simple_apr_pct']:>8.1f}%  "
                  f"{r['time_to_res_days']:>4.0f}d "
                  f"{int(r['n_markets']):>3} "
                  f"${depth:>8.0f} "
                  f"{neg:>8}  {title}")
    else:
        print("  No candidates pass all strict filters.")
        print("  This is the expected outcome — Polymarket is efficient.")

    # ================================================================
    # SHOW WHAT WAS FILTERED OUT (for debugging the filter)
    # ================================================================
    print("\n" + "=" * 78)
    print("WHAT WAS FILTERED OUT (and why)")
    print("=" * 78)

    pre_filter = events_df[events_df["buy_all_yes_net_edge"] > 0.01].copy()

    print(f"\nTotal events with net edge > 1%: {len(pre_filter)}")

    n_no_depth = (pre_filter["min_depth_usd"].fillna(0) < MIN_DEPTH_PER_MARKET).sum()
    n_expired = (pre_filter["time_to_res_days"] < MIN_DAYS_TO_RESOLUTION).sum()
    n_too_long = (pre_filter["time_to_res_days"] > MAX_DAYS_TO_RESOLUTION).sum()
    n_non_exh = (pre_filter["non_exhaustive_reason"] != "").sum()
    n_too_low = (pre_filter["sum_yes_ask"] < 0.5).sum()

    print(f"  Filtered: insufficient depth:       {n_no_depth}")
    print(f"  Filtered: expired/expires today:    {n_expired}")
    print(f"  Filtered: locks capital > 1 year:   {n_too_long}")
    print(f"  Filtered: non-exhaustive pattern:   {n_non_exh}")
    print(f"  Filtered: sum_yes_ask < 0.5:        {n_too_low}")

    # show top 10 filtered out so user can review the rules
    print(f"\nTop 10 events filtered as non-exhaustive (for review):")
    print("-" * 78)
    non_exh = events_df[events_df["non_exhaustive_reason"] != ""].nlargest(
        10, "buy_all_yes_gross_edge"
    )
    for _, r in non_exh.iterrows():
        title = str(r["event_title"])[:45]
        reason = r["non_exhaustive_reason"]
        print(f"  edge={r['buy_all_yes_gross_edge'] * 100:>5.1f}%  [{reason}]")
        print(f"     {title}")

    # ================================================================
    # SAVE
    # ================================================================
    output_path = REPORT_DIR / "strict_arb_candidates.csv"
    if not strict.empty:
        strict.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"\nSaved: {output_path}")

    # ================================================================
    # FINAL ADVICE
    # ================================================================
    print("\n" + "=" * 78)
    print("WHAT TO DO WITH SURVIVORS")
    print("=" * 78)
    print("""
For each candidate above, BEFORE trading:

1. Open the Polymarket event page (use the event_slug in the CSV)
2. Read the actual market questions — are they truly mutually exclusive?
3. Check if "Other" or "No" or "Off the chart" is one of the options
4. Check the resolution rules — what happens on tie/draw/cancellation?
5. Verify the order book LIVE — snapshot data is already stale
6. Check best ask vs average ask — depth at top of book may be tiny
7. Calculate slippage for your intended size

Even after all that, EXECUTION RISK is real:
  - Filling one leg but not others = directional exposure
  - Gas fees and Polygon network delays
  - Front-running by faster bots

This is why most arbitrage on Polymarket is done by automated bots
running 24/7 with sub-second reaction times.
""")


if __name__ == "__main__":
    main()