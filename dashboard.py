"""
Dashboard — the pipeline's findings, for people who did not write it.
=====================================================================

Everything upstream of this file is written for whoever maintains the
pipeline: codes, funnels, verdicts. The audience here is a financial
analyst who wants to know what the system saw today and whether skipping a
particular market was the right call. So every raw code is translated
through glossary.py, and every table is built to be scanned by eye rather
than grepped.

The data comes from three places, in decreasing order of permanence:

    opportunities / near_misses   the long record; kept forever
    event_verdicts                one row per event per scan, pruned to a
                                  window (see VERDICT_RETENTION_SCANS)
    rejections / scan_timings     aggregate counters, kept forever

Read-only by design: this process opens the same SQLite file the monitor
writes and never issues anything but SELECT. WAL mode is what makes that
safe to do while a scan is running.

Run:
    python dashboard.py --hash-password       # make a password hash
    python dashboard.py                       # development server

In production it runs under systemd; see deploy/polly-dash.service.
"""

import argparse
import getpass
import json
import os
import secrets
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)

import config
import dashauth
import glossary

# =====================================================================
# Settings
# =====================================================================

DB_PATH = Path(config.DB_PATH) if config.DB_PATH else (
    Path(__file__).parent / "arb_monitor.db")

# Accounts live in their own database next to the market one. The two env
# vars are the pre-accounts single login and are still honoured: on first
# start they are imported as the first account, so an existing deployment
# keeps working through the upgrade instead of locking everyone out.
AUTH_DB_PATH = Path(os.getenv("POLLY_DASH_AUTH_DB") or
                    (DB_PATH.parent / "dashboard.db"))


def _check_auth_db_usable(path: Path):
    """
    Fail with the reason rather than with sqlite3's "unable to open".

    The account CLI is run by hand, so it does not get the unit's
    EnvironmentFile. Without DB_PATH the auth database resolves next to the
    code in /opt/polly, which the service user cannot write — and if it
    ever could, accounts would be created in a file the running service
    never reads, which is worse than an error.
    """
    if path.exists():
        return
    parent = path.parent
    if not parent.exists():
        raise SystemExit(
            f"مسیر {parent} وجود ندارد.\n"
            f"احتمالاً فایل تنظیمات لود نشده. این‌طور اجرا کنید:\n\n"
            f"  sudo -u polly bash -c 'set -a; . /etc/polly/polly.env; "
            f"set +a; \\\n"
            f"      /opt/polly/venv/bin/python /opt/polly/dashboard.py ...'")
    if not os.access(parent, os.W_OK):
        raise SystemExit(
            f"اجازه‌ی نوشتن در {parent} نیست، پس {path.name} ساخته نمی‌شود.\n\n"
            f"اگر DB_PATH تنظیم نشده باشد، این مسیر کنار کد حساب می‌شود که "
            f"عمداً فقط‌خواندنی است.\n"
            f"فایل تنظیمات را لود کنید:\n\n"
            f"  sudo -u polly bash -c 'set -a; . /etc/polly/polly.env; "
            f"set +a; \\\n"
            f"      /opt/polly/venv/bin/python /opt/polly/dashboard.py ...'\n\n"
            f"یا مسیر را صریح بدهید:  --auth-db /var/lib/polly/dashboard.db")

DASH_USER = os.getenv("POLLY_DASH_USER", "")
DASH_PASSWORD_HASH = os.getenv("POLLY_DASH_PASSWORD_HASH", "")
SECRET_KEY = os.getenv("POLLY_DASH_SECRET_KEY", "")

DASH_HOST = os.getenv("POLLY_DASH_HOST", "127.0.0.1")
DASH_PORT = int(os.getenv("POLLY_DASH_PORT") or 8000)

# Units the System tab is allowed to read. A fixed list, never anything
# derived from the request — the alternative is handing a URL parameter to
# a subprocess.
UNITS = ("polly-monitor", "polly-live", "polly-dash")

PAGE_SIZE = 50

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


# =====================================================================
# Accounts
# =====================================================================
# The hashing itself lives in dashauth so the CLI and the request path
# cannot drift apart on scrypt parameters.

hash_password = dashauth.hash_password


def auth_db():
    if "auth" not in g:
        conn = dashauth.connect(AUTH_DB_PATH)
        _seed_first_account(conn)
        g.auth = conn
    return g.auth


def _seed_first_account(conn):
    """
    Carry the pre-accounts single login into the users table, once.

    Without this, upgrading a running dashboard would leave nobody able to
    log in until someone read the release notes.
    """
    if dashauth.user_count(conn) or not (DASH_USER and DASH_PASSWORD_HASH):
        return
    conn.execute("""
        INSERT INTO dash_users (username, display_name, password_hash,
                                created_at)
        VALUES (?, ?, ?, ?)
    """, (DASH_USER, "imported from polly.env", DASH_PASSWORD_HASH,
          dashauth.utcnow()))
    conn.commit()
    app.logger.info("imported %r from the environment as the first account",
                    DASH_USER)


def client_ip() -> str:
    return (request.headers.get("X-Forwarded-For",
                                request.remote_addr or "?")
            .split(",")[0].strip())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# =====================================================================
# Database
# =====================================================================


def db():
    if "db" not in g:
        if not DB_PATH.exists():
            abort(503, "دیتابیس هنوز ساخته نشده است.")
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    for key in ("db", "auth"):
        conn = g.pop(key, None)
        if conn is not None:
            conn.close()


def rows(sql, params=()):
    return db().execute(sql, params).fetchall()


def one(sql, params=()):
    return db().execute(sql, params).fetchone()


def table_exists(name: str) -> bool:
    return one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
               (name,)) is not None


