"""
Recorder — freeze the exact input a scan saw, so it can be replayed later.
==========================================================================

The core debugging problem of this project: the input is gone by the time
you want to look at it. An order book that produced a suspicious signal at
14:32 does not exist at 14:33, and no amount of logging brings it back.
You cannot re-run the decision, so you cannot ever prove why it was made.

This module records the RAW input to every scanned event — the Gamma event
payload and the CLOB books, exactly as received — into a fixture file.
replay.py then feeds those files back through the real analysis code.

What that buys:

  * debugging with no internet, no VPN, no API being up
  * a suspicious signal from last week can be re-examined byte for byte
  * a fixed bug can be pinned with the real book that triggered it, by
    copying one file into tests/fixtures/
  * refactors are verifiable: replay before and after, diff the verdicts

Fixtures are newline-delimited JSON (one event per line) so a recording can
be appended to while it runs and read back without loading it all at once.

Usage is opt-in from the scanner:

    RECORD=1 python arb_monitor.py
    python replay.py recordings/scan-2026-07-27T14-32-05Z.jsonl
"""

import gzip
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

RECORDINGS_DIR = Path(__file__).parent / "recordings"
FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"

# Bump when the on-disk shape changes so replay can refuse mismatched files
# instead of silently misinterpreting them.
FORMAT_VERSION = 1

log = logging.getLogger("recorder")


def recording_enabled() -> bool:
    """Recording is opt-in — it costs disk, and most scans are unremarkable."""
    return os.getenv("RECORD", "").lower() in ("1", "yes", "true")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


class Recorder:
    """
    Writes one JSON object per scanned event.

    Deliberately dumb: it stores raw payloads and does no analysis of its
    own. If the recorder interpreted the data, a bug in the interpretation
    would be baked into every fixture and the whole exercise would be
    circular.
    """

    def __init__(self, name: str = "scan", compress: bool = True,
                 directory: Path = None):
        # resolved at call time, not bound as a default: a default argument
        # is captured at import and would ignore any later change to
        # RECORDINGS_DIR — including the one tests rely on
        directory = Path(directory) if directory else RECORDINGS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".jsonl.gz" if compress else ".jsonl"
        self.path = directory / f"{name}-{_timestamp_slug()}{suffix}"
        self.compress = compress
        self.count = 0
        self._fh = (gzip.open(self.path, "wt", encoding="utf-8")
                    if compress else
                    open(self.path, "w", encoding="utf-8"))
        self._write({
            "type": "header",
            "format_version": FORMAT_VERSION,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "name": name,
        })
        log.info("Recording to %s", self.path)

    def _write(self, obj: dict):
        self._fh.write(json.dumps(obj, default=str) + "\n")

    def record_event(self, event: dict, books: dict, *,
                     fee_rate: float = None, is_binary: bool = None,
                     note: str = None):
        """
        Store one event's complete input.

        `books` maps token_id -> raw CLOB book dict, exactly as the API
        returned it. Raw, not normalized: normalization is code under test.
        """
        self._write({
            "type": "event",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "slug": event.get("slug"),
            "title": event.get("title"),
            "is_binary": is_binary,
            "fee_rate": fee_rate,
            "note": note,
            "event": event,
            "books": books,
        })
        self.count += 1

    def close(self):
        self._write({"type": "footer", "events": self.count,
                     "closed_at": datetime.now(timezone.utc).isoformat()})
        self._fh.close()
        size_kb = self.path.stat().st_size / 1024
        log.info("Recorded %d events to %s (%.0f KB)",
                 self.count, self.path.name, size_kb)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# =====================================================================
# Reading recordings back
# =====================================================================


def _open_any(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def read_recording(path) -> Iterator[dict]:
    """
    Yield the recorded events from a fixture file.

    Streams line by line: a long recording can be gigabytes and there is
    never a reason to hold it all in memory.
    """
    path = Path(path)
    with _open_any(path) as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.warning("%s:%d is not valid JSON, skipping",
                            path.name, line_no)
                continue

            if obj.get("type") == "header":
                version = obj.get("format_version")
                if version != FORMAT_VERSION:
                    raise ValueError(
                        f"{path.name} was written in format v{version}, "
                        f"this code reads v{FORMAT_VERSION}")
                continue
            if obj.get("type") == "footer":
                continue
            if obj.get("type") == "event":
                yield obj


def recording_header(path) -> Optional[dict]:
    with _open_any(Path(path)) as fh:
        first = fh.readline().strip()
    try:
        obj = json.loads(first)
    except json.JSONDecodeError:
        return None
    return obj if obj.get("type") == "header" else None


def list_recordings(directory: Path = RECORDINGS_DIR) -> list:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.jsonl*"), reverse=True)


def promote_to_fixture(recording_path, slug: str, fixture_name: str) -> Path:
    """
    Pull one event out of a recording and save it as a permanent test
    fixture.

    This is how a real-world bug becomes a regression test: find the event
    that misbehaved, promote it, and write a test that asserts the correct
    verdict against the actual book that caused the problem.
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for record in read_recording(recording_path):
        if record.get("slug") == slug:
            out = FIXTURES_DIR / f"{fixture_name}.json"
            out.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return out
    raise KeyError(f"event '{slug}' not found in {recording_path}")


def load_fixture(name: str) -> dict:
    """Load a promoted fixture by name (no .json suffix needed)."""
    path = FIXTURES_DIR / (name if name.endswith(".json") else f"{name}.json")
    return json.loads(path.read_text(encoding="utf-8"))
