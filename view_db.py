"""
View arbitrage monitor history from the SQLite database.

Usage:
    python view_db.py            # summary: recent scans + best findings
    python view_db.py opps       # all stored opportunities
    python view_db.py near       # recent near misses
    python view_db.py scans      # all scan records
    python view_db.py sig        # live-engine signals (edges + lifetimes)
    python view_db.py life       # how long edges survive — the key question
    python view_db.py exec       # execution attempts (dry and live)
"""

import json
import sys

import db as dblib


def show_scans(db, limit=20):
    rows = db.execute(
        "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    print(f"\n{'ID':>4} {'Started (UTC)':<20} {'Status':<8} "
          f"{'Scanned':>8} {'Opps':>5} {'Near':>5} {'Errs':>5}")
    print("-" * 62)
    for r in rows:
        started = (r["started_at"] or "")[:19].replace("T", " ")
        print(f"{r['id']:>4} {started:<20} {r['status']:<8} "
              f"{r['events_scanned']:>8} {r['opportunities_found']:>5} "
              f"{r['near_misses_saved']:>5} {r['errors']:>5}")


def show_opportunities(db, limit=50):
    rows = db.execute("""
        SELECT * FROM opportunities ORDER BY net_edge DESC LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        print("\nNo opportunities stored yet.")
        return

    print(f"\n{len(rows)} opportunities (best edge first):\n")
    for r in rows:
        found = (r["found_at"] or "")[:19].replace("T", " ")
        print(f"[{r['market_type']}] {(r['event_title'] or '')[:60]}")
        print(f"  found: {found} UTC | scan #{r['scan_id']}")
        print(f"  sum_asks={r['sum_best_asks']:.4f} | "
              f"net_edge={r['net_edge']*100:.2f}% | "
              f"volume=${r['volume_24h'] or 0:,.0f}")
        print(f"  best: ${r['best_profit']:.2f} profit @ ${r['best_capital']:.0f} "
              f"({r['best_roi_pct']:.1f}% ROI)")
        curve = json.loads(r["slippage_curve"] or "[]")
        if curve:
            print(f"  curve: " + " | ".join(
                f"${c['capital']}->${c['profit']:.2f}" for c in curve))
        print(f"  {r['url']}\n")


def show_near_misses(db, limit=40):
    rows = db.execute("""
        SELECT * FROM near_misses ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        print("\nNo near misses stored yet.")
        return

    print(f"\nLast {len(rows)} near misses (newest first):\n")
    print(f"{'Scan':>5} {'Type':<7} {'NetEdge':>8} {'SumAsks':>8} "
          f"{'Legs':>5}  Title")
    print("-" * 90)
    for r in rows:
        print(f"{r['scan_id']:>5} {r['market_type']:<7} "
              f"{r['net_edge']*100:>7.2f}% {r['sum_best_asks']:>8.4f} "
              f"{r['num_outcomes']:>5}  {(r['event_title'] or '')[:45]}")


def show_signals(db, limit=40):
    rows = db.execute(
        "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("\nNo signals recorded yet. Run: python live_engine.py")
        return

    print(f"\nLast {len(rows)} live signals (newest first):\n")
    print(f"{'First seen':<20} {'Type':<7} {'Edge':>8} {'Lived':>9} "
          f"{'Upd':>4} {'Peak $':>8}  Title")
    print("-" * 100)
    for r in rows:
        seen = (r["first_seen"] or "")[:19].replace("T", " ")
        lived = f"{(r['duration_ms'] or 0)/1000:.2f}s"
        print(f"{seen:<20} {r['market_type'] or '':<7} "
              f"{(r['best_net_edge'] or 0)*100:>7.3f}% {lived:>9} "
              f"{r['updates'] or 0:>4} {r['peak_profit'] or 0:>8.2f}  "
              f"{(r['event_title'] or '')[:40]}")


def show_lifetimes(db):
    """
    The single most decision-relevant query in the database.

    If the median edge lives 200ms, no REST-based executor can ever catch
    one and the honest move is to stop building. If it lives 10s, this is
    a real business. Nothing else answers that question.
    """
    rows = db.execute(
        "SELECT duration_ms FROM signals WHERE duration_ms IS NOT NULL "
        "ORDER BY duration_ms").fetchall()
    if not rows:
        print("\nNo signal lifetimes recorded yet. Run: python live_engine.py")
        return

    durations = [r["duration_ms"] for r in rows]
    n = len(durations)

    def pct(p):
        return durations[min(int(n * p), n - 1)]

    print("\n" + "=" * 60)
    print("Edge lifetime distribution")
    print("=" * 60)
    print(f"  signals        : {n}")
    print(f"  min            : {durations[0]:,} ms")
    print(f"  p25            : {pct(0.25):,} ms")
    print(f"  median         : {pct(0.50):,} ms")
    print(f"  p75            : {pct(0.75):,} ms")
    print(f"  p95            : {pct(0.95):,} ms")
    print(f"  max            : {durations[-1]:,} ms")
    print(f"  mean           : {sum(durations)/n:,.0f} ms")

    survivable = sum(1 for d in durations if d >= 1000)
    print(f"\n  lived >= 1s    : {survivable} ({survivable/n*100:.1f}%)")
    print("  A REST round-trip to Polymarket is ~200-500ms. Edges shorter")
    print("  than that were never executable from this machine.")


def show_executions(db, limit=30):
    rows = db.execute(
        "SELECT * FROM executions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("\nNo executions recorded yet. "
              "Run: python executor.py --check")
        return

    print(f"\nLast {len(rows)} executions:\n")
    for r in rows:
        started = (r["started_at"] or "")[:19].replace("T", " ")
        flag = "!!" if r["status"] in ("partial", "failed") else "  "
        print(f"{flag}#{r['id']:<4} [{r['mode']:<4}] {r['status']:<11} "
              f"{started}  {(r['event_title'] or '')[:40]}")
        print(f"     planned: {r['planned_shares'] or 0:.2f} baskets, "
              f"cost ${r['planned_cost'] or 0:.2f}, "
              f"profit ${r['planned_profit'] or 0:.2f} "
              f"(edge {(r['planned_net_edge'] or 0)*100:.3f}%)")
        if r["filled_shares"]:
            print(f"     filled : {r['filled_shares']:.2f} baskets, "
                  f"cost ${r['actual_cost'] or 0:.2f}, "
                  f"fee ${r['actual_fee'] or 0:.2f}")
        if r["abort_reason"]:
            print(f"     reason : {r['abort_reason']}")

        legs = db.execute(
            "SELECT * FROM execution_legs WHERE execution_id = ? "
            "ORDER BY leg_index", (r["id"],)).fetchall()
        for leg in legs:
            print(f"       - {str(leg['outcome'])[:26]:<28} "
                  f"{leg['status'] or '':<9} "
                  f"limit={leg['limit_price'] or 0:.3f} "
                  f"filled={leg['filled_shares'] or 0:.2f}"
                  + (f"  ERR: {leg['error']}" if leg["error"] else ""))
        print()


def show_summary(db):
    n_scans = db.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"]
    n_opps = db.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
    n_near = db.execute("SELECT COUNT(*) c FROM near_misses").fetchone()["c"]

    print("=" * 60)
    print("Arbitrage Monitor — Database Summary")
    print("=" * 60)
    print(f"Total scans: {n_scans} | opportunities: {n_opps} | "
          f"near misses: {n_near}")

    print("\nRecent scans:")
    show_scans(db, limit=10)

    if n_opps:
        print("\nTop opportunities:")
        show_opportunities(db, limit=5)

    # best near miss ever — how close did we get?
    best = db.execute("""
        SELECT * FROM near_misses ORDER BY net_edge DESC LIMIT 5
    """).fetchall()
    if best:
        print("\nClosest near misses ever (how close the market got to arb):")
        for r in best:
            print(f"  net_edge={r['net_edge']*100:+.2f}% "
                  f"sum_asks={r['sum_best_asks']:.4f} "
                  f"[{r['market_type']}] {(r['event_title'] or '')[:50]}")


def main():
    db = dblib.connect()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"

    if cmd == "opps":
        show_opportunities(db)
    elif cmd == "near":
        show_near_misses(db)
    elif cmd == "scans":
        show_scans(db, limit=100)
    elif cmd == "sig":
        show_signals(db)
    elif cmd == "life":
        show_lifetimes(db)
    elif cmd == "exec":
        show_executions(db)
    else:
        show_summary(db)


if __name__ == "__main__":
    main()
