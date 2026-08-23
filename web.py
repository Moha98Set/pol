"""
Polymarket WebSocket Client - Market Channel
=============================================

Connects to the market channel and receives real-time order book updates,
price changes, and trades. No authentication required.

Install:
    pip install websockets requests

Run:
    python polymarket_ws.py
"""

import asyncio
import json
from datetime import datetime

import requests
import websockets
from websockets.exceptions import ConnectionClosed

# ====================================================================
# Config
# ====================================================================
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"
PING_INTERVAL = 10  # send PING every 10 seconds

# ====================================================================
# Helper: get a few active token_ids for testing
# ====================================================================

def get_sample_token_ids(count: int = 2) -> list:
    """Fetch token_ids from active markets for testing"""
    response = requests.get(
        f"{GAMMA_URL}/markets",
        params={"active": "true", "closed": "false", "limit": count},
        timeout=10,
    )
    response.raise_for_status()
    markets = response.json()

    token_ids = []
    for market in markets:
        clob_token_ids = market.get("clobTokenIds")
        if clob_token_ids:
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)
            # each market has two tokens (Yes and No) - we only take Yes
            token_ids.append(clob_token_ids[0])
            print(f"Market: {market.get('question')[:60]}")
            print(f"   Yes Token: {clob_token_ids[0][:20]}...")

    return token_ids


# ====================================================================
# Message handler
# ====================================================================

def handle_message(msg: dict):
    """Pretty-print incoming messages"""
    event_type = msg.get("event_type")
    timestamp = datetime.now().strftime("%H:%M:%S")

    if event_type == "book":
        # full order book snapshot
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        best_bid = bids[-1] if bids else {"price": "N/A", "size": "0"}
        best_ask = asks[0] if asks else {"price": "N/A", "size": "0"}
        print(
            f"[{timestamp}] BOOK SNAPSHOT | "
            f"Bid: {best_bid.get('price')} ({best_bid.get('size')}) | "
            f"Ask: {best_ask.get('price')} ({best_ask.get('size')})"
        )

    elif event_type == "price_change":
        # incremental order book changes
        changes = msg.get("changes", [])
        for c in changes:
            side = c.get("side")
            price = c.get("price")
            size = c.get("size")
            if size == "0":
                print(f"[{timestamp}] REMOVED   | {side} @ {price}")
            else:
                print(f"[{timestamp}] PRICE CHG | {side} @ {price} -> size: {size}")

    elif event_type == "last_trade_price":
        # latest executed trade
        price = msg.get("price")
        size = msg.get("size")
        side = msg.get("side")
        print(f"[{timestamp}] TRADE     | {side} {size} @ {price}")

    elif event_type == "tick_size_change":
        # important: limit orders may be rejected if you use the old tick size
        new_tick = msg.get("new_tick_size")
        print(f"[{timestamp}] TICK CHANGE | new tick size: {new_tick}")

    else:
        print(f"[{timestamp}] {event_type}: {msg}")


# ====================================================================
# WebSocket logic
# ====================================================================

async def send_pings(ws):
    """Send PING every 10 seconds to keep the connection alive"""
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            await ws.send("PING")
        except ConnectionClosed:
            break


async def listen(token_ids: list):
    """Connect to WebSocket and listen for messages"""

    print(f"\nConnecting to {WS_URL}")

    async with websockets.connect(WS_URL) as ws:
        # 1) send subscribe message
        subscribe_msg = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,  # enables best_bid_ask, new_market, market_resolved events
        }
        await ws.send(json.dumps(subscribe_msg))
        print(f"Subscribed to {len(token_ids)} token(s)")
        print(f"Waiting for data... (Ctrl+C to exit)\n")

        # 2) start ping task in background
        ping_task = asyncio.create_task(send_pings(ws))

        # 3) main receive loop
        try:
            async for message in ws:
                # server replies with PONG to our PING
                if message == "PONG":
                    continue

                try:
                    data = json.loads(message)
                    # messages may come as a list
                    if isinstance(data, list):
                        for msg in data:
                            handle_message(msg)
                    else:
                        handle_message(data)
                except json.JSONDecodeError:
                    print(f"Non-JSON message: {message[:100]}")
        finally:
            ping_task.cancel()


# ====================================================================
# Main
# ====================================================================

async def main():
    print("=" * 60)
    print("Polymarket WebSocket Client")
    print("=" * 60)

    # get a few token_ids for testing
    print("\n[1] Fetching active markets...")
    token_ids = get_sample_token_ids(count=2)

    if not token_ids:
        print("No active markets found")
        return

    # connect with auto-reconnect
    print("\n[2] Connecting to WebSocket...")
    while True:
        try:
            await listen(token_ids)
        except (ConnectionClosed, websockets.exceptions.WebSocketException) as e:
            print(f"\nConnection lost: {e}")
            print("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except KeyboardInterrupt:
            print("\nExiting")
            break
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting")