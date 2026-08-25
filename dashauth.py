"""
Dashboard accounts and the login record.
========================================

Kept in its own SQLite file rather than in arb_monitor.db. Credentials and
market data have nothing to do with each other, and separating them means
the market database can be copied to a laptop for analysis, or restored
from a backup, without carrying password hashes along with it.

A login record is only worth keeping if it can name a person, so each
analyst gets an account. With one shared login every row would say the
same thing and answer nothing.

Failures are counted per (username, IP) rather than per IP alone. With
several analysts behind one office address, a per-IP lockout means one
person mistyping their password locks out the whole team; with a per-user
lockout, anyone who knows a colleague's username can lock them out on
purpose. The pair narrows both.

    python dashboard.py --add-user sara
    python dashboard.py --list-users
    python dashboard.py --disable-user sara
"""

import hashlib
import hmac
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS dash_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled_at TEXT,                   -- NULL while the account is usable
    last_login_at TEXT,
    last_login_ip TEXT
);

-- Every attempt, successful or not. Written before the session is handed
-- out, so a failure is recorded even when the response is a refusal.
CREATE TABLE IF NOT EXISTS dash_logins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    username TEXT,                      -- as typed, even if no such user
    ip TEXT,
    user_agent TEXT,
    success INTEGER NOT NULL,
    reason TEXT                         -- why it was refused
);
CREATE INDEX IF NOT EXISTS idx_login_at ON dash_logins(at);
CREATE INDEX IF NOT EXISTS idx_login_user ON dash_logins(username, at);
CREATE INDEX IF NOT EXISTS idx_login_ok ON dash_logins(success, at);
"""

# scrypt parameters. Stored as scrypt$<salt>$<key> so they can change later
# without invalidating hashes already written.
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)

LOCKOUT_ATTEMPTS = 8            # per (username, ip)
LOCKOUT_SECONDS = 300
IP_ATTEMPTS = 40                # per ip, whatever username was tried
IP_SECONDS = 900


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# Passwords
# =====================================================================


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, key_hex = (stored or "").split("$")
        if algo != "scrypt":
            return False
        key = hashlib.scrypt(password.encode("utf-8"),
                             salt=bytes.fromhex(salt_hex), **SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), key_hex)


# =====================================================================
# Database
# =====================================================================


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    db.commit()
    return db


# =====================================================================
# Accounts
# =====================================================================


def add_user(db, username: str, password: str, display_name: str = None):
    db.execute("""
        INSERT INTO dash_users (username, display_name, password_hash,
                                created_at)
        VALUES (?, ?, ?, ?)
    """, (username, display_name, hash_password(password), utcnow()))
    db.commit()


def set_password(db, username: str, password: str) -> bool:
    cur = db.execute("UPDATE dash_users SET password_hash = ? "
                     "WHERE username = ?", (hash_password(password), username))
    db.commit()
    return cur.rowcount > 0


def set_disabled(db, username: str, disabled: bool) -> bool:
    cur = db.execute("UPDATE dash_users SET disabled_at = ? WHERE username = ?",
                     (utcnow() if disabled else None, username))
    db.commit()
    return cur.rowcount > 0


def get_user(db, username: str) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM dash_users WHERE username = ?",
                      (username,)).fetchone()


def list_users(db) -> list:
    return db.execute("""
        SELECT u.*,
               (SELECT COUNT(*) FROM dash_logins l
                 WHERE l.username = u.username AND l.success = 1) logins
        FROM dash_users u ORDER BY username
    """).fetchall()


def user_count(db) -> int:
    return db.execute("SELECT COUNT(*) c FROM dash_users").fetchone()["c"]


# =====================================================================
# Login record and rate limiting
# =====================================================================


def record_login(db, *, username: str, ip: str, user_agent: str,
                 success: bool, reason: str = None):
    db.execute("""
        INSERT INTO dash_logins (at, username, ip, user_agent, success, reason)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (utcnow(), username, ip, (user_agent or "")[:400],
          1 if success else 0, reason))
    if success:
        db.execute("UPDATE dash_users SET last_login_at = ?, last_login_ip = ?"
                   " WHERE username = ?", (utcnow(), ip, username))
    db.commit()


def _recent_failures(db, since_seconds: int, *, username=None, ip=None) -> int:
    cutoff = datetime.fromtimestamp(time.time() - since_seconds, timezone.utc)
    sql = "SELECT COUNT(*) c FROM dash_logins WHERE success = 0 AND at >= ?"
    params = [cutoff.isoformat()]
    if username is not None:
        sql += " AND username = ?"
        params.append(username)
    if ip is not None:
        sql += " AND ip = ?"
        params.append(ip)
    return db.execute(sql, params).fetchone()["c"]


def lockout_reason(db, username: str, ip: str) -> Optional[str]:
    """
    Why this attempt must be refused before the password is even checked,
    or None if it may proceed.
    """
    if _recent_failures(db, LOCKOUT_SECONDS,
                        username=username, ip=ip) >= LOCKOUT_ATTEMPTS:
        return "too_many_for_user"
    if _recent_failures(db, IP_SECONDS, ip=ip) >= IP_ATTEMPTS:
        return "too_many_for_ip"
    return None


def authenticate(db, username: str, password: str, *, ip: str,
                 user_agent: str) -> tuple:
    """
    Returns (user_row_or_None, reason). Every path writes a login row.
    """
    locked = lockout_reason(db, username, ip)
    if locked:
        record_login(db, username=username, ip=ip, user_agent=user_agent,
                     success=False, reason=locked)
        return None, locked

    user = get_user(db, username)

    # The password check runs even when the user does not exist, against a
    # throwaway hash, so a wrong username and a wrong password take the
    # same time and neither can be identified by how long the answer took.
    stored = user["password_hash"] if user else hash_password(secrets.token_hex(8))
    ok = verify_password(password, stored)

    if user is None or not ok:
        record_login(db, username=username, ip=ip, user_agent=user_agent,
                     success=False, reason="bad_credentials")
        return None, "bad_credentials"

    if user["disabled_at"]:
        record_login(db, username=username, ip=ip, user_agent=user_agent,
                     success=False, reason="disabled")
        return None, "disabled"

    record_login(db, username=username, ip=ip, user_agent=user_agent,
                 success=True)
    return user, None


def recent_logins(db, limit: int = 200) -> list:
    return db.execute("SELECT * FROM dash_logins ORDER BY at DESC LIMIT ?",
                      (limit,)).fetchall()
