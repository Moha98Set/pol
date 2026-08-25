"""
End-to-end tests: API payload in, database row out.

The unit tests prove each part is right. These prove the parts are still
connected — which is a different question, and the one that actually breaks
during a refactor. Everything here runs against tests/fakeapi.py, so the
suite stays offline and deterministic.
"""

import json

import pytest

import arb_monitor
import config
import db as dblib
import logging_setup
import metrics
import scanner
import validate
from tests import fakeapi


@pytest.fixture
def api(monkeypatch):
    fake = fakeapi.FakeAPI()
    monkeypatch.setattr(scanner, "SESSION", fake)
    monkeypatch.setattr(scanner, "API_SLEEP", 0)
    return fake


@pytest.fixture
def database(tmp_path):
    db = dblib.connect(tmp_path / "integration.db")
    yield db
    db.close()


# =====================================================================
# Fetch and pagination
# =====================================================================


def test_fetch_pages_until_the_api_runs_out(api):
    api.page_size = 100
    api.add_many(fakeapi.binary_event(slug=f"e{i}") for i in range(250))

    events = scanner.fetch_all_events()

    assert len(events) == 250
    offsets = [params.get("offset") for _url, params in api.get_calls]
    # the third page comes back short (50), which ends the loop — no
    # wasted fourth request for a page that cannot exist
    assert offsets == [0, 100, 200]


def test_fetch_is_uncapped_by_default(api):
    """
    The monitor's job is completeness. A cap that crept into the default
    would silently shrink coverage while everything still looked healthy.
    """
    api.add_many(fakeapi.binary_event(slug=f"e{i}") for i in range(150))
    assert len(scanner.fetch_all_events()) == 150


def test_fetch_stops_early_when_capped(api):
    api.add_many(fakeapi.binary_event(slug=f"e{i}") for i in range(500))

    events = scanner.fetch_all_events(max_events=100)

    assert len(events) == 100
    assert len(api.get_calls) == 1      # stopped after one page, not five


def test_a_capped_fetch_keeps_the_most_liquid_events(api):
    """
    Because the API orders by volume, the cap takes the top slice. A fast
    run is therefore a smaller run, not a differently-biased one.
    """
    api.add_many(fakeapi.binary_event(slug=f"e{i}", volume=100_000 - i * 100)
                 for i in range(300))

    events = scanner.fetch_all_events(max_events=50)

    volumes = [e["markets"][0]["volume24hr"] for e in events]
    assert volumes == sorted(volumes, reverse=True)
    assert volumes[0] == 100_000


def test_fetch_orders_by_volume_not_by_id(api):
    """
    Ordering by id returns the newest events, which all have zero volume.
    The whole scan would then look at markets that have never traded.
    """
    api.add(fakeapi.binary_event())
    scanner.fetch_all_events()

    _url, params = api.get_calls[0]
    assert params["order"] == "volume24hr"
    assert params["ascending"] == "false"


def test_books_are_fetched_in_one_batched_call(api):
    """
    One POST per 100 tokens, not one per token. A regression here is a
    100x increase in API calls that produces identical numbers, so nothing
    but this test would catch it.
    """
    tokens = [f"tok-{i}" for i in range(250)]
    api.books = {t: fakeapi.book([(0.5, 10)]) for t in tokens}

    books = scanner.fetch_order_books(tokens)

    assert len(books) == 250
    assert api.book_requests == 3          # 100 + 100 + 50


def test_a_transient_book_failure_is_retried(api):
    api.books = {"tok": fakeapi.book([(0.5, 10)])}
    api.fail_books_times = 1

    assert scanner.fetch_order_books(["tok"]) != {}


def test_a_persistent_book_failure_gives_up_without_raising(api):
    """A broken endpoint must degrade the scan, not end it."""
    api.books = {"tok": fakeapi.book([(0.5, 10)])}
    api.fail_books_times = 99

    assert scanner.fetch_order_books(["tok"]) == {}


# =====================================================================
# Scan -> verdict
# =====================================================================


def test_a_binary_arbitrage_is_found_end_to_end(api):
    api.add(fakeapi.binary_event(yes=(0.40, 100), no=(0.55, 100)))

    events = scanner.fetch_all_events()
    result = scanner.scan_event(scanner.prefilter_event(events[0]))

    assert result["kind"] == "opportunity"
    assert result["sum_best_asks"] == pytest.approx(0.95)
    assert result["best_profit"] > 0
    assert result["legs_detail"][0]["token_id"]    # carried for the executor


