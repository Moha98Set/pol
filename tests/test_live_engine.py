"""
Tests for the live engine.

This module had none, which was the largest untested surface in the
project — and it is the one that decides, in real time, whether to hand a
trade to the executor. The book-maintenance code is the risky part: it is
incremental, so a single mishandled update silently corrupts the book and
every price derived from it stays wrong until the next full snapshot.

Nothing here opens a socket. Messages are fed to `handle_message` exactly
as the CLOB sends them, which is also the seam that makes the engine
testable at all.
"""

import time

import pytest

import config
import live_engine
from live_engine import LiveBook, LiveEngine, WatchedEvent


def levels(*pairs):
    """API-shaped price levels."""
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


@pytest.fixture
def engine():
    """An engine with no database and no network."""
    return LiveEngine(store=False, capitals=[100], min_edge=0.003)


def multi_group(slug="election", n=3, fee_rate=0.0):
    return {
        "event": {"slug": slug, "title": f"Event {slug}"},
        "is_binary": False,
        "fee_rate": fee_rate,
        "markets": [],
    }


def watch(engine, slug="election", n=3, fee_rate=0.0, binary=False,
          with_opposite=True):
    """Register an event with n legs and give each an empty book."""
    group = multi_group(slug, n, fee_rate)
    group["is_binary"] = binary
    watched = WatchedEvent(group)
    watched.legs = ([("Yes", f"{slug}-y"), ("No", f"{slug}-n")] if binary
                    else [(f"Leg{i}", f"{slug}-{i}") for i in range(n)])
    if with_opposite and not binary:
        # every market has two tokens; the NO one is what a NO basket buys
        watched.opposite = {f"{slug}-{i}": f"{slug}-{i}-no" for i in range(n)}
    engine.events[slug] = watched
    for _name, token in watched.legs:
        engine.books[token] = LiveBook(token)
        engine.token_to_events.setdefault(token, []).append(slug)
    return watched


# =====================================================================
# LiveBook — both sides
# =====================================================================


def test_a_snapshot_loads_both_sides():
    book = LiveBook("t")
    book.apply_snapshot(asks=levels((0.40, 100)), bids=levels((0.30, 50)))

    assert book.levels == [(0.40, 100.0)]
    assert book.no_levels == [(0.70, 50.0)]      # 1 - 0.30
    assert book.ready and book.no_ready


def test_a_snapshot_without_bids_leaves_the_no_side_empty():
    book = LiveBook("t")
    book.apply_snapshot(asks=levels((0.40, 100)))
    assert book.ready
    assert not book.no_ready
    assert book.no_levels == []


def test_a_snapshot_replaces_rather_than_merges():
    """
    Snapshots arrive after a resync, precisely when the incremental state
    is suspect. Merging would preserve the corruption it exists to fix.
    """
    book = LiveBook("t")
    book.apply_snapshot(levels((0.40, 100), (0.45, 100)), levels((0.30, 10)))
    book.apply_snapshot(levels((0.50, 10)), levels((0.20, 5)))

    assert book.levels == [(0.50, 10.0)]
    assert book.no_levels == [(0.80, 5.0)]


def test_an_ask_change_does_not_touch_the_bid_side():
    """
    The bug this guards: routing every update into one dict. It would make
    the NO book drift out of sync with reality one message at a time, and
    nothing would look broken until a trade was placed on it.
    """
    book = LiveBook("t")
    book.apply_snapshot(levels((0.40, 100)), levels((0.30, 50)))

    book.apply_change("0.42", "10", "SELL")

    assert book.levels == [(0.40, 100.0), (0.42, 10.0)]
    assert book.no_levels == [(0.70, 50.0)]


def test_a_bid_change_does_not_touch_the_ask_side():
    book = LiveBook("t")
    book.apply_snapshot(levels((0.40, 100)), levels((0.30, 50)))

    book.apply_change("0.35", "20", "BUY")

    assert book.levels == [(0.40, 100.0)]
    assert book.no_levels == [(0.65, 20.0), (0.70, 50.0)]


