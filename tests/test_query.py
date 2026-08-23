"""
Tests for the query CLI.

The health command is the one that matters: it is what gets run to decide
whether the system is working. A health check that reports "fine" when the
scan analysed nothing is worse than no health check, so most of these tests
are about it failing when it should.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import db as dblib
import metrics
import query


@pytest.fixture
def database(tmp_path):
    db = dblib.connect(tmp_path / "q.db")
    yield db
    db.close()


class Args:
    """Stand-in for the parsed argparse namespace."""
    def __init__(self, **kw):
        self.scans = kw.get("scans", 20)
        self.limit = kw.get("limit", 25)
        self.stale_after = kw.get("stale_after", 3600)
        self.statement = kw.get("statement", "")
        self.json = kw.get("json", False)


def finished_scan(db, *, events_total=1000, events_scanned=100,
                  opportunities=0, errors=0, status="done", **codes):
    scan_id = dblib.start_scan(db)
    funnel = metrics.Funnel(scan_id)
    funnel.saw_event(events_total)
    for code, count in codes.items():
        funnel.reject("prefilter", code, count)
    funnel.save(db)
    dblib.finish_scan(db, scan_id, events_total=events_total,
                      events_scanned=events_scanned,
                      events_skipped_filter=events_total - events_scanned,
                      opportunities_found=opportunities,
                      near_misses_saved=0, errors=errors, status=status)
    return scan_id


def opportunity(db, scan_id, **kw):
    dblib.save_opportunity(db, scan_id, {
        "market_type": kw.get("market_type", "binary"),
        "event_title": kw.get("title", "Some event"),
        "event_slug": kw.get("slug", "some-event"),
        "num_outcomes": 2,
        "sum_best_asks": 0.95,
        "gross_edge": 0.05,
        "net_edge": kw.get("net_edge", 0.05),
        "fee_rate": kw.get("fee_rate", 0.04),
        "best_capital": 100,
        "best_shares": 100,
        "best_real_cost": 95,
        "best_profit": kw.get("profit", 5.0),
        "best_roi_pct": 5.2,
        "volume_24h": 50000,
        "slippage_curve": [],
        "legs_detail": [],
        "suspicions": kw.get("suspicions", []),
        "url": "https://polymarket.com/event/some-event",
    })


# =====================================================================
# health
# =====================================================================


def test_health_reports_a_problem_when_there_are_no_scans(database, capsys):
    assert query.cmd_health(database, Args()) == 1
    assert "No scans recorded" in capsys.readouterr().out


def test_health_is_clean_on_a_normal_scan(database, capsys):
    finished_scan(database, events_total=1000, events_scanned=120)
    assert query.cmd_health(database, Args()) == 0
    assert "No problems detected" in capsys.readouterr().out


def test_health_flags_a_scan_that_fetched_nothing(database, capsys):
    """Zero events is the network or the API, never a quiet market."""
    finished_scan(database, events_total=0, events_scanned=0)
    assert query.cmd_health(database, Args()) == 1
    assert "zero events" in capsys.readouterr().out


def test_health_flags_everything_being_filtered_out(database, capsys):
    """
    The failure this whole command exists for: the monitor runs perfectly,
    logs nothing alarming, and analyses zero of three thousand events
    because a filter is wrong. In a log file that is indistinguishable
    from a day with no arbitrage.
    """
    finished_scan(database, events_total=3000, events_scanned=0)
    assert query.cmd_health(database, Args()) == 1
    assert "every event was filtered out" in capsys.readouterr().out


def test_health_flags_a_failed_scan(database, capsys):
    finished_scan(database, status="failed")
    assert query.cmd_health(database, Args()) == 1
    assert "did not finish" in capsys.readouterr().out


def test_health_flags_a_high_error_rate(database, capsys):
    finished_scan(database, events_scanned=100, errors=30)
    assert query.cmd_health(database, Args()) == 1
    assert "errored" in capsys.readouterr().out


def test_a_few_errors_are_tolerated(database, capsys):
    """Some events are always malformed; a couple is not a problem."""
    finished_scan(database, events_scanned=100, errors=2)
    assert query.cmd_health(database, Args()) == 0


def test_health_flags_a_stale_monitor(database, capsys):
    """A scan that finished hours ago means the loop died quietly."""
    scan_id = finished_scan(database)
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    database.execute("UPDATE scans SET started_at = ? WHERE id = ?",
                     (old, scan_id))
    database.commit()

    assert query.cmd_health(database, Args(stale_after=3600)) == 1
    assert "is the monitor running" in capsys.readouterr().out


def test_a_recent_scan_is_not_stale(database):
    finished_scan(database)
    assert query.cmd_health(database, Args(stale_after=3600)) == 0


def test_health_flags_repeated_failures(database, capsys):
    for _ in range(5):
        finished_scan(database, status="failed")
    assert query.cmd_health(database, Args()) == 1
    assert "of the last 10 scans failed" in capsys.readouterr().out


# =====================================================================
# funnel
# =====================================================================


def test_funnel_shows_reasons_ranked_by_count(database, capsys):
    scan_id = finished_scan(database, events_total=1000,
                            low_volume=800, not_neg_risk=100)
    query.cmd_funnel(database, Args())

    out = capsys.readouterr().out
    assert "low_volume" in out
    assert out.index("low_volume") < out.index("not_neg_risk")
    assert "80.0%" in out


def test_funnel_says_so_when_there_is_no_data(database, capsys):
    finished_scan(database)
    query.cmd_funnel(database, Args())
    assert "No funnel data" in capsys.readouterr().out


# =====================================================================
# drift
# =====================================================================


def test_drift_marks_an_order_of_magnitude_change(database, capsys):
    """
    The silent-breakage detector: nothing errors, but dry legs go from
    rare to dominant because something upstream changed shape.
    """
    for _ in range(3):
        finished_scan(database, dry_leg=10)
    for _ in range(3):
        finished_scan(database, dry_leg=400)

    query.cmd_drift(database, Args(scans=3))
    out = capsys.readouterr().out
    assert "dry_leg" in out
    assert "big change" in out


def test_drift_labels_a_reason_that_never_fired_before_as_new(database, capsys):
    for _ in range(2):
        finished_scan(database, low_volume=100)
    for _ in range(2):
        finished_scan(database, low_volume=100, crossed_book=50)

    query.cmd_drift(database, Args(scans=2))
    assert "new" in capsys.readouterr().out


def test_drift_is_quiet_without_history(database, capsys):
    query.cmd_drift(database, Args())
    assert "Not enough history" in capsys.readouterr().out


# =====================================================================
# opportunities and suspects
# =====================================================================


def test_opps_ranks_by_return_not_by_dollar_profit(database, capsys):
    """
    Sorting by profit floats NO-side baskets to the top for no reason but
    their size: they earn their dollars on N-1 times the capital. The
    ranking has to be edge per dollar, so a small, efficient trade beats a
    large, capital-hungry one.
    """
    scan_id = finished_scan(database)
    opportunity(database, scan_id, title="Fat but slow", profit=90.0,
                net_edge=0.001)
    opportunity(database, scan_id, title="Small but sharp", profit=1.0,
                net_edge=0.05)

    query.cmd_opps(database, Args())
    out = capsys.readouterr().out
    assert out.index("Small but sharp") < out.index("Fat but slow")


def test_opps_points_at_the_funnel_when_empty(database, capsys):
    finished_scan(database)
    query.cmd_opps(database, Args())
    assert "funnel" in capsys.readouterr().out


def test_opps_shows_flags_inline(database, capsys):
    scan_id = finished_scan(database)
    opportunity(database, scan_id, suspicions=["thin_book"])
    query.cmd_opps(database, Args())
    assert "thin_book" in capsys.readouterr().out


def test_suspects_lists_only_flagged_opportunities(database, capsys):
    scan_id = finished_scan(database)
    opportunity(database, scan_id, title="Clean one", suspicions=[])
    opportunity(database, scan_id, title="Dodgy one", suspicions=["thin_book"])

    query.cmd_suspects(database, Args())
    out = capsys.readouterr().out
    assert "Dodgy one" in out
    assert "Clean one" not in out


# =====================================================================
# fees and timings
# =====================================================================


def test_fees_groups_by_rate(database, capsys):
    scan_id = finished_scan(database)
    opportunity(database, scan_id, fee_rate=0.0, profit=10)
    opportunity(database, scan_id, fee_rate=0.0, profit=5)
    opportunity(database, scan_id, fee_rate=0.07, profit=1)

    query.cmd_fees(database, Args())
    out = capsys.readouterr().out
    assert "0%" in out and "7%" in out
    assert "$15.00" in out


def test_timings_shows_the_share_per_phase(database, capsys):
    scan_id = dblib.start_scan(database)
    funnel = metrics.Funnel(scan_id)
    funnel.timing("fetch", 1000)
    funnel.timing("analysis", 3000)
    funnel.save(database)

    query.cmd_timings(database, Args())
    out = capsys.readouterr().out
    assert "analysis" in out
    assert "75.0%" in out


# =====================================================================
# sql escape hatch
# =====================================================================


def test_sql_runs_a_select(database, capsys):
    finished_scan(database)
    assert query.cmd_sql(database, Args(
        statement="SELECT COUNT(*) AS n FROM scans")) == 0
    assert "n" in capsys.readouterr().out


def test_sql_refuses_to_write(database, capsys):
    """
    A read-only tool that can DROP TABLE is not a read-only tool. This is
    the difference between a typo costing a query and costing the history.
    """
    assert query.cmd_sql(database, Args(
        statement="DELETE FROM scans")) == 1
    assert "Only SELECT" in capsys.readouterr().out
    assert database.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"] == 0


def test_sql_allows_a_cte(database, capsys):
    finished_scan(database)
    assert query.cmd_sql(database, Args(
        statement="WITH x AS (SELECT 1 AS a) SELECT * FROM x")) == 0


# =====================================================================
# CLI plumbing
# =====================================================================


def test_main_exits_nonzero_when_health_finds_a_problem(tmp_path):
    path = tmp_path / "cli.db"
    dblib.connect(path).close()
    assert query.main(["health", "--db", str(path)]) == 1


def test_main_reports_an_unopenable_database(capsys):
    assert query.main(["health", "--db", "/nonexistent/dir/x.db"]) == 2
    assert "Cannot open" in capsys.readouterr().out


def test_json_health_is_machine_readable(tmp_path, capsys):
    path = tmp_path / "cli.db"
    db = dblib.connect(path)
    finished_scan(db, events_total=42)
    db.close()

    query.main(["health", "--json", "--db", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["events_total"] == 42
