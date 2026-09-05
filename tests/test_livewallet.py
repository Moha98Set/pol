"""
The live paper wallet.

Unlike the replay, this one keeps state between restarts and is driven by
timers that can fire at the same moment, so the tests are mostly about the
two ways it could quietly become wrong: losing track of money across a
restart, and letting two concurrent decisions spend the same dollar.

The rest hold onto the property the whole module exists for — that the
entry is priced against the book as it is when the order would land, not
as it was when the edge appeared.
"""

import threading

import pytest

import db as dblib
import livewallet


@pytest.fixture
def database(tmp_path):
    # Same flag the engine uses, for the same reason: the concurrency test
    # below drives the wallet from several threads, which is exactly how
    # asyncio.to_thread drives it in production.
    db = dblib.connect(tmp_path / "live.db", check_same_thread=False)
    yield db
    db.close()


class FakeEvent:
    def __init__(self, slug="ev", end_date=None):
        self.slug = slug
        self.end_date = end_date


def book(edge=0.010, sum_asks=0.990, fillable=500.0, side="yes",
         payout=1.0, legs=3):
    """What engine.price() returns, reduced to what the wallet reads."""
    return side, {
        "sum_best_asks": sum_asks,
        "num_legs": legs,
        "best": {"real_cost": fillable},
    }, edge


def signal(slug="ev", legs=3, edge=0.010, sum_asks=0.990, side="yes",
           payout=1.0, age_ms=250.0):
    return {
        "event_slug": slug, "event_title": slug, "side": side,
        "num_outcomes": legs, "payout_per_basket": payout, "fee_rate": 0.02,
        "best_net_edge": edge, "best_sum_asks": sum_asks,
        "age_ms": age_ms, "planned_delay_ms": 1300.0,
        "first_seen_ts": None, "url": "",
    }


def wallet(db, priced=None, **kw):
    return livewallet.LiveWallet(db, lambda w: priced or book(), **kw)


# =====================================================================
# Entry price — the reason this module exists
# =====================================================================


def test_the_entry_is_priced_from_the_current_book_not_the_signal(database):
    """
    The signal said 3%. By the time the order lands the book says 0.8%, and
    0.8% is what gets booked. Taking the signal's number would make this a
    slower replay with the same lie in it.
    """
    w = wallet(database, priced=book(edge=0.008, sum_asks=0.992))
    row = w.consider(signal(edge=0.030, sum_asks=0.970), FakeEvent())

    assert row["taken"] == 1
    assert row["signal_edge"] == pytest.approx(0.030)
    assert row["entry_edge"] == pytest.approx(0.008)
    assert row["profit"] == pytest.approx(row["shares"] * 0.008)


def test_an_edge_that_died_during_the_wait_is_a_refusal(database):
    w = wallet(database, priced=book(edge=-0.002, sum_asks=1.002))
    row = w.consider(signal(edge=0.030), FakeEvent())

    assert row["taken"] == 0
    assert row["reason"] == livewallet.SKIP_EDGE_GONE
    assert row["entry_edge"] == pytest.approx(-0.002)


def test_legs_that_vanished_during_the_wait_are_their_own_reason(database):
    """
    A leg going dry and an edge shrinking are different failures — one is
    the market moving, the other is us losing sight of it.
    """
    w = livewallet.LiveWallet(database, lambda watched: (None, None, None))
    row = w.consider(signal(), FakeEvent())

    assert row["reason"] == livewallet.SKIP_LEGS_GONE


def test_a_side_flip_during_the_wait_is_not_the_signalled_trade(database):
    w = wallet(database, priced=book(side="no"))
    row = w.consider(signal(side="yes"), FakeEvent())

    assert row["taken"] == 0
    assert row["reason"] == livewallet.SKIP_EDGE_GONE


def test_the_real_elapsed_time_is_recorded(database):
    """
    The whole point of running live: report what the delay actually was,
    so the replay's constants can be checked against something.
    """
    import time
    w = wallet(database)
    sig = signal()
    sig["first_seen_ts"] = time.time() - 1.5
    row = w.consider(sig, FakeEvent())

    assert row["total_ms"] == pytest.approx(1500, abs=200)


