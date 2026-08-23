"""
query.py — ask the database questions without writing SQL.
==========================================================

    python query.py health          # is the monitor actually working?
    python query.py funnel          # where did all the events go?
    python query.py drift           # what changed over the last N scans?
    python query.py opps            # opportunities, best first
    python query.py suspects        # signals the validator flagged
    python query.py fees            # which categories produce edges
    python query.py timings         # where the time goes
    python query.py sql "SELECT ..."   # escape hatch

Every command takes --scans N (default 20) to set the window, and --json
to emit machine-readable output instead of a table.

Why this exists
---------------
The data has been accumulating since the first scan, and until now the only
way to look at it was to open sqlite3 and remember the schema. That means
the questions that get asked are the ones someone happens to remember how
to write, which in practice means almost none. Each command here is a
question worth asking regularly, written down once.

`health` is the one to run first after any change. It answers "is this
thing still working" using the shape of the data rather than the absence of
errors — a monitor that runs happily and quietly analyses zero events looks
perfectly healthy in a log file.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import db as dblib
import metrics


# =====================================================================
# Output helpers
# =====================================================================


def table(rows, headers, aligns=None):
    if not rows:
        return "  (nothing)"
    rows = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [max(len(str(h)), max((len(r[i]) for r in rows), default=0))
              for i, h in enumerate(headers)]
    aligns = aligns or ["<"] * len(headers)

    out = ["  " + "  ".join(f"{h:<{w}}" for h, w in zip(headers, widths)),
           "  " + "  ".join("-" * w for w in widths)]
    for row in rows:
        out.append("  " + "  ".join(
            f"{c:{a}{w}}" for c, a, w in zip(row, aligns, widths)))
    return "\n".join(out)


def section(title):
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def pct(part, whole):
    return f"{part / whole * 100:.1f}%" if whole else "-"


def ago(iso_string):
    """Human-readable age, because absolute UTC timestamps hide staleness."""
    if not iso_string:
        return "never"
    try:
        then = datetime.fromisoformat(str(iso_string).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_string)
    delta = datetime.now(timezone.utc) - then
    seconds = delta.total_seconds()
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds/60:.0f}m ago"
    if seconds < 172800:
        return f"{seconds/3600:.1f}h ago"
    return f"{seconds/86400:.1f}d ago"


# =====================================================================
# health
# =====================================================================


def cmd_health(db, args):
    """
    Is the monitor working?

    Not "is it running" — a process that scans nothing, or scans everything
    and rejects all of it, stays up forever and logs nothing alarming. These
    checks look at the shape of the output instead.
    """
    section("Health")

    last = db.execute(
        "SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    if not last:
        print("  No scans recorded yet. Run: python arb_monitor.py")
        return 1

    problems = []

    print(f"  last scan       : #{last['id']}  {ago(last['started_at'])}"
          f"  [{last['status']}]")
    print(f"  events fetched  : {last['events_total']}")
    print(f"  events analysed : {last['events_scanned']}")
    print(f"  opportunities   : {last['opportunities_found']}")
    print(f"  errors          : {last['errors']}")

    # A scan that fetched nothing means the API, the network, or the VPN —
    # not the market being quiet.
    if not last["events_total"]:
        problems.append("last scan fetched zero events (API or network?)")

    # Fetching thousands and analysing none means the filters are wrong,
    # which looks identical to "no arbitrage today" in a log.
    if last["events_total"] and not last["events_scanned"]:
        problems.append("every event was filtered out — check the funnel")

    if last["status"] == "failed":
        problems.append("last scan did not finish")

    if last["errors"] and last["events_scanned"]:
        rate = last["errors"] / last["events_scanned"]
        if rate > 0.05:
            problems.append(f"{rate*100:.0f}% of events errored")

    stale_after = timedelta(seconds=args.stale_after)
    started = datetime.fromisoformat(
        str(last["started_at"]).replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - started > stale_after:
        problems.append(f"no scan in {ago(last['started_at'])} "
                        f"— is the monitor running?")

    recent = db.execute(
        "SELECT COUNT(*) c FROM scans WHERE status = 'failed' "
        "AND id > (SELECT MAX(id) - 10 FROM scans)").fetchone()["c"]
    if recent > 2:
        problems.append(f"{recent} of the last 10 scans failed")

    totals = db.execute(
        "SELECT COUNT(*) opps, MAX(found_at) latest FROM opportunities"
    ).fetchone()
    print(f"\n  opportunities all time : {totals['opps']}"
          f"  (latest {ago(totals['latest'])})")

    signals = db.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]
    print(f"  live signals recorded  : {signals}")

    if problems:
        print("\n  PROBLEMS")
        for problem in problems:
            print(f"    - {problem}")
        return 1

    print("\n  No problems detected.")
    return 0


# =====================================================================
# funnel
# =====================================================================


def cmd_funnel(db, args):
    section(f"Rejection funnel (last {args.scans} scans)")

    total_events = db.execute(
        "SELECT COALESCE(SUM(events_total), 0) t FROM scans "
        "WHERE id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)",
        (args.scans,)).fetchone()["t"]

    rows = db.execute("""
        SELECT stage, code, SUM(count) AS total
        FROM rejections
        WHERE stage != 'suspicion'
          AND scan_id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
        GROUP BY stage, code
        ORDER BY total DESC
    """, (args.scans,)).fetchall()

    if not rows:
        print("  No funnel data. It is recorded from the next scan onward.")
        return 0

    print(f"  events fetched: {total_events:,}\n")
    print(table(
        [(r["stage"], r["code"], f"{r['total']:,}", pct(r["total"], total_events))
         for r in rows],
        ["stage", "reason", "count", "share"],
        ["<", "<", ">", ">"]))

    analysed = db.execute(
        "SELECT COALESCE(SUM(events_scanned), 0) t FROM scans "
        "WHERE id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)",
        (args.scans,)).fetchone()["t"]
    print(f"\n  survived to analysis: {analysed:,} "
          f"({pct(analysed, total_events)})")
    return 0


# =====================================================================
# drift
# =====================================================================


def cmd_drift(db, args):
    """
    Compare the recent window against the one before it.

    This is the check that catches silent breakage. Nothing errors, nothing
    logs a warning, and the dry-leg rate quietly goes from 3% to 40% because
    an endpoint changed shape. The only visible symptom is the absence of
    signals, which looks exactly like a quiet market.
    """
    section(f"Drift: last {args.scans} scans vs the {args.scans} before")

    def window(offset):
        return db.execute("""
            SELECT code, SUM(count) AS total
            FROM rejections
            WHERE stage != 'suspicion'
              AND scan_id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
              AND scan_id <= (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
            GROUP BY code
        """, (offset + args.scans, offset)).fetchall()

    recent = {r["code"]: r["total"] for r in window(0)}
    previous = {r["code"]: r["total"] for r in window(args.scans)}

    if not recent and not previous:
        print("  Not enough history yet.")
        return 0

    rows = []
    for code in sorted(set(recent) | set(previous)):
        now, before = recent.get(code, 0), previous.get(code, 0)
        if before:
            change = f"{(now - before) / before * 100:+.0f}%"
        else:
            change = "new" if now else "-"
        flag = ""
        # An order-of-magnitude move in a rejection reason is worth a look
        # even when the absolute numbers are small.
        if before and (now > before * 3 or now * 3 < before):
            flag = "  <-- big change"
        rows.append((code, f"{before:,}", f"{now:,}", change + flag))

    print(table(rows, ["reason", "before", "now", "change"],
                ["<", ">", ">", "<"]))
    return 0


# =====================================================================
# opportunities / suspects / fees / timings
# =====================================================================


def cmd_opps(db, args):
    section(f"Opportunities (last {args.scans} scans)")
    rows = db.execute("""
        SELECT found_at, market_type, event_title, num_outcomes,
               net_edge, fee_rate, best_profit, best_capital, best_real_cost,
               payout_per_basket, suspicions, url
        FROM opportunities
        WHERE scan_id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
        ORDER BY net_edge DESC
        LIMIT ?
    """, (args.scans, args.limit)).fetchall()

    if not rows:
        print("  None found. `python query.py funnel` shows what was rejected.")
        return 0

    # Ranked by edge per dollar, not by profit: a NO-side basket earns its
    # dollars on far more capital, and sorting by profit would float those
    # to the top purely for being large.
    print(table(
        [(ago(r["found_at"]),
          "NO" if r["market_type"] == "multi_no" else "YES",
          (r["event_title"] or "")[:38],
          r["num_outcomes"], f"{r['net_edge']*100:+.2f}%",
          f"{(r['fee_rate'] or 0)*100:.0f}%",
          f"${r['best_profit']:.2f}", f"${r['best_real_cost'] or 0:.0f}",
          ",".join(json.loads(r["suspicions"] or "[]")) or "-")
         for r in rows],
        ["when", "side", "event", "legs", "edge/$", "fee", "profit", "cost",
         "flags"],
        ["<", "<", "<", ">", ">", ">", ">", ">", "<"]))
    return 0


def cmd_suspects(db, args):
    """
    Opportunities the validator flagged but did not reject.

    Kept separate on purpose: these are the ones to review by hand before
    ever executing. Over time this table also answers whether a given
    suspicion was ever justified — which is how a SUSPECT rule earns
    promotion to REJECT, or gets deleted.
    """
    section(f"Flagged signals (last {args.scans} scans)")

    counts = db.execute("""
        SELECT code, SUM(count) AS total FROM rejections
        WHERE stage = 'suspicion'
          AND scan_id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
        GROUP BY code ORDER BY total DESC
    """, (args.scans,)).fetchall()

    if counts:
        print(table([(r["code"], f"{r['total']:,}") for r in counts],
                    ["flag", "events"], ["<", ">"]))

    rows = db.execute("""
        SELECT found_at, event_title, net_edge, best_profit, suspicions
        FROM opportunities
        WHERE suspicions IS NOT NULL AND suspicions != '[]'
          AND scan_id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
        ORDER BY found_at DESC LIMIT ?
    """, (args.scans, args.limit)).fetchall()

    print("\n  Opportunities carrying a flag:")
    print(table(
        [(ago(r["found_at"]), (r["event_title"] or "")[:44],
          f"{r['net_edge']*100:+.2f}%", f"${r['best_profit']:.2f}",
          ",".join(json.loads(r["suspicions"] or "[]")))
         for r in rows],
        ["when", "event", "edge", "profit", "flags"],
        ["<", "<", ">", ">", "<"]))
    return 0


def cmd_fees(db, args):
    """
    Which fee categories actually produce edges.

    Useful for deciding where to point the live engine: if every real
    opportunity for a month came from 0%-fee events, streaming crypto
    markets is spending the socket budget in the wrong place.
    """
    section("Opportunities by fee rate")
    rows = db.execute("""
        SELECT COALESCE(fee_rate, -1) AS fee_rate,
               COUNT(*) AS n,
               AVG(net_edge) AS avg_edge,
               MAX(net_edge) AS max_edge,
               SUM(best_profit) AS total_profit
        FROM opportunities
        GROUP BY fee_rate ORDER BY n DESC
    """).fetchall()

    print(table(
        [(f"{r['fee_rate']*100:.0f}%" if r["fee_rate"] >= 0 else "unknown",
          r["n"], f"{r['avg_edge']*100:+.2f}%", f"{r['max_edge']*100:+.2f}%",
          f"${r['total_profit']:.2f}")
         for r in rows],
        ["fee", "count", "avg edge", "best edge", "total profit"],
        ["<", ">", ">", ">", ">"]))
    return 0


def cmd_timings(db, args):
    section(f"Where the time goes (last {args.scans} scans)")
    timings = metrics.phase_timings(db, args.scans)
    if not timings:
        print("  No timing data yet.")
        return 0

    total = sum(timings.values())
    print(table(
        [(phase, f"{ms/1000:.1f}s", pct(ms, total))
         for phase, ms in sorted(timings.items(), key=lambda x: -x[1])],
        ["phase", "avg per scan", "share"], ["<", ">", ">"]))
    print(f"\n  average scan: {total/1000:.1f}s")
    return 0


def cmd_sql(db, args):
    """Escape hatch. Read-only by construction: only SELECT is accepted."""
    statement = args.statement.strip()
    if not statement.lower().startswith(("select", "with")):
        print("  Only SELECT/WITH statements are allowed here.")
        return 1
    rows = db.execute(statement).fetchall()
    if not rows:
        print("  (no rows)")
        return 0
    headers = rows[0].keys()
    print(table([[r[h] for h in headers] for r in rows], list(headers)))
    return 0


# =====================================================================
# CLI
# =====================================================================

COMMANDS = {
    "health": cmd_health,
    "funnel": cmd_funnel,
    "drift": cmd_drift,
    "opps": cmd_opps,
    "suspects": cmd_suspects,
    "fees": cmd_fees,
    "timings": cmd_timings,
    "sql": cmd_sql,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Query the arbitrage monitor's database")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("statement", nargs="?", default="",
                        help="SQL, for the `sql` command")
    parser.add_argument("--scans", type=int, default=20,
                        help="how many recent scans to consider")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--stale-after", type=int, default=3600,
                        help="seconds before the last scan counts as stale")
    parser.add_argument("--db", default=None, help="path to the database")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    path = args.db or dblib.DB_PATH
    try:
        db = dblib.connect(path)
    except sqlite3.Error as e:
        print(f"Cannot open {path}: {e}")
        return 2

    if args.json and args.command == "health":
        last = db.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        print(json.dumps(dict(last) if last else {}, indent=2, default=str))
        return 0

    return COMMANDS[args.command](db, args)


if __name__ == "__main__":
    sys.exit(main())
