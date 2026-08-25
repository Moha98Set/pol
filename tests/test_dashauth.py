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
