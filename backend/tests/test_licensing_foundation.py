"""
tests/test_licensing_foundation.py — the first licensing unit (spec §13
steps 2–5): licensing_time, the schema and its startup assertion,
require_account_owner(), and claim_capacity()'s non-concurrent behaviour.

The concurrency guarantee itself is test 4, in test_licensing_capacity_pg.py.
Tests here run on SQLite unless they are about a Postgres-specific fact
(column types, pg_indexes), in which case they use pg_database.
"""
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from conftest import OTHER_USER, TEST_USER


@pytest.fixture(autouse=True)
def _clean_clock(monkeypatch):
    import config
    from services import licensing_time

    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", False)
    licensing_time.reset_clock()
    yield
    licensing_time.reset_clock()


def _claim(pool_id, n=0, **overrides):
    from services.licensing_service import claim_capacity

    kwargs = dict(
        machine_fingerprint_hash=f"fp-{n}",
        fingerprint_confidence="strong",
        fingerprint_signals_json='{"system_uuid": "X"}',
        machine_label=f"PC {n}",
        performed_by="owner@school.example",
        actor_role="institution_owner",
    )
    kwargs.update(overrides)
    return claim_capacity(pool_id, **kwargs)


def _query(sql, params=()):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        return [dict(r) if not hasattr(r, "keys") else {k: r[k] for k in r.keys()} for r in cur.fetchall()]
    finally:
        db.close()


def _execute(sql, params=()):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# licensing_time
# ---------------------------------------------------------------------------

def test_to_datetime_normalises_both_engine_shapes():
    from services.licensing_time import to_datetime

    expected = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    assert to_datetime("2026-10-07T12:00:00+00:00") == expected      # isoformat() as written
    assert to_datetime("2026-10-07T12:00:00Z") == expected
    assert to_datetime("2026-10-07 12:00:00") == expected            # SQLite datetime('now'), naive = UTC
    assert to_datetime(datetime(2026, 10, 7, 12, 0)) == expected     # naive datetime = UTC
    assert to_datetime(expected) == expected                         # Postgres TIMESTAMPTZ
    plus_one = to_datetime("2026-10-07T13:00:00+01:00")
    assert plus_one == expected and plus_one.tzinfo == timezone.utc
    assert to_datetime(None) is None
    assert to_datetime("  ") is None


def test_to_datetime_refuses_to_guess():
    from services.licensing_time import to_datetime

    with pytest.raises(ValueError):
        to_datetime("next tuesday")
    with pytest.raises(TypeError):
        to_datetime(1696680000)


def test_is_expired_boundaries():
    from services.licensing_time import is_expired

    at = datetime(2026, 10, 7, tzinfo=timezone.utc)
    assert is_expired(None, at=at)                       # NULL = no entitlement
    assert is_expired("2026-10-06T00:00:00+00:00", at=at)
    assert is_expired(at, at=at)                         # expiry instant is expired
    assert not is_expired("2026-10-08T00:00:00+00:00", at=at)
    assert not is_expired(datetime(2026, 10, 8), at="2026-10-07T00:00:00Z")


def test_test_clock_is_ignored_unless_allowed(monkeypatch):
    import config
    from services import licensing_time

    licensing_time.set_clock_offset(timedelta(days=40))
    real = datetime.now(timezone.utc)
    assert abs(licensing_time.now() - real) < timedelta(seconds=5)

    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", True)
    assert licensing_time.now() - real > timedelta(days=39)
    assert licensing_time.to_datetime(licensing_time.now_iso()) - real > timedelta(days=39)


# ---------------------------------------------------------------------------
# is_paid_user — the SQLite TypeError this unit fixes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("delta_days, expected", [(30, True), (-1, False)])
def test_is_paid_user_reads_sqlite_text_paid_until(sqlite_database, delta_days, expected):
    from services.access_control import is_paid_user

    paid_until = (datetime.now(timezone.utc) + timedelta(days=delta_days)).isoformat()
    _execute(
        "INSERT INTO users (identifier, salt, pw_hash, is_paid, paid_until) VALUES (?, ?, ?, ?, ?)",
        ("paid.student@example.com", "s", "h", 1, paid_until),
    )
    # Before the fix this raised TypeError: str > datetime.
    assert is_paid_user({"sub": "paid.student@example.com"}) is expected


