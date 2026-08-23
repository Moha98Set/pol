"""
Polymarket Arbitrage Monitor — Task 1: periodic scanner + storage
==================================================================
Every 15 minutes: scan all liquid Polymarket events for arbitrage using
real slippage analysis (walking the order book), store results in SQLite.

Files:
    db.py       — database layer
    scanner.py  — fetching, filters, slippage engine
    view_db.py  — inspect stored history

Install:
    pip install requests

Run:
    python arb_monitor.py

Deploy on a server:
    see DEPLOY.md — systemd unit files live in deploy/.
"""

import signal
import threading
import time

import config
import db as dblib
import logging_setup
import metrics
import recorder
import scanner
from logging_setup import fields, timed

SCAN_INTERVAL = config.SCAN_INTERVAL
NEAR_MISSES_PER_SCAN = config.NEAR_MISSES_PER_SCAN

logging_setup.configure("arb_monitor")
log = logging_setup.get_logger("arb_monitor")

# Under systemd, `stop` means SIGTERM, and the default patience is 90
# seconds — far less than a full scan takes. Killed mid-scan the row stays
# in 'running' until the next start cleans it up, which makes an ordinary
# restart indistinguishable in the history from a crash. So the signal sets
# a flag instead: the scan loop notices, stops analysing, and still writes
# what it found. An interrupted scan is then an honest short scan rather
# than a missing one.
STOP = threading.Event()


def _request_stop(signum, _frame):
    log.info("shutdown signal received", extra=fields(
        stage="shutdown", signal=signal.Signals(signum).name))
    STOP.set()


def run_scan(db) -> tuple:
    """One full scan cycle. Returns (opportunities_found, near_misses_saved)."""
    scan_id = dblib.start_scan(db)

    funnel = metrics.Funnel(scan_id)

    log.info("fetching active events", extra=fields(scan_id=scan_id,
                                                    stage="fetch"))
    fetch_start = time.perf_counter()
    with timed(log, "events fetched", scan_id=scan_id, stage="fetch"):
        events = scanner.fetch_all_events()
    funnel.timing("fetch", (time.perf_counter() - fetch_start) * 1000)
    funnel.saw_event(len(events))
    log.info("events retrieved", extra=fields(
        scan_id=scan_id, stage="fetch", events_total=len(events)))

    # cheap pre-filters: volume, time window, negRisk, patterns.
    # Every rejection is counted by reason — an event that vanishes without
    # explanation is the hardest thing in this pipeline to debug.
    prefilter_start = time.perf_counter()
    groups = []
    for event in events:
        group, verdict = scanner.prefilter_event_verbose(event)
        if group is None:
            funnel.reject("prefilter", verdict.code)
        else:
            groups.append(group)
            funnel.suspect(group.get("suspicions", []))
    funnel.timing("prefilter", (time.perf_counter() - prefilter_start) * 1000)

    skipped = len(events) - len(groups)
    n_binary = sum(1 for g in groups if g["is_binary"])
    log.info("pre-filter complete", extra=fields(
        scan_id=scan_id, stage="prefilter",
        events_total=len(events), events_kept=len(groups),
        events_binary=n_binary, events_multi=len(groups) - n_binary,
        events_skipped=skipped,
        top_reasons=dict(funnel.top_reasons(5))))

    opportunities_found = 0
    near_misses = []
    errors = 0

    # RECORD=1 freezes every book this scan sees, so any decision made here
    # can be re-run offline later with replay.py
    if recorder.recording_enabled():
        scanner.RECORDER = recorder.Recorder(name=f"scan{scan_id}")

    analysis_start = time.perf_counter()

    for idx, group in enumerate(groups, 1):
        if STOP.is_set():
            log.info("scan cut short by shutdown", extra=fields(
                scan_id=scan_id, stage="shutdown", scanned=idx - 1,
                total=len(groups)))
            break

        if idx % 50 == 0:
            log.info("scan progress", extra=fields(
                scan_id=scan_id, stage="progress", scanned=idx,
                total=len(groups), opportunities=opportunities_found,
                near_misses=len(near_misses), errors=errors))

        slug = group["event"].get("slug")
        funnel.analysed()
        try:
            result, verdict = scanner.scan_event_verbose(group)
        except Exception as e:
            errors += 1
            funnel.errors += 1
            log.error("scan failed", exc_info=True, extra=fields(
                scan_id=scan_id, stage="error", event_slug=slug,
                error=f"{type(e).__name__}: {e}"))
            continue

        if not result:
            # rejected inside the scan, by its actual reason: a dry leg, an
            # implausible price, a crossed book, or simply no edge worth a
            # near-miss row
            funnel.reject("book", verdict.code)
            continue

        funnel.suspect(result.get("suspicions", []))

        if result["kind"] == "opportunity":
            dblib.save_opportunity(db, scan_id, result)
            opportunities_found += 1
            funnel.opportunities += 1
            log.info("opportunity found", extra=fields(
                scan_id=scan_id, stage="opportunity", event_slug=slug,
                market_type=result["market_type"],
                event_title=result["event_title"],
                num_outcomes=result["num_outcomes"],
                sum_best_asks=result["sum_best_asks"],
                net_edge=result["net_edge"],
                fee_rate=result["fee_rate"],
                best_profit=result["best_profit"],
                best_capital=result["best_capital"],
                volume_24h=result["volume_24h"]))
        elif result["kind"] == "near_miss":
            near_misses.append(result)
            funnel.reject("edge", verdict.code)

    funnel.timing("analysis", (time.perf_counter() - analysis_start) * 1000)

    if scanner.RECORDER is not None:
        scanner.RECORDER.close()
        scanner.RECORDER = None

    # keep only the best near misses (closest to arb)
    near_misses.sort(key=lambda x: x["net_edge"], reverse=True)
    top_misses = near_misses[:NEAR_MISSES_PER_SCAN]
    dblib.save_near_misses(db, scan_id, top_misses)

    if top_misses:
        best = top_misses[0]
        log.info("best near miss", extra=fields(
            scan_id=scan_id, stage="near_miss",
            event_slug=best.get("event_slug"),
            event_title=best["event_title"],
            net_edge=best["net_edge"],
            sum_best_asks=best["sum_best_asks"]))

    dblib.finish_scan(
        db, scan_id,
        events_total=len(events),
        events_scanned=len(groups),
        events_skipped_filter=skipped,
        opportunities_found=opportunities_found,
        near_misses_saved=len(top_misses),
        errors=errors,
    )

    funnel.near_misses = len(top_misses)
    funnel.save(db)

    log.info("scan complete", extra=fields(
        scan_id=scan_id, stage="scan_complete",
        events_total=len(events), events_scanned=len(groups),
        events_skipped=skipped, opportunities=opportunities_found,
        near_misses=len(top_misses), errors=errors,
        **{f"rejected_{code}": count
           for code, count in funnel.top_reasons(6)}))

    # the funnel as a block, so "where did everything go" is answerable by
    # reading rather than by querying
    for line in funnel.render().splitlines():
        log.info(line)

    return opportunities_found, len(top_misses)


