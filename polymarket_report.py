"""
Polymarket Comprehensive Market Report
=======================================

Fetches ALL active markets with full details optimized for arbitrage analysis.

Output:
  - markets_full.json     : raw data for every market
  - markets_summary.csv   : flat table for quick analysis in Excel/pandas
  - arb_opportunities.csv : pre-calculated arb candidates (yes_ask + no_ask < 1)
  - report_summary.txt    : human-readable overview

Install:
    pip install requests pandas tqdm

Run:
    python polymarket_report.py
"""

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from tqdm import tqdm

# ====================================================================
# Config
# ====================================================================
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

OUTPUT_DIR = Path("./polymarket_report")
OUTPUT_DIR.mkdir(exist_ok=True)

PAGE_SIZE = 500
REQUEST_TIMEOUT = 20
RETRY_DELAY = 2  # seconds between retries
SLEEP_BETWEEN_BATCHES = 0.1  # be nice to the API

# Cost assumptions for arbitrage filtering
POLYMARKET_FEE = 0.02  # 2% on winnings
MIN_ARB_EDGE = 0.001  # 0.1% minimum edge — keep low to surface candidates
MIN_DEPTH_USD = 50  # at least $50 tradable depth

# Volume filter — skip dead markets where you can't actually trade
# Set to 0 to fetch every active market (much slower)
# Suggested values:
#   100   = lots of markets, slow run, comprehensive
#   1000  = mid-tier markets, ~1 hour run
#   10000 = only high-volume markets, fast run
MIN_VOLUME_24HR = 1000  # USD

# ====================================================================
# Step 1: Fetch all markets via the /events endpoint
# Using /events is the recommended way to discover ALL markets, because
# /markets endpoint is capped at a small page count. Events paginate correctly.
# ====================================================================

EVENTS_PAGE_SIZE = 100  # /events recommended page size


def fetch_all_markets() -> list:
    """Page through every active event, then flatten markets out of them"""
    print("[Step 1] Fetching all active events from Gamma API...")

    all_events = []
    offset = 0

    with tqdm(desc="Fetching events", unit="events") as pbar:
        while True:
            params = {
                "closed": "false",
                "active": "true",
                "order": "id",
                "ascending": "false",
                "limit": EVENTS_PAGE_SIZE,
                "offset": offset,
            }

            data = None
            for attempt in range(3):
                try:
                    r = requests.get(f"{GAMMA_URL}/events", params=params, timeout=REQUEST_TIMEOUT)
                    r.raise_for_status()
                    data = r.json()
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"\n  Failed after 3 attempts: {e}")
                        break
                    time.sleep(RETRY_DELAY)

            if not data:
                break

            all_events.extend(data)
            pbar.update(len(data))
            offset += EVENTS_PAGE_SIZE

            # last page
            if len(data) < EVENTS_PAGE_SIZE:
                break

            time.sleep(SLEEP_BETWEEN_BATCHES)

    print(f"  Got {len(all_events):,} events\n")

    # extract markets from each event
    print("  Extracting markets from events...")
    all_markets = []
    seen_ids = set()
    for event in all_events:
        event_markets = event.get("markets", [])
        for market in event_markets:
            # only active + open markets that haven't been seen
            mid = market.get("id") or market.get("conditionId")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)

            if market.get("active") and not market.get("closed"):
                # carry event metadata onto the market for grouping later
                market["_event_id"] = event.get("id")
                market["_event_slug"] = event.get("slug")
                market["_event_title"] = event.get("title")
                all_markets.append(market)

    print(f"  Extracted {len(all_markets):,} unique active markets\n")
    return all_markets


# ====================================================================
# Step 2: Enrich with CLOB order book data
# ====================================================================

def parse_token_ids(market: dict) -> list:
    """Extract Yes/No token IDs from a market"""
    token_ids = market.get("clobTokenIds")
    if not token_ids:
        return []
    if isinstance(token_ids, str):
        try:
            return json.loads(token_ids)
        except json.JSONDecodeError:
            return []
    return token_ids if isinstance(token_ids, list) else []


