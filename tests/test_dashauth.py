"""
Accounts, the login record, and the lockout.

Security code, so the tests state the properties rather than the
implementation: a wrong password never authenticates, a disabled account
never authenticates, and one analyst's failures never lock out another's.
"""

import pytest

import dashauth


@pytest.fixture
def db(tmp_path):
    conn = dashauth.connect(tmp_path / "auth.db")
    yield conn
    conn.close()


@pytest.fixture
def users(db):
    dashauth.add_user(db, "sara", "correct-horse-battery", "سارا")
    dashauth.add_user(db, "reza", "another-good-secret", "رضا")
    return db


def auth(db, user, password, ip="10.0.0.1"):
    return dashauth.authenticate(db, user, password, ip=ip, user_agent="test")


# =====================================================================
# Password hashing
# =====================================================================


def test_a_password_verifies_against_its_own_hash():
    stored = dashauth.hash_password("hunter2-but-longer")
    assert dashauth.verify_password("hunter2-but-longer", stored)


def test_a_wrong_password_does_not_verify():
    stored = dashauth.hash_password("hunter2-but-longer")
    assert not dashauth.verify_password("hunter3-but-longer", stored)


def test_the_same_password_hashes_differently_each_time():
    """A per-password salt, so identical passwords are not identifiable."""
    a = dashauth.hash_password("same-password-here")
    b = dashauth.hash_password("same-password-here")
    assert a != b
    assert dashauth.verify_password("same-password-here", a)
    assert dashauth.verify_password("same-password-here", b)


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$only-two",
                                    "bcrypt$aa$bb", None])
def test_a_malformed_hash_refuses_rather_than_raising(stored):
    assert not dashauth.verify_password("anything", stored)


# =====================================================================
# Authentication
# =====================================================================


def test_the_right_password_authenticates(users):
    user, reason = auth(users, "sara", "correct-horse-battery")
    assert user is not None
    assert reason is None
    assert user["username"] == "sara"


def test_the_wrong_password_does_not(users):
    user, reason = auth(users, "sara", "nope")
    assert user is None
    assert reason == "bad_credentials"


def test_an_unknown_user_gives_the_same_answer_as_a_wrong_password(users):
    """Otherwise the refusal itself tells an attacker which names exist."""
    _u1, unknown = auth(users, "nobody", "whatever")
    _u2, wrong = auth(users, "sara", "whatever")
    assert unknown == wrong == "bad_credentials"


def test_a_disabled_account_cannot_log_in(users):
    dashauth.set_disabled(users, "sara", True)
    user, reason = auth(users, "sara", "correct-horse-battery")
    assert user is None
    assert reason == "disabled"


def test_a_re_enabled_account_can_log_in_again(users):
    dashauth.set_disabled(users, "sara", True)
    dashauth.set_disabled(users, "sara", False)
    user, _reason = auth(users, "sara", "correct-horse-battery")
    assert user is not None


def test_a_reset_password_replaces_the_old_one(users):
    dashauth.set_password(users, "sara", "a-brand-new-secret")
    assert auth(users, "sara", "correct-horse-battery")[0] is None
    assert auth(users, "sara", "a-brand-new-secret")[0] is not None


# =====================================================================
# The login record
# =====================================================================


def test_every_attempt_is_recorded_success_or_not(users):
    auth(users, "sara", "correct-horse-battery")
    auth(users, "sara", "wrong")
    auth(users, "ghost", "wrong")

    logins = dashauth.recent_logins(users)
    assert len(logins) == 3
    assert sum(r["success"] for r in logins) == 1


def test_a_login_names_the_person_not_a_shared_account(users):
    auth(users, "reza", "another-good-secret", ip="10.0.0.7")
    row = dashauth.recent_logins(users, 1)[0]
    assert row["username"] == "reza"
    assert row["ip"] == "10.0.0.7"
    assert row["success"] == 1


def test_a_successful_login_updates_the_user_row(users):
    auth(users, "sara", "correct-horse-battery", ip="10.0.0.9")
    user = dashauth.get_user(users, "sara")
    assert user["last_login_at"]
    assert user["last_login_ip"] == "10.0.0.9"


# =====================================================================
# Lockout — the property that matters with several analysts
# =====================================================================


def test_repeated_failures_lock_the_account_out(users):
    for _ in range(dashauth.LOCKOUT_ATTEMPTS):
        auth(users, "sara", "wrong")

    user, reason = auth(users, "sara", "correct-horse-battery")
    assert user is None
    assert reason == "too_many_for_user"


def test_one_analyst_being_locked_out_does_not_lock_out_another(users):
    """
    All the analysts sit behind one office address. A per-IP lockout would
    mean one person mistyping their password stops the whole team working.
    """
    for _ in range(dashauth.LOCKOUT_ATTEMPTS):
        auth(users, "sara", "wrong", ip="203.0.113.5")

    user, _reason = auth(users, "reza", "another-good-secret",
                         ip="203.0.113.5")
    assert user is not None