def startup_fields() -> dict:
    """
    Everything about this run, as one flat dict of scalars.

    The whole resolved config goes in so a run can be reproduced from its
    own log without guessing which env vars were set. Built as a dict and
    then updated — not as `fields(profile=..., **config)` — because
    config.as_dict() contains PROFILE, SCAN_INTERVAL and DB_PATH too, and
    passing both spellings raises TypeError on the duplicate keyword.
    """
    payload = {key.lower(): value
               for key, value in config.as_dict().items()
               if isinstance(value, (int, float, str, bool))}
    payload.update(
        stage="startup",
        profile=config.PROFILE,
        scan_interval=SCAN_INTERVAL,
        db_path=str(dblib.DB_PATH),
        recording=recorder.recording_enabled(),
    )
    return payload


def main():
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    log.info("monitor starting", extra=fields(**startup_fields()))

    db = dblib.connect()
    dblib.mark_stale_scans_failed(db)

    while not STOP.is_set():
        start = time.time()

        try:
            run_scan(db)
        except Exception as e:
            log.error("scan cycle failed", exc_info=True, extra=fields(
                stage="cycle_error", error=f"{type(e).__name__}: {e}"))

        elapsed = time.time() - start
        wait = max(0, SCAN_INTERVAL - elapsed)
        log.info("cycle finished", extra=fields(
            stage="cycle_complete", duration_ms=round(elapsed * 1000, 2),
            sleep_sec=round(wait, 1)))

        # Event.wait returns True the moment the flag is set, so a stop
        # during the idle gap is immediate rather than up to SCAN_INTERVAL
        # late — which is the difference between systemd stopping the unit
        # and systemd giving up and sending SIGKILL.
        if wait > 0 and STOP.wait(wait):
            break

    db.close()
    log.info("monitor stopped", extra=fields(stage="shutdown"))


if __name__ == "__main__":
    main()
