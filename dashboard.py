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
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)

import config
import glossary

# =====================================================================
# Settings
# =====================================================================

DB_PATH = Path(config.DB_PATH) if config.DB_PATH else (
    Path(__file__).parent / "arb_monitor.db")

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

# Failed logins per IP. In memory on purpose: a restart clearing the
# counters is fine, and it keeps the dashboard free of another table.
_failures = defaultdict(list)
LOCKOUT_ATTEMPTS = 8
LOCKOUT_SECONDS = 300

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)


# =====================================================================
# Passwords
# =====================================================================
# scrypt from the standard library rather than a dependency. The stored
# form is  scrypt$<salt>$<key>  so the parameters can change later without
# invalidating what is already in the environment file.

SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, key_hex = stored.split("$")
        if algo != "scrypt":
            return False
        key = hashlib.scrypt(password.encode("utf-8"),
                             salt=bytes.fromhex(salt_hex), **SCRYPT)
    except (ValueError, TypeError):
        return False
    # compare_digest, not ==, so a wrong password cannot be narrowed down
    # by timing how long the comparison took.
    return hmac.compare_digest(key.hex(), key_hex)


def locked_out(ip: str) -> int:
    """Seconds remaining on this IP's lockout, 0 if it may try again."""
    now = time.time()
    recent = [t for t in _failures[ip] if now - t < LOCKOUT_SECONDS]
    _failures[ip] = recent
    if len(recent) < LOCKOUT_ATTEMPTS:
        return 0
    return int(LOCKOUT_SECONDS - (now - recent[0]))


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
    conn = g.pop("db", None)
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
    reason_label=glossary.reason_label,
    stage_label=glossary.stage_label,
    outcome_label=glossary.outcome_label,
)
app.jinja_env.globals.update(reason=glossary.reason, stage_note=stage_note,
                             UNITS=UNITS)


# =====================================================================
# Auth routes
# =====================================================================


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASH_USER or not DASH_PASSWORD_HASH:
        return render_template("unconfigured.html"), 503

    ip = request.headers.get("X-Forwarded-For",
                             request.remote_addr or "?").split(",")[0].strip()

    if request.method == "POST":
        wait = locked_out(ip)
        if wait:
            flash(f"تلاش‌های ناموفق زیاد. {wait} ثانیه دیگر دوباره امتحان کنید.")
            return render_template("login.html"), 429

        user = request.form.get("username", "")
        password = request.form.get("password", "")
        # Both checks always run: returning early on an unknown username
        # would make usernames discoverable by response time.
        user_ok = hmac.compare_digest(user, DASH_USER)
        pass_ok = verify_password(password, DASH_PASSWORD_HASH)

        if user_ok and pass_ok:
            _failures.pop(ip, None)
            session.clear()
            session["user"] = user
            session.permanent = True
            nxt = request.args.get("next", "")
            # Only relative paths, so ?next= cannot bounce a logged-in user
            # to another site.
            return redirect(nxt if nxt.startswith("/") else url_for("overview"))

        _failures[ip].append(time.time())
        flash("نام کاربری یا رمز عبور نادرست است.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =====================================================================
# Overview
# =====================================================================


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

    # The trend an analyst actually watches: how close the market came,
    # scan by scan. Rendered as an inline sparkline, no chart library.
    trend = rows("""
        SELECT scan_id, MAX(net_edge) best FROM near_misses
        GROUP BY scan_id ORDER BY scan_id DESC LIMIT 40
    """)

    return render_template(
        "overview.html", last=last, today=today, best=best,
        recent_scans=recent_scans, top_reasons=top_reasons,
        trend=list(reversed(trend)),
        opportunities_total=one(
            "SELECT COUNT(*) c FROM opportunities")["c"],
        signals_total=one("SELECT COUNT(*) c FROM signals")["c"],
    )


# =====================================================================
# Opportunities
# =====================================================================


@app.route("/opportunities")
@login_required
def opportunities():
    page = max(1, request.args.get("page", 1, type=int))
    total = one("SELECT COUNT(*) c FROM opportunities")["c"]
    items = rows("""
        SELECT * FROM opportunities
        ORDER BY found_at DESC, net_edge DESC LIMIT ? OFFSET ?
    """, (PAGE_SIZE, (page - 1) * PAGE_SIZE))
    return render_template("opportunities.html", items=items, total=total,
                           page=page, pages=_pages(total))


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


@app.route("/near-misses")
@login_required
def near_misses():
    page = max(1, request.args.get("page", 1, type=int))
    scope = request.args.get("scope", "today")

    where = "WHERE date(found_at) = date('now')" if scope == "today" else ""
    total = one(f"SELECT COUNT(*) c FROM near_misses {where}")["c"]
    items = rows(f"""
        SELECT * FROM near_misses {where}
        ORDER BY net_edge DESC LIMIT ? OFFSET ?
    """, (PAGE_SIZE, (page - 1) * PAGE_SIZE))

    return render_template("near_misses.html", items=items, total=total,
                           page=page, pages=_pages(total), scope=scope)


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

    return " WHERE " + " AND ".join(clauses), params


@app.route("/markets")
@login_required
def markets():
    if not table_exists("event_verdicts"):
        return render_template("no_verdicts.html")

    page = max(1, request.args.get("page", 1, type=int))
    where, params = _verdict_filters()

    total = one(f"SELECT COUNT(*) c FROM event_verdicts {where}", params)["c"]
    items = rows(f"""
        SELECT * FROM event_verdicts {where}
        ORDER BY (net_edge IS NULL), net_edge DESC, volume_24h DESC
        LIMIT ? OFFSET ?
    """, (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))

    return render_template(
        "markets.html", items=items, total=total, page=page,
        pages=_pages(total), title="بازارهای بررسی‌شده",
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

    total = one(f"SELECT COUNT(*) c FROM event_verdicts {where}", params)["c"]
    items = rows(f"""
        SELECT * FROM event_verdicts {where}
        ORDER BY volume_24h DESC LIMIT ? OFFSET ?
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
    return render_template("glossary.html", reasons=glossary.REASONS,
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


def main():
    parser = argparse.ArgumentParser(description="Polymarket arb dashboard")
    parser.add_argument("--hash-password", action="store_true",
                        help="prompt for a password and print its hash")
    parser.add_argument("--host", default=DASH_HOST)
    parser.add_argument("--port", type=int, default=DASH_PORT)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.hash_password:
        pw = getpass.getpass("رمز عبور: ")
        again = getpass.getpass("تکرار رمز عبور: ")
        if pw != again:
            raise SystemExit("رمزها یکسان نیستند.")
        if len(pw) < 10:
            raise SystemExit("رمز باید دست‌کم ۱۰ کاراکتر باشد.")
        print("\nاین دو خط را در /etc/polly/polly.env بگذارید:\n")
        print(f"POLLY_DASH_PASSWORD_HASH={hash_password(pw)}")
        print(f"POLLY_DASH_SECRET_KEY={secrets.token_hex(32)}")
        return

    if not DASH_USER or not DASH_PASSWORD_HASH:
        print("[dashboard] WARNING: POLLY_DASH_USER / "
              "POLLY_DASH_PASSWORD_HASH are unset; login is disabled and "
              "every page will refuse. Run --hash-password first.")
    if not SECRET_KEY:
        print("[dashboard] WARNING: POLLY_DASH_SECRET_KEY is unset; a random "
              "key was generated, so sessions will not survive a restart.")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
