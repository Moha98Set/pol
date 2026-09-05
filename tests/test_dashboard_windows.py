"""
The window log's default view.

Around 95% of recorded windows never reach a positive edge. Recording
them is deliberate — they show how close the market comes — but they are
not what the page is for, so the default hides them. That default is only
safe if the hidden rows stay one click away and the page says what it is
doing, which is what these tests hold onto.
"""

import pytest

import db as dblib
import dashboard


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "arb.db"
    conn = dblib.connect(path)
    add(conn, edge=+0.010, crossed=1, slug="profitable-and-signalled")
    add(conn, edge=+0.002, crossed=0, slug="profitable-but-below-threshold")
    add(conn, edge=-0.004, crossed=0, slug="never-worth-buying")
    add(conn, edge=-0.015, crossed=0, slug="nowhere-near")
    conn.close()

    monkeypatch.setattr(dashboard, "DB_PATH", path)
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user"] = "tester"
        yield c


def add(conn, *, edge, crossed, slug):
    conn.execute("""
        INSERT INTO edge_windows (event_slug, event_title, side, num_outcomes,
            fee_rate, payout, opened_at, closed_at, duration_ms, ticks,
            opened_edge, best_edge, best_sum_asks, best_at, closed_edge,
            best_capital, best_profit, crossed, url)
        VALUES (?, ?, 'yes', 3, 0.02, 1.0,
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00',
                60000, 60, ?, ?, ?, '2026-01-01T00:00:30+00:00', ?,
                500.0, 5.0, ?, '')
    """, (slug, slug, edge, edge, 1 - edge, edge, crossed))
    conn.commit()


def body(client, query=""):
    res = client.get("/windows" + query)
    assert res.status_code == 200
    return res.get_data(as_text=True)


# =====================================================================
# What each slice contains
# =====================================================================


def test_default_shows_only_positive_edge(client):
    """Not a filter the analyst has to find — it is what they land on."""
    html = body(client)
    assert "profitable-and-signalled" in html
    assert "profitable-but-below-threshold" in html
    assert "never-worth-buying" not in html
    assert "nowhere-near" not in html


def test_all_shows_every_closed_window(client):
    html = body(client, "?show=all")
    for slug in ("profitable-and-signalled", "profitable-but-below-threshold",
                 "never-worth-buying", "nowhere-near"):
        assert slug in html


def test_crossed_shows_only_signals(client):
    html = body(client, "?show=crossed")
    assert "profitable-and-signalled" in html
    assert "profitable-but-below-threshold" not in html


def test_an_unknown_show_value_falls_back_to_the_default(client):
    """A hand-edited URL should not silently widen the view."""
    html = body(client, "?show=whatever")
    assert "never-worth-buying" not in html


def test_old_crossed_links_still_work(client):
    """Bookmarks made before this filter existed used ?crossed=1."""
    html = body(client, "?crossed=1")
    assert "profitable-and-signalled" in html
    assert "profitable-but-below-threshold" not in html


# =====================================================================
# Saying what is hidden
# =====================================================================


def test_the_page_reports_the_full_count_even_when_filtered(client):
    """
    Hiding most rows is only honest if the page says how many there are.
    """
    html = body(client)
    # The "show everything" option carries the full count, so the two rows
    # on screen are never mistaken for everything that was recorded.
    assert "شامل نزدیک‌شده‌ها (4)" in html


def test_the_default_view_explains_itself(client):
    html = body(client)
    assert "لبه‌ی مثبت" in html


def test_the_other_slices_are_one_click_away(client):
    html = body(client)
    assert 'value="all"' in html
    assert 'value="crossed"' in html


# =====================================================================
# Filters compose with the slice
# =====================================================================


def test_a_range_filter_narrows_within_the_slice(client):
    # The box is typed in percent, so 0.5 means an edge of 0.005.
    html = body(client, "?show=all&min_best_edge=0.5")
    assert "profitable-and-signalled" in html
    assert "profitable-but-below-threshold" not in html
    assert "never-worth-buying" not in html


def test_an_empty_result_offers_the_wider_view_rather_than_looking_broken(client):
    """
    "No windows recorded yet" would be a lie here — there are four, the
    filter just excluded them all.
    """
    html = body(client, "?min_best_profit=99999")
    assert "هنوز پنجره‌ای ثبت نشده" not in html
    assert "show=all" in html


def test_sorting_keeps_the_slice(client):
    """Clicking a column header must not quietly reveal the hidden rows."""
    html = body(client, "?sort=best_edge&dir=asc")
    assert "never-worth-buying" not in html
