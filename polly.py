"""
اسکریپت اتصال به Polymarket API
================================

این اسکریپت دو حالت داره:
1. حالت Read-Only (بدون احراز هویت): برای خوندن داده‌های بازار، قیمت‌ها و order book
2. حالت Authenticated (با کلید خصوصی): برای ثبت سفارش و معامله

نصب پیش‌نیازها:
    pip install py-clob-client requests python-dotenv

⚠️ هشدار مهم:
    - کلید خصوصی هرگز نباید در کد هاردکد بشه. از فایل .env استفاده کن.
    - طبق شرایط Polymarket، کاربران آمریکایی و بعضی کشورها اجازه ترید ندارن.
"""

import os
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, BookParams
from py_clob_client.order_builder.constants import BUY, SELL

# بارگذاری متغیرها از فایل .env
load_dotenv()

# ثابت‌ها
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # شبکه Polygon


# ====================================================================
# بخش ۱: کلاینت Read-Only - بدون نیاز به کلید
# ====================================================================

def get_readonly_client() -> ClobClient:
    """ساخت کلاینت بدون احراز هویت برای خوندن داده‌ها"""
    return ClobClient(CLOB_HOST)


def fetch_active_markets(limit: int = 10) -> list:
    """
    گرفتن لیست بازارهای فعال از Gamma API
    این endpoint عمومیه و نیاز به کلید نداره
    """
    response = requests.get(
        f"{GAMMA_HOST}/markets",
        params={"active": "true", "closed": "false", "limit": limit},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def get_market_data(token_id: str) -> dict:
    """گرفتن قیمت، midpoint و order book برای یه توکن خاص"""
    client = get_readonly_client()
    return {
        "midpoint": client.get_midpoint(token_id),
        "buy_price": client.get_price(token_id, side="BUY"),
        "sell_price": client.get_price(token_id, side="SELL"),
        "order_book": client.get_order_book(token_id),
        "spread": client.get_spread(token_id),
    }


# ====================================================================
# بخش ۲: کلاینت Authenticated - برای ثبت سفارش
# ====================================================================

def get_authenticated_client() -> ClobClient:
    """
    ساخت کلاینت با احراز هویت برای ثبت سفارش

    متغیرهای محیطی مورد نیاز در فایل .env:
        POLYMARKET_PRIVATE_KEY=کلید خصوصی شما
        POLYMARKET_FUNDER_ADDRESS=آدرس کیف پول شما
        POLYMARKET_SIGNATURE_TYPE=1  (برای کیف پول‌های Magic/email)
    """
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

    if not private_key or not funder:
        raise ValueError(
            "متغیرهای POLYMARKET_PRIVATE_KEY و POLYMARKET_FUNDER_ADDRESS "
            "باید در فایل .env تنظیم بشن"
        )

    client = ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=sig_type,  # 0=EOA/MetaMask, 1=Magic/email, 2=browser proxy
        funder=funder,
    )

    # دریافت یا ساخت اعتبارنامه‌های API
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def place_limit_order(
        client: ClobClient,
        token_id: str,
        price: float,
        size: float,
        side: str = BUY,
) -> dict:
    """
    ثبت سفارش لیمیت (Limit Order)

    Args:
        client: کلاینت احراز هویت شده
        token_id: شناسه توکن (Yes یا No)
        price: قیمت بین 0.01 تا 0.99
        size: تعداد سهم
        side: BUY یا SELL
    """
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
    )
    signed_order = client.create_order(order_args)
    return client.post_order(signed_order, OrderType.GTC)  # GTC = Good Till Cancelled


def get_my_orders(client: ClobClient) -> list:
    """گرفتن سفارش‌های باز کاربر"""
    return client.get_orders()


def cancel_order(client: ClobClient, order_id: str) -> dict:
    """لغو یه سفارش با شناسه"""
    return client.cancel(order_id=order_id)


# ====================================================================
# بخش ۳: مثال استفاده
# ====================================================================

def main():
    print("=" * 60)
    print("نمونه استفاده از Polymarket API")
    print("=" * 60)

    # ---- مرحله ۱: گرفتن لیست بازارهای فعال ----
    print("\n[۱] گرفتن بازارهای فعال...")
    markets = fetch_active_markets(limit=3)

    for i, market in enumerate(markets, 1):
        print(f"\n  بازار {i}: {market.get('question')}")
        print(f"  Slug: {market.get('slug')}")
        token_ids = market.get("clobTokenIds")
        if token_ids:
            # clobTokenIds ممکنه به صورت رشته JSON باشه
            import json
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            print(f"  Yes Token: {token_ids[0]}")
            print(f"  No Token:  {token_ids[1]}")

    # ---- مرحله ۲: گرفتن قیمت یه توکن خاص ----
    if markets:
        import json
        token_ids = markets[0].get("clobTokenIds")
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)

        if token_ids:
            yes_token = token_ids[0]
            print(f"\n[۲] گرفتن داده‌های قیمت برای توکن Yes اولین بازار...")
            try:
                data = get_market_data(yes_token)
                print(f"  Midpoint: {data['midpoint']}")
                print(f"  Buy Price: {data['buy_price']}")
                print(f"  Sell Price: {data['sell_price']}")
                print(f"  Spread: {data['spread']}")
            except Exception as e:
                print(f"  خطا در گرفتن داده: {e}")

    # ---- مرحله ۳ (اختیاری): ثبت سفارش ----
    # برای فعال‌سازی، فایل .env رو با کلیدهات پر کن و کامنت پایین رو بردار
    """
    print("\n[۳] ثبت سفارش...")
    auth_client = get_authenticated_client()

    response = place_limit_order(
        client=auth_client,
        token_id="<TOKEN_ID_HERE>",
        price=0.50,
        size=5.0,
        side=BUY,
    )
    print(f"  پاسخ: {response}")

    # نمایش سفارش‌های باز
    orders = get_my_orders(auth_client)
    print(f"  تعداد سفارش‌های باز: {len(orders)}")
    """


if __name__ == "__main__":
    main()