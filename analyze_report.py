"""
Polymarket Report Analyzer
===========================

Quick analysis of the markets_summary.csv to find:
  - Arbitrage opportunities (with various thresholds)
  - Structural arb candidates (multi-market events where sum != 1)
  - Best markets for monitoring (high volume + tight spread)
  - Markets resolving soon (potential mispricing)

Install:
    pip install pandas

Run:
    python analyze_report.py
"""

import pandas as pd
from pathlib import Path

CSV_PATH = Path("polymarket_report/markets_summary.csv")
POLYMARKET_FEE = 0.02

if not CSV_PATH.exists():
    print(f"File not found: {CSV_PATH}")
    print("Run polymarket_report.py first")
    exit(1)

# load CSV
df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
print(f"Loaded {len(df):,} markets\n")

# ====================================================================
# 1. Overall stats
# ====================================================================
print("=" * 70)
print("OVERALL STATISTICS")
print("=" * 70)

has_book = df["yes_ask"].notna() & df["no_ask"].notna()
print(f"Markets with valid order books: {has_book.sum():,} / {len(df):,}")

if has_book.sum() > 0:
    df_book = df[has_book].copy()
    print(f"\nSpread distribution (Yes side):")
    print(f"  Median spread: {df_book['yes_spread'].median()*100:.2f}%")
    print(f"  Mean spread:   {df_book['yes_spread'].mean()*100:.2f}%")
    print(f"  Tight spreads (<1%): {(df_book['yes_spread'] < 0.01).sum():,} markets")
    print(f"  Wide spreads (>5%):  {(df_book['yes_spread'] > 0.05).sum():,} markets")

# ====================================================================
# 2. Direct arbitrage opportunities (Yes + No < 1)
# ====================================================================
print("\n" + "=" * 70)
print("DIRECT ARBITRAGE (buy Yes + No < $1)")
print("=" * 70)

for threshold_pct in [0, 0.5, 1, 2, 5]:
    threshold = threshold_pct / 100
    count = ((df["buy_buy_arb_net"] > threshold) &
             (df["tradable_depth_usd"].fillna(0) > 50)).sum()
    print(f"  Net edge > {threshold_pct:>4}% (depth > $50): {count:,} markets")

# show top 10 with any positive net edge
top_arb = df[
    (df["buy_buy_arb_net"] > 0) &
    (df["tradable_depth_usd"].fillna(0) > 50)
].nlargest(10, "buy_buy_arb_net")

if not top_arb.empty:
    print(f"\nTop 10 direct arb opportunities:")
    print("-" * 70)
    for _, r in top_arb.iterrows():
        edge_pct = r["buy_buy_arb_net"] * 100
        depth = r["tradable_depth_usd"] or 0
        question = str(r["question"])[:60]
        print(f"  [{edge_pct:+5.2f}%] depth=${depth:>7.0f}  {question}")
else:
    print("\n  No direct arbitrage opportunities found right now (as expected)")

# ====================================================================
# 3. Reverse arbitrage (Yes + No > 1, split USDC and sell both)
# ====================================================================
print("\n" + "=" * 70)
print("REVERSE ARBITRAGE (sell Yes + No > $1 after split)")
print("=" * 70)

reverse = df[
    (df["sell_sell_arb_gross"] > 0) &
    (df["tradable_depth_usd"].fillna(0) > 50)
].nlargest(10, "sell_sell_arb_gross")

if not reverse.empty:
    print(f"\nTop 10 reverse arb:")
    print("-" * 70)
    for _, r in reverse.iterrows():
        edge_pct = r["sell_sell_arb_gross"] * 100
        print(f"  [{edge_pct:+5.2f}%]  {str(r['question'])[:60]}")
else:
    print("\n  No reverse arbitrage opportunities")

# ====================================================================
# 4. Structural arb: events where sum of yes_asks differs from 1
# ====================================================================
print("\n" + "=" * 70)
print("STRUCTURAL ARBITRAGE (multi-outcome events)")
print("=" * 70)

# IMPORTANT: structural arb only works when an event is
#   1) mutually exclusive (only ONE Yes can resolve true), AND
#   2) exhaustive (exactly one MUST resolve true)
#
# Polymarket has many multi-market events that violate these:
#   - "Top 10 of X" — 10 Yes resolve true, so sum ~ 10
#   - "Bitcoin above $X" with multiple price thresholds — overlapping
#   - "Highest temp in range A/B/C" — usually exclusive but possible "none"
#
# We use the negRisk flag and a tight sum range around 1.0 to filter
# only true mutually exclusive events.

df_struct = df[df["yes_ask"].notna()].copy()

event_groups = df_struct.groupby("event_id").agg(
    market_count=("question", "count"),
    sum_yes_ask=("yes_ask", "sum"),
    sum_no_bid=("no_bid", "sum"),
    event_title=("event_title", "first"),
    neg_risk_any=("neg_risk", "any"),  # if any market in event is negRisk
).reset_index()

# only events with 2+ markets
multi = event_groups[event_groups["market_count"] >= 2].copy()
print(f"Found {len(multi):,} multi-outcome events with order books")

# REAL structural arb candidates: sum should be CLOSE to 1
# (mutually exclusive + exhaustive events have sum = 1 in equilibrium)
# We keep events where sum is between 0.7 and 1.3 — outside that range
# the event is almost certainly not mutually exclusive
real_struct = multi[
    (multi["sum_yes_ask"] >= 0.7) &
    (multi["sum_yes_ask"] <= 1.3)
].copy()
print(f"After filtering for plausibly mutually-exclusive events: {len(real_struct):,}\n")