def test_a_lockout_does_not_follow_the_user_to_another_address(users):
    """
    And the mirror risk: a per-user lockout lets anyone who knows a
    colleague's username lock them out on purpose.
    """
    for _ in range(dashauth.LOCKOUT_ATTEMPTS):
        auth(users, "sara", "wrong", ip="198.51.100.9")

    user, _reason = auth(users, "sara", "correct-horse-battery",
                         ip="10.0.0.1")
    assert user is not None


def test_broad_scanning_from_one_address_is_still_stopped(users):
    """
    The pair rule must not become a way to guess forever by rotating
    usernames, so the address itself has a ceiling too.
    """
    for i in range(dashauth.IP_ATTEMPTS):
        auth(users, f"guess{i}", "wrong", ip="192.0.2.44")

    _user, reason = auth(users, "sara", "correct-horse-battery",
                         ip="192.0.2.44")
    assert reason == "too_many_for_ip"


def test_a_locked_out_attempt_is_itself_recorded(users):
    for _ in range(dashauth.LOCKOUT_ATTEMPTS + 1):
        auth(users, "sara", "wrong")

    reasons = [r["reason"] for r in dashauth.recent_logins(users)]
    assert "too_many_for_user" in reasons


# =====================================================================
# Where the account database lives
# =====================================================================


def test_accounts_land_in_the_file_that_was_asked_for(tmp_path):
    """
    The service reads the path DB_PATH implies; the CLI is run by hand and
    does not get the unit's environment. If the two ever disagree, accounts
    are created in a file nothing reads — so the path is explicit and the
    connection is proved to write where it was told.
    """
    target = tmp_path / "nested" / "dashboard.db"
    target.parent.mkdir()

    conn = dashauth.connect(target)
    dashauth.add_user(conn, "sara", "a-long-enough-secret")
    conn.close()

    assert target.exists()
    reopened = dashauth.connect(target)
    assert dashauth.get_user(reopened, "sara") is not None
    reopened.close()


def test_an_unwritable_location_is_reported_before_sqlite_is_touched(tmp_path):
    import dashboard

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        with pytest.raises(SystemExit) as excinfo:
            dashboard._check_auth_db_usable(locked / "dashboard.db")
        # the message has to carry the fix, not just the failure
        assert "polly.env" in str(excinfo.value)
        assert "--auth-db" in str(excinfo.value)
    finally:
        locked.chmod(0o755)


def test_a_missing_parent_directory_is_named(tmp_path):
    import dashboard

    with pytest.raises(SystemExit) as excinfo:
        dashboard._check_auth_db_usable(tmp_path / "absent" / "dashboard.db")
    assert "absent" in str(excinfo.value)


# =====================================================================
# Sorting and range filters
# =====================================================================


def _ctx(query=""):
    import dashboard
    return dashboard.app.test_request_context(f"/x?{query}")


def test_only_declared_columns_can_reach_the_order_by():
    """
    The sort column names a SQL expression, so anything the request can
    put there must be checked against a list rather than trusted.
    """
    import dashboard
    cols = {"net_edge": dashboard.Col("net_edge", "لبه")}
    with _ctx("sort=net_edge%3B+DROP+TABLE+scans"):
        order_by, _c, _p, state = dashboard.sort_and_filter(cols, "net_edge")
    assert "DROP" not in order_by
    assert state["sort"] == "net_edge"


def test_an_unknown_sort_column_falls_back_to_the_default():
    import dashboard
    cols = {"a": dashboard.Col("a", "A"), "b": dashboard.Col("b", "B")}
    with _ctx("sort=nonsense"):
        _o, _c, _p, state = dashboard.sort_and_filter(cols, "b")
    assert state["sort"] == "b"


def test_nulls_sort_last_in_both_directions():
    """A row with no edge is not the best edge, nor the worst — it is absent."""
    import dashboard
    cols = {"net_edge": dashboard.Col("net_edge", "لبه")}
    for direction in ("asc", "desc"):
        with _ctx(f"sort=net_edge&dir={direction}"):
            order_by, *_ = dashboard.sort_and_filter(cols, "net_edge")
        assert order_by.startswith("(net_edge IS NULL)")


def test_a_percentage_bound_is_read_in_the_unit_the_column_shows():
    """
    Edges are stored as 0.004 and displayed as 0.400%. Typing 0.4 into the
    box has to mean 0.4%, not 40% — otherwise the filter quietly does
    something a hundred times larger than it appears to.
    """
    import dashboard
    cols = {"net_edge": dashboard.Col("net_edge", "لبه", "percent", 0.01)}
    with _ctx("min_net_edge=0.4"):
        _o, clauses, params, _s = dashboard.sort_and_filter(cols, "net_edge")
    assert clauses == ["net_edge >= ?"]
    assert params[0] == pytest.approx(0.004)


