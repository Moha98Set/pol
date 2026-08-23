"""
Tests for record -> replay.

These prove the loop that makes offline debugging possible: an event
recorded to disk, read back, and pushed through the real scanner must
produce the verdict it produced live. If this round trip is lossy, every
fixture in the project is worthless, so it is worth testing directly.

Nothing here touches the network.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import recorder
import replay
import scanner


# =====================================================================
# Synthetic events — a hand-built stand-in for a Gamma payload
# =====================================================================


def future_date(days=30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def binary_event(yes_price="0.40", no_price="0.55", volume=50_000):
    """A binary event whose two legs sum to less than $1."""
    event = {
        "slug": "will-it-rain",
        "title": "Will it rain tomorrow?",
        "endDate": future_date(),
        "tags": [{"label": "Geopolitics"}],   # 0% fee keeps the math clean
        "markets": [{
            "question": "Will it rain tomorrow?",
            "slug": "will-it-rain",
            "closed": False,
            "enableOrderBook": True,
            "volume24hr": volume,
            "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        }],
    }
    books = {
        "tok-yes": {"asks": [{"price": yes_price, "size": "100"},
                             {"price": "0.45", "size": "100"}]},
        "tok-no": {"asks": [{"price": no_price, "size": "100"},
                            {"price": "0.60", "size": "100"}]},
    }
    return event, books


def multi_event(prices=("0.30", "0.30", "0.30"), volume=80_000):
    """A negRisk multi-outcome event with one leg per candidate."""
    markets = []
    books = {}
    for i, price in enumerate(prices):
        token = f"tok-{i}"
        markets.append({
            "question": f"Will candidate {i} win?",
            "groupItemTitle": f"Candidate {i}",
            "closed": False,
            "enableOrderBook": True,
            "volume24hr": volume / len(prices),
            "clobTokenIds": json.dumps([token, f"{token}-no"]),
        })
        books[token] = {"asks": [{"price": price, "size": "500"}]}

    event = {
        "slug": "who-wins",
        "title": "Who wins the election?",
        "endDate": future_date(),
        "negRisk": True,
        "tags": [{"label": "Geopolitics"}],
        "markets": markets,
    }
    return event, books


# =====================================================================
# Recorder file format
# =====================================================================


def test_round_trip_preserves_the_event(tmp_path):
    event, books = binary_event()

    with recorder.Recorder(name="t", compress=False, directory=tmp_path) as rec:
        rec.record_event(event, books, fee_rate=0.0, is_binary=True)

    records = list(recorder.read_recording(rec.path))
    assert len(records) == 1
    assert records[0]["slug"] == "will-it-rain"
    assert records[0]["event"] == event
    assert records[0]["books"] == books


def test_recording_works_compressed(tmp_path):
    event, books = binary_event()
    with recorder.Recorder(name="t", compress=True, directory=tmp_path) as rec:
        rec.record_event(event, books)
    assert rec.path.suffix == ".gz"
    assert len(list(recorder.read_recording(rec.path))) == 1


def test_header_and_footer_are_not_yielded_as_events(tmp_path):
    with recorder.Recorder(name="t", compress=False, directory=tmp_path) as rec:
        rec.record_event(*binary_event())
    assert all(r["type"] == "event" for r in recorder.read_recording(rec.path))


def test_reader_rejects_a_future_format_version(tmp_path):
    """
    A fixture written by newer code must fail loudly. Silently
    misinterpreting an old recording would produce confidently wrong
    conclusions, which is worse than an error.
    """
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"type": "header", "format_version": 999}) + "\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        list(recorder.read_recording(path))


def test_reader_skips_a_corrupt_line(tmp_path):
    """A half-written last line (process killed mid-scan) must not be fatal."""
    event, books = binary_event()
    with recorder.Recorder(name="t", compress=False, directory=tmp_path) as rec:
        rec.record_event(event, books)
    with open(rec.path, "a", encoding="utf-8") as fh:
        fh.write('{"type": "event", "slug": "trunc')

    assert len(list(recorder.read_recording(rec.path))) == 1


def test_promote_to_fixture_extracts_one_event(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "FIXTURES_DIR", tmp_path / "fixtures")

    with recorder.Recorder(name="t", compress=False, directory=tmp_path) as rec:
        rec.record_event(*binary_event())
        rec.record_event(*multi_event())

    out = recorder.promote_to_fixture(rec.path, "who-wins", "election_case")
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["slug"] == "who-wins"

    with pytest.raises(KeyError):
        recorder.promote_to_fixture(rec.path, "nope", "x")


# =====================================================================
# The scanner runs offline on recorded books
# =====================================================================


def test_scanner_accepts_injected_books_and_makes_no_network_call(monkeypatch):
    """
    The property that makes replay possible at all: given books, the
    scanner must never reach for the API. Any call is a bug that would
    make replays depend on live data.
    """
    def explode(*_a, **_k):
        raise AssertionError("scanner tried to hit the network during replay")

    monkeypatch.setattr(scanner, "fetch_order_books", explode)

    event, books = binary_event()
    group = scanner.prefilter_event(event)
    result = scanner.scan_event(group, books=books)

    assert result["kind"] == "opportunity"


def test_replay_reproduces_a_binary_opportunity():
    event, books = binary_event()
    verdict = replay.replay_record({
        "slug": event["slug"], "title": event["title"],
        "event": event, "books": books, "is_binary": True, "fee_rate": 0.0,
    })
    assert verdict["kind"] == "opportunity"
    assert verdict["sum_best_asks"] == pytest.approx(0.95)
    assert verdict["net_edge"] == pytest.approx(0.05)
    assert verdict["best_profit"] > 0


def test_replay_reproduces_a_multi_outcome_opportunity():
    event, books = multi_event(prices=("0.30", "0.30", "0.30"))
    verdict = replay.replay_record({
        "slug": event["slug"], "title": event["title"],
        "event": event, "books": books, "is_binary": False, "fee_rate": 0.0,
    })
    assert verdict["kind"] == "opportunity"
    assert verdict["num_outcomes"] == 3
    assert verdict["sum_best_asks"] == pytest.approx(0.90)


def test_replay_rejects_an_event_with_no_edge():
    event, books = binary_event(yes_price="0.55", no_price="0.60")
    verdict = replay.replay_record({
        "slug": event["slug"], "event": event, "books": books,
        "title": event["title"],
    })
    assert verdict["kind"] != "opportunity"


def test_replay_marks_events_that_no_longer_pass_the_prefilter():
    """
    Filters change over time. An event recorded when the volume floor was
    $1k must not silently vanish from a replay after the floor is raised —
    it is reported with prefiltered_out set, so the reason is visible.
    """
    event, books = binary_event(volume=10)   # below MIN_VOLUME_24H
    verdict = replay.replay_record({
        "slug": event["slug"], "title": event["title"],
        "event": event, "books": books, "is_binary": True, "fee_rate": 0.0,
    })
    assert verdict["prefiltered_out"] is True


def test_replay_reports_errors_instead_of_crashing(monkeypatch):
    """One broken event must not abort a replay of a thousand others."""
    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(scanner, "scan_event_verbose", boom)
    event, books = binary_event()
    verdict = replay.replay_record({
        "slug": "x", "title": "x", "event": event, "books": books,
    })
    assert verdict["kind"] == "error"
    assert "synthetic failure" in verdict["error"]


def test_replay_is_deterministic():
    """
    Same input, same verdict, every time. Without this the recording is
    just an expensive log file.
    """
    event, books = binary_event()
    record = {"slug": event["slug"], "title": event["title"],
              "event": event, "books": books, "fee_rate": 0.0}
    first = replay.replay_record(record)
    second = replay.replay_record(record)
    assert first == second


# =====================================================================
# Comparison mode
# =====================================================================


def test_compare_detects_a_changed_verdict(tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps([
        {"slug": "a", "title": "Event A", "kind": "opportunity",
         "net_edge": 0.05},
    ]), encoding="utf-8")

    replay.compare([{"slug": "a", "title": "Event A", "kind": "near_miss",
                     "net_edge": 0.001}], baseline)

    out = capsys.readouterr().out
    assert "changed   : 1" in out
    assert "opportunity -> near_miss" in out


def test_compare_reports_no_change_for_an_identical_replay(tmp_path, capsys):
    verdicts = [{"slug": "a", "title": "Event A", "kind": "opportunity",
                 "net_edge": 0.05}]
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(verdicts), encoding="utf-8")

    replay.compare(verdicts, baseline)
    assert "No behaviour change" in capsys.readouterr().out
