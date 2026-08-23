"""
Tests for configuration resolution.

config.py reads the environment at import time, so these tests set the
environment and re-import it. The precedence rule (env > profile > default)
is the whole contract, and getting it silently wrong would mean a run whose
settings do not match what anyone believes they are — the worst kind of bug
in a system that trades money.
"""

import importlib

import pytest

import config as config_module


def load_config(monkeypatch, **env):
    """Re-import config.py with a specific environment."""
    for key in ("PROFILE", "MIN_NET_EDGE", "MIN_VOLUME_24H", "TEST_CAPITALS",
                "SCAN_INTERVAL", "MAX_CAPITAL_PER_TRADE", "RECORD",
                "MAX_TRADES_PER_DAY", "LIVE_TOP_N", "DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return importlib.reload(config_module)


@pytest.fixture(autouse=True)
def restore_config():
    """Leave the module in its default state for every other test file."""
    yield
    importlib.reload(config_module)


# =====================================================================
# Precedence
# =====================================================================


def test_defaults_apply_with_an_empty_environment(monkeypatch):
    cfg = load_config(monkeypatch)
    assert cfg.PROFILE == "default"
    assert cfg.MIN_NET_EDGE == 0.003
    assert cfg.MIN_VOLUME_24H == 1000.0


def test_environment_overrides_the_default(monkeypatch):
    cfg = load_config(monkeypatch, MIN_NET_EDGE=0.02)
    assert cfg.MIN_NET_EDGE == 0.02


def test_profile_overrides_the_default(monkeypatch):
    cfg = load_config(monkeypatch, PROFILE="conservative")
    assert cfg.MIN_NET_EDGE == 0.010
    assert cfg.MAX_CAPITAL_PER_TRADE == 50.0


def test_environment_beats_the_profile(monkeypatch):
    """
    The precedence that matters most: a one-off override on the command
    line must win over the profile, or experiments silently do nothing.
    """
    cfg = load_config(monkeypatch, PROFILE="conservative", MIN_NET_EDGE=0.5)
    assert cfg.MIN_NET_EDGE == 0.5
    assert cfg.MAX_CAPITAL_PER_TRADE == 50.0   # still from the profile


def test_profile_leaves_unlisted_values_at_their_defaults(monkeypatch):
    cfg = load_config(monkeypatch, PROFILE="conservative")
    assert cfg.SUM_ASKS_MIN == 0.5             # not part of the profile


# =====================================================================
# Type handling
# =====================================================================


def test_integers_stay_integers(monkeypatch):
    cfg = load_config(monkeypatch, SCAN_INTERVAL=300)
    assert cfg.SCAN_INTERVAL == 300
    assert isinstance(cfg.SCAN_INTERVAL, int)


def test_lists_parse_from_comma_separated_values(monkeypatch):
    cfg = load_config(monkeypatch, TEST_CAPITALS="25, 250, 2500")
    assert cfg.TEST_CAPITALS == [25.0, 250.0, 2500.0]


def test_booleans_accept_the_usual_spellings(monkeypatch):
    assert load_config(monkeypatch, RECORD="yes").RECORD is True
    assert load_config(monkeypatch, RECORD="1").RECORD is True
    assert load_config(monkeypatch, RECORD="false").RECORD is False


# =====================================================================
# Bad input must be loud, never silent
# =====================================================================


def test_a_garbage_value_warns_and_falls_back(monkeypatch, capsys):
    """
    MIN_NET_EDGE=abc must not become 0, which would make every event look
    like an opportunity. It warns and keeps the default.
    """
    cfg = load_config(monkeypatch, MIN_NET_EDGE="abc")
    assert cfg.MIN_NET_EDGE == 0.003
    assert "WARNING" in capsys.readouterr().out


def test_an_unknown_profile_warns_and_uses_default(monkeypatch, capsys):
    cfg = load_config(monkeypatch, PROFILE="aggressive-yolo")
    assert cfg.PROFILE == "default"
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "conservative" in out        # lists what is actually available


def test_a_malformed_list_warns_and_falls_back(monkeypatch, capsys):
    cfg = load_config(monkeypatch, TEST_CAPITALS="10,twenty,30")
    assert cfg.TEST_CAPITALS == [10, 50, 100, 500, 1000, 5000]
    assert "WARNING" in capsys.readouterr().out


# =====================================================================
# Introspection — the run must be reproducible from its own log
# =====================================================================


def test_as_dict_exposes_settings_but_not_the_profile_table(monkeypatch):
    cfg = load_config(monkeypatch)
    settings = cfg.as_dict()
    assert "MIN_NET_EDGE" in settings
    assert "MAX_CAPITAL_PER_TRADE" in settings
    assert "PROFILES" not in settings


def test_describe_labels_where_each_value_came_from(monkeypatch):
    cfg = load_config(monkeypatch, PROFILE="conservative", MIN_VOLUME_24H=777)
    text = cfg.describe()
    assert "profile: conservative" in text
    # the env-set value is labelled [env], the profile-set one [profile]
    env_line = next(l for l in text.splitlines() if "MIN_VOLUME_24H" in l)
    profile_line = next(l for l in text.splitlines() if "MAX_TRADES_PER_DAY" in l)
    default_line = next(l for l in text.splitlines() if "SUM_ASKS_MIN" in l)
    assert "[env]" in env_line
    assert "[profile]" in profile_line
    assert "[default]" in default_line


# =====================================================================
# Consumers actually read from config
# =====================================================================


def test_scanner_thresholds_come_from_config(monkeypatch):
    """
    Guards against the old failure mode: a threshold defined in config but
    still hard-coded in the module that uses it.
    """
    load_config(monkeypatch, MIN_VOLUME_24H=4242)
    import scanner
    importlib.reload(scanner)
    assert scanner.MIN_VOLUME_24H == 4242
    importlib.reload(scanner)


def test_findmarket_and_scanner_share_the_same_thresholds(monkeypatch):
    """The specific bug this whole stage exists to prevent."""
    load_config(monkeypatch, MIN_VOLUME_24H=3333)
    import findmarket
    import scanner
    importlib.reload(scanner)
    importlib.reload(findmarket)
    assert findmarket.MIN_VOLUME_24H == scanner.MIN_VOLUME_24H == 3333
    importlib.reload(scanner)
    importlib.reload(findmarket)
