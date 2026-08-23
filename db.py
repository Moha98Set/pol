"""
SQLite database layer for the Polymarket arbitrage pipeline.

Shared by:
  - arb_monitor.py  (task 1: periodic scanner)
  - view_db.py      (history viewer)
  - future tasks    (websocket collector, live engine, executor)

Tables:
  scans         — one row per scan cycle
  opportunities — executable arb opportunities (net_edge >= threshold,
                  slippage-verified)
  near_misses   — best edges seen per scan even when below threshold,
                  to track how close the market gets to arb over time
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

# DB_PATH is overridable so tests and experiments never touch the real
# history: DB_PATH=/tmp/scratch.db python arb_monitor.py
DB_PATH = (Path(config.DB_PATH) if config.DB_PATH
           else Path(__file__).parent / "arb_monitor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    events_total INTEGER DEFAULT 0,
    events_scanned INTEGER DEFAULT 0,
    events_skipped_filter INTEGER DEFAULT 0,
    opportunities_found INTEGER DEFAULT 0,
    near_misses_saved INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    found_at TEXT NOT NULL,
    market_type TEXT NOT NULL,           -- 'binary' or 'multi'
    event_title TEXT,
    event_slug TEXT,
    question TEXT,
    slug TEXT,
    category TEXT,
    volume_24h REAL,

    num_outcomes INTEGER,
    yes_ask REAL,
    no_ask REAL,
    sum_best_asks REAL,
    gross_edge REAL,
    net_edge REAL,
    fee_rate REAL,

    best_capital REAL,
    best_shares REAL,
    best_real_cost REAL,
    best_profit REAL,
    best_roi_pct REAL,

    slippage_curve TEXT,                 -- JSON array
    legs_detail TEXT,                    -- JSON: per-leg best ask info

    url TEXT,

    FOREIGN KEY (scan_id) REFERENCES scans(id)
);

CREATE INDEX IF NOT EXISTS idx_opp_scan ON opportunities(scan_id);
CREATE INDEX IF NOT EXISTS idx_opp_edge ON opportunities(net_edge DESC);
CREATE INDEX IF NOT EXISTS idx_opp_time ON opportunities(found_at);

CREATE TABLE IF NOT EXISTS near_misses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    found_at TEXT NOT NULL,
    market_type TEXT NOT NULL,
    event_title TEXT,
    event_slug TEXT,
    num_outcomes INTEGER,
    sum_best_asks REAL,
    gross_edge REAL,
    net_edge REAL,
    fee_rate REAL,
    volume_24h REAL,
    url TEXT,
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);
CREATE INDEX IF NOT EXISTS idx_nm_scan ON near_misses(scan_id);
CREATE INDEX IF NOT EXISTS idx_nm_edge ON near_misses(net_edge DESC);

-- ------------------------------------------------------------------
-- Live engine (live_engine.py)
-- ------------------------------------------------------------------
-- A signal is an edge observed from the WebSocket feed at a specific
-- instant. Unlike `opportunities`, which are snapshots taken every 15
-- minutes, signals carry a lifetime: first_seen/last_seen tell you
-- whether an edge lasted 200ms or 40 seconds. That distinction decides
-- whether this strategy is executable at all, so it is stored, not logged.
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_slug TEXT NOT NULL,
    event_title TEXT,
    market_type TEXT,
    num_outcomes INTEGER,
    fee_rate REAL,

    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    duration_ms INTEGER,
    updates INTEGER DEFAULT 1,          -- book updates while edge persisted

    best_net_edge REAL,                 -- best edge seen during its lifetime
    best_sum_asks REAL,
    peak_profit REAL,                   -- $ at the best capital
    peak_capital REAL,

    legs_detail TEXT,                   -- JSON snapshot at peak
    curve TEXT,                         -- JSON slippage curve at peak
    acted_on INTEGER DEFAULT 0,         -- 1 if handed to the executor
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_edge ON signals(best_net_edge DESC);
CREATE INDEX IF NOT EXISTS idx_sig_seen ON signals(first_seen);
CREATE INDEX IF NOT EXISTS idx_sig_slug ON signals(event_slug);

-- ------------------------------------------------------------------
-- Executor (executor.py)
-- ------------------------------------------------------------------
-- One row per attempted basket. `mode` is 'dry' or 'live'; dry rows are
-- written with exactly the same code path as live ones, so a paper-trading
-- history is directly comparable to a real one.
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    opportunity_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,                 -- 'dry' | 'live'
    status TEXT NOT NULL,               -- planned|revalidated|placing|
                                        -- filled|partial|aborted|failed
    abort_reason TEXT,

    event_slug TEXT,
    event_title TEXT,
    fee_rate REAL,

    planned_shares REAL,
    planned_cost REAL,
    planned_fee REAL,
    planned_profit REAL,
    planned_net_edge REAL,

    filled_shares REAL,                 -- min across legs = complete baskets
    actual_cost REAL,
    actual_fee REAL,
    realized_profit REAL,               -- at resolution, filled in later

    plan TEXT,                          -- JSON: full plan as executed
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_time ON executions(started_at);
CREATE INDEX IF NOT EXISTS idx_exec_status ON executions(status);

CREATE TABLE IF NOT EXISTS execution_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL,
    leg_index INTEGER NOT NULL,
    outcome TEXT,
    token_id TEXT,
    side TEXT DEFAULT 'BUY',

    planned_shares REAL,
    limit_price REAL,                   -- worst price we accept on this leg
    expected_avg_price REAL,

    order_id TEXT,
    status TEXT,                        -- placed|filled|partial|rejected|skipped
    filled_shares REAL,
    avg_fill_price REAL,
    error TEXT,

    FOREIGN KEY (execution_id) REFERENCES executions(id)
);
CREATE INDEX IF NOT EXISTS idx_exec_legs ON execution_legs(execution_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    # Default SQLite fsyncs on every statement, which made opening a fresh
    # database take 3.5 seconds here — the schema alone is ~40 statements.
    # WAL plus synchronous=NORMAL is the usual pairing for a workload like
    # this one: still durable across a process crash, and it lets view_db
    # and query read while a scan is writing instead of blocking on it.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")

    db.executescript(SCHEMA)
    # metrics owns its own tables; keeping the DDL next to the code that
    # writes it means neither can be added without the other
    import metrics
    db.executescript(metrics.SCHEMA)
    _migrate(db)
    db.commit()
    return db


# Columns added after a release. Applied with ALTER TABLE ... ADD COLUMN,
# which SQLite makes cheap and which leaves existing rows untouched — an
# old database keeps working and simply has NULLs for the new fields.
MIGRATIONS = [
    ("opportunities", "fee_rate", "REAL"),
    ("near_misses", "fee_rate", "REAL"),
    ("opportunities", "suspicions", "TEXT"),      # JSON array of codes
    ("near_misses", "suspicions", "TEXT"),
    # NO-side baskets pay N-1 per basket rather than 1, so the payout has
    # to be stored: without it, net_edge and profit cannot be re-derived
    # from the row and a stored opportunity cannot be re-checked later.
    ("opportunities", "payout_per_basket", "REAL"),
    ("opportunities", "net_edge_per_basket", "REAL"),
]


def _migrate(db: sqlite3.Connection):
    for table, column, coltype in MIGRATIONS:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except sqlite3.OperationalError:
            pass  # column already exists


# ====================================================================
# Scan lifecycle
# ====================================================================


def start_scan(db: sqlite3.Connection) -> int:
    cur = db.execute(
        "INSERT INTO scans (started_at, status) VALUES (?, 'running')",
        (utcnow(),))
    db.commit()
    return cur.lastrowid


def finish_scan(db: sqlite3.Connection, scan_id: int, *,
                events_total: int, events_scanned: int,
                events_skipped_filter: int, opportunities_found: int,
                near_misses_saved: int, errors: int,
                status: str = "done"):
    db.execute("""
        UPDATE scans
        SET finished_at = ?, events_total = ?, events_scanned = ?,
            events_skipped_filter = ?, opportunities_found = ?,
            near_misses_saved = ?, errors = ?, status = ?
        WHERE id = ?
    """, (utcnow(), events_total, events_scanned, events_skipped_filter,
          opportunities_found, near_misses_saved, errors, status, scan_id))
    db.commit()


def mark_stale_scans_failed(db: sqlite3.Connection):
    """Mark any scans left 'running' from a previous crashed process."""
    db.execute(
        "UPDATE scans SET status = 'failed' WHERE status = 'running'")
    db.commit()


# ====================================================================
# Saving results
# ====================================================================


def save_opportunity(db: sqlite3.Connection, scan_id: int, opp: dict):
    db.execute("""
        INSERT INTO opportunities (
            scan_id, found_at, market_type,
            event_title, event_slug, question, slug, category, volume_24h,
            num_outcomes, yes_ask, no_ask, sum_best_asks, gross_edge, net_edge,
            fee_rate,
            best_capital, best_shares, best_real_cost, best_profit, best_roi_pct,
            slippage_curve, legs_detail, suspicions, url,
            payout_per_basket, net_edge_per_basket
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?)
    """, (
        scan_id, utcnow(), opp["market_type"],
        opp.get("event_title"), opp.get("event_slug"), opp.get("question"),
        opp.get("slug"), opp.get("category"), opp.get("volume_24h"),
        opp.get("num_outcomes"), opp.get("yes_ask"), opp.get("no_ask"),
        opp.get("sum_best_asks"), opp.get("gross_edge"), opp.get("net_edge"),
        opp.get("fee_rate"),
        opp.get("best_capital"), opp.get("best_shares"),
        opp.get("best_real_cost"), opp.get("best_profit"),
        opp.get("best_roi_pct"),
        json.dumps(opp.get("slippage_curve")),
        json.dumps(opp.get("legs_detail")),
        json.dumps(opp.get("suspicions") or []),
        opp.get("url"),
        opp.get("payout_per_basket", 1.0),
        opp.get("net_edge_per_basket", opp.get("net_edge")),
    ))
    db.commit()


def save_near_misses(db: sqlite3.Connection, scan_id: int, misses: list):
    """Save the top near-miss edges for this scan."""
    for nm in misses:
        db.execute("""
            INSERT INTO near_misses (
                scan_id, found_at, market_type, event_title, event_slug,
                num_outcomes, sum_best_asks, gross_edge, net_edge, fee_rate,
                volume_24h, suspicions, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_id, utcnow(), nm["market_type"],
            nm.get("event_title"), nm.get("event_slug"),
            nm.get("num_outcomes"), nm.get("sum_best_asks"),
            nm.get("gross_edge"), nm.get("net_edge"), nm.get("fee_rate"),
            nm.get("volume_24h"), json.dumps(nm.get("suspicions") or []),
            nm.get("url"),
        ))
    db.commit()