def test_is_paid_user_ignores_the_licensing_test_clock(sqlite_database, monkeypatch):
    import config
    from services import licensing_time
    from services.access_control import is_paid_user

    paid_until = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    _execute(
        "INSERT INTO users (identifier, salt, pw_hash, is_paid, paid_until) VALUES (?, ?, ?, ?, ?)",
        ("paid.student@example.com", "s", "h", 1, paid_until),
    )
    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", True)
    licensing_time.set_clock_offset(timedelta(days=365))
    assert is_paid_user({"sub": "paid.student@example.com"}) is True


# ---------------------------------------------------------------------------
# Schema and assert_licensing_constraints()
# ---------------------------------------------------------------------------

LICENSING_TABLES = ["accounts", "seat_pools", "institution_seats", "seat_activation_log", "licence_ambiguities"]


def test_sqlite_schema_and_required_index(sqlite_database):
    from db import LICENSING_REQUIRED_INDEX, LICENSING_TABLES as declared

    names = {r["name"] for r in _query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(LICENSING_TABLES) <= names
    for table, columns in declared:
        cols = {r["name"] for r in _query(f"PRAGMA table_info({table})")}
        assert cols == {name for name, _ in columns}, table
    idx = _query("SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", (LICENSING_REQUIRED_INDEX,))
    assert idx


def test_init_db_is_idempotent(sqlite_database):
    from db import init_db

    init_db(sqlite_database)
    init_db(sqlite_database)


def test_required_index_allows_one_active_binding_per_installation(sqlite_database, make_pool):
    pool = make_pool(5)
    insert = (
        "INSERT INTO institution_seats (id, seat_pool_id, account_id, machine_fingerprint_hash, "
        "installation_id, revoked_at) VALUES (?, ?, ?, ?, ?, ?)"
    )
    args = (pool["pool_id"], pool["account_id"], "fp")
    _execute(insert, ("b1", *args, "inst-1", "2026-10-01T00:00:00+00:00"))  # revoked
    _execute(insert, ("b2", *args, "inst-1", None))                         # active: allowed
    with pytest.raises(sqlite3.IntegrityError):
        _execute(insert, ("b3", *args, "inst-1", None))                     # second active: refused


def test_assert_licensing_constraints_raises_when_index_missing_sqlite(sqlite_database):
    from db import LicensingConstraintError, assert_licensing_constraints

    assert_licensing_constraints(sqlite_database)
    _execute("DROP INDEX ux_seat_bindings_active_installation")
    with pytest.raises(LicensingConstraintError, match="ux_seat_bindings_active_installation"):
        assert_licensing_constraints(sqlite_database)


def test_required_index_failure_closes_licensing_not_startup(tmp_path, monkeypatch, caplog):
    """The required index is not swallowed as a warning — but its failure is
    caught by init_db(), logged at ERROR and recorded, never raised (§4.E)."""
    import logging

    import db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "LICENSING_REQUIRED_INDEX_SQL", "CREATE UNIQUE INDEX broken ON no_such_table(x);")
    with caplog.at_level(logging.WARNING):
        db.init_db(str(tmp_path / "a.db"))  # does not raise

    status = db.licensing_status()
    assert status["ready"] is False
    assert "no_such_table" in status["reason"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "LICENSING UNAVAILABLE" in r.getMessage()]
    assert errors and errors[0].exc_info is not None


def test_init_db_runs_the_assertion_and_records_it(tmp_path, monkeypatch):
    import db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Index creation "succeeds" but creates nothing: only the assertion can catch it.
    monkeypatch.setattr(db, "LICENSING_REQUIRED_INDEX_SQL", "SELECT 1;")
    db.init_db(str(tmp_path / "b.db"))
    status = db.licensing_status()
    assert status["ready"] is False
    assert status["reason"].startswith("LicensingConstraintError")


def test_healthy_init_records_licensing_ready(sqlite_database):
    import db

    assert db.licensing_status() == {"ready": True, "reason": None}