def test_a_multi_outcome_arbitrage_is_found_end_to_end(api):
    api.add(fakeapi.multi_event(legs=((0.30, 500), (0.30, 500), (0.30, 500))))

    events = scanner.fetch_all_events()
    result = scanner.scan_event(scanner.prefilter_event(events[0]))

    assert result["kind"] == "opportunity"
    assert result["num_outcomes"] == 3
    assert result["sum_best_asks"] == pytest.approx(0.90)


def test_an_efficiently_priced_market_yields_nothing(api):
    """The normal case. Most of the time there is no edge, and that is fine."""
    api.add(fakeapi.binary_event(yes=(0.52, 100), no=(0.50, 100)))

    events = scanner.fetch_all_events()
    result = scanner.scan_event(scanner.prefilter_event(events[0]))

    assert result is None or result["kind"] == "near_miss"


def test_a_crypto_fee_can_erase_an_edge_a_free_market_would_keep(api):
    """
    Same book, different tag. 7% crypto vs 0% geopolitics is the difference
    between a trade and a loss, and it is decided by event metadata alone.
    """
    api.add(fakeapi.binary_event(slug="geo", yes=(0.49, 100), no=(0.50, 100),
                                 tags=("Geopolitics",)))
    api.add(fakeapi.binary_event(slug="btc", yes=(0.49, 100), no=(0.50, 100),
                                 tags=("Crypto",)))

    events = scanner.fetch_all_events()
    free = scanner.scan_event(scanner.prefilter_event(events[0]))
    charged = scanner.scan_event(scanner.prefilter_event(events[1]))

    assert free["kind"] == "opportunity"
    assert charged is None or charged["kind"] == "near_miss"


# =====================================================================
# Validation, end to end
# =====================================================================


def test_a_dry_leg_kills_the_whole_basket(api):
    event, books = fakeapi.multi_event()
    api.add((event, books))
    api.books[f"{event['slug']}-1"] = fakeapi.book([])      # no sellers

    events = scanner.fetch_all_events()
    assert scanner.scan_event(scanner.prefilter_event(events[0])) is None


def test_a_crossed_book_is_rejected(api):
    """Bid above ask means one side is stale — neither can be trusted."""
    event, books = fakeapi.binary_event()
    books[f"{event['slug']}-yes"] = fakeapi.book(
        [(0.40, 100)], bids=[(0.55, 100)])
    api.add((event, books))

    events = scanner.fetch_all_events()
    assert scanner.scan_event(scanner.prefilter_event(events[0])) is None


def test_a_non_negrisk_multi_event_never_reaches_the_book(api):
    """
    The filter must run before any API call. Fetching books for events that
    can never qualify is the main way a scan gets slow.
    """
    api.add(fakeapi.multi_event(neg_risk=False))

    events = scanner.fetch_all_events()
    group, verdict = scanner.prefilter_event_verbose(events[0])

    assert group is None
    assert verdict.code == validate.NOT_NEG_RISK
    assert api.book_requests == 0


def test_a_dry_leg_is_reported_as_a_dry_leg(api):
    """
    Before this, a quarter of all events were rejected inside the scan and
    landed in the funnel as one anonymous bucket. "534 events rejected"
    with no reason is not diagnosis, it is a shrug.
    """
    event, books = fakeapi.multi_event()
    api.add((event, books))
    api.books[f"{event['slug']}-1"] = fakeapi.book([])

    events = scanner.fetch_all_events()
    result, verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert result is None
    assert verdict.code == validate.DRY_LEG
    assert "Candidate 1" in verdict.detail


def test_a_crossed_book_is_reported_as_crossed(api):
    event, books = fakeapi.binary_event()
    books[f"{event['slug']}-yes"] = fakeapi.book(
        [(0.40, 100)], bids=[(0.55, 100)])
    api.add((event, books))

    events = scanner.fetch_all_events()
    _result, verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))
    assert verdict.code == validate.CROSSED_BOOK


def test_an_efficient_market_is_not_reported_as_a_fault(api):
    """
    The distinction that keeps the funnel honest: a market priced correctly
    today is not a broken pipeline. These get their own codes so a healthy
    scan cannot be mistaken for a failing one.
    """
    api.add(fakeapi.binary_event(yes=(0.52, 100), no=(0.50, 100)))

    events = scanner.fetch_all_events()
    _result, verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert verdict.code in (validate.BELOW_MIN_EDGE, validate.FAR_BELOW_EDGE)
    assert verdict.code not in (validate.DRY_LEG, validate.CROSSED_BOOK)