# =====================================================================
# Formatting helpers, exposed to templates
# =====================================================================


def fa_num(value, digits=2):
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def pct(value, digits=2):
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}٪"


def money(value, digits=0):
    if value is None:
        return "—"
    return f"${value:,.{digits}f}"


def ago(iso_string):
    """Relative time in Persian; the absolute stamp goes in the tooltip."""
    if not iso_string:
        return "—"
    try:
        then = datetime.fromisoformat(iso_string)
    except ValueError:
        return iso_string
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 60:
        return "همین الان"
    if seconds < 3600:
        return f"{int(seconds // 60)} دقیقه پیش"
    if seconds < 86400:
        return f"{int(seconds // 3600)} ساعت پیش"
    return f"{int(seconds // 86400)} روز پیش"


def duration(ms):
    """
    A span of milliseconds in the unit that keeps it readable.

    Everything used to be printed in minutes, which turns a four-second
    window into 0.1 and an hour-long one into 61.4 — both of which have to
    be converted in the reader's head before they mean anything. Each range
    gets the unit a person would use out loud for it.
    """
    if ms is None:
        return "—"
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return "—"

    if ms < 1000:
        return f"{ms:.0f} میلی‌ثانیه"
    seconds = ms / 1000
    if seconds < 60:
        # one decimal below ten seconds, where the difference between 2 and
        # 2.5 decides whether a window was reachable at all
        return f"{seconds:.1f} ثانیه" if seconds < 10 else f"{seconds:.0f} ثانیه"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} دقیقه"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} ساعت"
    return f"{hours / 24:.1f} روز"


def fromjson(raw):
    """Decode a JSON column, treating anything unparseable as empty."""
    try:
        return json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []


def stage_note(stage):
    return glossary.STAGES.get(stage, ("", ""))[1]


app.jinja_env.filters.update(
    fa_num=fa_num, pct=pct, money=money, ago=ago, fromjson=fromjson,
    duration=duration,
    reason_label=glossary.reason_label,
    stage_label=glossary.stage_label,
    outcome_label=glossary.outcome_label,
)
app.jinja_env.globals.update(reason=glossary.reason, stage_note=stage_note,
                             UNITS=UNITS)


# =====================================================================
# Sorting and numeric range filters
# =====================================================================
# Both are applied in SQL rather than in the browser. A table can hold two
# thousand rows across forty pages, and sorting only the fifty currently
# rendered would answer "the largest on this page" while looking like it
# answered "the largest". That is worse than no sorting at all.
#
# Column names never reach SQL from the request. Each view declares the
# columns it allows and the expression each one maps to; anything else is
# ignored.


class Col:
    """
    One sortable, filterable column.

    `scale` converts what a person types into what is stored. Edges live as
    0.003 but are read as 0.300%, and a filter box that silently means
    "30%" when someone types 0.3 is a trap — so percentage columns carry
    0.01 and durations carry 60000, and the input stays in the unit shown
    in the table.
    """

    def __init__(self, expr, label, kind="number", scale=1.0, step="any"):
        self.expr = expr
        self.label = label
        self.kind = kind          # number | percent | money | duration
        self.scale = scale
        self.step = step


# Time windows offered next to the numeric ranges. Kept short and coarse:
# an analyst narrowing a table wants "the last few hours", and a date
# picker for that is friction. Custom from/to is still available for the
# cases these do not cover.
SINCE_CHOICES = [
    ("", "همه‌ی زمان‌ها"),
    ("1h", "۱ ساعت اخیر"),
    ("6h", "۶ ساعت اخیر"),
    ("24h", "۲۴ ساعت اخیر"),
    ("7d", "۷ روز اخیر"),
    ("30d", "۳۰ روز اخیر"),
]

_SINCE_DELTA = {"1h": 3600, "6h": 6 * 3600, "24h": 86400,
                "7d": 7 * 86400, "30d": 30 * 86400}


def _time_clauses(time_col: str, default_since: str = ""):
    """
    Narrow by time. Timestamps are stored as ISO 8601 with a +00:00 offset
    throughout, so a lexicographic comparison against another such string
    is also a chronological one — no date parsing in SQL.
    """
    clauses, params, state = [], [], {}

    since = request.args.get("since")
    if since is None:
        since = default_since
    if since in _SINCE_DELTA:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=_SINCE_DELTA[since])
        clauses.append(f"{time_col} >= ?")
        params.append(cutoff.isoformat())
        state["since"] = since

    # A bare date from a date input means midnight; the `to` bound is made
    # inclusive of that whole day, which is what a person picking a day
    # means by it.
    frm = (request.args.get("from") or "").strip()
    if frm:
        clauses.append(f"{time_col} >= ?")
        params.append(frm)
        state["from"] = frm
    to = (request.args.get("to") or "").strip()
    if to:
        clauses.append(f"{time_col} <= ?")
        params.append(to + "T23:59:59.999999+00:00" if len(to) == 10 else to)
        state["to"] = to

    return clauses, params, state