def fetch_order_book(token_id: str) -> Optional[dict]:
    """Get full order book depth for a single token"""
    try:
        r = requests.get(
            f"{CLOB_URL}/book",
            params={"token_id": token_id},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def extract_book_metrics(book: Optional[dict]) -> dict:
    """Convert a raw order book into useful metrics"""
    if not book:
        return {
            "best_bid": None, "best_ask": None,
            "bid_size": 0, "ask_size": 0,
            "spread": None, "midpoint": None,
            "book_depth_bid": 0, "book_depth_ask": 0,
            "levels_bid": 0, "levels_ask": 0,
        }

    bids = book.get("bids", [])  # sorted ascending, best bid is last
    asks = book.get("asks", [])  # sorted descending, best ask is last

    best_bid = float(bids[-1]["price"]) if bids else None
    best_ask = float(asks[-1]["price"]) if asks else None
    bid_size = float(bids[-1]["size"]) if bids else 0
    ask_size = float(asks[-1]["size"]) if asks else 0

    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    midpoint = ((best_bid + best_ask) / 2) if (best_bid is not None and best_ask is not None) else None

    # total $ depth on each side (price * size for every level)
    depth_bid = sum(float(b["price"]) * float(b["size"]) for b in bids)
    depth_ask = sum(float(a["price"]) * float(a["size"]) for a in asks)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread": spread,
        "midpoint": midpoint,
        "book_depth_bid": depth_bid,
        "book_depth_ask": depth_ask,
        "levels_bid": len(bids),
        "levels_ask": len(asks),
    }


def enrich_markets_with_books(markets: list) -> list:
    """Add CLOB order book metrics to every market"""
    print("[Step 2] Fetching order books from CLOB API...")
    print(f"  This will make ~{len(markets) * 2:,} requests, please be patient\n")

    enriched = []
    for market in tqdm(markets, desc="Order books", unit="markets"):
        token_ids = parse_token_ids(market)

        yes_book = None
        no_book = None
        if len(token_ids) >= 2:
            yes_book = fetch_order_book(token_ids[0])
            no_book = fetch_order_book(token_ids[1])

        market["_yes_token_id"] = token_ids[0] if len(token_ids) >= 1 else None
        market["_no_token_id"] = token_ids[1] if len(token_ids) >= 2 else None
        market["_yes_book"] = extract_book_metrics(yes_book)
        market["_no_book"] = extract_book_metrics(no_book)

        enriched.append(market)
        time.sleep(0.02)  # ~50 req/s, well under rate limit

    print()
    return enriched


# ====================================================================
# Step 3: Calculate derived arbitrage metrics
# ====================================================================

def calculate_arb_metrics(market: dict) -> dict:
    """Calculate fields that don't exist in the API but matter for arbitrage"""
    yb = market.get("_yes_book", {})
    nb = market.get("_no_book", {})

    yes_ask = yb.get("best_ask")
    yes_bid = yb.get("best_bid")
    no_ask = nb.get("best_ask")
    no_bid = nb.get("best_bid")

    # internal arbitrage: buy both Yes and No, total cost < 1 means profit
    buy_buy_arb = None
    if yes_ask is not None and no_ask is not None:
        buy_buy_arb = 1.0 - (yes_ask + no_ask)  # positive = arb opportunity

    # reverse: if yes_bid + no_bid > 1, you can split USDC into shares and sell both
    sell_sell_arb = None
    if yes_bid is not None and no_bid is not None:
        sell_sell_arb = (yes_bid + no_bid) - 1.0  # positive = arb opportunity

    # net arb after fees
    net_buy_buy_arb = None
    if buy_buy_arb is not None:
        net_buy_buy_arb = buy_buy_arb - POLYMARKET_FEE

    # time to resolution in days
    time_to_res_days = None
    end_date = market.get("endDate") or market.get("endDateIso")
    if end_date:
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            time_to_res_days = (end - now).total_seconds() / 86400
        except Exception:
            pass

    # liquidity score: min of bid and ask depth (you can only arb up to the smaller side)
    min_depth = None
    yes_depth = yb.get("book_depth_ask", 0)
    no_depth = nb.get("book_depth_ask", 0)
    if yes_depth and no_depth:
        min_depth = min(yes_depth, no_depth)

    return {
        "yes_ask": yes_ask,
        "yes_bid": yes_bid,
        "no_ask": no_ask,
        "no_bid": no_bid,
        "yes_spread": yb.get("spread"),
        "no_spread": nb.get("spread"),
        "buy_buy_arb_gross": buy_buy_arb,
        "buy_buy_arb_net": net_buy_buy_arb,
        "sell_sell_arb_gross": sell_sell_arb,
        "tradable_depth_usd": min_depth,
        "time_to_resolution_days": time_to_res_days,
    }