# overprice arb: sum(yes_ask) slightly > 1 means probabilities oversum → buy No on all
real_struct["overprice_edge"] = real_struct["sum_yes_ask"] - 1.0
overpriced = real_struct[real_struct["overprice_edge"] > 0.01].nlargest(15, "overprice_edge")

if not overpriced.empty:
    print(f"Events where sum(Yes asks) > 1.01 (potential overprice arb):")
    print("-" * 70)
    for _, r in overpriced.iterrows():
        title = str(r["event_title"])[:50]
        neg = "negRisk" if r["neg_risk_any"] else "       "
        print(f"  [+{r['overprice_edge']*100:5.2f}%] [{neg}] "
              f"({int(r['market_count'])} markets, sum={r['sum_yes_ask']:.3f}) {title}")
else:
    print("No overprice events found in the 0.7-1.3 range")

# underprice arb: sum(yes_ask) slightly < 1 means buying all Yes could lock profit
underpriced = real_struct[
    real_struct["sum_yes_ask"] < 0.99
].nsmallest(15, "sum_yes_ask")

if not underpriced.empty:
    print(f"\nEvents where sum(Yes asks) < 0.99 (potential underprice arb):")
    print("-" * 70)
    for _, r in underpriced.iterrows():
        title = str(r["event_title"])[:50]
        edge_pct = (1 - r["sum_yes_ask"]) * 100
        neg = "negRisk" if r["neg_risk_any"] else "       "
        print(f"  [+{edge_pct:5.2f}%] [{neg}] "
              f"({int(r['market_count'])} markets, sum={r['sum_yes_ask']:.3f}) {title}")

print(f"\nNote: events with sum far from 1.0 (like Eurovision Top 10) are NOT")
print(f"arbitrage — they're just non-mutually-exclusive events. We filtered them out.")

# ====================================================================
# 5. Best markets to monitor (high vol + tight spread)
# ====================================================================
print("\n" + "=" * 70)
print("BEST MARKETS TO MONITOR (high volume + tight spread)")
print("=" * 70)

monitor = df[
    (df["yes_ask"].notna()) &
    (df["volume_24hr"].fillna(0) > 10000) &  # $10k+ volume
    (df["yes_spread"].fillna(1) < 0.01) &     # <1% spread
    (df["time_to_resolution_days"].fillna(0) > 1)  # not about to resolve
].nlargest(15, "volume_24hr")

print(f"\nTop 15 high-quality markets (vol > $10k, spread < 1%):")
print("-" * 70)
for _, r in monitor.iterrows():
    vol = r["volume_24hr"] or 0
    spread = (r["yes_spread"] or 0) * 100
    print(f"  vol=${vol:>10,.0f}  spread={spread:.2f}%  "
          f"{str(r['question'])[:50]}")

# ====================================================================
# 6. Markets resolving in next 24 hours (often have weird pricing)
# ====================================================================
print("\n" + "=" * 70)
print("MARKETS RESOLVING SOON (< 24 hours)")
print("=" * 70)

soon = df[
    (df["time_to_resolution_days"].fillna(99) < 1) &
    (df["time_to_resolution_days"].fillna(-1) > 0) &
    (df["volume_24hr"].fillna(0) > 5000)
].nlargest(15, "volume_24hr")

print(f"\n{len(soon)} markets with vol > $5k resolving within 24h")
if not soon.empty:
    print("-" * 70)
    for _, r in soon.head(15).iterrows():
        hours = (r["time_to_resolution_days"] or 0) * 24
        vol = r["volume_24hr"] or 0
        last = r["last_trade_price"] or 0
        print(f"  in {hours:>4.1f}h  vol=${vol:>8,.0f}  last={last:.3f}  "
              f"{str(r['question'])[:45]}")

# ====================================================================
# 7. Save filtered views as additional CSVs
# ====================================================================
print("\n" + "=" * 70)
print("SAVING FILTERED VIEWS")
print("=" * 70)

output_dir = Path("polymarket_report")

# arb candidates (any positive edge)
arb_candidates = df[
    (df["buy_buy_arb_net"] > 0) &
    (df["tradable_depth_usd"].fillna(0) > 20)
].sort_values("buy_buy_arb_net", ascending=False)
arb_candidates.to_csv(output_dir / "arb_all_positive.csv",
                      index=False, encoding="utf-8-sig")
print(f"  Saved: arb_all_positive.csv ({len(arb_candidates)} rows)")

# high-quality markets for monitoring
quality = df[
    (df["yes_ask"].notna()) &
    (df["volume_24hr"].fillna(0) > 5000) &
    (df["yes_spread"].fillna(1) < 0.02)
].sort_values("volume_24hr", ascending=False)
quality.to_csv(output_dir / "monitor_watchlist.csv",
               index=False, encoding="utf-8-sig")
print(f"  Saved: monitor_watchlist.csv ({len(quality)} rows)")

# structural arb opportunities (only filtered real candidates)
if not real_struct.empty:
    real_struct_sorted = real_struct.sort_values("overprice_edge",
                                                  key=abs, ascending=False)
    real_struct_sorted.to_csv(output_dir / "structural_arb_events.csv",
                              index=False, encoding="utf-8-sig")
    print(f"  Saved: structural_arb_events.csv ({len(real_struct_sorted)} rows)")

print("\nDone!")