def test_a_microscopic_book_still_fills_and_is_flagged(api):
    """
    Two cents of depth is not "unfillable" — the engine buys the 0.02
    shares that exist and reports the fraction of a cent it makes. What
    protects you here is not rejection but the flag: thin_book, plus a
    max size that speaks for itself.
    """
    api.add(fakeapi.binary_event(yes=(0.40, 0.02), no=(0.55, 0.02),
                                 extra_levels=False))

    events = scanner.fetch_all_events()
    result, _verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert result["kind"] == "opportunity"
    assert result["best_profit"] < 0.01
    assert validate.THIN_BOOK in result["suspicions"]


def test_an_edge_with_no_fillable_size_says_so(api, monkeypatch):
    """
    An edge at the best ask that no size can capture. Rare in practice —
    any positive edge is usually profitable at *some* size — so the branch
    is exercised directly rather than by contriving a book that would not
    occur.
    """
    monkeypatch.setattr(scanner, "compute_slippage_curve",
                        lambda *a, **k: [])
    api.add(fakeapi.binary_event(yes=(0.40, 100), no=(0.55, 100)))

    events = scanner.fetch_all_events()
    result, verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert verdict.code == validate.NO_FILLABLE_SIZE
    assert result["kind"] == "near_miss"


def test_a_verbose_scan_reports_a_clean_opportunity(api):
    api.add(fakeapi.binary_event(yes=(0.40, 100), no=(0.55, 100)))
    events = scanner.fetch_all_events()
    result, verdict = scanner.scan_event_verbose(
        scanner.prefilter_event(events[0]))

    assert result["kind"] == "opportunity"
    assert verdict.ok
    assert verdict.code == validate.OK


def test_scan_event_still_returns_just_the_result(api):
    """The plain wrapper stays a drop-in for every existing caller."""
    api.add(fakeapi.binary_event(yes=(0.40, 100), no=(0.55, 100)))
    events = scanner.fetch_all_events()
    result = scanner.scan_event(scanner.prefilter_event(events[0]))
    assert isinstance(result, dict)
    assert result["kind"] == "opportunity"


def test_the_funnel_breaks_down_scan_rejections_by_reason(api, database):
    """End to end: distinct causes must land in distinct funnel buckets."""
    dry_event, dry_books = fakeapi.multi_event(slug="dry")
    dry_books["dry-1"] = fakeapi.book([])
    api.add((dry_event, dry_books))

    crossed_event, crossed_books = fakeapi.binary_event(slug="crossed")
    crossed_books["crossed-yes"] = fakeapi.book([(0.40, 100)],
                                                bids=[(0.55, 100)])
    api.add((crossed_event, crossed_books))

    api.add(fakeapi.binary_event(slug="efficient", yes=(0.52, 100),
                                 no=(0.50, 100)))

    arb_monitor.run_scan(database)

    scan_id = database.execute("SELECT MAX(id) id FROM scans").fetchone()["id"]
    book_stage = metrics.funnel_for_scan(database, scan_id).get("book", {})

    assert book_stage.get(validate.DRY_LEG) == 1
    assert book_stage.get(validate.CROSSED_BOOK) == 1
    assert "rejected_in_scan" not in book_stage


def test_a_low_volume_event_is_rejected_with_a_reason(api):
    api.add(fakeapi.binary_event(volume=10))
    events = scanner.fetch_all_events()
    _group, verdict = scanner.prefilter_event_verbose(events[0])
    assert verdict.code == validate.LOW_VOLUME


# =====================================================================
# Full scan cycle: API -> database
# =====================================================================


def test_a_full_scan_writes_an_opportunity_to_the_database(api, database):
    api.add(fakeapi.binary_event(slug="arb", yes=(0.40, 100), no=(0.55, 100)))
    api.add(fakeapi.binary_event(slug="efficient", yes=(0.52, 100),
                                 no=(0.50, 100)))

    found, _misses = arb_monitor.run_scan(database)

    assert found == 1
    row = database.execute("SELECT * FROM opportunities").fetchone()
    assert row["event_slug"] == "arb"
    assert row["net_edge"] == pytest.approx(0.05)
    assert row["best_profit"] > 0
    assert json.loads(row["slippage_curve"])          # curve was stored
    assert json.loads(row["legs_detail"])[0]["token_id"]


