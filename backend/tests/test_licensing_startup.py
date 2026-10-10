"""
tests/test_licensing_startup.py — spec §12 acceptance test 31, as replaced
on 8 October 2026 (brief §4.E): a licensing schema failure closes licensing,
never the backend.

The old test 31 ("drop the index, restart, backend fails to start") could
not reproduce a startup failure at all: init_db() runs CREATE UNIQUE INDEX
IF NOT EXISTS again and simply recreates a dropped index. The realistic
failure is the index REFUSING to build because existing data violates it —
two active bindings sharing one installation_id. That is what this sets up.

Runs on Postgres (production's engine) and SQLite. Startup is driven through
the real app: `with TestClient(app)` runs the startup event, which calls
init_db() against the broken database — so "the backend still starts" is
observed, not inferred.
"""
import logging
import secrets

import pytest
from fastapi import HTTPException


def _query(sql, params=()):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        return [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
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


def _claim(pool_id):
    from services.licensing_service import claim_capacity

    return claim_capacity(
        pool_id,
        machine_fingerprint_hash="fp-new",
        fingerprint_confidence="strong",
        performed_by="owner@school.example",
        actor_role="institution_owner",
    )


@pytest.mark.parametrize("engine", ["pg_database", "sqlite_database"])
def test_31_licensing_failure_closes_licensing_not_the_backend(request, engine, make_pool, caplog):
    import db
    from fastapi.testclient import TestClient

    from app import app

    request.getfixturevalue(engine)
    pool = make_pool(5)
    shared_installation = secrets.token_hex(16)

    try:
        # --- break it the realistic way --------------------------------
        _execute("DROP INDEX ux_seat_bindings_active_installation")
        for binding_id in ("dup-a-" + shared_installation[:8], "dup-b-" + shared_installation[:8]):
            _execute(
                "INSERT INTO institution_seats (id, seat_pool_id, account_id, "
                "machine_fingerprint_hash, installation_id) VALUES (?, ?, ?, ?, ?)",
                (binding_id, pool["pool_id"], pool["account_id"], "fp-dup", shared_installation),
            )

        # --- restart: the backend starts --------------------------------
        with caplog.at_level(logging.WARNING):
            with TestClient(app) as client:  # runs the startup event -> init_db()
                health = client.get("/health")

        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.json()["licensing"] == "unavailable"

        status = db.licensing_status()
        assert status["ready"] is False
        assert status["reason"]
        assert any(
            r.levelno == logging.ERROR and "LICENSING UNAVAILABLE" in r.getMessage()
            for r in caplog.records
        ), "the failure must be logged at ERROR, not as a skipped-index warning"

        # Core schema untouched and usable.
        assert _query("SELECT COUNT(*) AS c FROM users")[0]["c"] >= 0

        # --- licensing is closed at the chokepoint, and writes nothing --
        before_bindings = _query("SELECT id FROM institution_seats WHERE seat_pool_id = ?", (pool["pool_id"],))
        with pytest.raises(HTTPException) as exc:
            _claim(pool["pool_id"])
        assert (exc.value.status_code, exc.value.detail) == (503, "Licensing is unavailable")
        assert _query("SELECT id FROM institution_seats WHERE seat_pool_id = ?", (pool["pool_id"],)) == before_bindings
        assert _query("SELECT id FROM seat_activation_log WHERE seat_pool_id = ?", (pool["pool_id"],)) == []

        # --- repair: revoke one duplicate, init again -------------------
        _execute(
            "UPDATE institution_seats SET revoked_at = ? WHERE id = ?",
            ("2026-10-08T00:00:00+00:00", "dup-b-" + shared_installation[:8]),
        )
        db.init_db(None if engine == "pg_database" else request.getfixturevalue("sqlite_database"))

        assert db.licensing_status() == {"ready": True, "reason": None}
        db.assert_licensing_constraints(None if engine == "pg_database" else request.getfixturevalue("sqlite_database"))
        with TestClient(app) as client:
            assert client.get("/health").json()["licensing"] == "ready"

        result = _claim(pool["pool_id"])
        assert result["ok"] is True and result["active_count"] == 2
    finally:
        # The Postgres database is shared by every PG test: whatever happened
        # above, leave no active duplicate behind and the index rebuilt.
        _execute(
            "UPDATE institution_seats SET revoked_at = ? WHERE installation_id = ? AND revoked_at IS NULL",
            ("2026-10-08T00:00:00+00:00", shared_installation),
        )
        db.init_db(None if engine == "pg_database" else request.getfixturevalue("sqlite_database"))
        db.assert_licensing_constraints(None if engine == "pg_database" else request.getfixturevalue("sqlite_database"))


_LICENSING_TABLE_NAMES = ["licence_ambiguities", "seat_activation_log", "institution_seats", "seat_pools", "accounts"]


@pytest.mark.parametrize("engine", ["pg_database", "sqlite_database"])
def test_missing_licensing_tables_refuse_with_503_not_500(request, engine):
    """The licensing step can fail before it creates any table. Then the
    lock UPDATE in claim_capacity() would be the first statement to touch
    seat_pools and fail as a database error (a 500). The early index check,
    which needs no licensing table, must answer first with a clean 503."""
    import db

    fixture_value = request.getfixturevalue(engine)
    db_path = None if engine == "pg_database" else fixture_value

    try:
        for table in _LICENSING_TABLE_NAMES:
            _execute(f"DROP TABLE IF EXISTS {table}")

        with pytest.raises(HTTPException) as exc:
            _claim("any-pool-id")
        assert (exc.value.status_code, exc.value.detail) == (503, "Licensing is unavailable")

        # Wrote nothing: the tables are still absent (no statement recreated
        # or wrote to them), and core schema is untouched.
        if engine == "pg_database":
            rows = _query(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name IN (?, ?, ?, ?, ?)",
                tuple(_LICENSING_TABLE_NAMES),
            )
        else:
            rows = _query(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?, ?, ?)",
                tuple(_LICENSING_TABLE_NAMES),
            )
        assert rows == []
        assert _query("SELECT COUNT(*) AS c FROM users")[0]["c"] >= 0
    finally:
        # Recreate the licensing schema — ep_test is shared by every PG test.
        db.init_db(db_path)
        db.assert_licensing_constraints(db_path)
        assert db.licensing_status() == {"ready": True, "reason": None}