def test_a_zero_size_removes_the_level_on_the_right_side():
    book = LiveBook("t")
    book.apply_snapshot(levels((0.40, 100), (0.45, 50)),
                        levels((0.30, 10), (0.25, 20)))

    book.apply_change("0.45", "0", "SELL")
    book.apply_change("0.30", "0", "BUY")

    assert book.levels == [(0.40, 100.0)]
    assert book.no_levels == [(0.75, 20.0)]      # 0.25 bid survives


def test_the_default_side_is_the_ask():
    """Old call sites passed no side; they meant asks."""
    book = LiveBook("t")
    book.apply_change("0.40", "10")
    assert book.levels == [(0.40, 10.0)]
    assert book.no_levels == []


def test_malformed_changes_are_ignored():
    book = LiveBook("t")
    book.apply_snapshot(levels((0.40, 100)), levels((0.30, 50)))

    book.apply_change("abc", "10", "SELL")
    book.apply_change(None, "10", "BUY")
    book.apply_change("-0.5", "10", "SELL")

    assert book.levels == [(0.40, 100.0)]
    assert book.no_levels == [(0.70, 50.0)]


def test_the_no_side_is_ordered_cheapest_first():
    """
    Reflection reverses order. If the sort were skipped, every NO basket
    would be priced off the worst level in the book instead of the best.
    """
    book = LiveBook("t")
    book.apply_snapshot([], levels((0.10, 5), (0.30, 10), (0.20, 7)))
    assert book.no_levels == [(0.70, 10.0), (0.80, 7.0), (0.90, 5.0)]


def test_levels_are_cached_until_the_book_changes():
    book = LiveBook("t")
    book.apply_snapshot(levels((0.40, 100)), levels((0.30, 50)))

    assert book.levels is book.levels               # cached
    book.apply_change("0.41", "5", "SELL")
    assert book.levels == [(0.40, 100.0), (0.41, 5.0)]


def test_a_book_with_no_updates_is_not_ready():
    book = LiveBook("t")
    assert not book.ready and not book.no_ready


def test_staleness_is_measured_from_the_last_update(monkeypatch):
    book = LiveBook("t")
    book.apply_snapshot(levels((0.40, 100)))
    assert not book.is_stale

    monkeypatch.setattr(live_engine, "STALE_BOOK_SEC", 0)
    book.last_update = time.time() - 1
    assert book.is_stale


# =====================================================================
# Message handling
# =====================================================================


def test_a_book_message_fills_both_sides(engine):
    watch(engine, n=2)
    engine.handle_message({
        "event_type": "book", "asset_id": "election-0",
        "asks": levels((0.40, 100)), "bids": levels((0.30, 50)),
    })

    book = engine.books["election-0"]
    assert book.levels == [(0.40, 100.0)]
    assert book.no_levels == [(0.70, 50.0)]


def test_price_changes_are_routed_by_side(engine):
    watch(engine, n=2)
    engine.handle_message({
        "event_type": "price_change", "asset_id": "election-0",
        "changes": [
            {"side": "SELL", "price": "0.40", "size": "100"},
            {"side": "BUY", "price": "0.30", "size": "50"},
        ],
    })

    book = engine.books["election-0"]
    assert book.levels == [(0.40, 100.0)]
    assert book.no_levels == [(0.70, 50.0)]


def test_an_unknown_side_is_skipped(engine):
    watch(engine, n=2)
    engine.handle_message({
        "event_type": "price_change", "asset_id": "election-0",
        "changes": [{"side": "SOMETHING", "price": "0.4", "size": "1"}],
    })
    assert engine.books["election-0"].levels == []


def test_messages_for_unknown_tokens_are_ignored(engine):
    engine.handle_message({"event_type": "book", "asset_id": "nope",
                           "asks": levels((0.4, 10))})   # must not raise


def test_a_resolved_market_drops_off_the_watchlist(engine):
    watch(engine, slug="gone", n=2)
    engine.handle_message({"event_type": "market_resolved",
                           "asset_id": "gone-0"})
    assert "gone" not in engine.events


def test_a_tick_size_change_is_recorded(engine):
    watch(engine, n=2)
    engine.handle_message({"event_type": "tick_size_change",
                           "asset_id": "election-0",
                           "new_tick_size": "0.001"})
    assert engine.books["election-0"].tick_size == 0.001


# =====================================================================
# Two-sided evaluation
# =====================================================================


