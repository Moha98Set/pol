"""
Configuration — every tunable number in the system, in one place.
=================================================================

Before this file, thresholds were module constants scattered across
scanner.py, findmarket.py, live_engine.py and executor.py. Two problems
came from that: the same idea had different values in different files, and
changing anything meant editing code — which means a diff, which means the
change is indistinguishable from a bug fix in the history.

Every value here can be overridden from the environment, so the code is
never edited to run an experiment:

    MIN_NET_EDGE=0.01 MIN_VOLUME_24H=5000 python arb_monitor.py

Or pick a whole profile:

    PROFILE=conservative python arb_monitor.py

Profiles exist because these numbers are not independent. "Conservative"
is not one threshold moved, it is a coherent set: higher edge floor, more
liquidity required, smaller size, shorter capital lock. Naming the set
makes the intent reviewable.

Print the resolved values with:

    python config.py
"""

import os
from typing import Any, Dict


# =====================================================================
# Environment parsing
# =====================================================================


def _env(name: str, default, cast=float):
    """Read `name` from the environment, falling back to `default`."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        if cast is bool:
            return raw.strip().lower() in ("1", "yes", "true", "on")
        return cast(raw)
    except (TypeError, ValueError):
        # A typo in an env var must not silently half-apply. Say so and use
        # the default rather than crashing a long-running monitor.
        print(f"[config] WARNING: {name}={raw!r} is not a valid "
              f"{cast.__name__}; using default {default!r}")
        return default


def _env_list(name: str, default, cast=float):
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return [cast(part) for part in raw.replace(" ", "").split(",") if part]
    except ValueError:
        print(f"[config] WARNING: {name}={raw!r} is not a valid list; "
              f"using default")
        return default


# =====================================================================
# Profiles
# =====================================================================
# Only the values that actually differ are listed; everything else falls
# through to the module defaults below.

PROFILES: Dict[str, Dict[str, Any]] = {
    "default": {},

    # Only near-certain, liquid, quickly-resolving trades. This is the one
    # to run when real money is involved and you are still building trust
    # in the pipeline.
    "conservative": {
        "MIN_VOLUME_24H": 10_000,
        "MIN_NET_EDGE": 0.010,
        "MIN_NET_EDGE_TO_EXECUTE": 0.015,
        "MAX_DAYS_TO_RES": 90,
        "MAX_CAPITAL_PER_TRADE": 50.0,
        "MAX_TRADES_PER_DAY": 5,
        "LIVE_TOP_N": 25,
    },

    # Catch everything, execute nothing. For research runs whose output is
    # the near_misses and signals tables rather than trades.
    "research": {
        "MIN_VOLUME_24H": 200,
        "MIN_NET_EDGE": 0.0005,
        "NEAR_MISS_MIN_NET": -0.15,
        "LIVE_MIN_EDGE": 0.0005,
        "LIVE_TOP_N": 120,
        "NEAR_MISSES_PER_SCAN": 100,
    },

    # Fast and small — for checking the plumbing works end to end without
    # waiting fifteen minutes to find out.
    "smoke": {
        "SCAN_INTERVAL": 60,
        "MAX_EVENTS": 40,
        "MAX_EVENTS_SCAN": 100,     # one page — a scan in seconds, not minutes
        "LIVE_TOP_N": 5,
        "TEST_CAPITALS": [10, 100],
    },
}

PROFILE = os.getenv("PROFILE", "default").lower()
if PROFILE not in PROFILES:
    print(f"[config] WARNING: unknown PROFILE={PROFILE!r}; "
          f"known: {', '.join(PROFILES)}. Using 'default'.")
    PROFILE = "default"

_profile = PROFILES[PROFILE]


def _value(name: str, default, cast=float):
    """
    Resolution order, most specific first:
        environment variable  >  active profile  >  built-in default
    """
    if os.getenv(name) is not None:
        return _env(name, _profile.get(name, default), cast)
    return _profile.get(name, default)


def _value_list(name: str, default, cast=float):
    if os.getenv(name) is not None:
        return _env_list(name, _profile.get(name, default), cast)
    return _profile.get(name, default)


# =====================================================================
# API
# =====================================================================

GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_URL = os.getenv("CLOB_URL", "https://clob.polymarket.com")
WS_URL = os.getenv(
    "WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")

REQUEST_TIMEOUT = _value("REQUEST_TIMEOUT", 15.0)
API_SLEEP = _value("API_SLEEP", 0.04)
EVENTS_PAGE_SIZE = _value("EVENTS_PAGE_SIZE", 100, int)
API_RETRIES = _value("API_RETRIES", 3, int)


# =====================================================================
# Filters — which events are worth analysing at all
# =====================================================================

MIN_VOLUME_24H = _value("MIN_VOLUME_24H", 1000.0)
MIN_DAYS_TO_RES = _value("MIN_DAYS_TO_RES", 0.02)     # ~30 minutes
MAX_DAYS_TO_RES = _value("MAX_DAYS_TO_RES", 365.0)

# Below SUM_ASKS_MIN the legs are almost certainly not mutually exclusive,
# whatever the API says — a "free" basket is a modelling error, not a gift.
SUM_ASKS_MIN = _value("SUM_ASKS_MIN", 0.5)
SUM_ASKS_MAX = _value("SUM_ASKS_MAX", 1.0)

MAX_EVENTS = _value("MAX_EVENTS", 500, int)           # findmarket only

# Cap on how many events the monitor fetches per scan. 0 means no cap,
# which is the right default: the monitor's job is completeness. Because
# events arrive ordered by volume, a cap keeps the most liquid slice —
# so a capped run is a fast run, not a biased one.
MAX_EVENTS_SCAN = _value("MAX_EVENTS_SCAN", 0, int)


# =====================================================================
# Edge thresholds
# =====================================================================

MIN_NET_EDGE = _value("MIN_NET_EDGE", 0.003)          # store as opportunity

# Also check the mirror trade on multi-outcome events: buy NO on every leg
# (payout N-1) instead of YES on every leg (payout 1). Free in API calls —
# the NO book is the YES book's bid side — but it doubles the analysis
# work per event, so it can be turned off.
SCAN_NO_SIDE = _env("SCAN_NO_SIDE", True, bool)
NEAR_MISS_MIN_NET = _value("NEAR_MISS_MIN_NET", -0.05)
MIN_FILLABLE = _value("MIN_FILLABLE", 50.0)           # findmarket only

TEST_CAPITALS = _value_list("TEST_CAPITALS", [10, 50, 100, 500, 1000, 5000])


# =====================================================================
# Periodic scanner
# =====================================================================

SCAN_INTERVAL = _value("SCAN_INTERVAL", 900, int)     # seconds
NEAR_MISSES_PER_SCAN = _value("NEAR_MISSES_PER_SCAN", 20, int)


# =====================================================================
# Live engine
# =====================================================================

LIVE_TOP_N = _value("LIVE_TOP_N", 40, int)
LIVE_MIN_EDGE = _value("LIVE_MIN_EDGE", 0.003)
WATCHLIST_REFRESH = _value("WATCHLIST_REFRESH", 1800, int)
MAX_TOKENS_PER_SOCKET = _value("MAX_TOKENS_PER_SOCKET", 400, int)
PING_INTERVAL = _value("PING_INTERVAL", 10, int)
RECONNECT_DELAY = _value("RECONNECT_DELAY", 5, int)

# An edge must persist this long before it is believed. A single tick below
# $1 is usually a stale quote about to be pulled.
MIN_SIGNAL_AGE_MS = _value("MIN_SIGNAL_AGE_MS", 250, int)
STALE_BOOK_SEC = _value("STALE_BOOK_SEC", 300, int)


# =====================================================================
# Edge recording — the shape of an edge, not just its threshold crossing
# =====================================================================
# The engine evaluates the edge on every book update and used to keep only
# what beat LIVE_MIN_EDGE. The episodes worth studying are mostly the ones
# that never got there: a market that dips for ten minutes and recovers
# leaves no trace in either signals or the 15-minute scan.

LIVE_RECORD = _env("LIVE_RECORD", True, bool)

# The watch band. Deliberately far below LIVE_MIN_EDGE (0.003) so a window
# that came close is recorded too — "how near did we get, and for how
# long" is the question this data exists to answer.
LIVE_RECORD_MIN_EDGE = _value("LIVE_RECORD_MIN_EDGE", -0.02)

# One tick per event per interval. A busy book updates several times a
# second; at this resolution the shape of a ten-minute dip survives intact
# while the row count stays in the tens of thousands per day.
LIVE_TICK_MIN_INTERVAL_MS = _value("LIVE_TICK_MIN_INTERVAL_MS", 1000, int)

# Ticks are the zoomed-in view and age out. edge_windows — one row per
# episode — is the permanent record and is never pruned.
TICK_RETENTION_DAYS = _value("TICK_RETENTION_DAYS", 7, int)

# A window must last at least this long to be kept. A single stray
# evaluation is noise, not an episode.
MIN_WINDOW_MS = _value("MIN_WINDOW_MS", 2000, int)


# =====================================================================
# Executor — risk limits
# =====================================================================

MAX_CAPITAL_PER_TRADE = _value("MAX_CAPITAL_PER_TRADE", 100.0)
MAX_TRADES_PER_DAY = _value("MAX_TRADES_PER_DAY", 20, int)

# Higher than MIN_NET_EDGE on purpose: executing has costs that observing
# does not, so the bar to act is above the bar to record.
MIN_NET_EDGE_TO_EXECUTE = _value("MIN_NET_EDGE_TO_EXECUTE", 0.008)
MAX_SIGNAL_AGE_MS = _value("MAX_SIGNAL_AGE_MS", 3000, int)
REVALIDATE_TIMEOUT = _value("REVALIDATE_TIMEOUT", 5.0)
LIMIT_BUFFER_TICKS = _value("LIMIT_BUFFER_TICKS", 1, int)


# =====================================================================
# Storage and diagnostics
# =====================================================================

DB_PATH = os.getenv("DB_PATH", "")                    # "" = next to db.py
RECORD = _env("RECORD", False, bool)

# Per-event verdicts are what the dashboard reads to answer "which markets
# did we skip, and why". A scan writes one row per event, so the table
# grows by roughly MAX_EVENTS_SCAN rows every SCAN_INTERVAL and has to be
# bounded. 96 scans is a day at the default interval. 0 disables pruning,
# which is the right setting only if you are deliberately collecting a
# long history and watching the file size.
VERDICT_RETENTION_SCANS = _value("VERDICT_RETENTION_SCANS", 96, int)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()  # "text" | "json"
LOG_FILE = os.getenv("LOG_FILE", "")


# =====================================================================
# Paper wallet — replaying recorded windows with fake money
# =====================================================================
# Answers "would this have made money", which no other part of the system
# can. Replay rather than live, because the question is statistical: the
# same history has to be runnable again with different parameters, and a
# live run cannot be repeated.

PAPER_START_CASH = _value("PAPER_START_CASH", 1000.0)

# A window has to last at least this long to be worth entering. The floor
# is execution latency: roughly 250ms before a signal is believed, ~150ms
# to refetch the books, then ~300ms per leg placed in sequence. Below a
# couple of seconds nothing is reachable at all.
PAPER_MIN_WINDOW_MS = _value("PAPER_MIN_WINDOW_MS", 5000, int)

# What entering actually costs in time, and therefore which tick the
# simulation is allowed to buy at. Buying at the window's best tick would
# be a lie — that price is gone before an order reaches the exchange.
PAPER_LATENCY_BASE_MS = _value("PAPER_LATENCY_BASE_MS", 400, int)
PAPER_LATENCY_PER_LEG_MS = _value("PAPER_LATENCY_PER_LEG_MS", 300, int)

PAPER_MIN_EDGE = _value("PAPER_MIN_EDGE", 0.003)
PAPER_MAX_PER_TRADE = _value("PAPER_MAX_PER_TRADE", 250.0)

# Below this the trade is not worth the capital lock, whatever the edge.
PAPER_MIN_CAPITAL = _value("PAPER_MIN_CAPITAL", 20.0)

# More legs means more sequential orders and more ways to end up holding
# an unhedged half-basket.
PAPER_MAX_LEGS = _value("PAPER_MAX_LEGS", 12, int)


# =====================================================================
# Alerts
# =====================================================================
# Opportunities are rare and brief. Without a push, the dashboard becomes
# a record of what was missed rather than a way to catch anything.
# Unconfigured, notify.py is a no-op and can be called unconditionally.

ALERT_TELEGRAM_TOKEN = os.getenv("ALERT_TELEGRAM_TOKEN", "")
ALERT_TELEGRAM_CHAT_ID = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")

# Above MIN_NET_EDGE on purpose: the bar to interrupt a person is higher
# than the bar to write a row.
ALERT_MIN_EDGE = _value("ALERT_MIN_EDGE", 0.005)

# One alert per event per cooldown. A market sitting on the threshold can
# cross it dozens of times a minute, and an alert per crossing teaches
# people to mute the channel.
ALERT_COOLDOWN_SEC = _value("ALERT_COOLDOWN_SEC", 900, int)
ALERT_TIMEOUT = _value("ALERT_TIMEOUT", 8.0)


# =====================================================================
# Introspection
# =====================================================================


def as_dict() -> Dict[str, Any]:
    """Every resolved setting — used by the log header and `python config.py`."""
    return {
        name: value for name, value in globals().items()
        if name.isupper() and not name.startswith("_")
        and name not in ("PROFILES",)
    }


def describe() -> str:
    lines = [
        "=" * 58,
        f"Configuration (profile: {PROFILE})",
        "=" * 58,
    ]
    for name, value in sorted(as_dict().items()):
        source = ("env" if os.getenv(name) is not None
                  else "profile" if name in _profile
                  else "default")
        lines.append(f"  {name:<26} {str(value):<24} [{source}]")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
