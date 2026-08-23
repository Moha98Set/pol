"""
Tests for the funnel counters.

The funnel's job is to make "nothing was found" explainable. These tests
pin the arithmetic (survivors at each stage) and the persistence, because
a miscounted funnel is worse than none: it sends you looking in the wrong
place with confidence.
"""

import sqlite3

import pytest

import db as dblib
import metrics


@pytest.fixture
def database(tmp_path):
    db = dblib.connect(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def scan_id(database):
    return dblib.start_scan(database)


# =====================================================================
# Counting
# =====================================================================


def test_a_fresh_funnel_is_empty():
    funnel = metrics.Funnel()
    assert funnel.events_seen == 0
    assert funnel.total_rejected() == 0
    assert funnel.top_reasons() == []


def test_rejections_are_counted_per_stage_and_code():
    funnel = metrics.Funnel(1)
    funnel.saw_event(100)
    funnel.reject("prefilter", "low_volume", 60)
    funnel.reject("prefilter", "not_neg_risk", 12)
    funnel.reject("book", "dry_leg", 20)

    assert funnel.total_rejected("prefilter") == 72
    assert funnel.total_rejected("book") == 20
    assert funnel.total_rejected() == 92


def test_top_reasons_are_ranked_across_stages():
    funnel = metrics.Funnel()
    funnel.reject("prefilter", "low_volume", 5)
    funnel.reject("book", "dry_leg", 40)
    funnel.reject("edge", "below_min_edge", 12)

    assert funnel.top_reasons(2) == [("dry_leg", 40), ("below_min_edge", 12)]


def test_the_same_code_at_two_stages_is_summed_in_top_reasons():
    funnel = metrics.Funnel()
    funnel.reject("book", "dry_leg", 10)
    funnel.reject("basket", "dry_leg", 5)
    assert funnel.top_reasons() == [("dry_leg", 15)]


def test_suspicions_are_counted_separately_from_rejections():
    """
    Suspicions did not reject anything, so they must never inflate the
    rejection total — otherwise the funnel stops adding up.
    """
    funnel = metrics.Funnel()
    funnel.saw_event(10)
    funnel.suspect(["thin_book", "no_end_date"])
    funnel.suspect(["thin_book"])

    assert funnel.total_rejected() == 0
    assert funnel.suspicions["thin_book"] == 2
    assert funnel.suspicions["no_end_date"] == 1


def test_timings_accumulate_per_phase():
    funnel = metrics.Funnel()
    funnel.timing("books", 120.5)
    funnel.timing("books", 80.0)
    funnel.timing("fetch", 10.0)
    assert funnel.timings["books"] == pytest.approx(200.5)
    assert funnel.timings["fetch"] == pytest.approx(10.0)


def test_an_unknown_stage_is_accepted():
    """A new stage must not need a schema change to start counting."""
    funnel = metrics.Funnel()
    funnel.reject("some_new_stage", "some_code")
    assert funnel.total_rejected() == 1


# =====================================================================
# Rendering
# =====================================================================


def test_render_shows_survivors_after_each_stage():
    funnel = metrics.Funnel(7)
    funnel.saw_event(1000)
    funnel.reject("prefilter", "low_volume", 900)
    funnel.reject("book", "dry_leg", 60)
    funnel.analysed(100)
    funnel.opportunities = 2

    text = funnel.render()
    assert "scan #7" in text
    assert "1000" in text
    assert "-> 100 remain" in text     # 1000 - 900
    assert "-> 40 remain" in text      # 100 - 60
    assert "low_volume" in text


def test_render_shows_percentages_of_the_total():
    funnel = metrics.Funnel()
    funnel.saw_event(200)
    funnel.reject("prefilter", "low_volume", 50)
    assert "25.0%" in funnel.render()


def test_render_survives_a_scan_that_saw_nothing():
    """Division by zero here would crash the scan at its last line."""
    text = metrics.Funnel(1).render()
    assert "Scan funnel" in text


def test_render_lists_suspicions_and_timings():
    funnel = metrics.Funnel()
    funnel.saw_event(10)
    funnel.suspect(["thin_book"])
    funnel.timing("fetch", 2500)
    text = funnel.render()
    assert "Flagged but not rejected" in text
    assert "thin_book" in text
    assert "2.5s" in text


# =====================================================================
# Persistence
# =====================================================================


def test_save_writes_one_row_per_stage_and_code(database, scan_id):
    funnel = metrics.Funnel(scan_id)
    funnel.reject("prefilter", "low_volume", 60)
    funnel.reject("book", "dry_leg", 20)
    funnel.suspect(["thin_book"])
    funnel.save(database)

    rows = database.execute(
        "SELECT stage, code, count FROM rejections ORDER BY code").fetchall()
    assert len(rows) == 3
    assert (rows[0]["stage"], rows[0]["code"], rows[0]["count"]) == \
           ("book", "dry_leg", 20)


def test_save_persists_timings(database, scan_id):
    funnel = metrics.Funnel(scan_id)
    funnel.timing("fetch", 1234.5)
    funnel.save(database)

    row = database.execute(
        "SELECT phase, duration_ms FROM scan_timings").fetchone()
    assert row["phase"] == "fetch"
    assert row["duration_ms"] == pytest.approx(1234.5)


def test_save_without_a_scan_id_is_refused():
    """A funnel with no scan is unattributable data; better to fail loudly."""
    with pytest.raises(ValueError, match="scan_id"):
        metrics.Funnel().save(None)


def test_saving_an_empty_funnel_writes_nothing(database, scan_id):
    metrics.Funnel(scan_id).save(database)
    assert database.execute("SELECT COUNT(*) c FROM rejections").fetchone()["c"] == 0


def test_funnel_for_scan_reads_back_what_was_written(database, scan_id):
    funnel = metrics.Funnel(scan_id)
    funnel.reject("prefilter", "low_volume", 60)
    funnel.reject("book", "dry_leg", 20)
    funnel.save(database)

    assert metrics.funnel_for_scan(database, scan_id) == {
        "prefilter": {"low_volume": 60},
        "book": {"dry_leg": 20},
    }


# =====================================================================
# History — the drift detector
# =====================================================================


def _finished_scan(db, events_total, **codes):
    scan_id = dblib.start_scan(db)
    funnel = metrics.Funnel(scan_id)
    funnel.saw_event(events_total)
    for code, count in codes.items():
        funnel.reject("book", code, count)
    funnel.save(db)
    dblib.finish_scan(db, scan_id, events_total=events_total,
                      events_scanned=0, events_skipped_filter=0,
                      opportunities_found=0, near_misses_saved=0, errors=0)
    return scan_id


def test_rejection_trend_returns_scans_oldest_first(database):
    _finished_scan(database, 1000, dry_leg=30)
    _finished_scan(database, 1000, dry_leg=400)

    trend = metrics.rejection_trend(database, "dry_leg")
    assert [row[3] for row in trend] == [30, 400]


def test_rejection_trend_includes_scans_where_the_code_never_fired(database):
    """
    A gap is data. Showing only the scans that hit the code would hide the
    moment it started, which is exactly what you are looking for.
    """
    _finished_scan(database, 1000)
    _finished_scan(database, 1000, dry_leg=400)

    trend = metrics.rejection_trend(database, "dry_leg")
    assert [row[3] for row in trend] == [0, 400]


def test_top_rejection_codes_ignores_suspicions(database, scan_id):
    funnel = metrics.Funnel(scan_id)
    funnel.reject("book", "dry_leg", 20)
    funnel.suspect(["thin_book"] * 99)
    funnel.save(database)

    codes = dict(metrics.top_rejection_codes(database))
    assert codes == {"dry_leg": 20}


def test_phase_timings_averages_recent_scans(database):
    for ms in (100.0, 300.0):
        scan_id = dblib.start_scan(database)
        funnel = metrics.Funnel(scan_id)
        funnel.timing("fetch", ms)
        funnel.save(database)

    assert metrics.phase_timings(database)["fetch"] == pytest.approx(200.0)


# =====================================================================
# Schema
# =====================================================================


def test_connect_creates_the_metrics_tables(tmp_path):
    """
    metrics owns its DDL, but db.connect() must apply it — otherwise the
    first scan on a fresh database crashes at the very last step.
    """
    db = dblib.connect(tmp_path / "fresh.db")
    tables = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"rejections", "scan_timings"} <= tables


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    dblib.connect(path).close()
    dblib.connect(path).close()   # must not raise on the second run
