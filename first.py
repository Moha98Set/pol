import urllib.request
import json
import sys

BASE_URL = "https://gamma-api.polymarket.com"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read())

def format_volume(v):
    try:
        v = float(v)
        if v >= 1_000_000:
            return f"${v/1_000_000:.1f}M"
        elif v >= 1_000:
            return f"${v/1_000:.0f}K"
        else:
            return f"${v:.0f}"
    except:
        return "N/A"

def get_yes_price(market):
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        return f"{float(prices[0])*100:.0f}%"
    except:
        return "N/A"

def list_markets(limit=20, order="volume_24hr"):
    print(f"\nFetching active markets from Polymarket...\n")
    url = (
        f"{BASE_URL}/markets"
        f"?active=true&closed=false"
        f"&order={order}&ascending=false"
        f"&limit={limit}"
    )
    markets = fetch(url)

    if not markets:
        print("No markets found.")
        return

    print(f"{'#':<4} {'Market Title':<55} {'YES':>5}  {'24h Vol':>9}")
    print("-" * 78)

    for i, m in enumerate(markets, 1):
        question = m.get("question", "No title")
        if len(question) > 52:
            question = question[:49] + "..."
        yes_price = get_yes_price(m)
        volume = format_volume(m.get("volume24hr") or m.get("volumeNum") or 0)
        print(f"{i:<4} {question:<55} {yes_price:>5}  {volume:>9}")

    print("-" * 78)
    print(f"\n{len(markets)} active markets displayed.")
    print("INFO: YES price = market-implied probability of the event occurring\n")

def search_markets(keyword, limit=10):
    print(f"\nSearching for: '{keyword}'\n")
    url = (
        f"{BASE_URL}/markets"
        f"?active=true&closed=false"
        f"&limit=100"
    )
    markets = fetch(url)
    results = [
        m for m in markets
        if keyword.lower() in (m.get("question") or "").lower()
    ][:limit]

    if not results:
        print("No results found.")
        return

    print(f"{'#':<4} {'Market Title':<55} {'YES':>5}  {'24h Vol':>9}")
    print("-" * 78)
    for i, m in enumerate(results, 1):
        question = m.get("question", "No title")
        if len(question) > 52:
            question = question[:49] + "..."
        yes_price = get_yes_price(m)
        volume = format_volume(m.get("volume24hr") or 0)
        print(f"{i:<4} {question:<55} {yes_price:>5}  {volume:>9}")

    print("-" * 78)
    print(f"\n{len(results)} result(s) found.\n")

def main():
    args = sys.argv[1:]

    if not args:
        list_markets(limit=20)
        return

    if args[0] == "search" and len(args) > 1:
        search_markets(" ".join(args[1:]))
    elif args[0] == "top":
        limit = int(args[1]) if len(args) > 1 else 10
        list_markets(limit=limit, order="volume_24hr")
    elif args[0] == "new":
        list_markets(limit=20, order="start_date")
    elif args[0] == "help":
        print("""
Usage:
  python polymarket_markets.py              -> Top 20 markets by volume
  python polymarket_markets.py top 5        -> Top 5 markets
  python polymarket_markets.py new          -> Newest markets
  python polymarket_markets.py search btc   -> Search markets by keyword
  python polymarket_markets.py help         -> Show this help
""")
    else:
        print("Unknown command. Run: python polymarket_markets.py help")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nError: {e}")
        print("Make sure you have a working internet connection (VPN may be required).")