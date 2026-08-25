"""
Live arbitrage engine — WebSocket order books, sub-second re-evaluation.
========================================================================

Why this exists
---------------
arb_monitor.py scans every 15 minutes. By the time it prints "FOUND", the
book it measured is minutes old and the edge is gone — which is exactly why
the periodic scanner finds near misses and never anything executable. A
mispricing on Polymarket does not last 15 minutes; the question is whether
it lasts 15 *seconds*.

This engine closes that gap:

    REST scan (once)  -> pick a watchlist of events that trade near sum=1
    WebSocket         -> stream every book update for their tokens
    on each update    -> rebuild that event's legs, re-run arbmath
    edge appears      -> open a signal, track it
    edge persists     -> hand it to a callback (the executor)
    edge dies         -> close the signal, store its lifetime

The stored *lifetime* is the point. Before risking money you want the
answer to "how long do these edges actually live?", and only this file can
produce it. Run it for a day in observe-only mode and query the signals
table; if the median duration is 300ms, no retail executor can win and the
honest conclusion is to stop.

Run:
    python live_engine.py                    # observe only, store signals
    python live_engine.py --top 60           # watch more events
    python live_engine.py --min-edge 0.005   # only log edges above 0.5%

Install:
    pip install websockets requests
"""

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

import arbmath
import config
import db as dblib
import notify
import scanner

WS_URL = config.WS_URL
PING_INTERVAL = config.PING_INTERVAL
RECONNECT_DELAY = config.RECONNECT_DELAY
WATCHLIST_REFRESH = config.WATCHLIST_REFRESH
MAX_TOKENS_PER_SOCKET = config.MAX_TOKENS_PER_SOCKET

# An edge must survive this long before it is worth acting on. A book that
# flickers below $1 for one tick is usually a stale quote about to be pulled,
# not a fill you can get.
MIN_SIGNAL_AGE_MS = config.MIN_SIGNAL_AGE_MS

# If a leg's book has not been touched in this long, treat it as unreliable:
# the socket may have silently dropped that token's updates.
STALE_BOOK_SEC = config.STALE_BOOK_SEC

log = logging.getLogger("live_engine")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# Live order book
# =====================================================================


class LiveBook:
    """
    One token's book, both sides, maintained incrementally from the socket.

    The bid side used to be discarded here, on the reasoning that this
    strategy only ever buys. That reasoning was wrong: buying NO on every
    leg is buying, and a NO ask at p is a YES bid at 1-p — the same resting
    order seen from the other side. So the bid side is not "the side we
    never read", it is the entire NO book, and dropping it made half the
    tradeable price space invisible.

    Levels are kept as price->size dicts because `price_change` messages
    address levels by price, and rebuilt into sorted lists only when the
    math actually needs them (lazily, via `levels` / `no_levels`).
    """

    __slots__ = ("token_id", "_asks", "_bids", "_sorted_asks", "_sorted_no",
                 "_asks_dirty", "_bids_dirty",
                 "last_update", "tick_size", "updates")

    def __init__(self, token_id: str):
        self.token_id = token_id
        self._asks: Dict[float, float] = {}
        self._bids: Dict[float, float] = {}
        self._sorted_asks: List[tuple] = []
        self._sorted_no: List[tuple] = []
        self._asks_dirty = True
        self._bids_dirty = True
        self.last_update = 0.0
        self.tick_size = 0.01
        self.updates = 0

    def apply_snapshot(self, asks: list, bids: list = None):
        """Full book replacement — sent on subscribe and after resync."""
        self._asks = {p: s for p, s in arbmath.normalize_asks(asks)}
        self._bids = {p: s for p, s in arbmath.normalize_asks(bids)}
        self._touch(both=True)

    def apply_change(self, price: str, size: str, side: str = "SELL"):
        """
        One incremental level update. size == 0 means the level is gone.

        `side` is the CLOB's own wording: SELL is a resting ask, BUY is a
        resting bid. Defaulted to SELL so older call sites keep their
        meaning rather than silently writing into the wrong side.
        """
        try:
            p = float(price)
            s = float(size)
        except (TypeError, ValueError):
            return
        if p <= 0:
            return

        book = self._bids if str(side).upper() == "BUY" else self._asks
        if s <= 0:
            book.pop(p, None)
        else:
            book[p] = s
        self._touch(bids=str(side).upper() == "BUY")

    def _touch(self, bids: bool = False, both: bool = False):
        if both or bids:
            self._bids_dirty = True
        if both or not bids:
            self._asks_dirty = True
        self.last_update = time.time()
        self.updates += 1

    @property
    def levels(self) -> List[tuple]:
        """The ask side, cheapest first — what a YES basket buys."""
        if self._asks_dirty:
            self._sorted_asks = sorted(self._asks.items())
            self._asks_dirty = False
        return self._sorted_asks

    @property
    def no_levels(self) -> List[tuple]:
        """
        The NO side, cheapest first — the bid side, reflected through 1.

        Note the reflection reverses the order: the highest bid to sell
        into is the cheapest NO to buy.
        """
        if self._bids_dirty:
            self._sorted_no = arbmath.no_asks_from_yes_bids(
                sorted(self._bids.items()))
            self._bids_dirty = False
        return self._sorted_no

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_update) > STALE_BOOK_SEC

    @property
    def ready(self) -> bool:
        return self.last_update > 0 and bool(self._asks)

    @property
    def no_ready(self) -> bool:
        return self.last_update > 0 and bool(self._bids)