def feed(engine, slug, asks_by_leg, bids_by_leg=None):
    for i, ask in enumerate(asks_by_leg):
        bid = bids_by_leg[i] if bids_by_leg else None
        engine.handle_message({
            "event_type": "book", "asset_id": f"{slug}-{i}",
            "asks": levels(ask) if ask else [],
            "bids": levels(bid) if bid else [],
        })


def test_a_yes_edge_opens_a_yes_signal(engine):
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])

    assert watched.signal is not None
    assert watched.signal["side"] == "yes"
    assert watched.signal["market_type"] == "multi"


def test_a_no_edge_opens_a_no_signal(engine):
    """
    YES asks sum to 1.20 — nothing to buy. YES bids sum to 1.05, so the
    basket can be sold for more than a dollar: NO asks sum to 1.95 against
    a payout of 2. Invisible to the old one-sided engine.
    """
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.40, 500), (0.40, 500), (0.40, 500)],
         bids_by_leg=[(0.35, 500), (0.35, 500), (0.35, 500)])

    assert watched.signal is not None
    assert watched.signal["side"] == "no"
    assert watched.signal["market_type"] == "multi_no"
    assert watched.signal["payout_per_basket"] == 2


def test_the_no_side_can_be_disabled(engine, monkeypatch):
    monkeypatch.setattr(config, "SCAN_NO_SIDE", False)
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.40, 500), (0.40, 500), (0.40, 500)],
         bids_by_leg=[(0.35, 500), (0.35, 500), (0.35, 500)])

    assert watched.signal is None


def test_no_signal_when_neither_side_has_an_edge(engine):
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.40, 500), (0.40, 500), (0.40, 500)],
         bids_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])

    assert watched.signal is None


def test_a_binary_event_never_uses_the_mirror(engine):
    """
    A binary event's NO leg is already its second token. Reading its bid
    side as a NO book would count the same position twice.
    """
    watched = watch(engine, slug="bin", binary=True)
    assert watched.build_no_legs(engine.books) is None


def test_one_dry_leg_blocks_the_whole_basket(engine):
    watched = watch(engine, n=3)
    feed(engine, "election", asks_by_leg=[(0.30, 500), (0.30, 500), None])
    assert watched.signal is None


def test_a_signal_closes_when_the_edge_disappears(engine):
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])
    assert watched.signal is not None

    engine.handle_message({
        "event_type": "price_change", "asset_id": "election-0",
        "changes": [{"side": "SELL", "price": "0.30", "size": "0"}],
    })
    assert watched.signal is None


def test_a_signal_keeps_its_best_moment_not_its_latest(engine):
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.20, 500), (0.20, 500), (0.20, 500)])
    peak = watched.signal["best_net_edge"]

    # the edge narrows but stays positive
    engine.handle_message({
        "event_type": "book", "asset_id": "election-0",
        "asks": levels((0.30, 500)), "bids": [],
    })

    assert watched.signal is not None
    assert watched.signal["best_net_edge"] == peak


def test_switching_sides_closes_and_reopens_the_signal(engine):
    """
    A YES edge and a NO edge on the same event are two different trades.
    Letting one signal morph into the other would blend their lifetimes,
    and lifetime is the number the signals table exists to measure.
    """
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])
    assert watched.signal["side"] == "yes"
    original = watched.signal

    feed(engine, "election",
         asks_by_leg=[(0.40, 500), (0.40, 500), (0.40, 500)],
         bids_by_leg=[(0.35, 500), (0.35, 500), (0.35, 500)])

    assert watched.signal["side"] == "no"
    assert watched.signal is not original
    # a fresh signal, not the old one carrying its history across
    assert watched.signal["updates"] == 1


# =====================================================================
# Leg detail handed to the executor
# =====================================================================


def test_leg_detail_reports_the_side_that_produced_the_signal(engine):
    """
    The executor places orders from these rows. A NO signal quoting YES
    depth would send it to buy the wrong side of the book.
    """
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.40, 500), (0.40, 500), (0.40, 500)],
         bids_by_leg=[(0.35, 500), (0.35, 500), (0.35, 500)])

    legs = watched.signal["legs_detail"]
    assert all(leg["side"] == "NO" for leg in legs)
    assert all(leg["outcome"].startswith("NO ") for leg in legs)
    # depth must come from the NO side: 500 shares at 0.65
    assert legs[0]["depth_usd"] == pytest.approx(0.65 * 500)


