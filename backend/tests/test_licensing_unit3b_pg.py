"""
tests/test_licensing_unit3b_pg.py — Unit 3b's race guards against Postgres
(brief §4.K K3, K5, K6; §4.L L3; §5).

Same seams as test_licensing_unit3a_pg.py: a barrier before the function
that takes the lock, so both requests have passed their early, unlocked
history check before either locks; and pause_after() around
licence_history.lookup_locked(), the last check each locked path makes
before it writes, so removing a lock makes both requests write, every run.

| Test                                            | Guard                                  |
|-------------------------------------------------|----------------------------------------|
| rename concurrent with release                  | binding lock (_lock_binding in rename) |
| two renames, one request_id, two labels         | binding lock + locked history re-check |
| shrink 20 → 15 concurrent with 24 claims        | claim_capacity()'s OWN pool lock (L3)  |
| two pool edits, one request_id, two bodies      | pool lock in _update_pool_locked       |
| two pool edits, one field in common             | pool lock: each `from` is the true one |
| two account edits, one request_id, two bodies   | account lock in _update_account_locked |
| two account edits, one field in common          | account lock: each `from` is true      |
| rename and the admin edits without the history  | the history fails closed (I4)          |

On the `from` tests (L3). Planning's wording is "two edits to different
fields". Each edit's UPDATE writes only the columns it changes, so two edits
whose fields are wholly DISJOINT cannot produce a stale `from` even with the
lock removed — nothing would fail, and the test would prove nothing. The
two edits here therefore differ in their fields but share one; that shared
field is where an unlocked read-then-write logs the same `from` twice. The
disjoint fields are asserted too, as surviving both edits.

Each guard was also removed by hand to show its test fails; see the PR.
"""
import json
import threading
import uuid

import pytest
from fastapi import HTTPException

from licensing_harness import (  # noqa: F401  (pg_lic is a fixture)
    assert_error, at_once, barrier_before, binding, log_rows, pause_after, pg_lic, query, register_user,
)

ITERATIONS = 3


def _seams(monkeypatch, name):
    from services import licence_history, licensing_service

    before = barrier_before(monkeypatch, licensing_service, name)
    pause_after(monkeypatch, licence_history, "lookup_locked")
    return before


def _chain(rows, field, initial, final):
    """
    The `from` → `to` transitions logged for `field` must form ONE chain from
    its initial value to its final value: each row's `from` is the value the
    previous change left. Two rows with the same `from` is the lost read.
    """
    steps = []
    for row in rows:
        detail = json.loads(row["detail_json"])
        before, after = detail["from"], detail["to"]
        if isinstance(before, dict):
            if field not in before:
                continue
            before, after = before[field], after[field]
        steps.append((before, after))
    value = initial
    remaining = list(steps)
    while remaining:
        step = next((s for s in remaining if s[0] == value), None)
        assert step is not None, f"{field}: no logged change starts from {value!r}; logged {steps}"
        remaining.remove(step)
        value = step[1]
    assert value == final, f"{field}: the chain ends at {value!r}, the row holds {final!r}; logged {steps}"


# ===========================================================================
# K3 — rename
# ===========================================================================

