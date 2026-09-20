"""
Daily wallet usage.

The ledger already holds every movement, but a movement at a time is the
wrong altitude for "how hard is this wallet working" — which is the
question that went unanswered while capital sat locked for weeks. These
tests are about the arithmetic of the rollup, not its markup.
"""

import pytest

import db as dblib
import dashboard


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "arb.db"
    conn = dblib.connect(path)
    conn.execute("""INSERT INTO paper_runs (id, started_at, label, params,
                        start_cash, end_cash, locked, realised_profit)
                    VALUES (7, '2026-01-01T00:00:00+00:00', 'r', '{}',
                            1000, 400, 620, 20)""")
    # day one: two baskets bought, nothing settled
    move(conn, 1, "2026-03-01T09:00:00+00:00", "buy", -200, legs=3,
         fee=4, cash=800, locked=200)
    move(conn, 2, "2026-03-01T15:00:00+00:00", "buy", -300, legs=5,
         fee=6, cash=500, locked=500)
    # day two: one more bought, then one settles and returns with profit
    move(conn, 3, "2026-03-02T09:00:00+00:00", "buy", -120, legs=2,
         fee=2, cash=380, locked=620)
    move(conn, 4, "2026-03-02T18:00:00+00:00", "settle", 210, legs=3,
         fee=4, profit=10, cash=590, locked=420)
    conn.commit()
    conn.close()

    monkeypatch.setattr(dashboard, "DB_PATH", path)
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user"] = "tester"
        yield c


def move(conn, seq, at, kind, amount, *, legs, fee=0, profit=0, cash, locked):
    conn.execute("""
        INSERT INTO paper_ledger (run_id, seq, at, kind, event_slug,
            event_title, amount, capital, fee, profit, balance_after,
            locked_after, equity_after, num_outcomes)
        VALUES (7, ?, ?, ?, 'e', 'E', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (seq, at, kind, amount, abs(amount) - fee, fee, profit,
          cash, locked, cash + locked, legs))


def usage(client):
    res = client.get("/paper/7/ledger")
    assert res.status_code == 200
    return res.get_data(as_text=True)


def days(client):
    """The rollup rows, newest first, straight from the helper."""
    with dashboard.app.test_request_context("/paper/7/ledger"):
        return dashboard._daily_usage("paper_ledger", "seq",
                                      "run_id = ?", (7,))


# =====================================================================
# The arithmetic
# =====================================================================


def test_each_day_is_its_own_row_newest_first(client):
    d = days(client)
    assert [r["day"] for r in d] == ["2026-03-02", "2026-03-01"]


def test_spending_is_summed_per_day(client):
    d = {r["day"]: r for r in days(client)}
    assert d["2026-03-01"]["spent"] == pytest.approx(500)
    assert d["2026-03-02"]["spent"] == pytest.approx(120)


def test_baskets_and_legs_are_counted_separately(client):
    """
    Ten dollars across one six-leg basket and across three two-leg ones
    are different commitments; the row has to show both numbers.
    """
    d = {r["day"]: r for r in days(client)}
    assert d["2026-03-01"]["buys"] == 2
    assert d["2026-03-01"]["legs"] == 8          # 3 + 5
    assert d["2026-03-02"]["buys"] == 1
    assert d["2026-03-02"]["legs"] == 2


def test_settlements_do_not_count_as_spending(client):
    d = {r["day"]: r for r in days(client)}
    assert d["2026-03-02"]["returned"] == pytest.approx(210)
    assert d["2026-03-02"]["closes"] == 1
    assert d["2026-03-02"]["realised"] == pytest.approx(10)
    assert d["2026-03-01"]["returned"] == pytest.approx(0)


def test_peak_commitment_is_the_high_water_mark_not_the_close(client):
    """
    Day two ends at $420 locked but touched $620. Reporting the closing
    figure would hide that the wallet was 62% committed that day.
    """
    d = {r["day"]: r for r in days(client)}
    assert d["2026-03-02"]["peak_locked"] == pytest.approx(620)
    assert d["2026-03-02"]["end_locked"] == pytest.approx(420)


def test_the_days_closing_balances_come_from_its_last_movement(client):
    d = {r["day"]: r for r in days(client)}
    assert d["2026-03-01"]["end_cash"] == pytest.approx(500)
    assert d["2026-03-02"]["end_cash"] == pytest.approx(590)
    assert d["2026-03-02"]["end_equity"] == pytest.approx(1010)


def test_fees_are_totalled_for_the_day(client):
    d = {r["day"]: r for r in days(client)}
    assert d["2026-03-01"]["fees"] == pytest.approx(10)


# =====================================================================
# On the page
# =====================================================================


def test_the_page_shows_the_usage_table(client):
    html = usage(client)
    assert "مصرف کیف، روزبه‌روز" in html
    assert "2026-03-01" in html


def test_commitment_is_shown_against_the_wallet_size(client):
    """620 locked out of a 1000 wallet is 62%."""
    assert "62.0٪" in usage(client)


def test_the_movements_table_shows_each_baskets_leg_count(client):
    html = usage(client)
    assert "<th>پا</th>" in html