def test_core_schema_failure_still_stops_startup(tmp_path, monkeypatch):
    import db

    monkeypatch.delenv("DATABASE_URL", raising=False)

    def broken_core(db_path=None):
        raise sqlite3.OperationalError("core schema broke")

    monkeypatch.setattr(db, "_init_db_sqlite", broken_core)
    with pytest.raises(sqlite3.OperationalError, match="core schema broke"):
        db.init_db(str(tmp_path / "c.db"))


def test_postgres_timestamp_columns_are_timestamptz(pg_database):
    from db import LICENSING_TABLES as declared

    rows = _query(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name IN (?, ?, ?, ?, ?)",
        tuple(LICENSING_TABLES),
    )
    types = {(r["table_name"], r["column_name"]): r["data_type"] for r in rows}
    for table, columns in declared:
        for name, _ in columns:
            expected = "timestamp with time zone" if name.endswith("_at") else None
            if expected:
                assert types[(table, name)] == expected, (table, name)
            else:
                assert types[(table, name)] != "timestamp with time zone", (table, name)
    assert types[("institution_seats", "lease_serial")] == "integer"


def test_assert_licensing_constraints_raises_when_index_missing_postgres(pg_database):
    from db import LicensingConstraintError, assert_licensing_constraints, init_db

    assert_licensing_constraints()
    try:
        _execute("DROP INDEX ux_seat_bindings_active_installation")
        with pytest.raises(LicensingConstraintError):
            assert_licensing_constraints()
    finally:
        init_db()  # puts the index back for every other test in ep_test
    assert_licensing_constraints()


# ---------------------------------------------------------------------------
# require_account_owner()
# ---------------------------------------------------------------------------

def test_owner_passes_and_other_user_is_refused(sqlite_database, make_pool):
    from services.access_control import require_account_owner

    pool = make_pool(5, owner_identifier=TEST_USER)
    assert require_account_owner({"sub": TEST_USER}, pool["account_id"]) == TEST_USER
    with pytest.raises(HTTPException) as exc:
        require_account_owner({"sub": OTHER_USER}, pool["account_id"])
    assert exc.value.status_code == 403


def test_owner_comparison_ignores_case_and_whitespace(sqlite_database, make_pool):
    from services.access_control import require_account_owner

    pool = make_pool(5, owner_identifier="  Test.Student@Example.com ")
    assert require_account_owner({"sub": TEST_USER}, pool["account_id"]) == TEST_USER


@pytest.mark.parametrize("user", [None, {}, {"sub": ""}])
def test_unauthenticated_is_refused(sqlite_database, make_pool, user):
    from services.access_control import require_account_owner

    pool = make_pool(5)
    with pytest.raises(HTTPException) as exc:
        require_account_owner(user, pool["account_id"])
    assert exc.value.status_code == 403


def test_unknown_account_is_403_not_404(sqlite_database):
    from services.access_control import require_account_owner

    with pytest.raises(HTTPException) as exc:
        require_account_owner({"sub": TEST_USER}, "no-such-account")
    assert exc.value.status_code == 403


def test_admin_passes_on_any_account(sqlite_database, make_pool, monkeypatch):
    from services import access_control

    monkeypatch.setattr(access_control, "ADMIN_IDENTIFIERS", frozenset({"boss@exampartner.example"}))
    pool = make_pool(5, owner_identifier=TEST_USER)
    assert access_control.require_account_owner(
        {"sub": "boss@exampartner.example"}, pool["account_id"]
    ) == "boss@exampartner.example"


# ---------------------------------------------------------------------------
# claim_capacity() — everything except the race (that is test 4)
# ---------------------------------------------------------------------------