def test_a_full_scan_records_the_funnel(api, database):
    api.add(fakeapi.binary_event(slug="ok"))
    api.add(fakeapi.binary_event(slug="poor", volume=5))
    api.add(fakeapi.multi_event(slug="notneg", neg_risk=False))

    arb_monitor.run_scan(database)

    scan_id = database.execute("SELECT MAX(id) id FROM scans").fetchone()["id"]
    funnel = metrics.funnel_for_scan(database, scan_id)

    assert funnel["prefilter"][validate.LOW_VOLUME] == 1
    assert funnel["prefilter"][validate.NOT_NEG_RISK] == 1


def test_every_fetched_event_gets_exactly_one_verdict_row(api, database):
    """
    The verdict table is a partition of what the scan fetched.

    Nothing may be counted twice by passing a stage and being logged again
    later, and nothing may vanish — a market absent from this table is a
    market the dashboard silently cannot explain.
    """
    api.add(fakeapi.binary_event(slug="arb", yes=(0.40, 100), no=(0.55, 100)))
    api.add(fakeapi.binary_event(slug="poor", volume=5))
    api.add(fakeapi.multi_event(slug="notneg", neg_risk=False))

    arb_monitor.run_scan(database)

    scan = database.execute("SELECT * FROM scans").fetchone()
    rows = database.execute("SELECT * FROM event_verdicts").fetchall()

    assert len(rows) == scan["events_total"] == 3
    assert len({r["event_slug"] for r in rows}) == 3


def test_a_verdict_row_carries_the_reason_the_funnel_only_counts(api, database):
    api.add(fakeapi.binary_event(slug="poor", volume=5))
    api.add(fakeapi.multi_event(slug="notneg", neg_risk=False))

    arb_monitor.run_scan(database)

    by_slug = {r["event_slug"]: r for r in
               database.execute("SELECT * FROM event_verdicts")}

    assert by_slug["poor"]["code"] == validate.LOW_VOLUME
    assert by_slug["poor"]["outcome"] == "rejected"
    assert by_slug["poor"]["stage"] == "prefilter"
    assert by_slug["notneg"]["code"] == validate.NOT_NEG_RISK


def test_a_rejecting_verdict_keeps_its_code(api, database):
    """
    Verdict.__bool__ returns .ok, so `if verdict` is False for exactly the
    verdicts worth recording. Written as a test because the truthy form
    reads correctly and silently stored 'unknown' for every rejection.
    """
    api.add(fakeapi.binary_event(slug="poor", volume=5))

    arb_monitor.run_scan(database)

    row = database.execute("SELECT * FROM event_verdicts").fetchone()
    assert row["code"] != "unknown"
    assert row["code"] == validate.LOW_VOLUME


def test_an_opportunity_verdict_carries_its_numbers(api, database):
    api.add(fakeapi.binary_event(slug="arb", yes=(0.40, 100), no=(0.55, 100)))

    arb_monitor.run_scan(database)

    row = database.execute("SELECT * FROM event_verdicts").fetchone()
    assert row["outcome"] == "opportunity"
    assert row["sum_best_asks"] == pytest.approx(0.95)
    assert row["net_edge"] is not None
    assert row["market_type"] == "binary"
    assert row["url"].endswith("/arb")


def test_verdicts_are_pruned_to_the_retention_window(api, database):
    api.add(fakeapi.binary_event(slug="e1"))

    for _ in range(4):
        arb_monitor.run_scan(database)

    dblib.prune_event_verdicts(database, keep_scans=2)

    remaining = {r["scan_id"] for r in
                 database.execute("SELECT scan_id FROM event_verdicts")}
    assert remaining == {3, 4}


def test_pruning_is_disabled_by_a_non_positive_window(api, database):
    api.add(fakeapi.binary_event(slug="e1"))
    for _ in range(3):
        arb_monitor.run_scan(database)

    assert dblib.prune_event_verdicts(database, keep_scans=0) == 0
    assert database.execute(
        "SELECT COUNT(*) c FROM event_verdicts").fetchone()["c"] == 3


def test_a_scan_marks_itself_finished(api, database):
    api.add(fakeapi.binary_event())
    arb_monitor.run_scan(database)

    scan = database.execute("SELECT * FROM scans").fetchone()
    assert scan["status"] == "done"
    assert scan["finished_at"]
    assert scan["events_total"] == 1