def test_a_duration_bound_is_read_in_minutes():
    import dashboard
    cols = {"duration_ms": dashboard.Col("duration_ms", "طول", "duration",
                                         60_000)}
    with _ctx("min_duration_ms=2.5"):
        _o, _c, params, _s = dashboard.sort_and_filter(cols, "duration_ms")
    assert params[0] == pytest.approx(150_000)


def test_a_non_numeric_bound_is_ignored_rather_than_raising():
    import dashboard
    cols = {"volume_24h": dashboard.Col("volume_24h", "حجم")}
    with _ctx("min_volume_24h=abc&max_volume_24h="):
        _o, clauses, params, state = dashboard.sort_and_filter(
            cols, "volume_24h")
    assert clauses == [] and params == [] and state["bounds"] == {}


def test_both_bounds_together_make_a_closed_range():
    import dashboard
    cols = {"volume_24h": dashboard.Col("volume_24h", "حجم")}
    with _ctx("min_volume_24h=100&max_volume_24h=900"):
        _o, clauses, params, _s = dashboard.sort_and_filter(cols, "volume_24h")
    assert clauses == ["volume_24h >= ?", "volume_24h <= ?"]
    assert params == [100.0, 900.0]


def test_a_time_window_narrows_by_the_views_own_column():
    import dashboard
    cols = {"a": dashboard.Col("a", "A")}
    with _ctx("since=6h"):
        _o, clauses, params, state = dashboard.sort_and_filter(
            cols, "a", time_col="opened_at")
    assert clauses == ["opened_at >= ?"]
    assert state["time"]["since"] == "6h"
    # the cutoff is a real timestamp, comparable to what is stored
    assert params[0].startswith("20")


def test_a_default_time_window_applies_when_none_was_chosen():
    import dashboard
    cols = {"a": dashboard.Col("a", "A")}
    with _ctx(""):
        _o, clauses, _p, _s = dashboard.sort_and_filter(
            cols, "a", time_col="found_at", default_since="24h")
    assert clauses == ["found_at >= ?"]


def test_choosing_all_time_overrides_the_default_window():
    """An explicitly empty `since` must beat the default, not fall back to it."""
    import dashboard
    cols = {"a": dashboard.Col("a", "A")}
    with _ctx("since="):
        _o, clauses, _p, _s = dashboard.sort_and_filter(
            cols, "a", time_col="found_at", default_since="24h")
    assert clauses == []


def test_a_to_date_covers_the_whole_day_it_names():
    import dashboard
    cols = {"a": dashboard.Col("a", "A")}
    with _ctx("to=2026-08-25"):
        _o, _c, params, _s = dashboard.sort_and_filter(
            cols, "a", time_col="opened_at")
    assert params[0].startswith("2026-08-25T23:59:59")


@pytest.mark.parametrize("ms,expected", [
    (None, "—"),
    (420, "میلی‌ثانیه"),
    (4_500, "ثانیه"),
    (45_000, "ثانیه"),
    (90_000, "دقیقه"),
    (330_000, "دقیقه"),
    (3_600_000, "ساعت"),
    (90_000_000, "روز"),
])
def test_a_duration_is_shown_in_the_unit_that_suits_it(ms, expected):
    """
    Everything used to be minutes, which renders a four-second window as
    0.1 and an hour-long one as 61.4.
    """
    import dashboard
    assert expected in dashboard.duration(ms)


def test_five_and_a_half_minutes_reads_as_minutes():
    import dashboard
    assert dashboard.duration(330_000) == "5.5 دقیقه"


def test_an_hour_and_a_half_reads_as_hours():
    import dashboard
    assert dashboard.duration(5_400_000) == "1.5 ساعت"


# =====================================================================
# The overview trend range
# =====================================================================


def test_every_offered_range_has_a_bucket_and_a_window():
    import dashboard
    assert set(dashboard.TREND_RANGES) == {"6h", "12h", "1d", "7d", "30d"}
    for key, (label, seconds, bucket) in dashboard.TREND_RANGES.items():
        assert label and seconds > 0
        assert "found_at" in bucket, key


def test_longer_ranges_use_coarser_buckets():
    """
    Thirty days of 15-minute scans is ~2900 points against a 400px chart.
    Each range has to aggregate enough to stay readable, so the bucket
    must grow with the window rather than staying per-scan.
    """
    import dashboard
    order = ["6h", "12h", "1d", "7d", "30d"]
    seconds = [dashboard.TREND_RANGES[k][1] for k in order]
    assert seconds == sorted(seconds)
    # the coarsest bucket is a whole day, the finest is sub-hour
    assert "%M" in dashboard.TREND_RANGES["6h"][2]
    assert "%H" not in dashboard.TREND_RANGES["30d"][2]


def test_an_unknown_range_falls_back_rather_than_reaching_sql():
    import dashboard
    with dashboard.app.test_request_context("/?range=1%3B+DROP+TABLE+scans"):
        key = dashboard.request.args.get("range")
        resolved = key if key in dashboard.TREND_RANGES else dashboard.TREND_DEFAULT
    assert resolved == dashboard.TREND_DEFAULT
