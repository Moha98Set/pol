"""
Alerts — telling someone while the edge still exists.
=====================================================

The whole system is built to catch things that are rare and short-lived.
A dashboard nobody happens to be looking at turns those into a record of
what was missed, so the interesting events push rather than wait.

Two rules shape everything here:

1. AN ALERT MUST NEVER BREAK A SCAN. Every failure is caught and logged.
   A dead Telegram token is an annoyance; a scan cycle that dies because
   of one is a data outage.

2. ALERTS ARE RATE LIMITED PER EVENT. A market hovering at the threshold
   can cross it dozens of times a minute, and an alert per crossing
   trains people to ignore the channel.

Configure with, in /etc/polly/polly.env:

    ALERT_TELEGRAM_TOKEN=...
    ALERT_TELEGRAM_CHAT_ID=...

Unconfigured, everything here is a no-op, which is why it can be called
unconditionally from the scan loop.
"""

import logging
import threading
import time
from typing import Optional

import requests

import config

log = logging.getLogger("notify")

_lock = threading.Lock()
_last_sent = {}          # dedupe key -> unix time

SESSION = requests.Session()


def enabled() -> bool:
    return bool(config.ALERT_TELEGRAM_TOKEN and config.ALERT_TELEGRAM_CHAT_ID)


def _should_send(key: str) -> bool:
    """One alert per key per cooldown. The clock is per-process."""
    now = time.time()
    with _lock:
        last = _last_sent.get(key, 0)
        if now - last < config.ALERT_COOLDOWN_SEC:
            return False
        _last_sent[key] = now
        return True


def send(text: str, *, key: Optional[str] = None) -> bool:
    """
    Deliver one alert. Returns whether it went out.

    `key` is the dedupe bucket — normally the event slug, so two different
    markets crossing at the same moment both get through while one market
    flickering does not.
    """
    if not enabled():
        return False
    if key is not None and not _should_send(key):
        return False

    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{config.ALERT_TELEGRAM_TOKEN}"
            f"/sendMessage",
            json={
                "chat_id": config.ALERT_TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=config.ALERT_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("Alert refused by Telegram: %s %s",
                        r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        # Deliberately broad: this is called from inside a scan loop and
        # from a WebSocket handler, and neither may die for a notification.
        log.warning("Alert failed: %s: %s", type(e).__name__, e)
        return False


def _fa_pct(value) -> str:
    return "—" if value is None else f"{value * 100:.3f}%"


def opportunity(opp: dict) -> bool:
    """A stored opportunity: the rarest thing this system produces."""
    if opp.get("net_edge") is None or opp["net_edge"] < config.ALERT_MIN_EDGE:
        return False

    text = (
        f"🟢 <b>فرصت آربیتراژ</b>\n\n"
        f"{opp.get('event_title') or opp.get('event_slug')}\n\n"
        f"لبه خالص: <b>{_fa_pct(opp.get('net_edge'))}</b>\n"
        f"مجموع قیمت: {opp.get('sum_best_asks'):.4f}\n"
        f"کارمزد: {_fa_pct(opp.get('fee_rate'))}\n"
        f"سود تخمینی: ${opp.get('best_profit') or 0:.2f} "
        f"با ${opp.get('best_capital') or 0:.0f}\n"
    )
    if opp.get("url"):
        text += f"\n{opp['url']}"
    return send(text, key=f"opp:{opp.get('event_slug')}")


def window_crossed(win: dict) -> bool:
    """
    A live window just crossed the execute threshold.

    Sent while it is still open, which is the only time the information is
    worth anything — hence no waiting for the window to close.
    """
    text = (
        f"⚡ <b>پنجره باز شد</b>\n\n"
        f"{win.get('event_title') or win.get('event_slug')}\n\n"
        f"لبه: <b>{_fa_pct(win.get('edge'))}</b>\n"
        f"مجموع قیمت: {win.get('sum_best_asks'):.4f}\n"
        f"سمت: {'خرید بله' if win.get('side') == 'yes' else 'خرید نه'}\n"
    )
    if win.get("url"):
        text += f"\n{win['url']}"
    return send(text, key=f"win:{win.get('event_slug')}")


def test() -> bool:
    """Prove the channel works without waiting for a real edge."""
    if not enabled():
        print("هشدار پیکربندی نشده: ALERT_TELEGRAM_TOKEN و "
              "ALERT_TELEGRAM_CHAT_ID را تنظیم کنید.")
        return False
    ok = send("✅ تست هشدار — اتصال داشبورد آربیتراژ برقرار است.")
    print("پیام ارسال شد." if ok else "ارسال نشد؛ لاگ را ببینید.")
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test()