# ====================================================================
# Step 4: Build flat summary row
# ====================================================================

def flatten_market(market: dict) -> dict:
    """Pick the most useful fields and flatten into a single row"""
    arb = calculate_arb_metrics(market)

    return {
        # identification
        "condition_id": market.get("conditionId"),
        "question_id": market.get("questionID"),
        "slug": market.get("slug"),
        "question": market.get("question"),
        "category": market.get("category"),
        "yes_token_id": market.get("_yes_token_id"),
        "no_token_id": market.get("_no_token_id"),

        # event grouping (key for structural / multi-outcome arbitrage)
        "event_id": market.get("_event_id"),
        "event_slug": market.get("_event_slug"),
        "event_title": market.get("_event_title"),

        # resolution
        "end_date": market.get("endDate") or market.get("endDateIso"),
        "time_to_resolution_days": arb["time_to_resolution_days"],
        "resolution_source": market.get("resolutionSource"),
        "neg_risk": market.get("negRisk"),
        "neg_risk_market_id": market.get("negRiskMarketID"),

        # status flags
        "active": market.get("active"),
        "closed": market.get("closed"),
        "archived": market.get("archived"),
        "accepting_orders": market.get("acceptingOrders"),
        "enable_order_book": market.get("enableOrderBook"),
        "restricted": market.get("restricted"),

        # prices from API (may be slightly stale vs order book)
        "last_trade_price": market.get("lastTradePrice"),
        "one_day_price_change": market.get("oneDayPriceChange"),

        # live order book metrics (fresh from CLOB)
        "yes_bid": arb["yes_bid"],
        "yes_ask": arb["yes_ask"],
        "no_bid": arb["no_bid"],
        "no_ask": arb["no_ask"],
        "yes_spread": arb["yes_spread"],
        "no_spread": arb["no_spread"],

        # arbitrage signals (the headline numbers)
        "buy_buy_arb_gross": arb["buy_buy_arb_gross"],
        "buy_buy_arb_net": arb["buy_buy_arb_net"],
        "sell_sell_arb_gross": arb["sell_sell_arb_gross"],

        # liquidity
        "volume_total": market.get("volumeNum") or market.get("volume"),
        "volume_24hr": market.get("volume24hr"),
        "volume_1wk": market.get("volume1wk"),
        "liquidity": market.get("liquidityNum") or market.get("liquidity"),
        "open_interest": market.get("openInterest"),
        "tradable_depth_usd": arb["tradable_depth_usd"],

        # microstructure
        "min_order_size": market.get("orderMinSize") or market.get("minimumOrderSize"),
        "min_tick_size": market.get("orderPriceMinTickSize") or market.get("minimumTickSize"),
        "rewards_enabled": market.get("rewardsEnabled"),

        # depth details
        "yes_levels_bid": market.get("_yes_book", {}).get("levels_bid", 0),
        "yes_levels_ask": market.get("_yes_book", {}).get("levels_ask", 0),
        "no_levels_bid": market.get("_no_book", {}).get("levels_bid", 0),
        "no_levels_ask": market.get("_no_book", {}).get("levels_ask", 0),
    }


# ====================================================================
# Step 5: Write outputs
# ====================================================================

