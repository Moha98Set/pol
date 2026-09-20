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


# =====================================================================
# Return per day held
# =====================================================================
#
# Edge alone cannot tell a good trade from a bad one, because it says
# nothing about how long the money is gone. The first live run locked the
# entire per-trade cap into a 17%-a-year market while a 207%-a-year one
# took what was left.


def days_from_now(days):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_a_trade_that_ties_money_up_too_long_for_its_profit_is_refused(database):
    w = wallet(database, min_annual_pct=20.0, min_capital=1)
    # 1% edge over a year is 1% a year
    ev = FakeEvent(end_date=days_from_now(365))

    row = w.consider(signal(), ev)

    assert row["taken"] == 0
    assert row["reason"] == livewallet.SKIP_SLOW


def test_the_same_profit_over_a_short_hold_is_accepted(database):
    w = wallet(database, min_annual_pct=20.0, min_capital=1)
    ev = FakeEvent(end_date=days_from_now(5))

    row = w.consider(signal(), ev)

    assert row["taken"] == 1


def test_the_rejected_rate_is_recorded_so_the_threshold_can_be_judged(database):
    """A refusal that does not say how close it came cannot be tuned."""
    w = wallet(database, min_annual_pct=20.0, min_capital=1)
    w.consider(signal(), FakeEvent(end_date=days_from_now(365)))

    row = database.execute("SELECT * FROM live_decisions").fetchone()
    assert row["annual_pct"] == pytest.approx(1.0, abs=0.2)
    assert row["hold_days"] == pytest.approx(365, abs=1)


def test_an_event_with_no_end_date_is_not_refused_for_it(database):
    """
    Refusing on missing data would silently drop whole categories of
    market rather than the slow trades the test is for.
    """
    w = wallet(database, min_annual_pct=1000.0, min_capital=1)

    row = w.consider(signal(), FakeEvent(end_date=None))

    assert row["taken"] == 1


def test_the_test_can_be_switched_off(database):
    w = wallet(database, min_annual_pct=0, min_capital=1)
    row = w.consider(signal(), FakeEvent(end_date=days_from_now(3650)))
    assert row["taken"] == 1


def test_a_market_resolving_within_a_day_cannot_claim_an_absurd_rate(database):
    """
    Dividing by hours would turn any profit into thousands of percent a
    year and wave through everything.
    """
    assert livewallet.days_until(days_from_now(0.01)) == 1.0


# =====================================================================
# Selling back early
# =====================================================================


def buy_one(database, *, edge=0.010, sum_asks=0.990, fillable=100.0, **kw):
    w = wallet(database, priced=book(edge=edge, sum_asks=sum_asks,
                                     fillable=fillable),
               min_capital=1, min_annual_pct=0, **kw)
    row = w.consider(signal(edge=edge, sum_asks=sum_asks), FakeEvent())
    assert row["taken"] == 1
    return w, row


def test_an_exit_below_what_holding_pays_is_refused(database):
    """The default bar is the whole locked-in profit, so this is a no-op."""
    w, row = buy_one(database)
    cash_before = w.cash

    sold = w.consider_exits(lambda slug, side: 0.99)

    assert sold == 0
    assert w.cash == cash_before
    assert len(w.open_positions()) == 1


def test_an_exit_that_beats_holding_is_taken(database):
    w, row = buy_one(database)
    # bids far above the 0.99 paid, so the proceeds clear cost plus profit
    sold = w.consider_exits(lambda slug, side: 1.20)

    assert sold == 1
    assert w.open_positions() == []
    assert w.locked == pytest.approx(0.0, abs=1e-9)


def test_selling_frees_the_capital_for_another_trade(database):
    """The whole reason the exit exists: the money comes back early."""
    w, row = buy_one(database)
    locked_before = w.locked
    assert locked_before > 0

    w.consider_exits(lambda slug, side: 1.20)

    assert w.locked == pytest.approx(0.0, abs=1e-9)
    assert w.cash > w.state["start_cash"]


def test_the_profit_booked_at_purchase_is_replaced_not_added_to(database):
    """
    The profit was recorded as certain when the basket was bought. An exit
    that pays more must not leave both numbers in the total.
    """
    w, row = buy_one(database)
    booked = row["profit"]

    w.consider_exits(lambda slug, side: 1.20)

    p = database.execute("SELECT * FROM live_positions").fetchone()
    assert p["settle_reason"] == "sold"
    assert w.state["realised_profit"] == pytest.approx(p["exit_profit"])
    assert w.state["realised_profit"] != pytest.approx(booked + p["exit_profit"])


def test_equity_after_an_exit_equals_cash_because_nothing_is_held(database):
    w, _row = buy_one(database)
    w.consider_exits(lambda slug, side: 1.20)
    assert w.equity == pytest.approx(w.cash)


def test_an_unpriceable_basket_is_left_alone(database):
    w, _row = buy_one(database)
    assert w.consider_exits(lambda slug, side: None) == 0
    assert len(w.open_positions()) == 1


