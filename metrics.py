"""
Metrics — count the funnel, so "nothing was found" has an explanation.
======================================================================

A scan reads three thousand events and analyses forty. The forty are
logged. The two thousand nine hundred and sixty are not, and that is
exactly backwards: when the monitor runs for a week and finds nothing, the
question is never "what did it find" but "where did everything go".

This module counts every stage of the funnel by rejection code and stores
the counts per scan, so the answer is a query rather than a guess:

    SELECT code, SUM(count) FROM rejections
    WHERE scan_id = (SELECT MAX(id) FROM scans) GROUP BY code

Then a week of history makes drift visible. If `dry_leg` climbs from 3% to
40% of events, something changed — the book endpoint, the token parsing,
or the market mix — and the number says so before anyone notices the
absence of signals.

Counters are plain integers in memory and one INSERT per code per scan.
Nothing here should ever be expensive enough to think about.
"""

import json
import sqlite3
from collections import Counter
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    stage TEXT NOT NULL,        -- 'prefilter' | 'book' | 'basket' | 'edge'
    code TEXT NOT NULL,         -- a validate.py code
    count INTEGER NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);
CREATE INDEX IF NOT EXISTS idx_rej_scan ON rejections(scan_id);
CREATE INDEX IF NOT EXISTS idx_rej_code ON rejections(code);

CREATE TABLE IF NOT EXISTS scan_timings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    phase TEXT NOT NULL,        -- 'fetch' | 'prefilter' | 'books' | 'analysis'
    duration_ms REAL NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);