@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_rename_concurrent_with_release_never_renames_a_released_computer(pg_lic, make_pool, monkeypatch,
                                                                          iteration):
    from services import licence_history, licensing_service

    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first = pg_lic.activated(pool["pool_id"])
    barrier = threading.Barrier(2, timeout=20)
    arrivals = []
    for name in ("_rename_locked", "_release_locked"):
        original = getattr(licensing_service, name)

        def wrapper(*args, _original=original, **kwargs):
            arrivals.append(1)
            barrier.wait()
            return _original(*args, **kwargs)

        monkeypatch.setattr(licensing_service, name, wrapper)
    pause_after(monkeypatch, licence_history, "lookup_locked")

    rename, release = at_once(lambda: pg_lic.rename(first["binding_id"], "RENAMED"),
                              lambda: pg_lic.release(first["binding_id"]))
    assert len(arrivals) == 2, "both requests must reach their locked step for the race to be real"
    assert release.status_code == 200 and release.json()["result"] == "released", release.text
    (release_row,) = log_rows(pool["account_id"], "release")
    row = binding(first["binding_id"])
    if rename.status_code == 200:
        # Renamed THEN released: the release, serialised behind the rename,
        # snapshots the new label.
        assert rename.json()["result"] == "renamed"
        assert release_row["machine_label"] == "RENAMED"
        assert row["machine_label"] == "RENAMED"
    else:
        # Released first: the rename sees it under the lock.
        assert_error(rename, 409, "binding_released")
        assert row["machine_label"] == "LAB-PC-07"
        assert log_rows(pool["account_id"], "rename") == []
    assert row["revoked_at"] is not None


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_renames_with_one_request_id_and_two_labels(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first = pg_lic.activated(pool["pool_id"])
    before = _seams(monkeypatch, "_rename_locked")
    request_id = str(uuid.uuid4())
    one, two = at_once(lambda: pg_lic.rename(first["binding_id"], "LABEL-ONE", request_id=request_id),
                       lambda: pg_lic.rename(first["binding_id"], "LABEL-TWO", request_id=request_id))
    assert len(before) == 2
    assert sorted([one.status_code, two.status_code]) == [200, 409], (one.text, two.text)
    winner, loser = (one, two) if one.status_code == 200 else (two, one)
    assert winner.json()["result"] == "renamed"
    assert_error(loser, 409, "request_id_reused")
    assert binding(first["binding_id"])["machine_label"] == winner.json()["machine_label"]
    assert len(log_rows(pool["account_id"], "rename")) == 1
    assert len(query("SELECT id FROM licence_requests WHERE request_id = ?", (request_id,))) == 1


# ===========================================================================
# K6 — pool editing
# ===========================================================================

WORKERS = 24


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_shrink_concurrent_with_24_claims_never_grants_beyond_the_size_in_force(pg_lic, make_pool, monkeypatch,
                                                                                iteration):
    """
    A CAPACITY test, not the resize lock's guard test (L3). What carries it is
    claim_capacity()'s own pool lock: a claim reads pool_size after taking
    that lock, and any UPDATE of pool_size waits behind a claim holding it,
    so every claim sees the size in force when it commits. Spec test 6
    covers removing that lock; removing the RESIZE's lock leaves this test
    passing, which is why its guard tests are the two below.
    """
    from db import get_db
    from services import licensing_service
    from services.licence_history import Entry

    pool = make_pool(20, owner_identifier=pg_lic.owner)
    local = threading.local()
    # Each claim worker runs on its own pre-opened connection (brief §4.D);
    # the resize thread opens fresh ones, because it uses several.
    monkeypatch.setattr(licensing_service, "db_conn",
                        lambda: local.conn if getattr(local, "conn", None) is not None else get_db())
    barrier = threading.Barrier(WORKERS + 1, timeout=30)
    lock = threading.Lock()
    results = {"grants": [], "refused": [], "errors": [], "pids": [], "resize": None}

    def claim(n):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT pg_backend_pid() AS pid")
            pid = cur.fetchone()["pid"]
            conn.commit()
            local.conn = conn
            with lock:
                results["pids"].append(pid)
            barrier.wait()
            request_id = f"shrink-{n}-{pool['pool_id']}"
            granted = licensing_service.claim_capacity(
                pool["pool_id"], request_id=request_id, request_fp=f"digest-{n}",
                history=Entry(request_id, "activate", f"digest-{n}"), machine_fingerprint_hash=f"fp-{n}",
                fingerprint_confidence="strong", fingerprint_signals_json=None, machine_label=f"PC {n}",
                performed_by=pg_lic.owner, actor_role="institution_owner")
            with lock:
                results["grants"].append((granted["active_count"], granted["pool_size"]))
        except HTTPException as exc:
            with lock:
                (results["refused"] if exc.status_code == 409 else results["errors"]).append(exc.detail)
        except Exception as exc:     # recorded and asserted on, never swallowed
            with lock:
                results["errors"].append(f"{type(exc).__name__}: {exc}")

    def resize():
        try:
            barrier.wait()
            results["resize"] = licensing_service.update_pool(
                pg_lic.admin, pool["pool_id"], {"request_id": str(uuid.uuid4()), "pool_size": 15})
        except Exception as exc:     # recorded and asserted on, never swallowed
            with lock:
                results["errors"].append(f"resize {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(WORKERS)]
    threads.append(threading.Thread(target=resize))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a worker hung"

    assert results["errors"] == []
    assert len(set(results["pids"])) == WORKERS, "claim workers shared connections — the race was not real"
    assert all(d["code"] == "pool_full" for d in results["refused"]), results["refused"]
    assert len(results["grants"]) + len(results["refused"]) == WORKERS
    for active_after, size_in_force in results["grants"]:
        assert size_in_force in (20, 15)
        assert active_after <= size_in_force, results["grants"]
    active = query("SELECT COUNT(*) AS n FROM institution_seats WHERE seat_pool_id = ? AND revoked_at IS NULL",
                   (pool["pool_id"],))[0]["n"]
    assert 15 <= active <= 20
    assert active == len(results["grants"])
    final = results["resize"]
    assert final["pool_size"] == 15
    assert query("SELECT pool_size FROM seat_pools WHERE id = ?", (pool["pool_id"],))[0]["pool_size"] == 15


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_pool_edits_with_one_request_id_and_two_bodies(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(20, owner_identifier=pg_lic.owner)
    before = _seams(monkeypatch, "_update_pool_locked")
    request_id = str(uuid.uuid4())
    one, two = at_once(lambda: pg_lic.edit_pool(pool["pool_id"], request_id=request_id, pool_size=25),
                       lambda: pg_lic.edit_pool(pool["pool_id"], request_id=request_id, pool_size=30))
    assert len(before) == 2
    assert sorted([one.status_code, two.status_code]) == [200, 409], (one.text, two.text)
    winner, loser = (one, two) if one.status_code == 200 else (two, one)
    assert_error(loser, 409, "request_id_reused")
    assert query("SELECT pool_size FROM seat_pools WHERE id = ?",
                 (pool["pool_id"],))[0]["pool_size"] == winner.json()["pool_size"]
    assert len(log_rows(pool["account_id"], "resize")) == 1
    assert len(query("SELECT id FROM licence_requests WHERE request_id = ?", (request_id,))) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_pool_edits_each_log_the_true_previous_value(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(20, owner_identifier=pg_lic.owner)
    before = _seams(monkeypatch, "_update_pool_locked")
    one, two = at_once(lambda: pg_lic.edit_pool(pool["pool_id"], pool_size=25, label="Annex"),
                       lambda: pg_lic.edit_pool(pool["pool_id"], pool_size=30, status="suspended"))
    assert len(before) == 2
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    row = query("SELECT * FROM seat_pools WHERE id = ?", (pool["pool_id"],))[0]
    # The disjoint fields both survive...
    assert (row["label"], row["status"]) == ("Annex", "suspended")
    # ...and every logged `from` is the value the previous change left.
    resize = log_rows(pool["account_id"], "resize")
    status = log_rows(pool["account_id"], "pool_status")
    assert len(resize) == 2 and len(status) == 1
    _chain(resize, "pool_size", 20, row["pool_size"])
    _chain(resize, "label", "Lab", row["label"])
    _chain(status, "status", "active", row["status"])


# ===========================================================================
# K5 — account editing
# ===========================================================================

@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_account_edits_with_one_request_id_and_two_bodies(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    before = _seams(monkeypatch, "_update_account_locked")
    request_id = str(uuid.uuid4())
    one, two = at_once(lambda: pg_lic.edit_account(pool["account_id"], request_id=request_id, name="One"),
                       lambda: pg_lic.edit_account(pool["account_id"], request_id=request_id, name="Two"))
    assert len(before) == 2
    assert sorted([one.status_code, two.status_code]) == [200, 409], (one.text, two.text)
    winner, loser = (one, two) if one.status_code == 200 else (two, one)
    assert_error(loser, 409, "request_id_reused")
    assert query("SELECT name FROM accounts WHERE id = ?",
                 (pool["account_id"],))[0]["name"] == winner.json()["account"]["name"]
    assert len(log_rows(pool["account_id"], "account_update")) == 1
    assert len(query("SELECT id FROM licence_requests WHERE request_id = ?", (request_id,))) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_account_edits_each_log_the_true_previous_value(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    new_owner = f"next-{uuid.uuid4().hex[:12]}@school.example"
    register_user(new_owner)
    before = _seams(monkeypatch, "_update_account_locked")
    one, two = at_once(
        lambda: pg_lic.edit_account(pool["account_id"], name="Name One", contact_email="it@one.example"),
        lambda: pg_lic.edit_account(pool["account_id"], name="Name Two", owner_identifier=new_owner))
    assert len(before) == 2
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    row = query("SELECT * FROM accounts WHERE id = ?", (pool["account_id"],))[0]
    assert (row["contact_email"], row["owner_identifier"]) == ("it@one.example", new_owner)
    rows = log_rows(pool["account_id"], "account_update")
    assert len(rows) == 2
    _chain(rows, "name", "Test School", row["name"])
    _chain(rows, "contact_email", None, row["contact_email"])
    _chain(rows, "owner_identifier", pg_lic.owner, row["owner_identifier"])


# ===========================================================================
# The history fails closed for the new operations too (I4) — Postgres, and SQLite
# ===========================================================================

@pytest.fixture(params=["postgres", "sqlite"])
def any_lic(request, licence_signing_key):
    """pg_lic on Postgres; on SQLite, the same calls on a fresh file."""
    if request.param == "postgres":
        return request.getfixturevalue("pg_lic")
    from fastapi.testclient import TestClient

    from app import app
    from licensing_harness import Licensing

    request.getfixturevalue("sqlite_database")
    tag = uuid.uuid4().hex[:12]
    owner, admin = f"owner-{tag}@school.example", f"admin-{tag}@exampartner.example"
    register_user(owner)
    register_user(admin, is_admin=True)
    return Licensing(TestClient(app), owner=owner, admin=admin)


def test_rename_and_the_admin_edits_fail_closed_without_the_history(any_lic, make_pool):
    from licensing_harness import execute

    lic = any_lic
    pool = make_pool(5, owner_identifier=lic.owner)
    first = lic.activated(pool["pool_id"])
    calls = {
        "rename": lambda: lic.rename(first["binding_id"], "RENAMED"),
        "account edit": lambda: lic.edit_account(pool["account_id"], name="Renamed School"),
        "pool edit": lambda: lic.edit_pool(pool["pool_id"], pool_size=9, status="suspended"),
    }

    def snapshot():
        return (query("SELECT * FROM institution_seats WHERE seat_pool_id = ?", (pool["pool_id"],)),
                query("SELECT * FROM accounts WHERE id = ?", (pool["account_id"],)),
                query("SELECT * FROM seat_pools WHERE id = ?", (pool["pool_id"],)),
                query("SELECT id FROM seat_activation_log WHERE account_id = ? ORDER BY id", (pool["account_id"],)))

    before = snapshot()
    execute("ALTER TABLE licence_requests RENAME TO licence_requests_hidden")
    try:
        for name, call in calls.items():
            assert_error(call(), 503, "licensing_unavailable"), name
        assert snapshot() == before
        # The reads take no history, and the rest of the backend is untouched.
        assert lic.pools(pool["account_id"]).status_code == 200
        assert lic.client.get("/content/version").status_code == 200
    finally:
        execute("ALTER TABLE licence_requests_hidden RENAME TO licence_requests")

    for name, call in calls.items():
        response = call()
        assert response.status_code == 200, (name, response.text)