def test_a_pricing_error_does_not_lose_the_position(database):
    def boom(slug, side):
        raise RuntimeError("book vanished")

    w, _row = buy_one(database)
    assert w.consider_exits(boom) == 0
    assert len(w.open_positions()) == 1


def test_the_exit_is_written_to_the_ledger(database):
    w, _row = buy_one(database)
    w.consider_exits(lambda slug, side: 1.20)

    kinds = [r["kind"] for r in database.execute(
        "SELECT kind FROM live_ledger ORDER BY id")]
    assert kinds == ["buy", "sell"]


def test_a_sold_position_does_not_settle_again(database):
    """Settling it twice would credit the wallet the capital twice."""
    w, _row = buy_one(database)
    w.consider_exits(lambda slug, side: 1.20)
    cash = w.cash

    assert w.settle_due() == 0
    assert w.settle_event("ev") == 0
    assert w.cash == pytest.approx(cash)


def test_a_lower_fraction_accepts_less_profit_for_the_liquidity(database):
    w, row = buy_one(database, exit_min_fraction=0.0)
    # bids that cover the cost exactly and nothing more
    cost_per_share = (row["capital"] + row["fee"]) / row["shares"]
    # plus the exit fee, which the proceeds must also carry
    per_share = cost_per_share + row["fee"] / row["shares"]

    sold = w.consider_exits(lambda slug, side: per_share)

    assert sold == 1
    p = database.execute("SELECT * FROM live_positions").fetchone()
    assert p["exit_profit"] == pytest.approx(0.0, abs=1e-6)


def test_leg_skew_is_recorded_at_signal_and_at_entry(database):
    w = wallet(database, min_capital=1, min_annual_pct=0)
    w.skew_now = lambda ev: 1234.0
    sig = dict(signal(), leg_skew_ms=55.0)

    w.consider(sig, FakeEvent())

    row = database.execute("SELECT * FROM live_decisions").fetchone()
    assert row["signal_leg_skew_ms"] == pytest.approx(55.0)
    assert row["entry_leg_skew_ms"] == pytest.approx(1234.0)


# =====================================================================
# What the basket was actually made of
# =====================================================================
#
# "We hold $48 of the Swedish election" does not say which outcomes were
# bought or at what price, and those are the numbers an analyst checks a
# position against.


class FakeWatched(FakeEvent):
    def __init__(self, slug="ev", end_date=None, names=("A", "B", "C")):
        super().__init__(slug, end_date)
        self.legs = [(n, f"{slug}-{n}") for n in names]


def priced(edge=0.010, sum_asks=0.990, fillable=500.0, side="yes",
           leg_asks=(0.30, 0.33, 0.36)):
    return side, {
        "sum_best_asks": sum_asks,
        "num_legs": len(leg_asks),
        "leg_best_asks": list(leg_asks),
        "best": {"real_cost": fillable},
    }, edge


def test_each_leg_is_recorded_with_the_price_entry_paid(database):
    w = wallet(database, priced=priced(), min_capital=1, min_annual_pct=0)

    w.consider(signal(), FakeWatched())

    import json
    legs = json.loads(database.execute(
        "SELECT legs FROM live_positions").fetchone()["legs"])
    assert [l["outcome"] for l in legs] == ["A", "B", "C"]
    assert [l["price"] for l in legs] == [0.30, 0.33, 0.36]


def test_a_no_basket_names_the_side_it_actually_bought(database):
    """
    Labelling these "A" would describe a position that is short A as one
    that is long it.
    """
    w = wallet(database, priced=priced(side="no"), min_capital=1,
               min_annual_pct=0)

    w.consider(signal(side="no"), FakeWatched())

    import json
    legs = json.loads(database.execute(
        "SELECT legs FROM live_positions").fetchone()["legs"])
    assert [l["outcome"] for l in legs] == ["NO A", "NO B", "NO C"]


def test_leg_prices_come_from_the_entry_not_the_signal(database):
    """
    The signal quoted a cheaper basket; the order would have paid the
    later price. Storing the signal's would describe orders never placed.
    """
    w = wallet(database, priced=priced(leg_asks=(0.40, 0.40, 0.19)),
               min_capital=1, min_annual_pct=0)

    w.consider(signal(sum_asks=0.900), FakeWatched())

    import json
    legs = json.loads(database.execute(
        "SELECT legs FROM live_positions").fetchone()["legs"])
    assert sum(l["price"] for l in legs) == pytest.approx(0.99)


def test_a_basket_with_no_leg_prices_still_records_the_position(database):
    w = wallet(database, priced=(("yes"), {"sum_best_asks": 0.99,
               "num_legs": 3, "best": {"real_cost": 500.0}}, 0.010),
               min_capital=1, min_annual_pct=0)

    row = w.consider(signal(), FakeWatched())

    assert row["taken"] == 1
    import json
    legs = json.loads(database.execute(
        "SELECT legs FROM live_positions").fetchone()["legs"])
    assert [l["price"] for l in legs] == [None, None, None]
