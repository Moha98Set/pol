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

    -- top of book only: the size available at the quoted price
    top_shares REAL,
    top_capital REAL,
    top_profit REAL,

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

-- One row per event per scan: the verdict that event received and the
-- numbers behind it.
--
-- The rejections table already counts *how many* events each reason
-- rejected, which is all the funnel needs. It cannot answer "which
-- markets did we skip, and why" because identity is discarded the moment
-- the counter increments. That question is the whole point of a dashboard
-- someone reads to form a judgement, so the identity is kept here.
--
-- Exactly one row per event per scan — written where the event came to
-- rest, not at every stage it passed through — so a scan's rows are a
-- partition of what it fetched and can be counted on without dedupe.
--
-- ~2100 rows per scan at 96 scans a day is 200k rows daily, so this table
-- is pruned to the most recent VERDICT_RETENTION_SCANS scans. It is a
-- window on recent behaviour, never the permanent record; opportunities,
-- near_misses and rejections remain that.
CREATE TABLE IF NOT EXISTS event_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,

    event_slug TEXT,
    event_title TEXT,
    category TEXT,
    market_type TEXT,                   -- 'binary' | 'multi' | NULL pre-filter
    num_outcomes INTEGER,
    volume_24h REAL,

    stage TEXT NOT NULL,                -- prefilter|book|edge|analysis|error
    code TEXT NOT NULL,                 -- a validate.py code, 'ok' if kept
    outcome TEXT NOT NULL,              -- rejected|near_miss|opportunity|error
    detail TEXT,                        -- the verdict's own explanation
    suspicions TEXT,                    -- JSON array: flagged, not rejected

    sum_best_asks REAL,
    gross_edge REAL,
    net_edge REAL,
    fee_rate REAL,
    fee_category TEXT,
    url TEXT,

    FOREIGN KEY (scan_id) REFERENCES scans(id)
);
-- An edge's shape over time, not just the moment it crossed a threshold.
--
-- live_engine already recomputes the edge on every book update — 113
-- evaluations a minute in a quiet market — and discarded all of it unless
-- it beat LIVE_MIN_EDGE. The windows analysts care about are exactly the
-- ones that never got there: a market opens, dips for ten minutes, and
-- closes again, entirely between two 15-minute scans.
--
-- edge_windows is one row per episode, which is the unit worth analysing:
-- how long these last, how deep they go, when in the day they happen.
-- edge_ticks is the shape inside one episode, for zooming in. Ticks are
-- rate-limited per event and pruned; windows are small and kept.
CREATE TABLE IF NOT EXISTS edge_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_slug TEXT NOT NULL,
    event_title TEXT,
    side TEXT,                          -- 'yes' | 'no'
    num_outcomes INTEGER,
    fee_rate REAL,
    payout REAL,                        -- 1 for YES, N-1 for NO

    opened_at TEXT NOT NULL,
    closed_at TEXT,                     -- NULL while still open
    duration_ms INTEGER,
    ticks INTEGER DEFAULT 0,

    opened_edge REAL,
    best_edge REAL,                     -- highest edge reached
    best_sum_asks REAL,                 -- and the basket price there
    best_at TEXT,
    closed_edge REAL,

    -- The most this window was ever worth in dollars, which is the number
    -- that decides whether it was an opportunity or a curiosity.
    best_capital REAL,                  -- absorbed, not requested
    best_profit REAL,

    -- 1 if it ever beat LIVE_MIN_EDGE, i.e. became a real signal rather
    -- than only a near approach
    crossed INTEGER DEFAULT 0,
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_win_slug ON edge_windows(event_slug);
CREATE INDEX IF NOT EXISTS idx_win_opened ON edge_windows(opened_at);
CREATE INDEX IF NOT EXISTS idx_win_best ON edge_windows(best_edge);
CREATE INDEX IF NOT EXISTS idx_win_open ON edge_windows(closed_at);