def test_claim_signature_rulings():
    """Brief §4.D: no installation_id parameter; performed_by and actor_role
    required with no defaults."""
    from services.licensing_service import claim_capacity

    params = inspect.signature(claim_capacity).parameters
    assert "installation_id" not in params
    for name in ("performed_by", "actor_role"):
        assert params[name].default is inspect.Parameter.empty
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_claim_creates_binding_and_audit_row(sqlite_database, make_pool):
    pool = make_pool(2)
    result = _claim(pool["pool_id"], n=7)

    assert result["ok"] is True
    assert result["account_id"] == pool["account_id"]
    assert result["active_count"] == 1
    assert len(result["installation_id"]) == 32  # server-generated token_hex(16)

    [binding] = _query("SELECT * FROM institution_seats WHERE id = ?", (result["binding_id"],))
    assert binding["installation_id"] == result["installation_id"]
    assert binding["account_id"] == pool["account_id"]
    assert binding["revoked_at"] is None
    assert binding["fingerprint_confidence"] == "strong"
    assert binding["lease_serial"] == 0

    [log] = _query("SELECT * FROM seat_activation_log WHERE binding_id = ?", (result["binding_id"],))
    assert log["action"] == "activate"
    assert log["performed_by"] == "owner@school.example"
    assert log["actor_role"] == "institution_owner"
    assert log["installation_id"] == result["installation_id"]
    # The log's own snapshots, so history survives capacity reuse.
    assert log["machine_fingerprint_hash"] == "fp-7"
    assert log["machine_label"] == "PC 7"
    assert log["fingerprint_signals_json"] == '{"system_uuid": "X"}'

    [p] = _query("SELECT last_claim_at FROM seat_pools WHERE id = ?", (pool["pool_id"],))
    assert p["last_claim_at"] is not None


def test_each_claim_gets_a_fresh_installation_id(sqlite_database, make_pool):
    pool = make_pool(3)
    ids = {_claim(pool["pool_id"], n=n)["installation_id"] for n in range(3)}
    assert len(ids) == 3


def test_full_pool_refuses_without_eviction_or_side_effects(sqlite_database, make_pool):
    pool = make_pool(2)
    _claim(pool["pool_id"], n=1)
    _claim(pool["pool_id"], n=2)
    [before] = _query("SELECT last_claim_at FROM seat_pools WHERE id = ?", (pool["pool_id"],))

    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"], n=3)
    assert exc.value.status_code == 409

    active = _query("SELECT id FROM institution_seats WHERE seat_pool_id = ? AND revoked_at IS NULL", (pool["pool_id"],))
    assert len(active) == 2
    # A refused claim is discarded entirely, lock write included.
    [after] = _query("SELECT last_claim_at FROM seat_pools WHERE id = ?", (pool["pool_id"],))
    assert after["last_claim_at"] == before["last_claim_at"]
    assert len(_query("SELECT id FROM seat_activation_log WHERE seat_pool_id = ?", (pool["pool_id"],))) == 2


def test_over_capacity_after_shrink_is_slot_agnostic(sqlite_database, make_pool):
    """The test-5 shape, at the capacity layer: shrink below active, release
    whichever machines, and claims resume only strictly below pool_size."""
    pool = make_pool(4)
    bindings = [_claim(pool["pool_id"], n=n)["binding_id"] for n in range(4)]
    _execute("UPDATE seat_pools SET pool_size = 2 WHERE id = ?", (pool["pool_id"],))

    def release(binding_id):
        _execute("UPDATE institution_seats SET revoked_at = ? WHERE id = ?", ("2026-10-07T00:00:00+00:00", binding_id))

    release(bindings[0])
    release(bindings[3])  # 2 active == pool_size: still refused
    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"], n=10)
    assert exc.value.status_code == 409

    release(bindings[1])  # 1 active: exactly one claim granted
    assert _claim(pool["pool_id"], n=11)["active_count"] == 2
    with pytest.raises(HTTPException):
        _claim(pool["pool_id"], n=12)


def test_missing_pool_is_404(sqlite_database):
    with pytest.raises(HTTPException) as exc:
        _claim("no-such-pool")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("pool_kwargs, status", [
    ({"pool_status": "suspended"}, 409),
    ({"subscription_status": "suspended"}, 402),
    ({"subscription_status": "expired"}, 402),
    ({"subscription_expires_at": None}, 402),                              # no entitlement yet
    ({"subscription_expires_at": "2020-01-01T00:00:00+00:00"}, 402),      # status says active, date says no
])
def test_entitlement_is_rechecked_inside_the_claim(sqlite_database, make_pool, pool_kwargs, status):
    pool = make_pool(5, **pool_kwargs)
    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"])
    assert exc.value.status_code == status
    assert _query("SELECT id FROM institution_seats WHERE seat_pool_id = ?", (pool["pool_id"],)) == []
    assert _query("SELECT id FROM seat_activation_log WHERE seat_pool_id = ?", (pool["pool_id"],)) == []