def test_a_no_signal_names_the_no_token_to_order(engine):
    """
    The money bug. A NO basket is PRICED off the YES book's bid side but
    must be ORDERED against the NO token. Naming the YES token here would
    make the executor buy the exact opposite of the intended position —
    every leg wrong, silently, with real funds.
    """
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.40, 500), (0.40, 500), (0.40, 500)],
         bids_by_leg=[(0.35, 500), (0.35, 500), (0.35, 500)])

    legs = watched.signal["legs_detail"]
    assert [leg["token_id"] for leg in legs] == [
        "election-0-no", "election-1-no", "election-2-no"]
    # the YES token is kept, but only as provenance for the quote
    assert [leg["quoted_from_token_id"] for leg in legs] == [
        "election-0", "election-1", "election-2"]


def test_a_yes_signal_orders_the_token_it_was_quoted_from(engine):
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])

    legs = watched.signal["legs_detail"]
    assert [leg["token_id"] for leg in legs] == [
        "election-0", "election-1", "election-2"]


def test_no_signal_without_a_known_no_token(engine):
    """
    An edge that cannot be ordered is not an opportunity. Better to stay
    silent than to hand the executor a leg it has no token for.
    """
    watched = watch(engine, n=3, with_opposite=False)
    feed(engine, "election",
         asks_by_leg=[(0.40, 500), (0.40, 500), (0.40, 500)],
         bids_by_leg=[(0.35, 500), (0.35, 500), (0.35, 500)])

    assert watched.build_no_legs(engine.books) is None
    assert watched.signal is None


def test_leg_detail_for_a_yes_signal_quotes_the_ask_side(engine):
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])

    legs = watched.signal["legs_detail"]
    assert all(leg["side"] == "YES" for leg in legs)
    assert legs[0]["depth_usd"] == pytest.approx(0.30 * 500)


def test_the_signal_callback_fires_once_past_the_age_gate(engine, monkeypatch):
    monkeypatch.setattr(live_engine, "MIN_SIGNAL_AGE_MS", 0)
    seen = []
    engine.on_signal = seen.append

    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])
    engine.handle_message({
        "event_type": "price_change", "asset_id": "election-1",
        "changes": [{"side": "SELL", "price": "0.29", "size": "500"}],
    })

    assert len(seen) == 1, "a signal must be handed over once, not per tick"


def test_a_failing_callback_does_not_take_down_the_engine(engine, monkeypatch):
    monkeypatch.setattr(live_engine, "MIN_SIGNAL_AGE_MS", 0)

    def boom(_payload):
        raise RuntimeError("executor exploded")

    engine.on_signal = boom
    watched = watch(engine, n=3)
    feed(engine, "election",
         asks_by_leg=[(0.30, 500), (0.30, 500), (0.30, 500)])

    assert watched.signal is not None      # engine survived


# =====================================================================
# Edge recording — the shape of a window, not just its peak
# =====================================================================


@pytest.fixture
def recording_engine(tmp_path, monkeypatch):
    """
    An engine that records to a scratch database.

    min_edge stays at the production 0.003 while the record band is far
    below it, because the episodes worth capturing are precisely the ones
    that never cross min_edge.
    """
    import db as dblib
    monkeypatch.setattr(config, "LIVE_RECORD", True)
    monkeypatch.setattr(config, "LIVE_RECORD_MIN_EDGE", -0.02)
    monkeypatch.setattr(config, "LIVE_TICK_MIN_INTERVAL_MS", 0)
    monkeypatch.setattr(config, "MIN_WINDOW_MS", 0)

    eng = LiveEngine(store=False, capitals=[100], min_edge=0.003)
    eng.db = dblib.connect(tmp_path / "live.db")
    eng.record = True
    yield eng
    eng.db.close()


def push(engine, watched, edge, sum_asks=1.0):
    """Push one evaluation result through the recorder."""
    engine._record_edge(watched, "yes",
                        {"sum_best_asks": sum_asks, "net_edge": edge}, edge)