def sort_and_filter(cols: dict, default: str, default_dir: str = "desc",
                    time_col: str = None, default_since: str = ""):
    """
    Build (order_by, where_clauses, params, state) from the query string.

    `state` is what the template needs to render the headers and the boxes
    without re-deriving any of it.
    """
    col = request.args.get("sort") or default
    if col not in cols:
        col = default
    direction = request.args.get("dir")
    if direction not in ("asc", "desc"):
        direction = default_dir

    expr = cols[col].expr
    # NULLs last in both directions: a row with no edge is not the best
    # edge, and it is not the worst one either — it is missing.
    order_by = f"({expr} IS NULL), {expr} {direction.upper()}"

    clauses, params, bounds = [], [], {}
    for name, spec in cols.items():
        for bound, op in (("min", ">="), ("max", "<=")):
            key = f"{bound}_{name}"
            raw = (request.args.get(key) or "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            bounds[key] = raw
            clauses.append(f"{spec.expr} {op} ?")
            params.append(value * spec.scale)

    time_state = {}
    if time_col:
        tc, tp, time_state = _time_clauses(time_col, default_since)
        clauses += tc
        params += tp

    return order_by, clauses, params, {
        "cols": cols, "sort": col, "dir": direction, "bounds": bounds,
        "time": time_state, "has_time": bool(time_col),
        "since_choices": SINCE_CHOICES,
        "active": len(bounds) + len(time_state),
    }


def sort_url(col: str) -> str:
    """
    Link for a column header. Clicking the active column flips direction;
    clicking another starts it descending, which is the interesting end for
    every number in this dashboard.
    """
    args = request.args.to_dict()
    if args.get("sort") == col and args.get("dir", "desc") == "desc":
        args["dir"] = "asc"
    else:
        args["dir"] = "desc"
    args["sort"] = col
    args.pop("page", None)        # a new order means page 1
    return f"{request.path}?{urlencode(args)}"


app.jinja_env.globals.update(sort_url=sort_url)


# =====================================================================
# Auth routes
# =====================================================================


REFUSAL_TEXT = {
    "too_many_for_user": "تلاش‌های ناموفق زیاد برای این حساب. چند دقیقه صبر کنید.",
    "too_many_for_ip": "تلاش‌های ناموفق زیاد از این آدرس. بعداً امتحان کنید.",
    "disabled": "این حساب غیرفعال شده است.",
    "bad_credentials": "نام کاربری یا رمز عبور نادرست است.",
}


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = auth_db()
    if not dashauth.user_count(conn):
        return render_template("unconfigured.html"), 503

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user, reason = dashauth.authenticate(
            conn, username, password, ip=client_ip(),
            user_agent=request.headers.get("User-Agent", ""))

        if user is not None:
            session.clear()
            session["user"] = user["username"]
            session["display"] = user["display_name"] or user["username"]
            session.permanent = True
            nxt = request.args.get("next", "")
            # Only relative paths, so ?next= cannot bounce a logged-in user
            # to another site.
            return redirect(nxt if nxt.startswith("/") else url_for("overview"))

        flash(REFUSAL_TEXT.get(reason, REFUSAL_TEXT["bad_credentials"]))
        if reason in ("too_many_for_user", "too_many_for_ip"):
            return render_template("login.html"), 429

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =====================================================================
# Overview
# =====================================================================


# The overview trend, by time rather than by scan count.
#
# Each range carries its own bucket, because 30 days at a 15-minute scan
# interval is ~2900 points and a 400px sparkline can show perhaps sixty of
# them. Bucketing keeps every range at roughly 24-30 points, so the shape
# stays readable and the x-axis stays honest — one point is one span of
# time, not one scan, and a gap where the monitor was down reads as a gap.
TREND_RANGES = {
    "6h":  ("۶ ساعت",  6 * 3600,
            "strftime('%Y-%m-%dT%H:', found_at) || "
            "printf('%02d', (CAST(strftime('%M', found_at) AS INTEGER)/15)*15)"),
    "12h": ("۱۲ ساعت", 12 * 3600,
            "strftime('%Y-%m-%dT%H:', found_at) || "
            "printf('%02d', (CAST(strftime('%M', found_at) AS INTEGER)/30)*30)"),
    "1d":  ("۱ روز",   86400,
            "strftime('%Y-%m-%dT%H:00', found_at)"),
    "7d":  ("۷ روز",   7 * 86400,
            "strftime('%Y-%m-%dT', found_at) || "
            "printf('%02d:00', (CAST(strftime('%H', found_at) AS INTEGER)/6)*6)"),
    "30d": ("۳۰ روز",  30 * 86400,
            "strftime('%Y-%m-%d', found_at)"),
}
TREND_DEFAULT = "1d"


def _trend(range_key: str):
    """Best edge per bucket over the chosen window, oldest first."""
    label, seconds, bucket = TREND_RANGES[range_key]
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=seconds)).isoformat()
    return rows(f"""
        SELECT {bucket} AS bucket,
               MAX(net_edge) best,
               COUNT(*) n
        FROM near_misses
        WHERE found_at >= ?
        GROUP BY bucket ORDER BY bucket
    """, (cutoff,))


@app.route("/")
@login_required
def overview():
    last = one("SELECT * FROM scans ORDER BY id DESC LIMIT 1")

    today = one("""
        SELECT COUNT(*) scans,
               COALESCE(SUM(events_total), 0) events,
               COALESCE(SUM(events_scanned), 0) analysed,
               COALESCE(SUM(opportunities_found), 0) opportunities,
               COALESCE(SUM(near_misses_saved), 0) near_misses,
               COALESCE(SUM(errors), 0) errors
        FROM scans WHERE date(started_at) = date('now')
    """)

    best = one("""
        SELECT * FROM near_misses
        WHERE date(found_at) = date('now')
        ORDER BY net_edge DESC LIMIT 1
    """)

    recent_scans = rows("""
        SELECT s.*,
               (SELECT duration_ms FROM scan_timings t
                 WHERE t.scan_id = s.id AND t.phase = 'analysis') analysis_ms
        FROM scans s ORDER BY s.id DESC LIMIT 12
    """)

    top_reasons = rows("""
        SELECT code, SUM(count) n FROM rejections
        WHERE stage != 'suspicion'
          AND scan_id IN (SELECT id FROM scans ORDER BY id DESC LIMIT 20)
        GROUP BY code ORDER BY n DESC LIMIT 6
    """)

    # The trend an analyst actually watches: how close the market came.
    # Rendered as an inline sparkline, no chart library.
    trend_range = request.args.get("range", TREND_DEFAULT)
    if trend_range not in TREND_RANGES:
        trend_range = TREND_DEFAULT
    trend = _trend(trend_range)

    return render_template(
        "overview.html", last=last, today=today, best=best,
        recent_scans=recent_scans, top_reasons=top_reasons,
        trend=trend, trend_range=trend_range,
        trend_label=TREND_RANGES[trend_range][0],
        trend_ranges=TREND_RANGES,
        opportunities_total=one(
            "SELECT COUNT(*) c FROM opportunities")["c"],
        signals_total=one("SELECT COUNT(*) c FROM signals")["c"],
    )