# =====================================================================
# Sizing
# =====================================================================


def test_a_position_never_exceeds_what_the_book_could_absorb(database):
    w = wallet(database, priced=book(fillable=37.0), max_per_trade=1000,
               min_capital=1)
    row = w.consider(signal(), FakeEvent())

    assert row["capital"] == pytest.approx(37.0)


def test_a_position_never_exceeds_the_per_trade_cap(database):
    w = wallet(database, priced=book(fillable=100_000.0), max_per_trade=250)
    row = w.consider(signal(), FakeEvent())

    assert row["capital"] == pytest.approx(250.0)


def test_the_wallet_reserves_for_the_fee_when_sizing(database):
    """
    Sizing on the capital alone and adding the fee afterwards overdraws by
    exactly the fee — small each time, and a negative balance eventually.
    """
    w = wallet(database, priced=book(edge=0.005, sum_asks=0.980,
                                     fillable=100_000.0),
               start_cash=100.0, max_per_trade=100_000)
    row = w.consider(signal(), FakeEvent())

    assert row["capital"] + row["fee"] == pytest.approx(100.0)
    assert w.cash >= 0


def test_a_book_too_thin_for_the_minimum_is_refused(database):
    w = wallet(database, priced=book(fillable=8.0), min_capital=20)
    row = w.consider(signal(), FakeEvent())

    assert row["reason"] == livewallet.SKIP_SMALL


def test_an_empty_wallet_says_so_rather_than_blaming_the_book(database):
    w = wallet(database, priced=book(fillable=100_000.0), start_cash=300,
               max_per_trade=250, min_capital=20)
    for i in range(3):
        w.consider(signal(slug=f"ev{i}"), FakeEvent(slug=f"ev{i}"))

    row = w.consider(signal(slug="last"), FakeEvent(slug="last"))
    assert row["reason"] == livewallet.SKIP_BROKE


def test_too_many_legs_is_refused_before_anything_else(database):
    w = wallet(database, max_legs=4)
    row = w.consider(signal(legs=9), FakeEvent())

    assert row["reason"] == livewallet.SKIP_LEGS


# =====================================================================
# One basket per event
# =====================================================================


def test_the_same_event_is_not_bought_twice_while_held(database):
    """
    A flickering signal on one market must not be able to absorb the whole
    wallet — that is the same bet repeated, not diversification.
    """
    w = wallet(database, priced=book(fillable=100_000.0))
    first = w.consider(signal(slug="same"), FakeEvent(slug="same"))
    second = w.consider(signal(slug="same"), FakeEvent(slug="same"))

    assert first["taken"] == 1
    assert second["reason"] == livewallet.SKIP_DUPLICATE


def test_the_same_event_can_be_bought_again_after_it_settles(database):
    w = wallet(database, priced=book(fillable=100_000.0))
    w.consider(signal(slug="same"), FakeEvent(slug="same"))
    w.settle_event("same")

    again = w.consider(signal(slug="same"), FakeEvent(slug="same"))
    assert again["taken"] == 1


# =====================================================================
# Money
# =====================================================================


def test_buying_moves_capital_and_fee_out_of_cash(database):
    w = wallet(database, start_cash=1000.0)
    row = w.consider(signal(), FakeEvent())

    assert w.cash == pytest.approx(1000.0 - row["capital"] - row["fee"])
    assert w.locked == pytest.approx(row["capital"] + row["fee"])
    assert w.equity == pytest.approx(1000.0)


def test_a_round_trip_nets_exactly_the_profit(database):
    """The fee is deducted and returned; only the profit is left behind."""
    w = wallet(database, start_cash=1000.0)
    row = w.consider(signal(), FakeEvent())
    w.settle_event("ev")

    assert w.locked == pytest.approx(0.0)
    assert w.cash == pytest.approx(1000.0 + row["profit"])


def test_settlement_frees_money_for_the_next_trade(database):
    w = wallet(database, priced=book(fillable=100_000.0), start_cash=260,
               max_per_trade=250, min_capital=20)
    w.consider(signal(slug="a"), FakeEvent(slug="a"))
    blocked = w.consider(signal(slug="b"), FakeEvent(slug="b"))
    assert blocked["taken"] == 0

    w.settle_event("a")
    now_ok = w.consider(signal(slug="c"), FakeEvent(slug="c"))
    assert now_ok["taken"] == 1


