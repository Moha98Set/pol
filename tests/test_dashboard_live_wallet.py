"""
The live wallet page.

The wallet's own arithmetic is covered in test_livewallet.py; these hold
onto what the page must not get wrong — showing a wallet that is merely
switched off as though it were a wallet that lost all its money, and
quietly dropping the refusals, which are most of what there is to see.
"""

import pytest

import dashboard
import db as dblib
import livewallet


class FakeEvent:
    slug = "ev"
    end_date = None


def priced(edge=0.010, sum_asks=0.990, fillable=500.0):
    return "yes", {"sum_best_asks": sum_asks, "num_legs": 3,
                   "best": {"real_cost": fillable}}, edge


def signal(slug="ev", edge=0.010):
    return {"event_slug": slug, "event_title": slug, "side": "yes",
            "num_outcomes": 3, "payout_per_basket": 1.0, "fee_rate": 0.02,
            "best_net_edge": edge, "best_sum_asks": 0.990, "age_ms": 250.0,
            "first_seen_ts": None, "url": ""}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def build(setup=None):
        path = tmp_path / "arb.db"
        conn = dblib.connect(path, check_same_thread=False)
        if setup:
            setup(conn)
        conn.close()

        monkeypatch.setattr(dashboard, "DB_PATH", path)
        dashboard.app.config["TESTING"] = True
        c = dashboard.app.test_client()
        with c.session_transaction() as s:
            s["user"] = "tester"
        return c
    return build


def body(client, query=""):
    res = client.get("/live-wallet" + query)
    assert res.status_code == 200
    return res.get_data(as_text=True)


def test_a_wallet_that_was_never_switched_on_says_so(make_client):
    """
    The tables exist as soon as the schema is applied, so an empty page
    here means "not enabled", not "lost everything". Saying the wrong one
    would be alarming and wrong.
    """
    html = body(make_client())
    assert "روشن نشده" in html
    assert "PAPER_LIVE_ENABLED" in html


def test_a_running_wallet_shows_its_totals(make_client):
    def setup(conn):
        w = livewallet.LiveWallet(conn, lambda e: priced(), start_cash=1000.0)
        w.consider(signal(), FakeEvent())

    html = body(make_client(setup))
    assert "روشن نشده" not in html
    assert "کل کیف (ناخالص)" in html
    assert "$1,000.00" in html      # equity is conserved by a purchase


def test_refusals_are_shown_not_only_purchases(make_client):
    def setup(conn):
        w = livewallet.LiveWallet(conn, lambda e: priced(edge=-0.004),
                                  start_cash=1000.0)
        w.consider(signal(edge=0.020), FakeEvent())

    html = body(make_client(setup))
    assert livewallet.SKIP_LABELS[livewallet.SKIP_EDGE_GONE] in html


def test_the_measured_latency_is_reported(make_client):
    """The one number the replay cannot produce about itself."""
    import time

    def setup(conn):
        w = livewallet.LiveWallet(conn, lambda e: priced(), start_cash=1000.0)
        sig = signal()
        sig["first_seen_ts"] = time.time() - 1.2
        w.consider(sig, FakeEvent())

    html = body(make_client(setup))
    assert "تأخیر واقعی" in html


def test_the_decision_table_can_be_filtered_to_refusals(make_client):
    def setup(conn):
        w = livewallet.LiveWallet(conn, lambda e: priced(), start_cash=1000.0)
        w.consider(signal(slug="bought"), FakeEvent())
        w.consider(signal(slug="bought"), FakeEvent())   # duplicate, refused

    html = body(make_client(setup), "?taken=0")
    assert livewallet.SKIP_LABELS[livewallet.SKIP_DUPLICATE] in html
