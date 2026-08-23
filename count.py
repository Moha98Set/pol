"""
Count Polymarket active markets
================================

Counts markets across multiple filters to give you a clear picture
of how many tradable markets exist right now.
"""

import requests

GAMMA_URL = "https://gamma-api.polymarket.com"


def count_markets(params: dict, label: str) -> int:
    """Count markets matching given filters using pagination"""
    total = 0
    offset = 0
    page_size = 500

    while True:
        p = {**params, "limit": page_size, "offset": offset}
        try:
            r = requests.get(f"{GAMMA_URL}/markets", params=p, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Error: {e}")
            return total

        if not data:
            break

        total += len(data)
        offset += page_size

        # last page (got fewer than page_size results)
        if len(data) < page_size:
            break

        # safety cap to avoid infinite loops
        if offset > 100000:
            print(f"  (capped at {offset:,} for safety)")
            break

    print(f"  {label}: {total:,}")
    return total


def main():
    print("=" * 60)
    print("Polymarket Active Market Counter")
    print("=" * 60)
    print()

    print("Counting markets (this may take a moment)...\n")

    # 1) tradable now: active and not closed
    tradable = count_markets(
        {"active": "true", "closed": "false"},
        "Active + not closed (tradable)"
    )

    # 2) with order book enabled (you can actually trade via CLOB)
    clob = count_markets(
        {"active": "true", "closed": "false", "enableOrderBook": "true"},
        "Active + CLOB order book enabled"
    )

    # 3) accepting orders right now
    accepting = count_markets(
        {"active": "true", "closed": "false", "acceptingOrders": "true"},
        "Active + accepting orders"
    )

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Each market has 2 tokens (Yes + No)")
    print(f"Total tokens you could subscribe to: ~{clob * 2:,}")
    print()
    print(f"Polymarket WS limit: 500 tokens per connection")
    print(f"To monitor all CLOB markets, you'd need: "
          f"{((clob * 2) // 500) + 1} parallel WebSocket connections")


if __name__ == "__main__":
    main()