def test_an_end_date_that_has_passed_settles_on_the_sweep(database):
    w = wallet(database, start_cash=1000.0)
    w.consider(signal(), FakeEvent(end_date="2020-01-01T00:00:00+00:00"))

    assert w.locked > 0
    assert w.settle_due() == 1
    assert w.locked == pytest.approx(0.0)


def test_a_basket_with_no_end_date_stays_locked_rather_than_guessed(database):
    w = wallet(database, start_cash=1000.0)
    w.consider(signal(), FakeEvent(end_date=None))

    assert w.settle_due() == 0
    assert w.locked > 0


def test_every_movement_lands_in_the_ledger(database):
    w = wallet(database, start_cash=1000.0)
    w.consider(signal(), FakeEvent())
    w.settle_event("ev")

    rows = database.execute(
        "SELECT * FROM live_ledger ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["buy", "settle"]
    assert rows[0]["amount"] < 0 < rows[1]["amount"]
    assert rows[-1]["equity_after"] == pytest.approx(w.equity)


def test_refusals_are_recorded_not_just_dropped(database):
    """
    "We saw 40 signals and took 2" is unreadable without the other 38.
    """
    w = wallet(database, priced=book(edge=-0.005))
    w.consider(signal(), FakeEvent())

    row = database.execute("SELECT * FROM live_decisions").fetchone()
    assert row["taken"] == 0
    assert row["reason"] == livewallet.SKIP_EDGE_GONE
    assert row["signal_age_ms"] == pytest.approx(250.0)


# =====================================================================
# Surviving a restart
# =====================================================================


def test_the_wallet_resumes_where_it_left_off(database):
    """
    State in memory would mean every deploy silently restarted the
    experiment with a full wallet and no positions.
    """
    first = wallet(database, start_cash=1000.0)
    row = first.consider(signal(), FakeEvent())

    second = livewallet.LiveWallet(database, lambda w: book(),
                                   start_cash=1000.0)
    assert second.cash == pytest.approx(first.cash)
    assert second.locked == pytest.approx(row["capital"] + row["fee"])
    assert second.state["trades"] == 1


def test_a_restart_does_not_reopen_a_second_wallet(database):
    wallet(database, start_cash=1000.0)
    wallet(database, start_cash=5000.0)

    rows = database.execute("SELECT * FROM live_wallet").fetchall()
    assert len(rows) == 1
    assert rows[0]["start_cash"] == pytest.approx(1000.0)


def test_a_position_held_across_a_restart_still_settles(database):
    first = wallet(database, start_cash=1000.0)
    first.consider(signal(), FakeEvent(end_date="2020-01-01T00:00:00+00:00"))

    second = livewallet.LiveWallet(database, lambda w: book(),
                                   start_cash=1000.0)
    assert second.settle_due() == 1
    assert second.locked == pytest.approx(0.0)
    assert second.cash > 1000.0


# =====================================================================
# Concurrency — the decisions are independent timers
# =====================================================================


def test_two_decisions_landing_together_cannot_overdraw_the_wallet(database):
    """
    Each signal's decision is its own timer, so two can fire at once. If
    they both size against the same cash figure before either deducts, the
    wallet spends money it does not have.
    """
    w = wallet(database, priced=book(fillable=100_000.0), start_cash=300.0,
               max_per_trade=250, min_capital=20)

    # The barrier releases all four at once, which is the collision this
    # test is about; it counts only the worker threads.
    start = threading.Barrier(4)
    results = []

    def buy(i):
        start.wait(timeout=10)
        results.append(w.consider(signal(slug=f"ev{i}"),
                                  FakeEvent(slug=f"ev{i}")))

    threads = [threading.Thread(target=buy, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a decision deadlocked"

    assert w.cash >= 0, f"overdrew to {w.cash}"
    spent = sum(r["capital"] + r["fee"] for r in results if r.get("taken"))
    assert spent <= 300.0 + 1e-9
    assert w.equity == pytest.approx(300.0)