def test_an_edge_inside_the_band_opens_a_window(recording_engine):
    watched = watch(recording_engine, "dip")

    push(recording_engine, watched, -0.005, sum_asks=1.005)

    row = recording_engine.db.execute("SELECT * FROM edge_windows").fetchone()
    assert row is not None
    assert row["event_slug"] == "dip"
    assert row["closed_at"] is None          # still open
    assert row["crossed"] == 0               # never beat min_edge


def test_an_edge_below_the_band_records_nothing(recording_engine):
    watched = watch(recording_engine, "far")

    push(recording_engine, watched, -0.5, sum_asks=1.5)

    assert recording_engine.db.execute(
        "SELECT COUNT(*) c FROM edge_windows").fetchone()["c"] == 0


def test_a_window_keeps_the_best_moment_not_the_last(recording_engine):
    """
    The analysts' scenario: a market dips, reaches its lowest price, then
    recovers. The row must remember the bottom, not wherever it happened to
    be when the window closed.
    """
    watched = watch(recording_engine, "dipspike")

    push(recording_engine, watched, -0.010, sum_asks=1.010)   # opens
    push(recording_engine, watched, -0.002, sum_asks=1.002)   # dips
    push(recording_engine, watched,  0.004, sum_asks=0.996)   # the bottom
    push(recording_engine, watched, -0.008, sum_asks=1.008)   # recovers
    push(recording_engine, watched, -0.50,  sum_asks=1.50)    # leaves band

    row = recording_engine.db.execute("SELECT * FROM edge_windows").fetchone()
    assert row["closed_at"] is not None
    assert row["best_edge"] == pytest.approx(0.004)
    assert row["best_sum_asks"] == pytest.approx(0.996)
    assert row["closed_edge"] == pytest.approx(-0.50)
    assert row["crossed"] == 1               # 0.004 beat min_edge 0.003


def test_every_tick_of_the_window_is_kept(recording_engine):
    watched = watch(recording_engine, "shape")

    for edge in (-0.010, -0.006, -0.001, 0.002, -0.004):
        push(recording_engine, watched, edge, sum_asks=1 - edge)
    push(recording_engine, watched, -0.9)     # closes and flushes

    ticks = recording_engine.db.execute(
        "SELECT * FROM edge_ticks ORDER BY id").fetchall()
    assert len(ticks) == 5
    assert [t["comparable_edge"] for t in ticks] == pytest.approx(
        [-0.010, -0.006, -0.001, 0.002, -0.004])


def test_leaving_and_re_entering_the_band_makes_two_windows(recording_engine):
    watched = watch(recording_engine, "twice")

    push(recording_engine, watched, -0.005)
    push(recording_engine, watched, -0.9)     # out
    push(recording_engine, watched, -0.004)   # back in
    push(recording_engine, watched, -0.9)     # out again

    rows = recording_engine.db.execute("SELECT * FROM edge_windows").fetchall()
    assert len(rows) == 2
    assert all(r["closed_at"] for r in rows)


def test_a_momentary_graze_is_not_kept_as_an_episode(recording_engine,
                                                     monkeypatch):
    """
    One stray evaluation is noise. Left in, it would drag down every
    average of "how long do these windows last".
    """
    monkeypatch.setattr(config, "MIN_WINDOW_MS", 5000)
    watched = watch(recording_engine, "graze")

    push(recording_engine, watched, -0.005)
    push(recording_engine, watched, -0.9)     # closes immediately

    assert recording_engine.db.execute(
        "SELECT COUNT(*) c FROM edge_windows").fetchone()["c"] == 0
    assert recording_engine.db.execute(
        "SELECT COUNT(*) c FROM edge_ticks").fetchone()["c"] == 0


def test_a_short_window_that_crossed_the_threshold_is_kept_anyway(
        recording_engine, monkeypatch):
    """A real signal is never noise, however briefly it lasted."""
    monkeypatch.setattr(config, "MIN_WINDOW_MS", 5000)
    watched = watch(recording_engine, "brief")

    push(recording_engine, watched, 0.02, sum_asks=0.98)
    push(recording_engine, watched, -0.9)

    row = recording_engine.db.execute("SELECT * FROM edge_windows").fetchone()
    assert row is not None
    assert row["crossed"] == 1