def test_one_broken_event_does_not_abort_the_scan(api, database, monkeypatch):
    """
    Resilience is the property that matters for a process meant to run for
    weeks. One malformed event must cost one event, not the whole cycle.
    """
    api.add(fakeapi.binary_event(slug="broken"))
    api.add(fakeapi.binary_event(slug="fine", yes=(0.40, 100), no=(0.55, 100)))

    real_scan = scanner.scan_event_verbose

    def flaky(group, books=None):
        if group["event"]["slug"] == "broken":
            raise RuntimeError("synthetic failure")
        return real_scan(group, books)

    monkeypatch.setattr(scanner, "scan_event_verbose", flaky)

    found, _ = arb_monitor.run_scan(database)

    assert found == 1
    assert database.execute(
        "SELECT errors FROM scans").fetchone()["errors"] == 1


def test_an_empty_api_produces_a_clean_empty_scan(api, database):
    """Zero events must not divide by zero in the funnel or crash the loop."""
    found, misses = arb_monitor.run_scan(database)
    assert (found, misses) == (0, 0)
    assert database.execute("SELECT status FROM scans").fetchone()["status"] == "done"


def test_near_misses_are_capped_and_ranked(api, database, monkeypatch):
    """Only the closest edges are kept — the ones worth watching."""
    monkeypatch.setattr(arb_monitor, "NEAR_MISSES_PER_SCAN", 3)
    for i in range(8):
        api.add(fakeapi.binary_event(
            slug=f"nm{i}", yes=(0.50, 100), no=(round(0.50 - i * 0.0005, 4), 100)))

    arb_monitor.run_scan(database)

    rows = database.execute(
        "SELECT net_edge FROM near_misses ORDER BY net_edge DESC").fetchall()
    assert len(rows) <= 3
    assert rows == sorted(rows, key=lambda r: -r["net_edge"])


# =====================================================================
# Startup
# =====================================================================


def test_startup_fields_are_loggable():
    """
    Regression: main() built its startup line as
    `fields(profile=..., **config.as_dict())`, and config.as_dict() also
    contains PROFILE — so the duplicate keyword raised TypeError before
    the first scan ever ran. Every other test called run_scan directly and
    never touched main(), so nothing caught it.
    """
    payload = arb_monitor.startup_fields()
    logging_setup.fields(**payload)          # must not raise

    assert payload["stage"] == "startup"
    assert payload["profile"] == config.PROFILE
    assert payload["scan_interval"] == arb_monitor.SCAN_INTERVAL


def test_startup_fields_include_the_resolved_config():
    """The point of the line: reproduce a run from its own log."""
    payload = arb_monitor.startup_fields()
    assert payload["min_net_edge"] == config.MIN_NET_EDGE
    assert payload["min_volume_24h"] == config.MIN_VOLUME_24H


def test_startup_fields_are_all_json_serializable():
    json.dumps(arb_monitor.startup_fields())


def test_main_survives_its_first_log_line(api, database, monkeypatch):
    """
    Runs main() far enough to prove startup works, then breaks out of the
    infinite loop on the first sleep.
    """
    monkeypatch.setattr(dblib, "connect", lambda *a, **k: database)

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(arb_monitor.time, "sleep", stop)
    monkeypatch.setattr(arb_monitor, "SCAN_INTERVAL", 900)
    api.add(fakeapi.binary_event())

    with pytest.raises(KeyboardInterrupt):
        arb_monitor.main()

    assert database.execute(
        "SELECT COUNT(*) c FROM scans").fetchone()["c"] == 1


# =====================================================================
# Recording, end to end
# =====================================================================


def test_a_recorded_scan_replays_to_the_same_verdicts(api, database,
                                                      tmp_path, monkeypatch):
    """
    The claim the whole recorder rests on: what is replayed offline equals
    what happened live. If this drifts, every stored fixture is a lie.
    """
    import recorder
    import replay

    api.add(fakeapi.binary_event(slug="arb", yes=(0.40, 100), no=(0.55, 100)))
    api.add(fakeapi.multi_event(slug="election"))

    monkeypatch.setenv("RECORD", "1")
    monkeypatch.setattr(recorder, "RECORDINGS_DIR", tmp_path)
    arb_monitor.run_scan(database)

    recordings = recorder.list_recordings(tmp_path)
    assert len(recordings) == 1

    verdicts = {v["slug"]: v for v in replay.replay_file(recordings[0])}
    live = database.execute(
        "SELECT event_slug, net_edge FROM opportunities").fetchall()

    assert live, "the scan should have found something to compare against"
    for row in live:
        assert verdicts[row["event_slug"]]["kind"] == "opportunity"
        assert verdicts[row["event_slug"]]["net_edge"] == \
               pytest.approx(row["net_edge"])