CREATE TABLE IF NOT EXISTS edge_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,             -- epoch ms: cheap range scans
    recorded_at TEXT NOT NULL,
    sum_best_asks REAL,
    net_edge REAL,
    comparable_edge REAL,               -- per dollar of capital

    -- Price alone cannot tell a real window from a decorative one. An
    -- edge that lasted ten minutes but was never fillable for more than
    -- five dollars looks identical on a price chart to one worth taking,
    -- and dry_leg is already the single biggest rejection reason after
    -- the pre-filter. These are what separate the two.
    fillable_capital REAL,              -- what the book actually absorbed
    fillable_profit REAL,               -- what it would have returned

    FOREIGN KEY (window_id) REFERENCES edge_windows(id)
);
CREATE INDEX IF NOT EXISTS idx_tick_window ON edge_ticks(window_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_tick_ts ON edge_ticks(ts_ms);

CREATE INDEX IF NOT EXISTS idx_verdict_scan ON event_verdicts(scan_id);
CREATE INDEX IF NOT EXISTS idx_verdict_code ON event_verdicts(code);
CREATE INDEX IF NOT EXISTS idx_verdict_outcome ON event_verdicts(outcome);
CREATE INDEX IF NOT EXISTS idx_verdict_slug ON event_verdicts(event_slug);
CREATE INDEX IF NOT EXISTS idx_verdict_edge ON event_verdicts(net_edge);
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
    # What fits at the quoted price, with no slippage at all. best_capital
    # is the profit-maximising rung of the ladder and gets there by walking
    # down the book at worse prices; these three answer the different
    # question of how much the top of book alone can take.
    ("opportunities", "top_shares", "REAL"),
    ("opportunities", "top_capital", "REAL"),
    ("opportunities", "top_profit", "REAL"),
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
            top_shares, top_capital, top_profit,
            slippage_curve, legs_detail, suspicions, url,
            payout_per_basket, net_edge_per_basket
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        opp.get("top_shares"), opp.get("top_capital"), opp.get("top_profit"),
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


def save_event_verdicts(db: sqlite3.Connection, scan_id: int, rows: list):
    """
    Write this scan's per-event verdicts.

    One executemany rather than a loop of execute: a scan produces a couple
    of thousand of these, and at that size the per-statement overhead is
    the difference between a rounding error and a visible pause at the end
    of every cycle.
    """
    if not rows:
        return

    now = utcnow()
    db.executemany("""
        INSERT INTO event_verdicts (
            scan_id, recorded_at, event_slug, event_title, category,
            market_type, num_outcomes, volume_24h, stage, code, outcome,
            detail, suspicions, sum_best_asks, gross_edge, net_edge,
            fee_rate, fee_category, url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(
        scan_id, now, r.get("event_slug"), r.get("event_title"),
        r.get("category"), r.get("market_type"), r.get("num_outcomes"),
        r.get("volume_24h"), r["stage"], r["code"], r["outcome"],
        r.get("detail"), json.dumps(r.get("suspicions") or []),
        r.get("sum_best_asks"), r.get("gross_edge"), r.get("net_edge"),
        r.get("fee_rate"), r.get("fee_category"), r.get("url"),
    ) for r in rows])
    db.commit()


def prune_event_verdicts(db: sqlite3.Connection, keep_scans: int) -> int:
    """
    Drop verdict rows older than the most recent `keep_scans` scans.

    Bounded by scan count rather than by age because that stays correct
    when SCAN_INTERVAL changes: the window is always "the last N scans I
    actually ran", not a wall-clock guess about how many that should have
    been. keep_scans <= 0 disables pruning.
    """
    if keep_scans <= 0:
        return 0

    row = db.execute("""
        SELECT scan_id FROM event_verdicts
        GROUP BY scan_id ORDER BY scan_id DESC LIMIT 1 OFFSET ?
    """, (keep_scans - 1,)).fetchone()
    if row is None:
        return 0

    cur = db.execute("DELETE FROM event_verdicts WHERE scan_id < ?",
                     (row["scan_id"],))
    db.commit()
    return cur.rowcount


# ====================================================================
# Edge windows — an episode and its shape
# ====================================================================


def open_edge_window(db: sqlite3.Connection, w: dict) -> int:
    cur = db.execute("""
        INSERT INTO edge_windows (
            event_slug, event_title, side, num_outcomes, fee_rate, payout,
            opened_at, opened_edge, best_edge, best_sum_asks, best_at,
            best_capital, best_profit, crossed, url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        w["event_slug"], w.get("event_title"), w.get("side"),
        w.get("num_outcomes"), w.get("fee_rate"), w.get("payout"),
        w["opened_at"], w.get("edge"), w.get("edge"),
        w.get("sum_best_asks"), w["opened_at"],
        w.get("fillable_capital"), w.get("fillable_profit"),
        int(bool(w.get("crossed"))), w.get("url"),
    ))
    db.commit()
    return cur.lastrowid


def update_edge_window(db: sqlite3.Connection, window_id: int, w: dict):
    """Extend an open window with a new best, if this tick beat the old one."""
    db.execute("""
        UPDATE edge_windows SET
            ticks = ?,
            best_edge = ?,
            best_sum_asks = ?,
            best_at = ?,
            best_capital = ?,
            best_profit = ?,
            crossed = MAX(crossed, ?)
        WHERE id = ?
    """, (w["ticks"], w["best_edge"], w["best_sum_asks"], w["best_at"],
          w.get("best_capital"), w.get("best_profit"),
          int(bool(w.get("crossed"))), window_id))
    db.commit()


def close_edge_window(db: sqlite3.Connection, window_id: int, *,
                      closed_at: str, duration_ms: int, closed_edge: float,
                      ticks: int):
    db.execute("""
        UPDATE edge_windows
        SET closed_at = ?, duration_ms = ?, closed_edge = ?, ticks = ?
        WHERE id = ?
    """, (closed_at, duration_ms, closed_edge, ticks, window_id))
    db.commit()


def save_edge_ticks(db: sqlite3.Connection, rows: list):
    """
    Append a batch of ticks.

    Batched by the caller rather than written per update: a busy market can
    produce several evaluations a second, and one commit each would put
    fsync on the hot path of a WebSocket handler.
    """
    if not rows:
        return
    db.executemany("""
        INSERT INTO edge_ticks (
            window_id, ts_ms, recorded_at, sum_best_asks, net_edge,
            comparable_edge, fillable_capital, fillable_profit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [(r["window_id"], r["ts_ms"], r["recorded_at"], r["sum_best_asks"],
           r["net_edge"], r["comparable_edge"], r.get("fillable_capital"),
           r.get("fillable_profit")) for r in rows])
    db.commit()


def close_orphan_windows(db: sqlite3.Connection) -> int:
    """
    Close windows a previous run left open.

    The engine is killed mid-window every time it restarts, so without this
    an old row stays open forever and every query for "currently open"
    returns something that ended days ago.
    """
    cur = db.execute("""
        UPDATE edge_windows
        SET closed_at = opened_at, duration_ms = 0, closed_edge = opened_edge
        WHERE closed_at IS NULL
    """)
    db.commit()
    return cur.rowcount


def prune_edge_ticks(db: sqlite3.Connection, keep_days: int) -> int:
    """
    Drop tick rows older than `keep_days`. Windows are never pruned here —
    they are small, and they are the record worth keeping.
    """
    if keep_days <= 0:
        return 0
    cutoff = int((datetime.now(timezone.utc).timestamp() - keep_days * 86400)
                 * 1000)
    cur = db.execute("DELETE FROM edge_ticks WHERE ts_ms < ?", (cutoff,))
    db.commit()
    return cur.rowcount


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