# =====================================================================
# Opportunities
# =====================================================================


OPP_COLS = {
    "num_outcomes":  Col("num_outcomes", "گزینه", step="1"),
    "sum_best_asks": Col("sum_best_asks", "مجموع قیمت", step="0.0001"),
    "net_edge":      Col("net_edge", "لبه‌ی خالص", "percent", 0.01, "0.001"),
    "fee_rate":      Col("fee_rate", "کارمزد", "percent", 0.01, "0.1"),
    "top_capital":   Col("top_capital", "سرمایه‌ی بی‌اسلیپیج", "money",
                         step="10"),
    "top_profit":    Col("top_profit", "سود بی‌اسلیپیج", "money", step="0.5"),
    "best_capital":  Col("best_capital", "سرمایه با اسلیپیج", "money",
                         step="10"),
    "best_profit":   Col("best_profit", "سود با اسلیپیج", "money", step="1"),
    "best_roi_pct":  Col("best_roi_pct", "بازده", step="0.1"),
}


@app.route("/opportunities")
@login_required
def opportunities():
    page = max(1, request.args.get("page", 1, type=int))
    order_by, clauses, params, sortstate = sort_and_filter(
        OPP_COLS, "top_profit", time_col="found_at")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    total = one(f"SELECT COUNT(*) c FROM opportunities{where}", params)["c"]
    items = rows(f"""
        SELECT * FROM opportunities{where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))
    return render_template("opportunities.html", items=items, total=total,
                           page=page, pages=_pages(total),
                           sortstate=sortstate)


@app.route("/opportunity/<int:opp_id>")
@login_required
def opportunity_detail(opp_id):
    opp = one("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
    if opp is None:
        abort(404)
    legs = json.loads(opp["legs_detail"] or "[]")
    curve = json.loads(opp["slippage_curve"] or "[]")
    return render_template("opportunity_detail.html", opp=opp, legs=legs,
                           curve=curve,
                           suspicions=json.loads(opp["suspicions"] or "[]"))


# =====================================================================
# Near misses
# =====================================================================


MISS_COLS = {
    "num_outcomes":  Col("num_outcomes", "گزینه", step="1"),
    "sum_best_asks": Col("sum_best_asks", "مجموع قیمت", step="0.0001"),
    "gross_edge":    Col("gross_edge", "لبه‌ی ناخالص", "percent", 0.01, "0.001"),
    "net_edge":      Col("net_edge", "لبه‌ی خالص", "percent", 0.01, "0.001"),
    "fee_rate":      Col("fee_rate", "کارمزد", "percent", 0.01, "0.1"),
    "volume_24h":    Col("volume_24h", "حجم ۲۴س", "money", step="100"),
}


@app.route("/near-misses")
@login_required
def near_misses():
    page = max(1, request.args.get("page", 1, type=int))

    # 24h by default. The page used to be pinned to "today", and dropping
    # to no window at all would quietly turn a short table into the whole
    # history the first time someone opened it.
    order_by, clauses, params, sortstate = sort_and_filter(
        MISS_COLS, "net_edge", time_col="found_at", default_since="24h")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    total = one(f"SELECT COUNT(*) c FROM near_misses{where}", params)["c"]
    items = rows(f"""
        SELECT * FROM near_misses{where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))

    return render_template("near_misses.html", items=items, total=total,
                           page=page, pages=_pages(total),
                           sortstate=sortstate)


# =====================================================================
# Markets — every event the scan read
# =====================================================================


def _verdict_filters():
    """Shared WHERE clause for the market tables, from the query string."""
    clauses, params = [], []

    scan = request.args.get("scan", type=int)
    if scan:
        clauses.append("scan_id = ?")
        params.append(scan)
    else:
        clauses.append("scan_id = (SELECT MAX(scan_id) FROM event_verdicts)")

    code = request.args.get("code", "")
    if code:
        clauses.append("code = ?")
        params.append(code)

    outcome = request.args.get("outcome", "")
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)

    q = request.args.get("q", "").strip()
    if q:
        clauses.append("event_title LIKE ?")
        params.append(f"%{q}%")

    # Flagged-but-not-rejected markets had no way to be found: they carry
    # no rejection code, so filtering by reason never surfaced them, and
    # they look identical to clean ones in the table.
    suspicion = request.args.get("suspicion", "")
    if suspicion == "any":
        clauses.append("suspicions != '[]' AND suspicions IS NOT NULL")
    elif suspicion:
        clauses.append("suspicions LIKE ?")
        params.append(f'%"{suspicion}"%')

    return " WHERE " + " AND ".join(clauses), params


VERDICT_COLS = {
    "num_outcomes":  Col("num_outcomes", "گزینه", step="1"),
    "sum_best_asks": Col("sum_best_asks", "مجموع قیمت", step="0.0001"),
    "net_edge":      Col("net_edge", "لبه‌ی خالص", "percent", 0.01, "0.001"),
    "fee_rate":      Col("fee_rate", "کارمزد", "percent", 0.01, "0.1"),
    "volume_24h":    Col("volume_24h", "حجم ۲۴س", "money", step="100"),
}