# =====================================================================
# Watched event
# =====================================================================


class WatchedEvent:
    """An event whose legs we are tracking live, plus its open signal state."""

    def __init__(self, group: dict):
        event = group["event"]
        self.slug = event.get("slug")
        self.title = event.get("title")
        self.is_binary = group["is_binary"]
        self.fee_rate = group["fee_rate"]
        self.url = f"https://polymarket.com/event/{self.slug}"

        # (outcome_name, token_id) for every leg we must buy
        self.legs: List[tuple] = []
        # token_id -> the opposite token of the same market. Needed because
        # a NO basket is priced off the YES book's bids but must be ORDERED
        # against the NO token; without this the executor would buy the
        # exact opposite of the intended position.
        self.opposite: Dict[str, str] = {}
        # open signal, if an edge is currently live
        self.signal: Optional[dict] = None
        self.last_eval = 0.0

        # Open recording window, if the edge is currently inside the watch
        # band. Independent of `signal`: the band is much wider than the
        # signal threshold, so a window routinely exists with no signal —
        # which is the whole point of recording it.
        self.window: Optional[dict] = None
        self.last_tick_ms = 0.0

    @property
    def token_ids(self) -> List[str]:
        return [t for _n, t in self.legs]

    def build_legs(self, books: Dict[str, LiveBook]) -> Optional[list]:
        """
        Assemble arbmath legs for the YES basket from the live books.

        Returns None unless EVERY leg is ready and fresh. A basket missing
        one leg is not a cheaper arbitrage, it is a directional bet — so a
        single dry or stale leg disqualifies the whole event.
        """
        legs = []
        for name, token_id in self.legs:
            book = books.get(token_id)
            if book is None or not book.ready or book.is_stale:
                return None
            legs.append((name, book.levels))
        return legs

    def build_no_legs(self, books: Dict[str, LiveBook]) -> Optional[list]:
        """
        The same, for the NO basket: every leg's bid side, reflected.

        Only meaningful for multi-outcome events. A binary event's NO leg
        is already its second token, so treating its bid side as a NO book
        would count the same position twice.
        """
        if self.is_binary:
            return None
        legs = []
        for name, token_id in self.legs:
            book = books.get(token_id)
            if book is None or not book.no_ready or book.is_stale:
                return None
            # without the NO token there is nothing to place an order
            # against, so the edge is real but untradeable — treat it as
            # absent rather than emit a signal the executor cannot fill
            if token_id not in self.opposite:
                return None
            legs.append((f"NO {name}", book.no_levels))
        return legs


# =====================================================================
# Engine
# =====================================================================