CREATE INDEX IF NOT EXISTS idx_timing_scan ON scan_timings(scan_id);
"""

# Funnel stages, in the order events pass through them. Kept as a list
# because the order is what makes the printed funnel readable.
STAGES = ["prefilter", "book", "basket", "edge"]


class Funnel:
    """
    Counts what happened to every event in one scan.

    Deliberately not a metrics library: this system has one process, one
    scan loop, and a database it already writes to. A Counter and an INSERT
    answer every question a Prometheus exporter would, without a second
    service to run and keep alive.
    """

    def __init__(self, scan_id: int = None):
        self.scan_id = scan_id
        self.rejected: Dict[str, Counter] = {s: Counter() for s in STAGES}
        self.suspicions = Counter()
        self.timings: Dict[str, float] = {}
        self.events_seen = 0
        self.events_analysed = 0
        self.opportunities = 0
        self.near_misses = 0
        self.errors = 0

    # -----------------------------------------------------------------
    # Recording
    # -----------------------------------------------------------------

    def saw_event(self, n: int = 1):
        self.events_seen += n

    def analysed(self, n: int = 1):
        self.events_analysed += n

    def reject(self, stage: str, code: str, n: int = 1):
        if stage not in self.rejected:
            self.rejected[stage] = Counter()
        self.rejected[stage][code] += n

    def suspect(self, codes):
        for code in codes:
            self.suspicions[code] += 1

    def timing(self, phase: str, duration_ms: float):
        self.timings[phase] = self.timings.get(phase, 0.0) + duration_ms

    # -----------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------

    def total_rejected(self, stage: str = None) -> int:
        if stage:
            return sum(self.rejected.get(stage, {}).values())
        return sum(sum(c.values()) for c in self.rejected.values())

    def top_reasons(self, n: int = 10) -> List[tuple]:
        """The most common rejection codes across all stages."""
        combined = Counter()
        for counter in self.rejected.values():
            combined.update(counter)
        return combined.most_common(n)

    def as_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "events_seen": self.events_seen,
            "events_analysed": self.events_analysed,
            "opportunities": self.opportunities,
            "near_misses": self.near_misses,
            "errors": self.errors,
            "rejected_total": self.total_rejected(),
            "rejected": {s: dict(c) for s, c in self.rejected.items() if c},
            "suspicions": dict(self.suspicions),
            "timings_ms": {k: round(v, 1) for k, v in self.timings.items()},
        }

    # -----------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------

    def render(self) -> str:
        """
        The funnel as a human-readable block, with each stage's survivors.

        Printed at the end of every scan. Reading it top to bottom answers
        "where did everything go" in about two seconds, which is the whole
        point.
        """
        lines = ["", "=" * 62,
                 f"Scan funnel" + (f" (scan #{self.scan_id})"
                                   if self.scan_id else ""),
                 "=" * 62]

        remaining = self.events_seen
        lines.append(f"  {'events fetched':<34}{remaining:>8}")

        for stage in STAGES:
            counter = self.rejected.get(stage)
            if not counter:
                continue
            stage_total = sum(counter.values())
            remaining -= stage_total
            lines.append(f"\n  {stage} rejected {stage_total} "
                         f"-> {max(remaining, 0)} remain")
            for code, count in counter.most_common():
                pct = (count / self.events_seen * 100) if self.events_seen else 0
                lines.append(f"      {code:<30}{count:>8}  {pct:>5.1f}%")

        lines.append("")
        lines.append(f"  {'analysed':<34}{self.events_analysed:>8}")
        lines.append(f"  {'opportunities':<34}{self.opportunities:>8}")
        lines.append(f"  {'near misses':<34}{self.near_misses:>8}")
        if self.errors:
            lines.append(f"  {'ERRORS':<34}{self.errors:>8}")

        if self.suspicions:
            lines.append("\n  Flagged but not rejected:")
            for code, count in self.suspicions.most_common():
                lines.append(f"      {code:<30}{count:>8}")

        if self.timings:
            lines.append("\n  Time spent:")
            for phase, ms in sorted(self.timings.items(),
                                    key=lambda x: -x[1]):
                lines.append(f"      {phase:<30}{ms/1000:>7.1f}s")

        lines.append("=" * 62)
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def save(self, db: sqlite3.Connection, scan_id: int = None,
             now: str = None):
        """One row per (stage, code). A scan writes a handful of rows."""
        scan_id = scan_id if scan_id is not None else self.scan_id
        if scan_id is None:
            raise ValueError("cannot save a funnel without a scan_id")

        from db import utcnow
        timestamp = now or utcnow()

        rows = [(scan_id, timestamp, stage, code, count)
                for stage, counter in self.rejected.items()
                for code, count in counter.items()]
        rows += [(scan_id, timestamp, "suspicion", code, count)
                 for code, count in self.suspicions.items()]

        if rows:
            db.executemany(
                "INSERT INTO rejections (scan_id, recorded_at, stage, code, "
                "count) VALUES (?, ?, ?, ?, ?)", rows)

        if self.timings:
            db.executemany(
                "INSERT INTO scan_timings (scan_id, recorded_at, phase, "
                "duration_ms) VALUES (?, ?, ?, ?)",
                [(scan_id, timestamp, phase, ms)
                 for phase, ms in self.timings.items()])

        db.commit()


# =====================================================================
# Reading history back
# =====================================================================


def funnel_for_scan(db: sqlite3.Connection, scan_id: int) -> dict:
    rows = db.execute(
        "SELECT stage, code, count FROM rejections WHERE scan_id = ?",
        (scan_id,)).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row["stage"], {})[row["code"]] = row["count"]
    return result


def rejection_trend(db: sqlite3.Connection, code: str,
                    limit: int = 20) -> List[tuple]:
    """
    How often one rejection code has fired over the last N scans.

    This is the drift detector. A code whose share of events climbs steadily
    is the earliest visible sign that something upstream changed.
    """
    rows = db.execute("""
        SELECT s.id, s.started_at, s.events_total,
               COALESCE(SUM(r.count), 0) AS hits
        FROM scans s
        LEFT JOIN rejections r ON r.scan_id = s.id AND r.code = ?
        WHERE s.status = 'done'
        GROUP BY s.id
        ORDER BY s.id DESC
        LIMIT ?
    """, (code, limit)).fetchall()
    return [(r["id"], r["started_at"], r["events_total"], r["hits"])
            for r in reversed(rows)]


def top_rejection_codes(db: sqlite3.Connection, scans: int = 10) -> List[tuple]:
    rows = db.execute("""
        SELECT code, SUM(count) AS total
        FROM rejections
        WHERE stage != 'suspicion'
          AND scan_id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
        GROUP BY code
        ORDER BY total DESC
    """, (scans,)).fetchall()
    return [(r["code"], r["total"]) for r in rows]


def phase_timings(db: sqlite3.Connection, scans: int = 10) -> Dict[str, float]:
    """Average milliseconds per phase over recent scans."""
    rows = db.execute("""
        SELECT phase, AVG(duration_ms) AS avg_ms
        FROM scan_timings
        WHERE scan_id > (SELECT COALESCE(MAX(id), 0) - ? FROM scans)
        GROUP BY phase
    """, (scans,)).fetchall()
    return {r["phase"]: r["avg_ms"] for r in rows}