@app.route("/markets")
@login_required
def markets():
    if not table_exists("event_verdicts"):
        return render_template("no_verdicts.html")

    page = max(1, request.args.get("page", 1, type=int))
    where, params = _verdict_filters()
    order_by, extra, extra_params, sortstate = sort_and_filter(
        VERDICT_COLS, "net_edge", time_col="recorded_at")
    if extra:
        where += " AND " + " AND ".join(extra)
        params = [*params, *extra_params]

    total = one(f"SELECT COUNT(*) c FROM event_verdicts {where}", params)["c"]
    items = rows(f"""
        SELECT * FROM event_verdicts {where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))

    return render_template(
        "markets.html", items=items, total=total, page=page,
        pages=_pages(total), title="بازارهای بررسی‌شده",
        suspicions=glossary.SUSPICIONS, sortstate=sortstate,
        codes=_codes_in_scope(), scans=_recent_scan_ids(),
        show_outcome_filter=True)


@app.route("/rejected")
@login_required
def rejected():
    if not table_exists("event_verdicts"):
        return render_template("no_verdicts.html")

    page = max(1, request.args.get("page", 1, type=int))
    where, params = _verdict_filters()
    where += " AND outcome = 'rejected'"
    order_by, extra, extra_params, sortstate = sort_and_filter(
        VERDICT_COLS, "volume_24h", time_col="recorded_at")
    if extra:
        where += " AND " + " AND ".join(extra)
        params = [*params, *extra_params]

    total = one(f"SELECT COUNT(*) c FROM event_verdicts {where}", params)["c"]
    items = rows(f"""
        SELECT * FROM event_verdicts {where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))

    # Reason breakdown for the scan in view, so the table has a summary
    # above it rather than only rows.
    breakdown = rows(f"""
        SELECT code, COUNT(*) n FROM event_verdicts {where}
        GROUP BY code ORDER BY n DESC
    """, params)

    return render_template(
        "markets.html", items=items, total=total, page=page,
        pages=_pages(total), title="بازارهای رد شده",
        suspicions=glossary.SUSPICIONS, sortstate=sortstate,
        codes=_codes_in_scope(rejected_only=True), scans=_recent_scan_ids(),
        breakdown=breakdown, show_outcome_filter=False)


@app.route("/market/<path:slug>")
@login_required
def market_detail(slug):
    history = rows("""
        SELECT * FROM event_verdicts WHERE event_slug = ?
        ORDER BY scan_id DESC LIMIT 200
    """, (slug,)) if table_exists("event_verdicts") else []

    if not history:
        abort(404)

    latest = history[0]
    misses = rows("""
        SELECT * FROM near_misses WHERE event_slug = ?
        ORDER BY found_at DESC LIMIT 20
    """, (slug,))
    opps = rows("""
        SELECT * FROM opportunities WHERE event_slug = ?
        ORDER BY found_at DESC LIMIT 20
    """, (slug,))

    trend = [r for r in reversed(history) if r["sum_best_asks"] is not None]

    return render_template(
        "market_detail.html", slug=slug, latest=latest, history=history,
        misses=misses, opps=opps, trend=trend,
        suspicions=json.loads(latest["suspicions"] or "[]"))


# =====================================================================
# Edge windows — short-lived episodes
# =====================================================================


WINDOW_COLS = {
    # duration is typed in minutes and stored in milliseconds — the filter
    # box must mean what the column shows
    "duration_ms":   Col("duration_ms", "طول", "duration", 60_000, "0.5"),
    "best_edge":     Col("best_edge", "بهترین لبه", "percent", 0.01, "0.001"),
    "best_sum_asks": Col("best_sum_asks", "کف قیمت", step="0.0001"),
    "best_capital":  Col("best_capital", "قابل جذب", "money", step="10"),
    "best_profit":   Col("best_profit", "سود", "money", step="0.5"),
    "ticks":         Col("ticks", "تیک", step="1"),
    "opened_at":     Col("opened_at", "باز شد", "text"),
}


