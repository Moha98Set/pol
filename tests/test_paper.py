"""
The paper wallet.

A backtest that flatters itself is worse than no backtest, so these tests
are mostly about the ways it could cheat: entering at a price no order
could reach, deploying money the book could not absorb, or spending cash
it does not have.
"""

import pytest

import config
import db as dblib
import paper


@pytest.fixture
def database(tmp_path):
    db = dblib.connect(tmp_path / "paper.db")
    yield db
    db.close()


def add_window(db, *, slug="w", title=None, legs=3, seconds=12,
               edge=0.010, depth=4000.0, fee_rate=0.02, side="yes",
               edge_at=None, tick_every=1000, start_ms=1_700_000_000_000):
    """
    One window with `seconds` of ticks, one per second by default.

    `edge_at(t)` overrides the flat edge so a decaying window can be
    described directly.
    """
    edges = [edge_at(t) if edge_at else edge for t in range(seconds)]
    best = max(edges)
    cur = db.execute("""
        INSERT INTO edge_windows (event_slug, event_title, side, num_outcomes,
            fee_rate, payout, opened_at, closed_at, duration_ms, ticks,
            opened_edge, best_edge, best_sum_asks, best_at, closed_edge,
            best_capital, best_profit, crossed, url)
        VALUES (?, ?, ?, ?, ?, 1.0, '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:01:00+00:00', ?, ?, ?, ?, ?,
                '2026-01-01T00:00:00+00:00', ?, ?, ?, 1, '')
    """, (slug, title or slug, side, legs, fee_rate,
          seconds * tick_every, seconds, edges[0], best, 1 - best,
          edges[-1], depth, depth * best))
    wid = cur.lastrowid
    for t, e in enumerate(edges):
        db.execute("""
            INSERT INTO edge_ticks (window_id, ts_ms, recorded_at,
                sum_best_asks, net_edge, comparable_edge,
                fillable_capital, fillable_profit)
            VALUES (?, ?, '2026-01-01T00:00:00+00:00', ?, ?, ?, ?, ?)
        """, (wid, start_ms + t * tick_every, 1 - e, e, e, depth, depth * e))
    db.commit()
    return wid


# =====================================================================
# Entry price — the main way a backtest lies
# =====================================================================


def test_entry_is_never_the_windows_best_tick(database):
    """
    The best price in a window is gone before an order reaches the
    exchange. Entering there would describe a trade nobody could place.
    """
    add_window(database, legs=3, seconds=12,
               edge_at=lambda t: 0.030 if t == 0 else 0.008)

    paper.replay(database, min_window_ms=1000)

    row = database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1").fetchone()
    assert row is not None
    assert row["best_edge"] == pytest.approx(0.030)
    assert row["entry_edge"] == pytest.approx(0.008)   # not the peak
    assert row["entry_ms"] >= paper.entry_latency_ms(3)