# ====================================================================
# Live engine
# ====================================================================


def save_signal(db: sqlite3.Connection, sig: dict) -> int:
    """Persist one completed signal (an edge that appeared and then died)."""
    cur = db.execute("""
        INSERT INTO signals (
            event_slug, event_title, market_type, num_outcomes, fee_rate,
            first_seen, last_seen, duration_ms, updates,
            best_net_edge, best_sum_asks, peak_profit, peak_capital,
            legs_detail, curve, acted_on, url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sig.get("event_slug"), sig.get("event_title"), sig.get("market_type"),
        sig.get("num_outcomes"), sig.get("fee_rate"),
        sig.get("first_seen"), sig.get("last_seen"), sig.get("duration_ms"),
        sig.get("updates", 1),
        sig.get("best_net_edge"), sig.get("best_sum_asks"),
        sig.get("peak_profit"), sig.get("peak_capital"),
        json.dumps(sig.get("legs_detail")), json.dumps(sig.get("curve")),
        1 if sig.get("acted_on") else 0, sig.get("url"),
    ))
    db.commit()
    return cur.lastrowid


# ====================================================================
# Executor
# ====================================================================


def start_execution(db: sqlite3.Connection, plan: dict, mode: str) -> int:
    """
    Record an execution attempt BEFORE any order is sent.

    Written first on purpose: if the process dies mid-basket, the row that
    says 'placing' is the only evidence that half a position may exist.
    """
    cur = db.execute("""
        INSERT INTO executions (
            signal_id, opportunity_id, started_at, mode, status,
            event_slug, event_title, fee_rate,
            planned_shares, planned_cost, planned_fee, planned_profit,
            planned_net_edge, plan, url
        ) VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        plan.get("signal_id"), plan.get("opportunity_id"), utcnow(), mode,
        plan.get("event_slug"), plan.get("event_title"), plan.get("fee_rate"),
        plan.get("shares"), plan.get("cost"), plan.get("fee"),
        plan.get("profit"), plan.get("net_edge"),
        json.dumps(plan, default=str), plan.get("url"),
    ))
    db.commit()
    execution_id = cur.lastrowid

    for i, leg in enumerate(plan.get("legs", [])):
        db.execute("""
            INSERT INTO execution_legs (
                execution_id, leg_index, outcome, token_id, side,
                planned_shares, limit_price, expected_avg_price, status
            ) VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, 'planned')
        """, (
            execution_id, i, leg.get("outcome"), leg.get("token_id"),
            leg.get("shares"), leg.get("limit_price"),
            leg.get("expected_avg_price"),
        ))
    db.commit()
    return execution_id


