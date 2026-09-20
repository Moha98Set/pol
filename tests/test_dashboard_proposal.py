"""
The proposal page.

An explainer that quotes figures is only worth having if the figures come
from the database it is explaining — otherwise it goes stale silently and
starts lying to the analyst it exists to help. These tests are about that
property, plus the page surviving a database that has nothing in it yet.
"""

import pytest

import db as dblib
import dashboard


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "arb.db"
    conn = dblib.connect(path)
    scan = dblib.start_scan(conn)
    conn.execute("UPDATE scans SET events_total = 1234 WHERE id = ?", (scan,))
    for i in range(3):
        conn.execute("""INSERT INTO opportunities (scan_id, found_at,
            market_type, event_slug, net_edge) VALUES (?, '2026-09-01', 'binary',
            ?, 0.01)""", (scan, f"o{i}"))
    # two real signals and one flicker
    add_window(conn, "real-1", ticks=40, ms=60000, crossed=1)
    add_window(conn, "real-2", ticks=12, ms=8000, crossed=1)
    add_window(conn, "phantom", ticks=1, ms=0, crossed=1)
    conn.commit()
    conn.close()

    monkeypatch.setattr(dashboard, "DB_PATH", path)
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user"] = "tester"
        yield c


def add_window(conn, slug, *, ticks, ms, crossed):
    conn.execute("""
        INSERT INTO edge_windows (event_slug, event_title, side, num_outcomes,
            fee_rate, payout, opened_at, closed_at, duration_ms, ticks,
            opened_edge, best_edge, best_sum_asks, crossed, url)
        VALUES (?, ?, 'yes', 3, 0.02, 1.0, '2026-09-01T00:00:00+00:00',
                '2026-09-01T00:01:00+00:00', ?, ?, 0.01, 0.01, 0.99, ?, '')
    """, (slug, slug, ms, ticks, crossed))


def body(client):
    res = client.get("/proposal")
    assert res.status_code == 200
    return res.get_data(as_text=True)


def test_the_page_is_in_the_navigation(client):
    assert "/proposal" in body(client)


def test_the_counts_are_read_from_the_database(client):
    html = body(client)
    assert "3" in html                      # three opportunities
    assert "1,234" in html                  # events seen by the scanner


def test_flicker_signals_are_excluded_from_the_signal_count(client):
    """
    Two real signals and one phantom. Reporting three would repeat, on the
    page that explains the bug, the very mistake it describes.
    """
    html = body(client)
    assert "1 سیگنال تقلبی شناسایی" in html     # the phantom, named as one
    assert ">2<" in html.replace(" ", "").replace("\n", "")  # the two real ones


def test_the_settings_it_quotes_are_the_live_ones(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "LIVE_TOP_N", 321)
    assert "321" in body(client)


def test_an_empty_database_does_not_break_the_page(tmp_path, monkeypatch):
    """A fresh deployment opens this page before any scan has run."""
    path = tmp_path / "fresh.db"
    dblib.connect(path).close()
    monkeypatch.setattr(dashboard, "DB_PATH", path)
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user"] = "tester"
        assert c.get("/proposal").status_code == 200


def test_the_median_ignores_the_phantoms(client):
    """
    Flicker windows all have zero duration and outnumber real signals, so
    a median over every crossing reports 0ms — the page would repeat the
    distortion it exists to explain.
    """
    from flask import template_rendered

    seen = []
    def record(sender, template, context, **extra):
        seen.append(context)
    template_rendered.connect(record, dashboard.app)
    try:
        client.get("/proposal")
    finally:
        template_rendered.disconnect(record, dashboard.app)

    # Real signals are 8s and 60s; the phantom is 0. Over all three the
    # median lands on 8s, over the real two it lands on 60s.
    assert seen[0]["f"]["median_window"] == 60000