def write_outputs(markets: list, flat_rows: list):
    """Write all output files"""
    print("[Step 3] Writing output files...\n")

    # 1) raw JSON dump
    raw_path = OUTPUT_DIR / "markets_full.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(markets, f, ensure_ascii=False, default=str)
    print(f"  Saved raw data: {raw_path}")

    # 2) flat CSV summary (open in Excel/pandas)
    # utf-8-sig adds a BOM so Excel on Windows displays non-Latin chars correctly
    if flat_rows:
        csv_path = OUTPUT_DIR / "markets_summary.csv"
        keys = list(flat_rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(flat_rows)
        print(f"  Saved summary CSV: {csv_path}")

    # 3) filtered arbitrage opportunities
    arb_rows = [
        r for r in flat_rows
        if r.get("buy_buy_arb_net") is not None
           and r["buy_buy_arb_net"] > MIN_ARB_EDGE
           and (r.get("tradable_depth_usd") or 0) > MIN_DEPTH_USD
    ]
    # sort by best arbitrage edge first
    arb_rows.sort(key=lambda r: r["buy_buy_arb_net"], reverse=True)

    if arb_rows:
        arb_path = OUTPUT_DIR / "arb_opportunities.csv"
        with open(arb_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(arb_rows[0].keys()))
            writer.writeheader()
            writer.writerows(arb_rows)
        print(f"  Saved arb opportunities: {arb_path} ({len(arb_rows)} candidates)")

    # 4) human-readable summary
    summary_path = OUTPUT_DIR / "report_summary.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"Polymarket Report - {datetime.now().isoformat()}\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Total markets analyzed: {len(markets):,}\n")
        f.write(f"Markets with valid order books: "
                f"{sum(1 for r in flat_rows if r['yes_ask'] is not None):,}\n")
        f.write(f"Potential arb opportunities (net > {MIN_ARB_EDGE * 100:.1f}%): "
                f"{len(arb_rows)}\n\n")

        if arb_rows:
            f.write("Top 10 arbitrage opportunities:\n")
            f.write("-" * 60 + "\n")
            for r in arb_rows[:10]:
                f.write(f"\n  {r['question'][:70]}\n")
                f.write(f"    Net edge: {r['buy_buy_arb_net'] * 100:.2f}%\n")
                f.write(f"    Yes ask: {r['yes_ask']}, No ask: {r['no_ask']}\n")
                f.write(f"    Depth: ${r['tradable_depth_usd']:.0f}\n")
                f.write(f"    URL: https://polymarket.com/event/{r['slug']}\n")

    print(f"  Saved summary report: {summary_path}\n")


# ====================================================================
# Main
# ====================================================================

def main():
    print("=" * 60)
    print("Polymarket Comprehensive Report Generator")
    print("=" * 60 + "\n")

    start = time.time()

    # fetch list of all markets
    markets = fetch_all_markets()
    if not markets:
        print("No markets found, exiting")
        return

    # filter step 1: only markets with CLOB enabled (others have no order book)
    clob_enabled = [m for m in markets if m.get("enableOrderBook")]
    print(f"  {len(clob_enabled):,} have order book enabled")

    # filter step 2: only markets with meaningful 24h volume
    # arbitrage on a $0 volume market is theoretical — no one will fill your order
    def get_volume_24hr(m: dict) -> float:
        v = m.get("volume24hr") or 0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    tradable = [m for m in clob_enabled if get_volume_24hr(m) >= MIN_VOLUME_24HR]

    print(f"  {len(tradable):,} have 24h volume >= ${MIN_VOLUME_24HR}")
    print(f"  (skipping {len(clob_enabled) - len(tradable):,} low-volume markets)\n")

    # show volume distribution so you can tune MIN_VOLUME_24HR next time
    volumes = sorted([get_volume_24hr(m) for m in clob_enabled], reverse=True)
    if volumes:
        print(f"  Volume stats (24h):")
        print(f"    Top market:    ${volumes[0]:>12,.0f}")
        print(f"    Top 10:        ${volumes[min(9, len(volumes) - 1)]:>12,.0f}")
        print(f"    Top 100:       ${volumes[min(99, len(volumes) - 1)]:>12,.0f}")
        print(f"    Top 500:       ${volumes[min(499, len(volumes) - 1)]:>12,.0f}")
        print(f"    Top 1000:      ${volumes[min(999, len(volumes) - 1)]:>12,.0f}")
        print()

    if not tradable:
        print("No markets meet the volume threshold. Try lowering MIN_VOLUME_24HR.")
        return

    # estimate how long the order book step will take
    est_seconds = len(tradable) * 2 * 0.02 + len(tradable) * 0.05  # rough estimate
    print(f"  Estimated order book fetch time: ~{est_seconds / 60:.1f} minutes\n")

    # enrich with order book data
    enriched = enrich_markets_with_books(tradable)

    # build flat rows
    flat_rows = [flatten_market(m) for m in enriched]

    # write outputs
    write_outputs(enriched, flat_rows)

    elapsed = time.time() - start
    print(f"Done in {elapsed / 60:.1f} minutes")
    print(f"Output directory: {OUTPUT_DIR.absolute()}")


if __name__ == "__main__":
    main()