def test_subscription_expiry_follows_the_licensing_clock(sqlite_database, make_pool, monkeypatch):
    import config
    from services import licensing_time

    soon = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    pool = make_pool(5, subscription_expires_at=soon)
    _claim(pool["pool_id"], n=1)

    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", True)
    licensing_time.set_clock_offset(timedelta(days=11))
    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"], n=2)
    assert exc.value.status_code == 402


@pytest.mark.parametrize("overrides", [
    {"actor_role": "client"},                  # a lease holder cannot activate (spec §9)
    {"actor_role": "someone"},
    {"performed_by": "  "},
    {"fingerprint_confidence": "certain"},
    {"machine_fingerprint_hash": ""},
    {"fingerprint_signals_json": {"not": "a string"}},
])
def test_invalid_claim_inputs_are_refused_before_touching_the_pool(sqlite_database, make_pool, overrides):
    pool = make_pool(5)
    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"], **overrides)
    assert exc.value.status_code == 400
    [p] = _query("SELECT last_claim_at FROM seat_pools WHERE id = ?", (pool["pool_id"],))
    assert p["last_claim_at"] is None


@pytest.mark.parametrize("pool_kwargs, status, detail", [
    ({"subscription_status": "suspended"}, 402, "Subscription is not active"),
    ({"subscription_expires_at": "2020-01-01T00:00:00+00:00"}, 402, "Subscription is not active"),
    ({"pool_status": "suspended"}, 409, "Seat pool is not active"),
])
def test_entitlement_is_reported_before_capacity(sqlite_database, make_pool, pool_kwargs, status, detail):
    """Brief §4.E point 5: a FULL pool whose entitlement has lapsed is told
    about the entitlement, not told to free a seat."""
    pool = make_pool(1, **pool_kwargs)
    _execute(
        "INSERT INTO institution_seats (id, seat_pool_id, account_id, machine_fingerprint_hash, installation_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("existing", pool["pool_id"], pool["account_id"], "fp", "inst-existing"),
    )
    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"])
    assert (exc.value.status_code, exc.value.detail) == (status, detail)


def test_claim_checks_the_index_live_not_the_startup_flag(sqlite_database, make_pool):
    """Brief §4.E point 2: init_db() recorded licensing ready, then the index
    disappeared. The claim must notice on its own and write nothing."""
    import db

    pool = make_pool(5)
    assert db.licensing_status()["ready"] is True
    _execute("DROP INDEX ux_seat_bindings_active_installation")

    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"])
    assert (exc.value.status_code, exc.value.detail) == (503, "Licensing is unavailable")
    assert _query("SELECT id FROM institution_seats WHERE seat_pool_id = ?", (pool["pool_id"],)) == []
    assert _query("SELECT id FROM seat_activation_log WHERE seat_pool_id = ?", (pool["pool_id"],)) == []
    [p] = _query("SELECT last_claim_at FROM seat_pools WHERE id = ?", (pool["pool_id"],))
    assert p["last_claim_at"] is None


def test_claim_on_postgres_round_trips_timestamps(pg_database, make_pool):
    from services.licensing_time import to_datetime

    pool = make_pool(2)
    result = _claim(pool["pool_id"])
    [binding] = _query("SELECT activated_at, created_at FROM institution_seats WHERE id = ?", (result["binding_id"],))
    assert isinstance(binding["activated_at"], datetime)  # TIMESTAMPTZ reads back as datetime
    assert to_datetime(binding["activated_at"]) == to_datetime(binding["created_at"])


def test_entitlement_recheck_on_postgres(pg_database, make_pool):
    pool = make_pool(2, subscription_expires_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(HTTPException) as exc:
        _claim(pool["pool_id"])
    assert exc.value.status_code == 402
