"""
tests/conftest.py — shared test setup and fixtures.

Puts the backend root on sys.path once, so individual test files do not each
repeat a sys.path.insert. pytest inserts the rootdir only under some
invocations, and `pytest` versus `python -m pytest` differ on whether the
working directory lands on the path — this removes the difference.
"""
import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# Tokens are minted with the application's own make_token() rather than
# hand-assembled here. auth_utils signs with JWT_SECRET and read_token()
# rejects anything whose signature does not match, so a fixture that built its
# own token would have to duplicate the signing scheme — and would then keep
# passing if that scheme ever changed, which is the opposite of what an auth
# test is for.
#
# The identifier is an email because that is what get_current_user() returns
# as "sub" and what every service in the codebase uses as user_id.

TEST_USER = "test.student@example.com"
OTHER_USER = "other.student@example.com"


def _bearer(identifier: str) -> dict:
    from services.auth_utils import make_token
    return {"Authorization": f"Bearer {make_token(identifier)}"}


@pytest.fixture
def auth_identifier() -> str:
    """The identifier auth_headers authenticates as.

    Exposed separately so a test can assert on stored rows — attachment
    ownership is recorded against this exact string.
    """
    return TEST_USER


@pytest.fixture
def auth_headers() -> dict:
    """Authorization header for a signed-in user."""
    return _bearer(TEST_USER)


@pytest.fixture
def other_auth_headers() -> dict:
    """A DIFFERENT signed-in user.

    Required for the ownership tests: proving one user cannot read another's
    attachment needs two real identities, not one identity and an absent
    header, which only proves that unauthenticated access is refused.
    """
    return _bearer(OTHER_USER)


# ---------------------------------------------------------------------------
# Databases for the licensing tests
# ---------------------------------------------------------------------------
#
# db.py has exactly one switch to Postgres — DATABASE_URL, read at call time —
# and no separate test-database setting. So a Postgres test reaches Postgres
# only by setting DATABASE_URL itself, in-process, from TEST_DATABASE_URL.
#
# The host check is the guard that keeps this from ever pointing at Neon:
# these tests create rows and drop indexes, and production holds live paying
# users' data. Anything other than a local server is refused.
#
# A missing TEST_DATABASE_URL FAILS rather than skips. A skipped concurrency
# test reads as a passing one in a summary line, which is the failure
# test_app_imports.py was written to prevent. SQLite is not a substitute:
# it serialises writers and passes concurrency tests that Postgres fails.

_LOCAL_PG_HOSTS = {"127.0.0.1", "localhost"}


@pytest.fixture
def pg_database(monkeypatch):
    """Point the app at the local throwaway Postgres and initialise schema."""
    from urllib.parse import urlparse

    url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    assert url, (
        "TEST_DATABASE_URL is not set. Postgres-only tests fail rather than "
        "skip — run ep-pg-start and use the cloud environment, or export a "
        "local throwaway database URL."
    )
    host = urlparse(url).hostname
    assert host in _LOCAL_PG_HOSTS, (
        f"TEST_DATABASE_URL host is {host!r}; refusing anything but a local "
        "server. These tests write rows and drop indexes."
    )

    monkeypatch.setenv("DATABASE_URL", url)
    from db import init_db
    init_db()
    return url


@pytest.fixture
def sqlite_database(tmp_path, monkeypatch):
    """A fresh, initialised SQLite file, isolated from exam_partner.db.

    Sets both DB_PATH bindings for the reason given on the client fixture in
    test_attachments.py: init_db() and config.db_conn() resolve the path
    differently.
    """
    import config

    db_file = str(tmp_path / "licensing.db")
    monkeypatch.setenv("DB_PATH", db_file)
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from db import init_db
    init_db(db_file)
    return db_file


@pytest.fixture
def make_pool():
    """Factory: insert an account and one seat pool, return their ids.

    Works on whichever engine DATABASE_URL currently selects, so request it
    AFTER pg_database or sqlite_database. Rows are written directly because
    the admin endpoints that create accounts and pools are a later unit.
    Every call uses fresh random ids, so tests never share a pool.
    """
    import secrets
    from datetime import timedelta

    from config import db_conn
    from services import licensing_time

    def _make(
        pool_size,
        owner_identifier=TEST_USER,
        pool_status="active",
        subscription_status="active",
        subscription_expires_at="__default__",
    ):
        if subscription_expires_at == "__default__":
            subscription_expires_at = (licensing_time.now() + timedelta(days=365)).isoformat()
        account_id = secrets.token_hex(16)
        pool_id = secrets.token_hex(16)
        stamp = licensing_time.now_iso()
        db = db_conn()
        try:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO accounts (id, name, account_type, owner_identifier, "
                "subscription_status, subscription_expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, "Test School", "institution", owner_identifier,
                 subscription_status, subscription_expires_at, stamp, stamp),
            )
            cur.execute(
                "INSERT INTO seat_pools (id, account_id, pool_size, label, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pool_id, account_id, pool_size, "Lab", pool_status, stamp, stamp),
            )
            db.commit()
        finally:
            db.close()
        return {"account_id": account_id, "pool_id": pool_id}

    return _make