@app.route("/windows")
@login_required
def windows():
    if not table_exists("edge_windows"):
        return render_template("no_windows.html")

    page = max(1, request.args.get("page", 1, type=int))
    crossed_only = request.args.get("crossed") == "1"

    order_by, extra, params, sortstate = sort_and_filter(
        WINDOW_COLS, "opened_at", time_col="opened_at")
    clauses = ["closed_at IS NOT NULL"]
    if crossed_only:
        clauses.append("crossed = 1")
    clauses += extra
    where = " WHERE " + " AND ".join(clauses)

    total = one(f"SELECT COUNT(*) c FROM edge_windows{where}", params)["c"]
    items = rows(f"""
        SELECT * FROM edge_windows{where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))

    summary = one("""
        SELECT COUNT(*) n,
               SUM(crossed) crossed,
               AVG(duration_ms) avg_ms,
               MAX(duration_ms) max_ms,
               MAX(best_edge) best
        FROM edge_windows WHERE closed_at IS NOT NULL
    """)
    live = one("SELECT COUNT(*) c FROM edge_windows "
               "WHERE closed_at IS NULL")["c"]

    # How long these episodes last, in buckets an analyst can act on: a
    # window under a minute is unreachable by hand, one over ten is a
    # different kind of opportunity entirely.
    buckets = rows("""
        SELECT CASE
                 WHEN duration_ms <   60000 THEN 'زیر ۱ دقیقه'
                 WHEN duration_ms <  300000 THEN '۱ تا ۵ دقیقه'
                 WHEN duration_ms <  600000 THEN '۵ تا ۱۰ دقیقه'
                 WHEN duration_ms < 1800000 THEN '۱۰ تا ۳۰ دقیقه'
                 ELSE 'بیش از ۳۰ دقیقه'
               END bucket,
               COUNT(*) n, SUM(crossed) crossed
        FROM edge_windows WHERE closed_at IS NOT NULL
        GROUP BY bucket ORDER BY MIN(duration_ms)
    """)

    # Time of day, so "when do these happen" is answerable. Stored UTC.
    by_hour = rows("""
        SELECT CAST(strftime('%H', opened_at) AS INTEGER) hour, COUNT(*) n
        FROM edge_windows WHERE closed_at IS NOT NULL
        GROUP BY hour ORDER BY hour
    """)

    return render_template(
        "windows.html", items=items, total=total, page=page,
        pages=_pages(total), summary=summary, live=live, buckets=buckets,
        by_hour={r["hour"]: r["n"] for r in by_hour},
        crossed_only=crossed_only, sortstate=sortstate)


@app.route("/window/<int:window_id>")
@login_required
def window_detail(window_id):
    win = one("SELECT * FROM edge_windows WHERE id = ?", (window_id,))
    if win is None:
        abort(404)

    ticks = rows("SELECT * FROM edge_ticks WHERE window_id = ? ORDER BY ts_ms",
                 (window_id,))
    others = rows("""
        SELECT * FROM edge_windows
        WHERE event_slug = ? AND id != ? AND closed_at IS NOT NULL
        ORDER BY opened_at DESC LIMIT 15
    """, (win["event_slug"], window_id))

    return render_template("window_detail.html", win=win, ticks=ticks,
                           others=others)


# =====================================================================
# Edge distribution — where the threshold actually sits
# =====================================================================


# Candidate thresholds, coarsest first. Shown against the real
# distribution so "we found nothing" becomes a statement about where the
# line is drawn rather than about whether the pipeline works.
THRESHOLDS = [0.010, 0.005, 0.003, 0.002, 0.001, 0.0005, 0.0]


@app.route("/distribution")
@login_required
def distribution():
    if not table_exists("event_verdicts"):
        return render_template("no_verdicts.html")

    scans = request.args.get("scans", 20, type=int)
    scope = ("scan_id IN (SELECT id FROM scans ORDER BY id DESC LIMIT ?)",
             [scans])

    edges = [r["net_edge"] for r in rows(
        f"SELECT net_edge FROM event_verdicts "
        f"WHERE net_edge IS NOT NULL AND {scope[0]} ORDER BY net_edge",
        scope[1])]

    sensitivity = [
        {"threshold": t, "n": sum(1 for e in edges if e >= t)}
        for t in THRESHOLDS
    ]

    # 30 equal buckets over the observed range; the interesting mass is
    # always near zero, so the axis is left as-is rather than log-scaled.
    hist = []
    if edges:
        lo, hi = edges[0], edges[-1]
        span = (hi - lo) or 1e-9
        nbuckets = 30
        counts = [0] * nbuckets
        for e in edges:
            idx = min(int((e - lo) / span * nbuckets), nbuckets - 1)
            counts[idx] += 1
        hist = [{"lo": lo + i * span / nbuckets,
                 "hi": lo + (i + 1) * span / nbuckets,
                 "n": c} for i, c in enumerate(counts)]

    windows_best = None
    if table_exists("edge_windows"):
        windows_best = one("SELECT MAX(best_edge) b FROM edge_windows")["b"]

    return render_template(
        "distribution.html", edges=edges, hist=hist,
        sensitivity=sensitivity, scans=scans,
        current=config.MIN_NET_EDGE,
        near_miss_floor=config.NEAR_MISS_MIN_NET,
        windows_best=windows_best,
        best=edges[-1] if edges else None,
        median=edges[len(edges) // 2] if edges else None)


# =====================================================================
# Paper wallet
# =====================================================================


@app.route("/paper")
@login_required
def paper_runs():
    if not table_exists("paper_runs"):
        return render_template("no_paper.html")

    runs = rows("""
        SELECT * FROM paper_runs ORDER BY id DESC LIMIT 40
    """)
    return render_template("paper.html", runs=runs)


@app.route("/paper/<int:run_id>")
@login_required
def paper_run(run_id):
    run = one("SELECT * FROM paper_runs WHERE id = ?", (run_id,))
    if run is None:
        abort(404)

    page = max(1, request.args.get("page", 1, type=int))
    taken = request.args.get("taken", "1")
    where = " WHERE run_id = ?"
    params = [run_id]
    if taken in ("0", "1"):
        where += " AND taken = ?"
        params.append(int(taken))

    order_by, extra, extra_params, sortstate = sort_and_filter(
        PAPER_COLS, "profit")
    if extra:
        where += " AND " + " AND ".join(extra)
        params = [*params, *extra_params]

    total = one(f"SELECT COUNT(*) c FROM paper_decisions{where}",
                params)["c"]
    items = rows(f"""
        SELECT * FROM paper_decisions{where}
        ORDER BY {order_by} LIMIT ? OFFSET ?
    """, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))

    reasons = rows("""
        SELECT reason, COUNT(*) n,
               COALESCE(SUM(capital), 0) capital,
               COALESCE(SUM(profit), 0) profit
        FROM paper_decisions WHERE run_id = ?
        GROUP BY reason ORDER BY n DESC
    """, (run_id,))

    return render_template(
        "paper_run.html", run=run, items=items, total=total, page=page,
        pages=_pages(total), reasons=reasons, taken=taken,
        sortstate=sortstate, params=fromjson(run["params"]) or {},
        labels=_paper_labels())


@app.route("/paper/<int:run_id>/ledger")
@login_required
def paper_ledger(run_id):
    run = one("SELECT * FROM paper_runs WHERE id = ?", (run_id,))
    if run is None:
        abort(404)

    page = max(1, request.args.get("page", 1, type=int))
    total = one("SELECT COUNT(*) c FROM paper_ledger WHERE run_id = ?",
                (run_id,))["c"]
    items = rows("""
        SELECT * FROM paper_ledger WHERE run_id = ?
        ORDER BY seq LIMIT ? OFFSET ?
    """, (run_id, PAGE_SIZE, (page - 1) * PAGE_SIZE))

    # The equity curve, thinned to something a small chart can draw. Every
    # movement is in the table below; this is the shape of them.
    curve = rows("""
        SELECT seq, at, equity_after, balance_after, kind
        FROM paper_ledger WHERE run_id = ? ORDER BY seq
    """, (run_id,))
    if len(curve) > 120:
        step = len(curve) // 120 + 1
        curve = curve[::step] + [curve[-1]]

    return render_template("paper_ledger.html", run=run, items=items,
                           total=total, page=page, pages=_pages(total),
                           curve=curve)


PAPER_COLS = {
    "window_ms":     Col("window_ms", "طول پنجره", "duration", 1000, "1"),
    "entry_ms":      Col("entry_ms", "تأخیر ورود", "duration", 1000, "0.5"),
    "best_edge":     Col("best_edge", "بهترین لبه", "percent", 0.01, "0.001"),
    "entry_edge":    Col("entry_edge", "لبه‌ی ورود", "percent", 0.01, "0.001"),
    "capital":       Col("capital", "سرمایه", "money", step="10"),
    "fee":           Col("fee", "کارمزد", "money", step="0.5"),
    "profit":        Col("profit", "سود", "money", step="0.5"),
    "fillable_capital": Col("fillable_capital", "عمق", "money", step="10"),
}


def _paper_labels():
    import paper
    return paper.SKIP_LABELS


# =====================================================================
# Funnel
# =====================================================================


@app.route("/funnel")
@login_required
def funnel():
    scans = request.args.get("scans", 20, type=int)
    scope = ("scan_id IN (SELECT id FROM scans ORDER BY id DESC LIMIT ?)",
             (scans,))

    fetched = one(f"""
        SELECT COALESCE(SUM(events_total), 0) n FROM scans
        WHERE id IN (SELECT id FROM scans ORDER BY id DESC LIMIT ?)
    """, (scans,))["n"]

    stages = defaultdict(list)
    for r in rows(f"""
        SELECT stage, code, SUM(count) n FROM rejections
        WHERE {scope[0]} AND stage != 'suspicion'
        GROUP BY stage, code ORDER BY n DESC
    """, scope[1]):
        stages[r["stage"]].append(r)

    suspicions = rows(f"""
        SELECT code, SUM(count) n FROM rejections
        WHERE {scope[0]} AND stage = 'suspicion'
        GROUP BY code ORDER BY n DESC
    """, scope[1])

    timings = rows(f"""
        SELECT phase, AVG(duration_ms) avg_ms, MAX(duration_ms) max_ms
        FROM scan_timings WHERE {scope[0]}
        GROUP BY phase ORDER BY avg_ms DESC
    """, scope[1])

    return render_template(
        "funnel.html", fetched=fetched, stages=stages,
        suspicions=suspicions, timings=timings, scans=scans,
        order=["prefilter", "book", "basket", "edge"])


@app.route("/fees")
@login_required
def fees_view():
    """
    Which fee categories actually produce edges.

    The rate runs from 0% on geopolitics to 7% on crypto, so the same
    gross edge is a trade in one category and a loss in another. Without
    this, an analyst has to hold the fee table in their head while reading
    every other page.
    """
    scans = request.args.get("scans", 20, type=int)

    if not table_exists("event_verdicts"):
        return render_template("no_verdicts.html")

    by_fee = rows("""
        SELECT fee_rate,
               COUNT(*) analysed,
               SUM(outcome = 'near_miss') near_misses,
               SUM(outcome = 'opportunity') opportunities,
               AVG(net_edge) avg_edge,
               MAX(net_edge) best_edge,
               AVG(gross_edge) avg_gross
        FROM event_verdicts
        WHERE fee_rate IS NOT NULL
          AND scan_id IN (SELECT id FROM scans ORDER BY id DESC LIMIT ?)
        GROUP BY fee_rate ORDER BY fee_rate
    """, (scans,))

    by_category = rows("""
        SELECT COALESCE(fee_category, category, 'نامشخص') cat,
               fee_rate, COUNT(*) n, MAX(net_edge) best_edge
        FROM event_verdicts
        WHERE scan_id IN (SELECT id FROM scans ORDER BY id DESC LIMIT ?)
        GROUP BY cat, fee_rate ORDER BY n DESC LIMIT 25
    """, (scans,))

    return render_template("fees.html", by_fee=by_fee,
                           by_category=by_category, scans=scans)


# =====================================================================
# System
# =====================================================================


@app.route("/system")
@login_required
def system():
    unit = request.args.get("unit", "polly-monitor")
    if unit not in UNITS:
        abort(400)

    lines = request.args.get("lines", 200, type=int)
    lines = max(20, min(lines, 2000))

    return render_template("system.html", unit=unit, lines=lines,
                           status=_unit_status(unit),
                           logs=_unit_logs(unit, lines),
                           db_size=_db_size(),
                           verdict_rows=_verdict_count())


@app.route("/glossary")
@login_required
def glossary_page():
    return render_template("glossary.html", terms=glossary.TERMS,
                           reasons=glossary.REASONS,
                           suspicions=glossary.SUSPICIONS,
                           stages=glossary.STAGES)


# =====================================================================
# Small helpers
# =====================================================================


def _pages(total):
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def _recent_scan_ids():
    return [r["id"] for r in
            rows("SELECT id FROM scans ORDER BY id DESC LIMIT 30")]


def _codes_in_scope(rejected_only=False):
    extra = " WHERE outcome = 'rejected'" if rejected_only else ""
    return rows(f"""
        SELECT code, COUNT(*) n FROM event_verdicts{extra}
        GROUP BY code ORDER BY n DESC
    """)


def _run(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout or out.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return f"({type(e).__name__}: {e})"


def _unit_status(unit):
    raw = _run(["systemctl", "show", unit, "--no-pager",
                "--property=ActiveState,SubState,ExecMainStartTimestamp,"
                "MemoryCurrent,NRestarts"])
    out = {}
    for line in raw.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def _unit_logs(unit, lines):
    return _run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager",
                 "--output=short-iso"], timeout=10)


def _db_size():
    try:
        total = DB_PATH.stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
            if sidecar.exists():
                total += sidecar.stat().st_size
        return total
    except OSError:
        return None


def _verdict_count():
    if not table_exists("event_verdicts"):
        return None
    return one("SELECT COUNT(*) c FROM event_verdicts")["c"]


# =====================================================================
# CLI
# =====================================================================


def _prompt_password() -> str:
    pw = getpass.getpass("رمز عبور: ")
    if len(pw) < 10:
        raise SystemExit("رمز باید دست‌کم ۱۰ کاراکتر باشد.")
    if pw != getpass.getpass("تکرار رمز عبور: "):
        raise SystemExit("رمزها یکسان نیستند.")
    return pw


def main():
    parser = argparse.ArgumentParser(description="Polymarket arb dashboard")
    parser.add_argument("--add-user", metavar="USERNAME",
                        help="create an analyst account")
    parser.add_argument("--name", metavar="DISPLAY_NAME",
                        help="full name, shown in the login record")
    parser.add_argument("--reset-password", metavar="USERNAME")
    parser.add_argument("--disable-user", metavar="USERNAME")
    parser.add_argument("--enable-user", metavar="USERNAME")
    parser.add_argument("--list-users", action="store_true")
    parser.add_argument("--auth-db", metavar="PATH",
                        help="account database (default: beside the market "
                             "database named by DB_PATH)")
    parser.add_argument("--logins", type=int, nargs="?", const=30,
                        metavar="N", help="print the last N login attempts")
    parser.add_argument("--hash-password", action="store_true",
                        help="print a hash for polly.env (pre-accounts style)")
    parser.add_argument("--host", default=DASH_HOST)
    parser.add_argument("--port", type=int, default=DASH_PORT)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.hash_password:
        pw = _prompt_password()
        print("\nاین دو خط را در /etc/polly/polly.env بگذارید:\n")
        print(f"POLLY_DASH_PASSWORD_HASH={hash_password(pw)}")
        print(f"POLLY_DASH_SECRET_KEY={secrets.token_hex(32)}")
        return

    account_flags = (args.add_user or args.reset_password or
                     args.disable_user or args.enable_user or
                     args.list_users or args.logins is not None)

    if account_flags:
        auth_path = Path(args.auth_db) if args.auth_db else AUTH_DB_PATH
        # Printed every time: accounts written to one file while the
        # service reads another is a silent failure, and the path is the
        # only thing that makes it visible.
        print(f"[dashboard] حساب‌ها در: {auth_path}")
        _check_auth_db_usable(auth_path)
        conn = dashauth.connect(auth_path)

        if args.add_user:
            if dashauth.get_user(conn, args.add_user):
                raise SystemExit(f"حساب {args.add_user!r} از قبل وجود دارد.")
            dashauth.add_user(conn, args.add_user, _prompt_password(),
                              args.name)
            print(f"✓ حساب {args.add_user!r} ساخته شد.")

        elif args.reset_password:
            if not dashauth.set_password(conn, args.reset_password,
                                         _prompt_password()):
                raise SystemExit(f"حساب {args.reset_password!r} پیدا نشد.")
            print(f"✓ رمز {args.reset_password!r} عوض شد.")

        elif args.disable_user or args.enable_user:
            name = args.disable_user or args.enable_user
            if not dashauth.set_disabled(conn, name, bool(args.disable_user)):
                raise SystemExit(f"حساب {name!r} پیدا نشد.")
            print(f"✓ {name!r} {'غیرفعال' if args.disable_user else 'فعال'} شد.")

        elif args.list_users:
            users = dashauth.list_users(conn)
            if not users:
                print("هیچ حسابی ساخته نشده. با --add-user بسازید.")
            print(f"{'کاربر':<18} {'نام':<24} {'وضعیت':<10} "
                  f"{'ورودها':>7}  آخرین ورود")
            for u in users:
                state = "غیرفعال" if u["disabled_at"] else "فعال"
                print(f"{u['username']:<18} {(u['display_name'] or '—'):<24} "
                      f"{state:<10} {u['logins']:>7}  "
                      f"{u['last_login_at'] or '—'}")

        elif args.logins is not None:
            for r in dashauth.recent_logins(conn, args.logins):
                mark = "✓" if r["success"] else "✗"
                print(f"{mark} {r['at']}  {(r['username'] or '?'):<16} "
                      f"{(r['ip'] or '?'):<18} {r['reason'] or ''}")

        conn.close()
        return

    if not SECRET_KEY:
        print("[dashboard] WARNING: POLLY_DASH_SECRET_KEY is unset; a random "
              "key was generated, so sessions will not survive a restart.")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