class LiveEngine:
    def __init__(self, *, top_n: int = None, min_edge: float = None,
                 capitals=None, on_signal: Callable[[dict], None] = None,
                 store: bool = True):
        self.top_n = config.LIVE_TOP_N if top_n is None else top_n
        self.min_edge = config.LIVE_MIN_EDGE if min_edge is None else min_edge
        self.capitals = capitals or config.TEST_CAPITALS
        self.on_signal = on_signal
        self.store = store

        self.books: Dict[str, LiveBook] = {}
        self.events: Dict[str, WatchedEvent] = {}     # slug -> event
        self.token_to_events: Dict[str, List[str]] = {}  # token -> [slug]
        self.db = dblib.connect() if store else None

        self._resubscribe = asyncio.Event()
        self.stats = {"updates": 0, "evals": 0, "signals": 0,
                      "windows": 0, "ticks": 0}

        self.record = config.LIVE_RECORD and store
        # Ticks are buffered and flushed in batches: a commit per book
        # update would put fsync inside the WebSocket read loop.
        self._tick_buffer: List[dict] = []

        if self.record and self.db is not None:
            orphans = dblib.close_orphan_windows(self.db)
            if orphans:
                log.info("Closed %d window(s) left open by a previous run",
                         orphans)

    # -----------------------------------------------------------------
    # Watchlist
    # -----------------------------------------------------------------

    def build_watchlist(self):
        """
        Pick which events to stream.

        We cannot subscribe to everything, so we spend the socket budget on
        events already trading near sum_asks = 1 — those are the only ones
        one bad quote away from arbitrage. Events at sum = 1.15 will not
        cross to below 1.0 between two refreshes.
        """
        log.info("Building watchlist (REST scan)...")
        events = scanner.fetch_all_events()
        groups = [g for g in (scanner.prefilter_event(e) for e in events) if g]
        log.info("%d events passed pre-filters", len(groups))

        scored = []
        for group in groups:
            markets = group["markets"]
            legs = []
            opposite = {}
            for m in markets:
                token_ids = scanner.parse_token_ids(m)
                if not token_ids:
                    legs = []
                    break
                if group["is_binary"]:
                    if len(token_ids) < 2:
                        legs = []
                        break
                    legs = [("Yes", token_ids[0]), ("No", token_ids[1])]
                    break
                name = m.get("groupItemTitle") or (m.get("question") or "")[:40]
                legs.append((name, token_ids[0]))
                if len(token_ids) >= 2:
                    opposite[token_ids[0]] = token_ids[1]

            if len(legs) < 2:
                continue

            # cheap proximity score from the REST snapshot: how far the
            # current sum of asks sits from the arbitrage boundary
            books = scanner.fetch_order_books([t for _n, t in legs])
            asks_by_leg = [(n, scanner.get_valid_asks(books.get(t)))
                           for n, t in legs]
            if any(not a for _n, a in asks_by_leg):
                continue
            sum_asks = sum(arbmath.best_ask(a) for _n, a in asks_by_leg)
            if not (scanner.SUM_ASKS_MIN <= sum_asks <= 1.25):
                continue

            we = WatchedEvent(group)
            we.legs = legs
            we.opposite = opposite
            scored.append((sum_asks, we))

        scored.sort(key=lambda x: x[0])  # closest to (and below) 1.0 first
        chosen = [we for _s, we in scored[:self.top_n]]

        self.events = {we.slug: we for we in chosen}
        self.token_to_events = {}
        for we in chosen:
            for token_id in we.token_ids:
                self.token_to_events.setdefault(token_id, []).append(we.slug)
                self.books.setdefault(token_id, LiveBook(token_id))

        # drop books for tokens no longer watched
        for token_id in list(self.books):
            if token_id not in self.token_to_events:
                del self.books[token_id]

        log.info("Watching %d events / %d tokens (best sum_asks=%.4f)",
                 len(chosen), len(self.token_to_events),
                 scored[0][0] if scored else float("nan"))

    # -----------------------------------------------------------------
    # Message handling
    # -----------------------------------------------------------------

    def handle_message(self, msg: dict):
        event_type = msg.get("event_type")
        token_id = msg.get("asset_id") or msg.get("market")

        if event_type == "book":
            book = self.books.get(token_id)
            if book is None:
                return
            book.apply_snapshot(msg.get("asks"), msg.get("bids"))
            self.stats["updates"] += 1
            self._reevaluate(token_id)

        elif event_type == "price_change":
            book = self.books.get(token_id)
            if book is None:
                return
            touched = False
            for change in msg.get("changes", []):
                # SELL is a resting ask (what a YES basket buys); BUY is a
                # resting bid (which IS the NO book, reflected). Both are
                # tracked now — dropping BUY made the NO side invisible.
                side = (change.get("side") or "").upper()
                if side in ("SELL", "ASK"):
                    book.apply_change(change.get("price"),
                                      change.get("size"), "SELL")
                elif side in ("BUY", "BID"):
                    book.apply_change(change.get("price"),
                                      change.get("size"), "BUY")
                else:
                    continue
                touched = True
            if touched:
                self.stats["updates"] += 1
                self._reevaluate(token_id)

        elif event_type == "tick_size_change":
            book = self.books.get(token_id)
            if book is not None:
                try:
                    # the executor rounds limit prices to this; a stale tick
                    # size gets orders rejected outright
                    book.tick_size = float(msg.get("new_tick_size"))
                except (TypeError, ValueError):
                    pass

        elif event_type == "market_resolved":
            # a resolved leg can print absurd prices; stop watching it
            for slug in self.token_to_events.get(token_id, []):
                self.events.pop(slug, None)
            log.info("Market resolved, dropped from watchlist: %s", token_id)

    # -----------------------------------------------------------------
    # Evaluation + signal lifecycle
    # -----------------------------------------------------------------

    def _reevaluate(self, token_id: str):
        for slug in self.token_to_events.get(token_id, []):
            watched = self.events.get(slug)
            if watched is not None:
                self._evaluate_event(watched)

    def _evaluate_event(self, watched: WatchedEvent):
        """
        Check both baskets: buy the event for less than $1, or sell it for
        more. They can never both be available — bids never exceed asks —
        so at most one of these produces a signal.
        """
        self.stats["evals"] += 1
        watched.last_eval = time.time()

        candidates = []

        legs = watched.build_legs(self.books)
        if legs is not None:
            result = arbmath.evaluate_basket(legs, watched.fee_rate,
                                             self.capitals)
            candidates.append(("yes", result, result["net_edge"]))

        if config.SCAN_NO_SIDE:
            no_legs = watched.build_no_legs(self.books)
            if no_legs is not None:
                n = len(no_legs)
                no_result = arbmath.evaluate_basket(
                    no_legs, watched.fee_rate, self.capitals,
                    payout_per_basket=n - 1)
                # compared per dollar: a NO basket's edge is measured
                # against an N-1 payout and costs about that much to hold
                candidates.append(("no", no_result,
                                   no_result["net_edge_per_dollar"]))

        if not candidates:
            self._close_signal(watched, reason="leg_unavailable")
            return

        side, result, edge = max(
            candidates, key=lambda c: c[2] if c[2] is not None else -9e9)

        # Record before the threshold test, not after. Everything below
        # min_edge used to be discarded here, and that discarded set is
        # exactly what a ten-minute window looks like.
        self._record_edge(watched, side, result, edge)

        if edge is None or edge < self.min_edge or not result["best"]:
            self._close_signal(watched, reason="edge_gone")
            return

        result = dict(result, side=side, comparable_edge=edge)
        self._open_or_update_signal(watched, result)

    # -----------------------------------------------------------------
    # Edge recording
    # -----------------------------------------------------------------

    def _record_edge(self, watched: WatchedEvent, side: str, result: dict,
                     edge: Optional[float]):
        """
        Track the edge's shape while it sits inside the watch band.

        A window opens when the edge first enters the band and closes when
        it leaves — so a market that dips for ten minutes and recovers
        becomes one row with a duration, a depth and a time of day, which
        is what makes it comparable to the next one.
        """
        if not self.record or self.db is None:
            return

        in_band = edge is not None and edge >= config.LIVE_RECORD_MIN_EDGE
        now = time.time()
        now_ms = now * 1000

        if not in_band:
            self._close_window(watched, now, edge)
            return

        sum_asks = result.get("sum_best_asks")
        crossed = edge >= self.min_edge

        # Depth, not just price. A ten-minute window that was never
        # fillable past five dollars charts identically to one worth
        # taking, and "you cannot fill it" is already the most common
        # reason a visible edge turns out not to be real.
        best = result.get("best") or {}
        # real_cost, not capital: `capital` is the ladder rung that was
        # requested, and it comes back unchanged whether the book could
        # absorb it or not. `real_cost` is what the book actually took —
        # $2.97 on a three-share leg against the same $100 request. Storing
        # `capital` would have made every window look equally deep, which
        # is the exact confusion this column exists to remove.
        cap = best.get("real_cost")
        prof = best.get("profit")

        if watched.window is None:
            stamp = utcnow()
            window_id = dblib.open_edge_window(self.db, {
                "event_slug": watched.slug,
                "event_title": watched.title,
                "side": side,
                "num_outcomes": len(watched.legs),
                "fee_rate": watched.fee_rate,
                "payout": 1.0 if side == "yes" else max(len(watched.legs) - 1, 1),
                "opened_at": stamp,
                "edge": edge,
                "sum_best_asks": sum_asks,
                "fillable_capital": cap,
                "fillable_profit": prof,
                "crossed": crossed,
                "url": watched.url,
            })
            watched.window = {
                "id": window_id, "opened": now, "ticks": 0,
                "best_edge": edge, "best_sum_asks": sum_asks,
                "best_capital": cap, "best_profit": prof,
                "best_at": stamp, "crossed": crossed, "dirty": False,
            }
            watched.last_tick_ms = 0.0
            self.stats["windows"] += 1

        win = watched.window
        if edge > win["best_edge"]:
            win.update(best_edge=edge, best_sum_asks=sum_asks,
                       best_capital=cap, best_profit=prof,
                       best_at=utcnow(), dirty=True)
        if crossed and not win["crossed"]:
            win["crossed"] = True
            win["dirty"] = True
            # Sent while the window is still open. Waiting for it to close
            # would mean every alert describes something already gone.
            notify.window_crossed({
                "event_slug": watched.slug, "event_title": watched.title,
                "edge": edge, "sum_best_asks": sum_asks, "side": side,
                "url": watched.url,
            })

        # Rate limit: several evaluations a second carry no more shape than
        # one, and the row count is the only thing that grows.
        if now_ms - watched.last_tick_ms < config.LIVE_TICK_MIN_INTERVAL_MS:
            return
        watched.last_tick_ms = now_ms

        win["ticks"] += 1
        win["dirty"] = True
        self.stats["ticks"] += 1
        self._tick_buffer.append({
            "window_id": win["id"],
            "ts_ms": int(now_ms),
            "recorded_at": utcnow(),
            "sum_best_asks": sum_asks,
            "net_edge": result.get("net_edge"),
            "comparable_edge": edge,
            "fillable_capital": cap,
            "fillable_profit": prof,
        })

        if len(self._tick_buffer) >= 50:
            self._flush_ticks()

    def _close_window(self, watched: WatchedEvent, now: float,
                      edge: Optional[float]):
        win = watched.window
        if win is None:
            return
        watched.window = None

        duration_ms = int((now - win["opened"]) * 1000)
        self._flush_ticks()

        # A lone evaluation that grazed the band is noise. Drop the row
        # rather than leaving a zero-length episode to skew every average
        # of "how long do these last".
        if duration_ms < config.MIN_WINDOW_MS and not win["crossed"]:
            self.db.execute("DELETE FROM edge_ticks WHERE window_id = ?",
                            (win["id"],))
            self.db.execute("DELETE FROM edge_windows WHERE id = ?",
                            (win["id"],))
            self.db.commit()
            self.stats["windows"] -= 1
            return

        dblib.update_edge_window(self.db, win["id"], {
            "ticks": win["ticks"], "best_edge": win["best_edge"],
            "best_sum_asks": win["best_sum_asks"], "best_at": win["best_at"],
            "best_capital": win.get("best_capital"),
            "best_profit": win.get("best_profit"),
            "crossed": win["crossed"],
        })
        dblib.close_edge_window(
            self.db, win["id"], closed_at=utcnow(),
            duration_ms=duration_ms, closed_edge=edge, ticks=win["ticks"])

        log.info("window closed | %s | %.1f min | best edge %.3f%% | %s",
                 (watched.title or watched.slug)[:44], duration_ms / 60000,
                 (win["best_edge"] or 0) * 100,
                 "SIGNAL" if win["crossed"] else "near only")

    def _flush_ticks(self):
        if not self._tick_buffer:
            return
        dblib.save_edge_ticks(self.db, self._tick_buffer)
        self._tick_buffer = []

    def _open_or_update_signal(self, watched: WatchedEvent, result: dict):
        now = time.time()
        best = result["best"]

        # `comparable_edge` is per dollar of capital, so a YES signal and a
        # NO signal on different events rank against each other honestly
        edge = result.get("comparable_edge", result["net_edge"])
        side = result.get("side", "yes")

        if watched.signal is None:
            watched.signal = {
                "event_slug": watched.slug,
                "event_title": watched.title,
                "market_type": ("binary" if watched.is_binary
                                else "multi_no" if side == "no" else "multi"),
                "side": side,
                "num_outcomes": result["num_legs"],
                "payout_per_basket": result.get("payout_per_basket", 1.0),
                "fee_rate": watched.fee_rate,
                "first_seen": utcnow(),
                "first_seen_ts": now,
                "updates": 0,
                "best_net_edge": edge,
                "best_sum_asks": result["sum_best_asks"],
                "peak_profit": best["profit"],
                "peak_capital": best["capital"],
                "legs_detail": self._legs_detail(watched, result),
                "curve": result["curve"],
                "url": watched.url,
                "acted_on": False,
            }
            self.stats["signals"] += 1
            log.info("EDGE OPEN  | %-3s | %s | net=%.3f%%/$ sum=%.4f "
                     "| $%.2f @ $%.0f",
                     side.upper(), (watched.title or "")[:42], edge * 100,
                     result["sum_best_asks"], best["profit"], best["capital"])

        signal = watched.signal
        signal["updates"] += 1
        signal["last_seen"] = utcnow()
        signal["last_seen_ts"] = now

        # A signal that flips sides is a different trade on the same event.
        # Closing and reopening keeps each side's lifetime measured
        # separately, which is the whole point of the signals table.
        if signal.get("side") != side:
            self._close_signal(watched, reason="side_flip")
            self._open_or_update_signal(watched, result)
            return

        # keep the best moment of the signal's life, not the latest
        if edge > signal["best_net_edge"]:
            signal["best_net_edge"] = edge
            signal["best_sum_asks"] = result["sum_best_asks"]
            signal["peak_profit"] = best["profit"]
            signal["peak_capital"] = best["capital"]
            signal["legs_detail"] = self._legs_detail(watched, result)
            signal["curve"] = result["curve"]

        age_ms = (now - signal["first_seen_ts"]) * 1000
        if (self.on_signal and not signal["acted_on"]
                and age_ms >= MIN_SIGNAL_AGE_MS):
            signal["acted_on"] = True
            payload = dict(signal, age_ms=age_ms, live_result=result,
                           legs=signal["legs_detail"])
            try:
                self.on_signal(payload)
            except Exception as e:
                log.error("on_signal callback failed: %s", e, exc_info=True)

    def _close_signal(self, watched: WatchedEvent, reason: str):
        signal = watched.signal
        if signal is None:
            return
        watched.signal = None

        duration_ms = int((signal["last_seen_ts"] - signal["first_seen_ts"]) * 1000)
        signal["duration_ms"] = duration_ms

        log.info("EDGE CLOSE | %s | lived %dms over %d updates | best=%.3f%% (%s)",
                 (watched.title or "")[:45], duration_ms, signal["updates"],
                 signal["best_net_edge"] * 100, reason)

        if self.db is not None:
            record = {k: v for k, v in signal.items()
                      if not k.endswith("_ts") and k != "live_result"}
            dblib.save_signal(self.db, record)

    def _legs_detail(self, watched: WatchedEvent, result: dict) -> list:
        """
        Per-leg detail for the side that actually produced the signal.

        The side matters here beyond labelling: the executor reads these
        rows to place orders, and a NO signal quoting YES depth would send
        it to buy the wrong side of the book.
        """
        side = result.get("side", "yes")
        detail = []
        for i, (name, token_id) in enumerate(watched.legs):
            book = self.books.get(token_id)
            levels = (book.no_levels if side == "no" and book
                      else book.levels if book else [])
            # priced off the YES book's bids, but ORDERED against the NO
            # token — naming the YES token here would have the executor
            # buy the exact opposite of the intended position
            order_token = (watched.opposite.get(token_id) if side == "no"
                           else token_id)
            detail.append({
                "outcome": f"NO {name}" if side == "no" else name,
                "token_id": order_token,
                "quoted_from_token_id": token_id,
                "side": "NO" if side == "no" else "YES",
                "best_ask": result["leg_best_asks"][i]
                            if i < len(result.get("leg_best_asks", [])) else None,
                "tick_size": book.tick_size if book else 0.01,
                "depth_usd": arbmath.depth_usd(levels),
            })
        return detail

    # -----------------------------------------------------------------
    # Socket loop
    # -----------------------------------------------------------------

    async def _pinger(self, ws):
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
            except ConnectionClosed:
                return

    async def _stream(self):
        token_ids = list(self.token_to_events)[:MAX_TOKENS_PER_SOCKET]
        if not token_ids:
            log.warning("Nothing to watch; retrying after refresh")
            await asyncio.sleep(30)
            return

        async with websockets.connect(WS_URL, max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "assets_ids": token_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }))
            log.info("Subscribed to %d tokens", len(token_ids))

            ping_task = asyncio.create_task(self._pinger(ws))
            try:
                async for raw in ws:
                    if raw == "PONG":
                        continue
                    if self._resubscribe.is_set():
                        # watchlist changed — drop the socket and rebuild it
                        return
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    for msg in (data if isinstance(data, list) else [data]):
                        if isinstance(msg, dict):
                            self.handle_message(msg)
            finally:
                ping_task.cancel()

    async def _refresher(self):
        """Rebuild the watchlist periodically; new events drift into range."""
        while True:
            await asyncio.sleep(WATCHLIST_REFRESH)
            try:
                await asyncio.to_thread(self.build_watchlist)
                self._resubscribe.set()
            except Exception as e:
                log.error("Watchlist refresh failed: %s", e)

    async def _reporter(self):
        while True:
            await asyncio.sleep(60)
            open_edges = sum(1 for e in self.events.values() if e.signal)
            open_windows = sum(1 for e in self.events.values() if e.window)
            # A window can stay inside the band for a long time without
            # producing a tick batch big enough to flush, so the buffer is
            # drained on this timer too — otherwise a slow episode is
            # invisible until it ends.
            if self.record:
                self._flush_ticks()
            log.info("stats | %d updates | %d evals | %d signals | %d open"
                     " | %d windows (%d live) | %d ticks",
                     self.stats["updates"], self.stats["evals"],
                     self.stats["signals"], open_edges,
                     self.stats["windows"], open_windows,
                     self.stats["ticks"])

    async def _pruner(self):
        """Age out tick rows. Windows are kept — they are the record."""
        while True:
            await asyncio.sleep(6 * 3600)
            if not self.record:
                continue
            try:
                dropped = await asyncio.to_thread(
                    dblib.prune_edge_ticks, self.db,
                    config.TICK_RETENTION_DAYS)
                if dropped:
                    log.info("Pruned %d tick(s) older than %d days",
                             dropped, config.TICK_RETENTION_DAYS)
            except Exception as e:
                log.error("Tick pruning failed: %s", e)

    async def run(self):
        await asyncio.to_thread(self.build_watchlist)

        refresher = asyncio.create_task(self._refresher())
        reporter = asyncio.create_task(self._reporter())
        pruner = asyncio.create_task(self._pruner())

        try:
            while True:
                try:
                    await self._stream()
                except (ConnectionClosed, OSError) as e:
                    log.warning("Socket dropped (%s); reconnecting in %ds",
                                e, RECONNECT_DELAY)
                    await asyncio.sleep(RECONNECT_DELAY)
                except Exception as e:
                    log.error("Stream error: %s", e, exc_info=True)
                    await asyncio.sleep(RECONNECT_DELAY)
                finally:
                    # every reconnect starts from a fresh snapshot, so any
                    # open signal was measured on a book we no longer trust
                    now = time.time()
                    for watched in self.events.values():
                        self._close_signal(watched, reason="disconnect")
                        # Close the window too: the gap in coverage is real,
                        # and an episode spanning it would report a duration
                        # that includes time nobody was watching.
                        self._close_window(watched, now, None)
                    self._resubscribe.clear()
        finally:
            refresher.cancel()
            reporter.cancel()
            pruner.cancel()
            if self.record:
                for watched in self.events.values():
                    self._close_window(watched, time.time(), None)
                self._flush_ticks()


# =====================================================================
# CLI
# =====================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Live WebSocket arbitrage engine (observe-only)")
    parser.add_argument("--top", type=int, default=config.LIVE_TOP_N,
                        help="how many events to stream")
    parser.add_argument("--min-edge", type=float, default=config.LIVE_MIN_EDGE,
                        help="minimum net edge to open a signal")
    parser.add_argument("--no-store", action="store_true",
                        help="do not write signals to the database")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    engine = LiveEngine(top_n=args.top, min_edge=args.min_edge,
                        store=not args.no_store)

    log.info("Live engine starting — OBSERVE ONLY, no orders will be placed.")
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        log.info("Stopped. %d signals recorded this session.",
                 engine.stats["signals"])


if __name__ == "__main__":
    main()
