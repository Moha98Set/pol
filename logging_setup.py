"""
Structured logging — one JSON object per line, queryable after the fact.
=======================================================================

The old logs looked like this:

    14:32:05 [INFO] FOUND: [multi] Who wins the election? | net_edge=0.42%

Readable, and useless for answering the questions that actually come up:
"which stage rejects the most events?", "did the dry-leg rate jump this
week?", "what was the latency distribution during that outage?". Answering
any of those from prose means writing a regex, and the regex breaks the
next time someone edits the message.

With LOG_FORMAT=json the same line becomes:

    {"ts": "...", "level": "INFO", "logger": "arb_monitor",
     "msg": "opportunity found", "event_slug": "who-wins",
     "stage": "opportunity", "net_edge": 0.0042, "duration_ms": 812}

which is a dataset. Then:

    grep '"stage":"dry_legs"' arb.log | wc -l
    jq -s 'map(.duration_ms) | add/length' arb.log

Human-readable text stays the default; JSON is opt-in via LOG_FORMAT=json,
because staring at JSON in a terminal while developing is miserable.

Usage:
    import logging_setup
    logging_setup.configure("arb_monitor")
    log = logging_setup.get_logger("arb_monitor")

    log.info("opportunity found", extra=logging_setup.fields(
        event_slug=slug, stage="opportunity", net_edge=0.0042))
"""

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import config

# Attribute name under which structured fields ride on a LogRecord. Anything
# not in this dict is a plain log message; anything in it becomes JSON keys.
FIELDS_KEY = "structured"

# LogRecord's own attributes — used to avoid colliding with them.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def fields(**kwargs) -> Dict[str, Dict[str, Any]]:
    """
    Wrap structured fields for a log call:

        log.info("scan finished", extra=fields(events=812, errors=3))

    Returns the `extra=` dict logging expects, with everything nested under
    one key so user fields can never shadow LogRecord internals like
    `message` or `args` (which raises a confusing KeyError deep inside
    logging if you pass them at the top level).
    """
    return {FIELDS_KEY: kwargs}


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(
                record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        structured = getattr(record, FIELDS_KEY, None)
        if structured:
            payload.update(structured)

        # anything attached directly via extra={} still gets through
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != FIELDS_KEY:
                payload.setdefault(key, value)

        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """
    Human format, with structured fields appended compactly.

    The fields are still shown — losing information just because a human is
    reading would defeat the point of attaching it.
    """

    def __init__(self):
        super().__init__(fmt="%(asctime)s [%(levelname)s] %(message)s",
                         datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        structured = getattr(record, FIELDS_KEY, None)
        if not structured:
            return base
        extras = " ".join(f"{k}={_compact(v)}" for k, v in structured.items())
        return f"{base}  |  {extras}"


def _compact(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def configure(name: str = None, *, force: bool = False):
    """
    Install the configured handlers on the root logger.

    Honours LOG_FORMAT (text|json), LOG_LEVEL and LOG_FILE from config.
    When LOG_FILE is set, JSON goes to the file and human-readable text to
    the console — the arrangement you want on a server: readable while you
    watch it, queryable afterwards.
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return
    root.handlers.clear()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        JsonFormatter() if config.LOG_FORMAT == "json" else TextFormatter())
    root.addHandler(console)

    if config.LOG_FILE:
        path = Path(config.LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    # requests/urllib3 log every connection at INFO — noise at this volume
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    if name:
        logging.getLogger(name).debug(
            "logging configured", extra=fields(
                format=config.LOG_FORMAT, level=config.LOG_LEVEL,
                file=config.LOG_FILE or None, pid=os.getpid()))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# =====================================================================
# Timing
# =====================================================================


@contextmanager
def timed(log: logging.Logger, msg: str, level: int = logging.INFO, **extra):
    """
    Time a block and log its duration as a queryable field.

        with timed(log, "book fetch", token_count=len(tokens)):
            books = fetch_order_books(tokens)

    Duration is logged whether or not the block raises, because the timing
    of a failure is usually the interesting part.
    """
    start = time.perf_counter()
    error = None
    try:
        yield
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        payload = dict(extra, duration_ms=round(duration_ms, 2))
        if error:
            payload["error"] = error
        log.log(logging.ERROR if error else level, msg, extra=fields(**payload))
