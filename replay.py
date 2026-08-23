"""
Replay — re-run the real analysis code against recorded order books.
====================================================================

    python replay.py                        # replay the newest recording
    python replay.py <file.jsonl.gz>        # replay a specific one
    python replay.py --list                 # what recordings exist
    python replay.py <file> --slug foo-bar  # one event, in full detail
    python replay.py <file> --verdicts out.json     # save verdicts
    python replay.py <file> --compare old.json      # diff against a baseline

Why this is the most useful debugging tool in the project
---------------------------------------------------------
Every other kind of investigation here fails on the same wall: the input is
gone. A book that produced a strange result cannot be fetched again. With a
recording, the exact bytes are on disk and the analysis becomes an ordinary
deterministic function you can run a thousand times, step through in a
debugger, or run under a profiler — offline, in a second, with no VPN.

The --compare mode is the one to reach for after a refactor: replay before,
replay after, and the diff tells you precisely which events changed verdict.
"Nothing changed" is exactly the answer a refactor should produce, and this
is the only way to prove it.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import arbmath
import recorder
import scanner

log = logging.getLogger("replay")


# =====================================================================
# Rebuilding scanner input from a record
# =====================================================================


def group_from_record(record: dict) -> dict:
    """
    Reconstruct the `group` dict that scanner.scan_event expects.

    The recorded event goes back through the real prefilter rather than
    having its group hand-built, so filter changes show up in replay too —
    replaying only the slippage math would hide the most common source of
    behaviour change.
    """
    event = record["event"]
    group = scanner.prefilter_event(event)
    if group is not None:
        return group

    # The event no longer passes the pre-filter (thresholds changed, or it
    # is now near resolution). Still analysable — build the group manually
    # and mark it, so the report can show the filter as the reason.
    markets = [m for m in event.get("markets", [])
               if not m.get("closed") and m.get("enableOrderBook")]
    return {
        "event": event,
        "markets": markets,
        "is_binary": record.get("is_binary", len(markets) == 1),
        "volume": sum(scanner.get_volume(m) for m in markets),
        "fee_rate": record.get("fee_rate", 0.05),
        "fee_category": "recorded",
        "_prefiltered_out": True,
    }


def replay_record(record: dict) -> dict:
    """Run one recorded event through the live analysis code."""
    group = group_from_record(record)
    books = record.get("books") or {}

    verdict = {
        "slug": record.get("slug"),
        "title": record.get("title"),
        "captured_at": record.get("captured_at"),
        "prefiltered_out": group.get("_prefiltered_out", False),
    }

    if not group.get("markets"):
        verdict["kind"] = "no_open_markets"
        return verdict

    try:
        result, reason = scanner.scan_event_verbose(group, books=books)
    except Exception as e:
        verdict["kind"] = "error"
        verdict["error"] = f"{type(e).__name__}: {e}"
        return verdict

    verdict["code"] = reason.code
    verdict["detail"] = reason.detail

    if result is None:
        verdict["kind"] = "rejected"
        return verdict

    verdict["kind"] = result["kind"]
    verdict["net_edge"] = result.get("net_edge")
    verdict["sum_best_asks"] = result.get("sum_best_asks")
    verdict["fee_rate"] = result.get("fee_rate")
    verdict["num_outcomes"] = result.get("num_outcomes")
    if result["kind"] == "opportunity":
        verdict["best_profit"] = result.get("best_profit")
        verdict["best_capital"] = result.get("best_capital")
        verdict["best_shares"] = result.get("best_shares")
    return verdict


# =====================================================================
# Detailed single-event view
# =====================================================================


def explain_record(record: dict):
    """
    Full leg-by-leg breakdown of one recorded event.

    This is the "why on earth did it say that" view: every leg's book depth,
    its best ask, and each stage of the arithmetic laid out separately so
    you can see which one is responsible.
    """
    group = group_from_record(record)
    books = record.get("books") or {}
    event = record["event"]

    print("=" * 74)
    print(f"{record.get('title')}")
    print("=" * 74)
    print(f"  slug         : {record.get('slug')}")
    print(f"  captured at  : {record.get('captured_at')}")
    print(f"  type         : {'binary' if group['is_binary'] else 'multi'}")
    print(f"  markets open : {len(group['markets'])}")
    print(f"  volume 24h   : ${group.get('volume', 0):,.0f}")
    print(f"  fee rate     : {group['fee_rate']*100:.0f}% ({group.get('fee_category')})")
    print(f"  negRisk      : {event.get('negRisk')}")
    print(f"  endDate      : {event.get('endDate')}")
    if group.get("_prefiltered_out"):
        print("  NOTE: this event no longer passes the current pre-filters")

    legs = []
    for m in group["markets"]:
        token_ids = scanner.parse_token_ids(m)
        if not token_ids:
            continue
        if group["is_binary"]:
            legs = [("Yes", token_ids[0]), ("No", token_ids[1])]
            break
        name = m.get("groupItemTitle") or (m.get("question") or "")[:36]
        legs.append((name, token_ids[0]))

    print(f"\n  {'Leg':<34}{'Best ask':>10}{'Depth $':>12}{'Levels':>8}")
    print("  " + "-" * 62)

    arb_legs = []
    for name, token_id in legs:
        asks = scanner.get_valid_asks(books.get(token_id))
        best = arbmath.best_ask(asks)
        print(f"  {str(name)[:32]:<34}"
              f"{(f'{best:.4f}' if best is not None else 'DRY'):>10}"
              f"{arbmath.depth_usd(asks):>12,.0f}{len(asks):>8}")
        arb_legs.append((name, asks))

    result = arbmath.evaluate_basket(arb_legs, group["fee_rate"])
    print()
    if result["dry_legs"]:
        print(f"  VERDICT: rejected — dry legs: {', '.join(result['dry_legs'])}")
        return

    print(f"  sum of best asks : {result['sum_best_asks']:.4f}")
    print(f"  gross edge       : {result['gross_edge']*100:+.3f}%")
    print(f"  fee per share    : {result['fee_per_share']*100:.3f}%")
    print(f"  net edge         : {result['net_edge']*100:+.3f}%")

    if not result["curve"]:
        print("\n  VERDICT: no executable size (book too thin or no edge)")
        return

    print(f"\n  {'Capital':>10}{'Baskets':>12}{'Cost':>11}{'Fee':>9}"
          f"{'Profit':>10}{'ROI':>8}")
    for c in result["curve"]:
        print(f"  ${c['capital']:>9,.0f}{c['shares']:>12,.2f}"
              f"${c['real_cost']:>10,.2f}${c['fee']:>8,.2f}"
              f"${c['profit']:>9,.2f}{c['roi']:>7.2f}%")

    best = result["best"]
    print(f"\n  VERDICT: opportunity — ${best['profit']:.2f} at "
          f"${best['capital']:,.0f}")


# =====================================================================
# Whole-recording replay
# =====================================================================


def replay_file(path: Path, limit: int = None) -> list:
    verdicts = []
    for i, record in enumerate(recorder.read_recording(path)):
        if limit and i >= limit:
            break
        verdicts.append(replay_record(record))
    return verdicts


def print_summary(verdicts: list, path: Path):
    kinds = {}
    for v in verdicts:
        kinds[v["kind"]] = kinds.get(v["kind"], 0) + 1

    print("=" * 66)
    print(f"Replayed {len(verdicts)} events from {path.name}")
    print("=" * 66)
    for kind, count in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {kind:<22}: {count}")

    # the reasons, not just the counts — "18 rejected" says nothing about
    # whether the pipeline is broken or the market is simply efficient
    codes = {}
    for v in verdicts:
        if v.get("code") and v["kind"] != "opportunity":
            codes[v["code"]] = codes.get(v["code"], 0) + 1
    if codes:
        print("\nReasons:")
        for code, count in sorted(codes.items(), key=lambda x: -x[1]):
            print(f"  {code:<22}: {count}")

    opps = [v for v in verdicts if v["kind"] == "opportunity"]
    if opps:
        print(f"\nOpportunities ({len(opps)}):")
        for v in sorted(opps, key=lambda x: -(x.get("best_profit") or 0)):
            print(f"  {v['net_edge']*100:+7.3f}%  "
                  f"${v.get('best_profit') or 0:>7.2f}  "
                  f"{(v['title'] or '')[:44]}")

    near = [v for v in verdicts if v["kind"] == "near_miss"
            and v.get("net_edge") is not None]
    if near:
        near.sort(key=lambda x: -x["net_edge"])
        print(f"\nClosest near misses:")
        for v in near[:8]:
            print(f"  {v['net_edge']*100:+7.3f}%  "
                  f"sum={v.get('sum_best_asks') or 0:.4f}  "
                  f"{(v['title'] or '')[:44]}")

    errors = [v for v in verdicts if v["kind"] == "error"]
    if errors:
        print(f"\nERRORS ({len(errors)}) — these are bugs, the input is fixed:")
        for v in errors[:10]:
            print(f"  {(v['title'] or v['slug'] or '?')[:44]}: {v['error']}")


def compare(current: list, baseline_path: Path):
    """
    Diff this replay against a saved one.

    The intended workflow around any change to the filters or the math:
    replay -> save verdicts -> make the change -> replay -> compare.
    Anything that shows up here is a behaviour change you either intended
    or just introduced by accident.
    """
    baseline = {v["slug"]: v for v in
                json.loads(baseline_path.read_text(encoding="utf-8"))}
    now = {v["slug"]: v for v in current}

    changed, appeared, vanished = [], [], []

    for slug, new in now.items():
        old = baseline.get(slug)
        if old is None:
            appeared.append(new)
        elif (old["kind"] != new["kind"]
              or _edge_moved(old.get("net_edge"), new.get("net_edge"))):
            changed.append((old, new))

    for slug, old in baseline.items():
        if slug not in now:
            vanished.append(old)

    print("=" * 66)
    print(f"Comparison against {baseline_path.name}")
    print("=" * 66)
    print(f"  unchanged : {len(now) - len(changed) - len(appeared)}")
    print(f"  changed   : {len(changed)}")
    print(f"  new       : {len(appeared)}")
    print(f"  missing   : {len(vanished)}")

    for old, new in changed:
        print(f"\n  {(new['title'] or new['slug'] or '?')[:56]}")
        print(f"    kind    : {old['kind']} -> {new['kind']}")
        if old.get("net_edge") is not None or new.get("net_edge") is not None:
            print(f"    net_edge: {_fmt_edge(old.get('net_edge'))} -> "
                  f"{_fmt_edge(new.get('net_edge'))}")

    if not changed and not appeared and not vanished:
        print("\n  No behaviour change. A refactor that touches nothing is "
              "the goal — this proves it.")


def _edge_moved(old, new, tol=1e-9) -> bool:
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return abs(old - new) > tol


def _fmt_edge(value) -> str:
    return "n/a" if value is None else f"{value*100:+.4f}%"


# =====================================================================
# CLI
# =====================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Replay recorded scans through the live analysis code")
    parser.add_argument("recording", nargs="?",
                        help="recording file (default: the newest one)")
    parser.add_argument("--list", action="store_true",
                        help="list available recordings and exit")
    parser.add_argument("--slug", help="explain one event in full detail")
    parser.add_argument("--limit", type=int, help="stop after N events")
    parser.add_argument("--verdicts", help="write verdicts to a JSON file")
    parser.add_argument("--compare", help="diff verdicts against a saved file")
    parser.add_argument("--promote", nargs=2, metavar=("SLUG", "NAME"),
                        help="save one event as a permanent test fixture")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    available = recorder.list_recordings()

    if args.list:
        if not available:
            print(f"No recordings in {recorder.RECORDINGS_DIR}\n"
                  f"Make one with:  RECORD=1 python arb_monitor.py")
            return
        print(f"Recordings in {recorder.RECORDINGS_DIR}:\n")
        for path in available:
            header = recorder.recording_header(path) or {}
            size_kb = path.stat().st_size / 1024
            print(f"  {path.name:<44} {size_kb:>8,.0f} KB  "
                  f"{header.get('recorded_at', '')[:19]}")
        return

    if args.recording:
        path = Path(args.recording)
    elif available:
        path = available[0]
        log.info("Using newest recording: %s\n", path.name)
    else:
        print("No recording given and none found.\n"
              "Make one with:  RECORD=1 python arb_monitor.py")
        sys.exit(1)

    if not path.exists():
        print(f"No such file: {path}")
        sys.exit(1)

    if args.promote:
        slug, name = args.promote
        out = recorder.promote_to_fixture(path, slug, name)
        print(f"Saved fixture: {out}")
        print("Now write a test that asserts the correct verdict for it.")
        return

    if args.slug:
        for record in recorder.read_recording(path):
            if record.get("slug") == args.slug:
                explain_record(record)
                return
        print(f"Event '{args.slug}' not found in {path.name}")
        sys.exit(1)

    verdicts = replay_file(path, limit=args.limit)
    print_summary(verdicts, path)

    if args.verdicts:
        Path(args.verdicts).write_text(
            json.dumps(verdicts, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote {len(verdicts)} verdicts to {args.verdicts}")

    if args.compare:
        print()
        compare(verdicts, Path(args.compare))


if __name__ == "__main__":
    main()
