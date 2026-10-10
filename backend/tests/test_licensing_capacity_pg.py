"""
tests/test_licensing_capacity_pg.py — spec §12 acceptance test 4, against
Postgres.

24 simultaneous claims against a 15-seat pool must grant exactly 15. SQLite
cannot run this test meaningfully: it serialises writers at the database
level and passes whether or not claim_capacity() locks anything.

The harness shape was measured, not assumed (spec §12 test 4):

- Each worker opens its connection BEFORE waiting on the barrier, so all 24
  are released together without connection-setup jitter staggering them.
  Unsynchronised threads still expose a missing lock, but with a different
  over-grant every run, so an exact assertion would be flaky. With the
  barrier, a missing lock grants all 24 every time and the lock grants
  exactly 15 every time.

- claim_capacity() opens its own connection through db_conn(). The test
  patches licensing_service.db_conn with a THREAD-LOCAL lookup, so each
  worker's claim runs on the connection that worker pre-opened. A single
  shared patched value would put every claim on one connection, serialise
  them, and pass for the wrong reason — so the test also records each
  worker's pg_backend_pid() and asserts there were 24 distinct backends.
  A thread that never set its connection raises AttributeError rather than
  quietly falling back to something shared.

Five iterations rather than one, as cheap insurance on a slower machine.
"""
import threading

import pytest
from fastapi import HTTPException

POOL_SIZE = 15
WORKERS = 24
ITERATIONS = 5


def _active_count(pool_id):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT COUNT(*) AS active FROM institution_seats "
            "WHERE seat_pool_id = ? AND revoked_at IS NULL",
            (pool_id,),
        )
        return int(cur.fetchone()["active"])
    finally:
        db.close()


def _run_simultaneous_claims(pool_id, monkeypatch):
    from db import get_db
    from services import licensing_service

    local = threading.local()
    monkeypatch.setattr(licensing_service, "db_conn", lambda: local.conn)

    barrier = threading.Barrier(WORKERS, timeout=30)
    lock = threading.Lock()
    results = {"granted": 0, "refused": 0, "errors": [], "pids": []}

    def worker(n):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT pg_backend_pid() AS pid")
            pid = cur.fetchone()["pid"]
            conn.commit()  # end the implicit transaction before the race
            local.conn = conn
            with lock:
                results["pids"].append(pid)

            barrier.wait()

            licensing_service.claim_capacity(
                pool_id,
                machine_fingerprint_hash=f"fp-{n}",
                fingerprint_confidence="strong",
                fingerprint_signals_json=None,
                machine_label=f"PC {n}",
                performed_by="owner@school.example",
                actor_role="institution_owner",
            )
            with lock:
                results["granted"] += 1
        except HTTPException as exc:
            with lock:
                if exc.status_code == 409:
                    results["refused"] += 1
                else:
                    results["errors"].append(f"HTTP {exc.status_code}: {exc.detail}")
        except Exception as exc:  # recorded and asserted on, never swallowed
            with lock:
                results["errors"].append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    return results


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_concurrent_claims_grant_exactly_pool_size(pg_database, make_pool, monkeypatch, iteration):
    pool = make_pool(POOL_SIZE)

    results = _run_simultaneous_claims(pool["pool_id"], monkeypatch)

    assert results["errors"] == []
    assert len(set(results["pids"])) == WORKERS, (
        f"workers shared connections: {len(set(results['pids']))} distinct "
        f"backends for {WORKERS} workers — the race was not real"
    )
    assert results["granted"] == POOL_SIZE
    assert results["refused"] == WORKERS - POOL_SIZE
    assert _active_count(pool["pool_id"]) == POOL_SIZE