def set_execution_status(db: sqlite3.Connection, execution_id: int,
                         status: str, abort_reason: str = None):
    db.execute(
        "UPDATE executions SET status = ?, abort_reason = ? WHERE id = ?",
        (status, abort_reason, execution_id))
    db.commit()


def update_execution_leg(db: sqlite3.Connection, execution_id: int,
                         leg_index: int, **fields):
    """Update one leg's outcome. Only known columns are accepted."""
    allowed = {"order_id", "status", "filled_shares",
               "avg_fill_price", "error", "limit_price"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    clause = ", ".join(f"{k} = ?" for k in sets)
    db.execute(
        f"UPDATE execution_legs SET {clause} "
        f"WHERE execution_id = ? AND leg_index = ?",
        (*sets.values(), execution_id, leg_index))
    db.commit()


def finish_execution(db: sqlite3.Connection, execution_id: int, *,
                     status: str, filled_shares: float = None,
                     actual_cost: float = None, actual_fee: float = None,
                     abort_reason: str = None):
    db.execute("""
        UPDATE executions
        SET finished_at = ?, status = ?, filled_shares = ?,
            actual_cost = ?, actual_fee = ?, abort_reason = ?
        WHERE id = ?
    """, (utcnow(), status, filled_shares, actual_cost, actual_fee,
          abort_reason, execution_id))
    db.commit()


def open_executions(db: sqlite3.Connection) -> list:
    """
    Executions left in a non-terminal state — i.e. a previous process died
    while orders were in flight. Check this before trading again.
    """
    return db.execute("""
        SELECT * FROM executions
        WHERE status IN ('planned', 'revalidated', 'placing')
        ORDER BY id DESC
    """).fetchall()