def test_ticks_are_rate_limited(recording_engine, monkeypatch):
    monkeypatch.setattr(config, "LIVE_TICK_MIN_INTERVAL_MS", 60_000)
    watched = watch(recording_engine, "busy")

    for _ in range(20):
        push(recording_engine, watched, -0.005)
    push(recording_engine, watched, -0.9)

    ticks = recording_engine.db.execute(
        "SELECT COUNT(*) c FROM edge_ticks").fetchone()["c"]
    assert ticks == 1          # the first; the rest fall inside the interval


def test_a_window_left_open_by_a_previous_run_is_closed_on_start(
        recording_engine):
    import db as dblib
    watched = watch(recording_engine, "orphan")
    push(recording_engine, watched, -0.005)          # leaves one open

    assert dblib.close_orphan_windows(recording_engine.db) == 1
    row = recording_engine.db.execute("SELECT * FROM edge_windows").fetchone()
    assert row["closed_at"] is not None


def test_a_dip_and_recovery_is_recorded_end_to_end(recording_engine):
    """
    The whole chain on the path that actually runs: book updates arrive,
    the engine re-evaluates, and the recorder keeps the shape.

    This is the analysts' case — a basket drifts down below a dollar for a
    while and then recovers. arb_monitor, scanning every fifteen minutes,
    can miss the entire episode; the socket sees every step of it.
    """
    watch(recording_engine, "dip", n=3)

    # 1.02 — inside the record band, nowhere near tradeable
    feed(recording_engine, "dip",
         asks_by_leg=[(0.34, 500), (0.34, 500), (0.34, 500)])
    # 0.99 — the bottom, and past min_edge
    feed(recording_engine, "dip",
         asks_by_leg=[(0.33, 500), (0.33, 500), (0.33, 500)])
    # 1.11 — back out of the band, closing the window
    feed(recording_engine, "dip",
         asks_by_leg=[(0.37, 500), (0.37, 500), (0.37, 500)])

    win = recording_engine.db.execute("SELECT * FROM edge_windows").fetchone()
    assert win is not None, "the episode left no record"
    assert win["event_slug"] == "dip"
    assert win["closed_at"] is not None
    assert win["crossed"] == 1
    # the bottom is remembered, not the price it recovered to
    assert win["best_sum_asks"] == pytest.approx(0.99)

    # One tick per re-evaluation, and the engine re-evaluates on every
    # token update rather than once per event — so a three-leg basket
    # repriced leg by leg is recorded stepping down 1.01, 1.00, 0.99.
    # That granularity is the point: it is the shape of the move.
    ticks = recording_engine.db.execute(
        "SELECT * FROM edge_ticks WHERE window_id = ? ORDER BY ts_ms",
        (win["id"],)).fetchall()
    prices = [t["sum_best_asks"] for t in ticks]
    assert prices == sorted(prices, reverse=True), "the dip lost its order"
    assert prices[-1] == pytest.approx(0.99)
    assert win["ticks"] == len(ticks)


def test_a_basket_that_never_enters_the_band_leaves_no_trace(recording_engine):
    watch(recording_engine, "quiet", n=3)

    feed(recording_engine, "quiet",
         asks_by_leg=[(0.50, 500), (0.50, 500), (0.50, 500)])   # 1.50

    assert recording_engine.db.execute(
        "SELECT COUNT(*) c FROM edge_windows").fetchone()["c"] == 0


def test_a_window_records_how_much_could_be_filled(recording_engine):
    """
    Price says whether an edge existed; depth says whether it was worth
    anything. A window that never held more than a few dollars is a
    curiosity, and only this column can tell it from a real one.
    """
    watch(recording_engine, "deep", n=3)

    feed(recording_engine, "deep",
         asks_by_leg=[(0.33, 5000), (0.33, 5000), (0.33, 5000)])
    feed(recording_engine, "deep",
         asks_by_leg=[(0.40, 5000), (0.40, 5000), (0.40, 5000)])   # closes

    win = recording_engine.db.execute("SELECT * FROM edge_windows").fetchone()
    assert win["best_capital"] is not None
    assert win["best_capital"] > 0
    assert win["best_profit"] is not None

    tick = recording_engine.db.execute(
        "SELECT * FROM edge_ticks ORDER BY ts_ms LIMIT 1").fetchone()
    assert tick["fillable_capital"] is not None