def test_more_legs_means_a_later_entry(database):
    """Orders are placed one after another, so a wide basket is slower."""
    add_window(database, slug="narrow", legs=2, seconds=20)
    add_window(database, slug="wide", legs=10, seconds=20)

    paper.replay(database, min_window_ms=1000)

    by_slug = {r["event_slug"]: r for r in database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1")}
    assert by_slug["wide"]["entry_ms"] > by_slug["narrow"]["entry_ms"]


def test_a_window_that_ends_before_an_order_lands_is_refused(database):
    """A two-second window cannot be reached by a ten-leg basket."""
    add_window(database, legs=10, seconds=2)

    paper.replay(database, min_window_ms=1000)

    row = database.execute("SELECT * FROM paper_decisions").fetchone()
    assert row["taken"] == 0
    assert row["reason"] == paper.SKIP_NO_TICKS


def test_an_edge_that_fades_before_entry_is_refused_not_booked(database):
    """
    The peak was 2%, which a naive replay would have booked. By the time
    an order could land it was negative.
    """
    add_window(database, legs=3, seconds=12,
               edge_at=lambda t: 0.020 - 0.010 * t)

    paper.replay(database, min_window_ms=1000, min_edge=0.003)

    row = database.execute("SELECT * FROM paper_decisions").fetchone()
    assert row["taken"] == 0
    assert row["reason"] == paper.SKIP_GONE
    assert row["best_edge"] == pytest.approx(0.020)


# =====================================================================
# Size — the other way a backtest lies
# =====================================================================


def test_a_position_never_exceeds_what_the_book_could_absorb(database):
    add_window(database, depth=37.0, seconds=12)

    paper.replay(database, min_window_ms=1000, max_per_trade=1000,
                 min_capital=1)

    row = database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1").fetchone()
    assert row["capital"] == pytest.approx(37.0)


def test_a_position_never_exceeds_the_per_trade_cap(database):
    add_window(database, depth=100_000.0, seconds=12)

    paper.replay(database, min_window_ms=1000, max_per_trade=250)

    row = database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1").fetchone()
    assert row["capital"] == pytest.approx(250.0)


def test_a_book_too_thin_for_the_minimum_is_refused(database):
    add_window(database, depth=8.0, seconds=12)

    paper.replay(database, min_window_ms=1000, min_capital=20)

    row = database.execute("SELECT * FROM paper_decisions").fetchone()
    assert row["taken"] == 0
    assert row["reason"] == paper.SKIP_SMALL


# =====================================================================
# The wallet itself
# =====================================================================


def test_the_wallet_cannot_spend_money_it_does_not_have(database):
    for i in range(10):
        add_window(database, slug=f"w{i}", depth=100_000.0, seconds=12,
                   start_ms=1_700_000_000_000 + i * 60_000)

    summary = paper.replay(database, cash=500, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    assert summary["cash"] >= 0
    assert summary["locked"] <= 500
    spent = sum(r["capital"] for r in database.execute(
        "SELECT capital FROM paper_decisions WHERE taken = 1"))
    assert spent <= 500


def test_running_out_of_cash_is_recorded_as_its_own_reason(database):
    """
    "We traded 2 of 10" means something different if the other 8 were bad
    trades than if the wallet was simply empty.
    """
    for i in range(6):
        add_window(database, slug=f"w{i}", depth=100_000.0, seconds=12,
                   start_ms=1_700_000_000_000 + i * 60_000)

    paper.replay(database, cash=300, min_window_ms=1000, max_per_trade=250,
                 min_capital=20)

    reasons = [r["reason"] for r in database.execute(
        "SELECT reason FROM paper_decisions")]
    assert paper.SKIP_BROKE in reasons


def test_profit_is_shares_times_the_edge_at_entry(database):
    add_window(database, legs=3, seconds=12, edge=0.010, depth=1000.0)

    paper.replay(database, min_window_ms=1000, max_per_trade=100)

    row = database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1").fetchone()
    # 100 dollars of a 0.99 basket, each share earning 0.010
    assert row["shares"] == pytest.approx(100 / 0.99)
    assert row["profit"] == pytest.approx((100 / 0.99) * 0.010)


# =====================================================================
# Bookkeeping
# =====================================================================


def test_every_window_produces_exactly_one_decision(database):
    """
    The refusals are the point as much as the trades. A window that leaves
    no row is one the run cannot explain.
    """
    add_window(database, slug="good", seconds=12)
    add_window(database, slug="short", seconds=2)
    add_window(database, slug="thin", depth=1.0, seconds=12)
    add_window(database, slug="weak", edge=0.0001, seconds=12)

    summary = paper.replay(database, min_window_ms=5000)

    n = database.execute(
        "SELECT COUNT(*) c FROM paper_decisions").fetchone()["c"]
    assert n == 4 == summary["windows"]
    assert summary["trades"] + summary["skipped"] == 4


def test_a_short_window_is_refused_for_being_short(database):
    add_window(database, seconds=2)
    paper.replay(database, min_window_ms=5000)
    row = database.execute("SELECT * FROM paper_decisions").fetchone()
    assert row["reason"] == paper.SKIP_SHORT


def test_a_thin_edge_is_refused_for_the_edge_not_the_size(database):
    add_window(database, edge=0.0005, seconds=12, depth=9000.0)
    paper.replay(database, min_window_ms=1000, min_edge=0.003)
    row = database.execute("SELECT * FROM paper_decisions").fetchone()
    assert row["reason"] == paper.SKIP_EDGE


def test_the_run_records_the_parameters_it_used(database):
    """A P&L number without its parameters is unreadable a week later."""
    summary = paper.replay(database, cash=2500, min_edge=0.007,
                           label="experiment")

    run = database.execute("SELECT * FROM paper_runs WHERE id = ?",
                           (summary["run_id"],)).fetchone()
    import json
    params = json.loads(run["params"])
    assert run["label"] == "experiment"
    assert params["cash"] == 2500
    assert params["min_edge"] == 0.007
    assert run["finished_at"]


def test_taking_everything_ignores_the_filters(database):
    """The control run: if the filtered one cannot beat this, they cost more
    than they save."""
    add_window(database, slug="short", seconds=3)
    add_window(database, slug="weak", edge=0.0001, seconds=12)

    filtered = paper.replay(database, min_window_ms=5000, min_edge=0.003)
    control = paper.replay(database, min_window_ms=5000, min_edge=0.003,
                           take_everything=True)

    assert control["trades"] > filtered["trades"]


def test_an_open_window_is_not_replayed(database):
    """Only closed windows have a duration to judge."""
    add_window(database, seconds=12)
    database.execute("UPDATE edge_windows SET closed_at = NULL")
    database.commit()

    summary = paper.replay(database, min_window_ms=1000)

    assert summary["windows"] == 0


def test_latency_grows_with_leg_count():
    assert (paper.entry_latency_ms(10) - paper.entry_latency_ms(2)
            == 8 * config.PAPER_LATENCY_PER_LEG_MS)


def test_a_second_writer_waits_for_the_lock_instead_of_failing(tmp_path):
    """
    Three processes write this file now — the monitor's batch of verdict
    rows, the live engine's ticks, and a paper replay. SQLite's default on
    meeting a held write lock is to fail at once, so without a busy
    timeout a replay started at the wrong moment dies on "database is
    locked" rather than waiting a moment.
    """
    path = tmp_path / "busy.db"
    a = dblib.connect(path)
    assert a.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    a.close()


# =====================================================================
# Settlement — money coming back
# =====================================================================


def add_dated_window(db, *, slug, opened, end_date, depth=100_000.0,
                     legs=3, seconds=12, edge=0.010, start_ms=None):
    """A window that knows when its market settles."""
    wid = add_window(db, slug=slug, legs=legs, seconds=seconds, edge=edge,
                     depth=depth,
                     start_ms=start_ms or 1_700_000_000_000)
    db.execute("UPDATE edge_windows SET opened_at = ?, end_date = ? "
               "WHERE id = ?", (opened, end_date, wid))
    db.commit()
    return wid


def test_capital_comes_back_when_the_market_settles(database):
    """
    A basket pays out at resolution, and the money is usable again. A
    wallet that never gets it back would report one trade where twenty
    were possible.
    """
    add_dated_window(database, slug="first", opened="2026-01-01T00:00:00+00:00",
                     end_date="2026-01-02T00:00:00+00:00",
                     start_ms=1_700_000_000_000)
    add_dated_window(database, slug="second", opened="2026-01-03T00:00:00+00:00",
                     end_date="2026-01-04T00:00:00+00:00",
                     start_ms=1_700_000_100_000)

    summary = paper.replay(database, cash=250, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    # the whole wallet went into the first basket; only settlement could
    # have funded the second
    assert summary["trades"] == 2
    assert summary["settled"] >= 1


def test_a_basket_still_open_at_the_end_keeps_its_capital_locked(database):
    add_dated_window(database, slug="pending",
                     opened="2026-01-01T00:00:00+00:00",
                     end_date="2099-01-01T00:00:00+00:00")

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    assert summary["unsettled"] == 1
    assert summary["locked"] == pytest.approx(250)
    assert summary["cash"] == pytest.approx(750)


def test_settlement_returns_the_profit_as_well_as_the_capital(database):
    add_dated_window(database, slug="a", opened="2026-01-01T00:00:00+00:00",
                     end_date="2026-01-02T00:00:00+00:00", edge=0.010)
    # a later window only exists to advance the clock past the settlement
    add_dated_window(database, slug="b", opened="2026-06-01T00:00:00+00:00",
                     end_date="2099-01-01T00:00:00+00:00",
                     start_ms=1_700_000_100_000)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    # start - both purchases + (first capital + first profit) returned
    trades = database.execute(
        "SELECT capital, profit FROM paper_decisions WHERE taken = 1 "
        "ORDER BY id").fetchall()
    expected = 1000 - sum(t["capital"] for t in trades) + \
        trades[0]["capital"] + trades[0]["profit"]
    assert summary["cash"] == pytest.approx(expected)


def test_a_window_with_no_resolution_date_stays_locked(database):
    """
    Windows recorded before the date was stored carry none. Locking their
    capital for the whole replay is pessimistic; inventing a date would be
    worse.
    """
    add_window(database, slug="undated", depth=100_000.0, seconds=12)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    assert summary["trades"] == 1
    assert summary["settled"] == 0
    assert summary["unsettled"] == 1


def test_settling_frees_money_for_a_trade_that_could_not_otherwise_happen(
        database):
    """The point of tracking it: the same capital doing more than one job."""
    for i in range(4):
        add_dated_window(
            database, slug=f"w{i}",
            opened=f"2026-01-{2*i+1:02d}T00:00:00+00:00",
            end_date=f"2026-01-{2*i+2:02d}T00:00:00+00:00",
            start_ms=1_700_000_000_000 + i * 60_000)

    recycled = paper.replay(database, cash=250, min_window_ms=1000,
                            max_per_trade=250, min_capital=20)

    # strip the dates and the same wallet can only afford one
    database.execute("UPDATE edge_windows SET end_date = NULL")
    database.commit()
    once = paper.replay(database, cash=250, min_window_ms=1000,
                        max_per_trade=250, min_capital=20)

    assert recycled["trades"] > once["trades"]
    assert once["trades"] == 1


# =====================================================================
# Fees — paid at purchase, returned at settlement
# =====================================================================


def test_the_fee_leaves_the_wallet_at_purchase_not_only_the_capital(database):
    """
    Cash out is the shares *and* the fee. Deducting only the capital lets
    the wallet commit to a trade it could not actually pay for.
    """
    add_window(database, legs=3, seconds=12, edge=0.010, depth=100_000.0,
               fee_rate=0.05)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    row = database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1").fetchone()
    assert row["fee"] > 0
    assert summary["cash"] == pytest.approx(1000 - row["capital"] - row["fee"])


def test_a_round_trip_nets_exactly_the_profit(database):
    """
    Out: capital + fee. Back at settlement: capital + fee + profit. The
    fee must not be lost on the way round or counted twice.
    """
    add_dated_window(database, slug="a", opened="2026-01-01T00:00:00+00:00",
                     end_date="2026-01-02T00:00:00+00:00", edge=0.010)
    add_dated_window(database, slug="clock", opened="2026-06-01T00:00:00+00:00",
                     end_date="2099-01-01T00:00:00+00:00",
                     start_ms=1_700_000_100_000)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    trades = database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1 ORDER BY id").fetchall()
    settled, held = trades[0], trades[1]
    # the settled one gave everything back plus its profit; the held one
    # is still out by its capital and fee
    expected = 1000 + settled["profit"] - held["capital"] - held["fee"]
    assert summary["cash"] == pytest.approx(expected)


def test_the_fee_total_is_reported(database):
    add_window(database, slug="a", depth=100_000.0, seconds=12, fee_rate=0.05)
    add_window(database, slug="b", depth=100_000.0, seconds=12, fee_rate=0.05,
               start_ms=1_700_000_100_000)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    charged = sum(r["fee"] for r in database.execute(
        "SELECT fee FROM paper_decisions WHERE taken = 1"))
    assert summary["fees"] == pytest.approx(charged)
    assert summary["fees"] > 0

    run = database.execute("SELECT * FROM paper_runs WHERE id = ?",
                           (summary["run_id"],)).fetchone()
    assert run["fees_paid"] == pytest.approx(summary["fees"])


def test_profit_is_already_net_of_the_fee(database):
    """
    net_edge subtracts the fee, so profit must not have it taken off a
    second time — the two together are the gross edge.
    """
    add_window(database, legs=3, seconds=12, edge=0.010, depth=100_000.0)

    paper.replay(database, cash=1000, min_window_ms=1000, max_per_trade=100,
                 min_capital=20)

    row = database.execute(
        "SELECT * FROM paper_decisions WHERE taken = 1").fetchone()
    # entry_edge is already net; profit is shares times that
    assert row["profit"] == pytest.approx(row["shares"] * row["entry_edge"])


def test_a_zero_fee_market_is_charged_nothing(database):
    """Geopolitics markets carry no fee, and must not be invented one."""
    add_window(database, legs=3, seconds=12, edge=0.010, depth=100_000.0)
    # payout - sum_asks - net_edge == 0 exactly when there is no fee
    database.execute("UPDATE edge_ticks SET sum_best_asks = 1.0 - net_edge")
    database.commit()

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    assert summary["fees"] == pytest.approx(0.0)


# =====================================================================
# The ledger
# =====================================================================


def test_the_wallet_never_goes_overdrawn(database):
    """
    The wallet pays for shares *and* fee. Sizing on the capital alone and
    adding the fee afterwards overdraws by exactly the fee — which the
    ledger shows as a negative balance.
    """
    add_window(database, legs=3, seconds=12, edge=0.010, depth=100_000.0,
               fee_rate=0.05)

    summary = paper.replay(database, cash=250, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    assert summary["cash"] >= -1e-9
    balances = [r["balance_after"] for r in database.execute(
        "SELECT balance_after FROM paper_ledger ORDER BY seq")]
    assert all(b >= -1e-9 for b in balances), balances


def test_a_buy_and_its_settlement_each_get_a_ledger_row(database):
    add_dated_window(database, slug="a", opened="2026-01-01T00:00:00+00:00",
                     end_date="2026-01-02T00:00:00+00:00")
    add_dated_window(database, slug="clock", opened="2026-06-01T00:00:00+00:00",
                     end_date="2099-01-01T00:00:00+00:00",
                     start_ms=1_700_000_100_000)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    kinds = [r["kind"] for r in database.execute(
        "SELECT kind FROM paper_ledger WHERE run_id = ? ORDER BY seq",
        (summary["run_id"],))]
    assert kinds.count("buy") == 2
    assert kinds.count("settle") == 1


def test_a_buy_leaves_and_a_settlement_returns(database):
    add_dated_window(database, slug="a", opened="2026-01-01T00:00:00+00:00",
                     end_date="2026-01-02T00:00:00+00:00")
    add_dated_window(database, slug="clock", opened="2026-06-01T00:00:00+00:00",
                     end_date="2099-01-01T00:00:00+00:00",
                     start_ms=1_700_000_100_000)

    paper.replay(database, cash=1000, min_window_ms=1000, max_per_trade=250,
                 min_capital=20)

    by_kind = {}
    for r in database.execute("SELECT * FROM paper_ledger ORDER BY seq"):
        by_kind.setdefault(r["kind"], []).append(r)
    assert all(r["amount"] < 0 for r in by_kind["buy"])
    assert all(r["amount"] > 0 for r in by_kind["settle"])


def test_the_running_balance_matches_the_movements(database):
    """The balance column has to be the sum of everything above it."""
    for i in range(3):
        add_dated_window(
            database, slug=f"w{i}",
            opened=f"2026-01-{2*i+1:02d}T00:00:00+00:00",
            end_date=f"2026-01-{2*i+2:02d}T00:00:00+00:00",
            start_ms=1_700_000_000_000 + i * 60_000)

    paper.replay(database, cash=500, min_window_ms=1000, max_per_trade=250,
                 min_capital=20)

    running = 500.0
    for r in database.execute("SELECT * FROM paper_ledger ORDER BY seq"):
        running += r["amount"]
        assert r["balance_after"] == pytest.approx(running)


def test_equity_is_cash_plus_what_is_locked_up(database):
    add_window(database, depth=100_000.0, seconds=12)

    paper.replay(database, cash=1000, min_window_ms=1000, max_per_trade=250,
                 min_capital=20)

    row = database.execute(
        "SELECT * FROM paper_ledger ORDER BY seq DESC LIMIT 1").fetchone()
    assert row["equity_after"] == pytest.approx(
        row["balance_after"] + row["locked_after"])


def test_equity_only_grows_when_a_basket_settles(database):
    """
    Buying moves money from cash into a basket; it does not create or
    destroy any. Only the payout does.
    """
    add_dated_window(database, slug="a", opened="2026-01-01T00:00:00+00:00",
                     end_date="2026-01-02T00:00:00+00:00")
    add_dated_window(database, slug="clock", opened="2026-06-01T00:00:00+00:00",
                     end_date="2099-01-01T00:00:00+00:00",
                     start_ms=1_700_000_100_000)

    paper.replay(database, cash=1000, min_window_ms=1000, max_per_trade=250,
                 min_capital=20)

    rows = database.execute("SELECT * FROM paper_ledger ORDER BY seq").fetchall()
    first_buy = next(r for r in rows if r["kind"] == "buy")
    assert first_buy["equity_after"] == pytest.approx(1000)


def test_an_outcome_is_known_at_purchase_not_at_settlement(database):
    """
    Profit is fixed the moment the basket is bought, so a trade counts as
    a winner immediately. Settlement is a cash-flow event, not a verdict —
    counting there would leave every open position uncategorised.
    """
    add_dated_window(database, slug="settles",
                     opened="2026-01-01T00:00:00+00:00",
                     end_date="2026-01-02T00:00:00+00:00")
    add_dated_window(database, slug="still_open",
                     opened="2026-06-01T00:00:00+00:00",
                     end_date="2099-01-01T00:00:00+00:00",
                     start_ms=1_700_000_100_000)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    # both traded; only one settled
    assert summary["trades"] == 2
    assert summary["settled"] == 1
    assert summary["wins"] == 2
    assert summary["losses"] == 0


def test_the_winning_and_losing_totals_add_up_to_the_net(database):
    """
    A bare "100% success" hides the size of what was won. The counts and
    the sums are reported separately so a run of many tiny wins reads
    differently from one large one.
    """
    add_window(database, slug="a", depth=100_000.0, seconds=12, edge=0.010)
    add_window(database, slug="b", depth=100_000.0, seconds=12, edge=0.020,
               start_ms=1_700_000_100_000)

    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    assert summary["wins"] == 2
    assert summary["profit_sum"] > 0
    assert summary["loss_sum"] == 0
    assert summary["realised"] == pytest.approx(
        summary["profit_sum"] + summary["loss_sum"])


def test_the_totals_are_stored_on_the_run(database):
    add_window(database, depth=100_000.0, seconds=12)
    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    run = database.execute("SELECT * FROM paper_runs WHERE id = ?",
                           (summary["run_id"],)).fetchone()
    assert run["gross_profit"] == pytest.approx(summary["profit_sum"])
    assert run["gross_loss"] == pytest.approx(summary["loss_sum"])
    assert run["wins"] == summary["wins"]


def test_gross_wallet_is_cash_plus_locked(database):
    """
    The two numbers answer different questions: cash is what can be spent
    now, gross is what the wallet is actually worth.
    """
    add_window(database, depth=100_000.0, seconds=12)
    summary = paper.replay(database, cash=1000, min_window_ms=1000,
                           max_per_trade=250, min_capital=20)

    assert summary["equity"] == pytest.approx(
        summary["cash"] + summary["locked"])
    assert summary["cash"] < summary["equity"]      # something is locked


def test_deploying_more_capital_is_not_the_same_as_choosing_better(database):
    """
    Taking every window commits more money, so it books more total profit
    almost by definition. Judging the filters on the raw total would
    always favour the control and say nothing about selection quality.
    """
    # one good window and several thin ones
    add_window(database, slug="good", depth=100_000.0, seconds=12, edge=0.020)
    for i in range(5):
        add_window(database, slug=f"weak{i}", depth=100_000.0, seconds=12,
                   edge=0.0005, start_ms=1_700_000_000_000 + (i + 1) * 60_000)

    filtered = paper.replay(database, cash=10_000, min_window_ms=1000,
                            min_edge=0.010, max_per_trade=250, min_capital=20)
    control = paper.replay(database, cash=10_000, min_window_ms=1000,
                           min_edge=0.010, max_per_trade=250, min_capital=20,
                           take_everything=True)

    used_f = filtered["start_cash"] - filtered["cash"]
    used_c = control["start_cash"] - control["cash"]

    # the control books more in absolute terms...
    assert control["realised"] > filtered["realised"]
    # ...only because it spent more; per dollar the filters chose better
    assert (filtered["realised"] / used_f) > (control["realised"] / used_c)
