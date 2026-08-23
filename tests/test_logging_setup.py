"""
Tests for structured logging.

The value of a log line here is that it can be queried later, so what
matters is that fields survive intact and typed: a net_edge that arrives as
the string "0.42%" instead of the number 0.0042 is worse than no field at
all, because it looks queryable and silently is not.
"""

import importlib
import json
import logging

import pytest

import config as config_module
import logging_setup


@pytest.fixture(autouse=True)
def clean_logging():
    yield
    logging.getLogger().handlers.clear()
    importlib.reload(config_module)
    importlib.reload(logging_setup)


def make_record(msg="hello", logger="test", level=logging.INFO, **extra):
    record = logging.LogRecord(logger, level, "f.py", 1, msg, (), None)
    if extra:
        setattr(record, logging_setup.FIELDS_KEY, extra)
    return record


# =====================================================================
# JSON output
# =====================================================================


def test_json_line_is_valid_json_with_the_standard_keys():
    out = logging_setup.JsonFormatter().format(make_record("scan complete"))
    payload = json.loads(out)
    assert payload["msg"] == "scan complete"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert "ts" in payload


def test_structured_fields_become_top_level_keys():
    out = logging_setup.JsonFormatter().format(make_record(
        "opportunity found", event_slug="who-wins", net_edge=0.0042,
        stage="opportunity"))
    payload = json.loads(out)
    assert payload["event_slug"] == "who-wins"
    assert payload["stage"] == "opportunity"
    assert payload["net_edge"] == 0.0042


def test_numbers_stay_numbers():
    """
    The point of the whole exercise. If net_edge arrives as a string,
    `jq 'select(.net_edge > 0.01)'` silently returns nothing.
    """
    payload = json.loads(logging_setup.JsonFormatter().format(
        make_record("x", net_edge=0.0042, events=812, ok=True)))
    assert isinstance(payload["net_edge"], float)
    assert isinstance(payload["events"], int)
    assert isinstance(payload["ok"], bool)


def test_json_is_one_line_even_with_newlines_in_the_message():
    """Multi-line output would break every line-based tool downstream."""
    out = logging_setup.JsonFormatter().format(make_record("line1\nline2"))
    assert "\n" not in out
    assert json.loads(out)["msg"] == "line1\nline2"


def test_non_ascii_survives():
    payload = json.loads(logging_setup.JsonFormatter().format(
        make_record("پیام فارسی", title="انتخابات")))
    assert payload["msg"] == "پیام فارسی"
    assert payload["title"] == "انتخابات"


def test_exceptions_are_captured_as_fields():
    try:
        raise ValueError("book was empty")
    except ValueError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, "f.py", 1,
                                   "scan failed", (), sys.exc_info())
    payload = json.loads(logging_setup.JsonFormatter().format(record))
    assert payload["exc_type"] == "ValueError"
    assert "book was empty" in payload["exc"]


def test_unserializable_values_do_not_break_the_line():
    """A log call must never take down the scanner it is describing."""
    payload = json.loads(logging_setup.JsonFormatter().format(
        make_record("x", obj=object())))
    assert isinstance(payload["obj"], str)


# =====================================================================
# fields() helper
# =====================================================================


def test_fields_nests_under_one_key():
    assert logging_setup.fields(a=1) == {logging_setup.FIELDS_KEY: {"a": 1}}


def test_fields_named_like_logrecord_internals_are_safe(caplog):
    """
    `extra={"message": ...}` raises deep inside logging. Nesting under one
    key means callers can use any field name they like, including 'msg',
    'name' or 'args'.
    """
    # no configure() here: it replaces the root handlers, including the one
    # caplog installs to capture records
    log = logging_setup.get_logger("test")
    with caplog.at_level(logging.INFO):
        log.info("ok", extra=logging_setup.fields(
            message="shadowed", name="also shadowed", args="third"))

    payload = json.loads(logging_setup.JsonFormatter().format(caplog.records[0]))
    assert payload["msg"] == "ok"
    assert payload["message"] == "shadowed"


# =====================================================================
# Text output
# =====================================================================


def test_text_format_appends_fields_instead_of_dropping_them():
    out = logging_setup.TextFormatter().format(make_record(
        "opportunity found", event_slug="who-wins", net_edge=0.0042))
    assert "opportunity found" in out
    assert "event_slug=who-wins" in out
    assert "net_edge=0.0042" in out


def test_text_format_without_fields_is_plain():
    out = logging_setup.TextFormatter().format(make_record("just a message"))
    assert out.endswith("just a message")
    assert "|" not in out


# =====================================================================
# configure()
# =====================================================================


def test_configure_honours_log_format(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    importlib.reload(config_module)
    importlib.reload(logging_setup)
    logging_setup.configure(force=True)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, logging_setup.JsonFormatter)


def test_configure_defaults_to_readable_text(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    importlib.reload(config_module)
    importlib.reload(logging_setup)
    logging_setup.configure(force=True)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, logging_setup.TextFormatter)


def test_log_file_gets_json_even_when_the_console_is_text(monkeypatch, tmp_path):
    """
    The arrangement you want on a server: readable while watching it,
    queryable afterwards.
    """
    log_file = tmp_path / "arb.log"
    monkeypatch.setenv("LOG_FORMAT", "text")
    monkeypatch.setenv("LOG_FILE", str(log_file))
    importlib.reload(config_module)
    importlib.reload(logging_setup)
    logging_setup.configure(force=True)

    logging_setup.get_logger("test").info(
        "opportunity found", extra=logging_setup.fields(net_edge=0.0042))
    for handler in logging.getLogger().handlers:
        handler.flush()

    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert json.loads(line)["net_edge"] == 0.0042


# =====================================================================
# timed()
# =====================================================================


def test_timed_logs_a_numeric_duration(caplog):
    # no configure() here: it replaces the root handlers, including the one
    # caplog installs to capture records
    log = logging_setup.get_logger("test")
    with caplog.at_level(logging.INFO):
        with logging_setup.timed(log, "book fetch", tokens=12):
            pass

    payload = getattr(caplog.records[-1], logging_setup.FIELDS_KEY)
    assert isinstance(payload["duration_ms"], float)
    assert payload["tokens"] == 12


def test_timed_still_logs_when_the_block_raises(caplog):
    """The timing of a failure is usually the interesting measurement."""
    # no configure() here: it replaces the root handlers, including the one
    # caplog installs to capture records
    log = logging_setup.get_logger("test")
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            with logging_setup.timed(log, "book fetch"):
                raise RuntimeError("timeout")

    record = caplog.records[-1]
    payload = getattr(record, logging_setup.FIELDS_KEY)
    assert record.levelno == logging.ERROR
    assert "timeout" in payload["error"]
    assert "duration_ms" in payload


def test_timed_does_not_swallow_the_exception():
    log = logging_setup.get_logger("test")
    with pytest.raises(ValueError):
        with logging_setup.timed(log, "x"):
            raise ValueError("must propagate")