def test_a_thin_window_is_distinguishable_from_a_deep_one(recording_engine):
    """The whole point: same price, different money."""
    watch(recording_engine, "thin", n=3)
    feed(recording_engine, "thin",
         asks_by_leg=[(0.33, 3), (0.33, 3), (0.33, 3)])
    feed(recording_engine, "thin", asks_by_leg=[(0.40, 3), (0.40, 3), (0.40, 3)])

    watch(recording_engine, "fat", n=3)
    feed(recording_engine, "fat",
         asks_by_leg=[(0.33, 9000), (0.33, 9000), (0.33, 9000)])
    feed(recording_engine, "fat",
         asks_by_leg=[(0.40, 9000), (0.40, 9000), (0.40, 9000)])

    by_slug = {r["event_slug"]: r for r in recording_engine.db.execute(
        "SELECT * FROM edge_windows")}
    assert by_slug["thin"]["best_capital"] < by_slug["fat"]["best_capital"]
    # identical prices, so the edge alone could never separate them
    assert by_slug["thin"]["best_sum_asks"] == pytest.approx(
        by_slug["fat"]["best_sum_asks"])


def push_side(engine, watched, side, edge, sum_asks=1.0):
    engine._record_edge(watched, side,
                        {"sum_best_asks": sum_asks, "net_edge": edge,
                         "best": {"real_cost": 100.0, "profit": 1.0}}, edge)


def test_a_window_that_flips_sides_becomes_two_windows(recording_engine):
    """
    YES pays 1 and NO pays N-1, priced off opposite sides of the book, so
    they are different baskets on the same event. Carried across the flip
    in one row, `side` and `payout` would describe the basket it opened on
    while the price came from the other, and the row's own arithmetic
    would not hold.
    """
    watched = watch(recording_engine, "flipper", n=3)

    push_side(recording_engine, watched, "yes", -0.010, sum_asks=1.010)
    push_side(recording_engine, watched, "no", -0.004, sum_asks=2.004)
    push_side(recording_engine, watched, "no", -0.9)      # leaves the band

    rows_ = recording_engine.db.execute(
        "SELECT * FROM edge_windows ORDER BY id").fetchall()
    assert len(rows_) == 2
    assert [r["side"] for r in rows_] == ["yes", "no"]


def test_each_side_keeps_its_own_price(recording_engine):
    watched = watch(recording_engine, "prices", n=3)

    push_side(recording_engine, watched, "yes", -0.010, sum_asks=1.010)
    push_side(recording_engine, watched, "no", -0.004, sum_asks=2.004)
    push_side(recording_engine, watched, "no", -0.9)

    by_side = {r["side"]: r for r in
               recording_engine.db.execute("SELECT * FROM edge_windows")}
    assert by_side["yes"]["best_sum_asks"] == pytest.approx(1.010)
    assert by_side["no"]["best_sum_asks"] == pytest.approx(2.004)


def test_a_window_payout_matches_the_side_it_recorded(recording_engine):
    """The wallet has to buy the basket the row describes."""
    watched = watch(recording_engine, "payouts", n=3)

    push_side(recording_engine, watched, "yes", -0.01, sum_asks=1.01)
    push_side(recording_engine, watched, "no", -0.01, sum_asks=2.01)
    push_side(recording_engine, watched, "no", -0.9)

    by_side = {r["side"]: r for r in
               recording_engine.db.execute("SELECT * FROM edge_windows")}
    assert by_side["yes"]["payout"] == pytest.approx(1.0)
    assert by_side["no"]["payout"] == pytest.approx(2.0)   # N-1 for 3 legs


def test_staying_on_one_side_still_makes_a_single_window(recording_engine):
    watched = watch(recording_engine, "steady", n=3)

    for edge in (-0.010, -0.006, -0.002, -0.008):
        push_side(recording_engine, watched, "yes", edge, sum_asks=1 - edge)
    push_side(recording_engine, watched, "yes", -0.9)

    assert recording_engine.db.execute(
        "SELECT COUNT(*) c FROM edge_windows").fetchone()["c"] == 1
