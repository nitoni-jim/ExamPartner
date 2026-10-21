"""
tests/test_licensing_unit3a_pg.py — Unit 3a's race guards against Postgres,
and the request history failing closed (brief §4.I I4–I7, §4.J J8–J9, §5).

Every race test uses two seams from licensing_harness:

- barrier_before() around the function that holds the lock, so both
  requests have passed their early, unlocked history check before either
  takes the lock;
- pause_after() around licence_history.lookup_locked(), the last check
  each locked path makes before it writes, so removing the lock makes both
  requests write, every run, rather than some runs (brief §9 R2).

Each guard was also removed by hand to show its test fails; see the PR.
SQLite would pass all of these regardless: it serialises writers.

| Test                                         | Guard                                   |
|----------------------------------------------|-----------------------------------------|
| two refreshes, one request_id                | binding lock + locked history re-check  |
| two refreshes, two request_ids               | binding lock + serial re-read under it  |
| two collections of one resolved ambiguity    | ambiguity-row lock                      |
| two resolves (recognize / treat_as_new)      | ambiguity-row lock; pool lock for claims|
| release concurrent with refresh              | refresh's revoked re-check under lock   |
| J8: stored pool_full vs a retry              | outcome committed under the pool lock   |
| J9: two releases of a released binding       | binding lock taken whatever revoked_at  |
"""
import threading
import uuid

import pytest

from conftest import make_degenerate_signals, make_signals
from licensing_harness import (  # noqa: F401  (pg_lic is a fixture)
    PC_B, active_bindings, all_bindings, ambiguity, assert_error, at_once, barrier_before, binding, execute,
    lease_payload, lock_waiters, log_rows, pause_after, pg_lic, query, raise_activation_ambiguity,
    raise_refresh_ambiguity,
)

ITERATIONS = 3


def _seams(monkeypatch, name):
    from services import licence_history, licensing_service

    before = barrier_before(monkeypatch, licensing_service, name)
    after = pause_after(monkeypatch, licence_history, "lookup_locked")
    return before, after


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_refreshes_with_one_request_id_issue_one_lease(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first = pg_lic.activated(pool["pool_id"])
    before, _ = _seams(monkeypatch, "_issue_refresh")
    request_id = str(uuid.uuid4())
    one, two = at_once(lambda: pg_lic.refresh(first["lease"], request_id=request_id),
                       lambda: pg_lic.refresh(first["lease"], request_id=request_id))
    assert len(before) == 2, "both requests must reach the locked issue for the race to be real"
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    assert one.json()["lease"] == two.json()["lease"]
    assert binding(first["binding_id"])["lease_serial"] == 2
    # ONE issue, not two that happen to look alike. Two unguarded issues of
    # one request compute the same serial in the same second, and Ed25519 is
    # deterministic, so their leases are byte-identical and the asserts above
    # cannot tell them apart; the history can — each issue records itself.
    assert len(query("SELECT id FROM licence_requests WHERE request_id = ?", (request_id,))) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_refreshes_with_different_request_ids_get_consecutive_serials(pg_lic, make_pool, monkeypatch,
                                                                         iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first = pg_lic.activated(pool["pool_id"])
    _seams(monkeypatch, "_issue_refresh")
    one, two = at_once(lambda: pg_lic.refresh(first["lease"]), lambda: pg_lic.refresh(first["lease"]))
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    serials = sorted(lease_payload(r.json()["lease"])["lease_serial"] for r in (one, two))
    assert serials == [2, 3]
    assert binding(first["binding_id"])["lease_serial"] == 3
    # The second presented a serial the first had already overtaken (J6).
    assert len(log_rows(pool["account_id"], "stale_serial_flagged")) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_a_release_during_a_refresh_wins(pg_lic, make_pool, monkeypatch, iteration):
    """The refresh decided before the release committed; its locked re-check must still see it."""
    from services import licensing_service

    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first = pg_lic.activated(pool["pool_id"])
    decided, go = threading.Event(), threading.Event()
    original = licensing_service._issue_refresh

    def held(*args, **kwargs):
        decided.set()
        assert go.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(licensing_service, "_issue_refresh", held)

    def release_then_go():
        assert decided.wait(20)
        response = pg_lic.release(first["binding_id"])
        go.set()
        return response

    refresh, release = at_once(lambda: pg_lic.refresh(first["lease"]), release_then_go)
    assert release.status_code == 200, release.text
    assert_error(refresh, 403, "lease_revoked")
    row = binding(first["binding_id"])
    assert row["lease_serial"] == 1 and row["current_lease"] == first["lease"]


# ---------------------------------------------------------------------------
# Collection and resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_collections_of_one_resolved_ambiguity_issue_one_lease(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(pg_lic, pool["pool_id"])
    pg_lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    token = pg_lic.token()
    before, _ = _seams(monkeypatch, "_collect_resolved")
    one, two = at_once(lambda: pg_lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, token=token),
                       lambda: pg_lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, token=token))
    assert len(before) == 2
    codes = sorted([one.status_code, two.status_code])
    assert codes == [200, 409], (one.text, two.text)
    loser = one if one.status_code == 409 else two
    assert_error(loser, 409, "ambiguity_consumed")
    assert len(log_rows(pool["account_id"], "restore")) == 1
    assert ambiguity(ambiguity_id)["status"] == "consumed"


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_refresh_collections_of_one_resolved_ambiguity_issue_one_lease(pg_lic, make_pool, monkeypatch,
                                                                          iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first, presented, _, ambiguity_id = raise_refresh_ambiguity(pg_lic, pool["pool_id"])
    pg_lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    _seams(monkeypatch, "_issue_refresh")
    one, two = at_once(lambda: pg_lic.refresh(first["lease"], presented, ambiguity_id=ambiguity_id),
                       lambda: pg_lic.refresh(first["lease"], presented, ambiguity_id=ambiguity_id))
    assert sorted([one.status_code, two.status_code]) == [200, 409], (one.text, two.text)
    assert_error(one if one.status_code == 409 else two, 409, "ambiguity_consumed")
    assert binding(first["binding_id"])["lease_serial"] == 2


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_recognize_resolutions_make_one(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first, _, _, ambiguity_id = raise_activation_ambiguity(pg_lic, pool["pool_id"])
    before, _ = _seams(monkeypatch, "_resolve_without_claim")
    one, two = at_once(lambda: pg_lic.resolve(ambiguity_id, "recognize_existing", first["binding_id"]),
                       lambda: pg_lic.resolve(ambiguity_id, "recognize_existing", first["binding_id"]))
    assert len(before) == 2
    assert sorted([one.status_code, two.status_code]) == [200, 409], (one.text, two.text)
    assert_error(one if one.status_code == 409 else two, 409, "ambiguity_already_resolved")
    assert len(log_rows(pool["account_id"], "ambiguity_resolved")) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_treat_as_new_resolutions_claim_at_most_one_binding(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    _, _, _, ambiguity_id = raise_activation_ambiguity(pg_lic, pool["pool_id"])
    before, _ = _seams(monkeypatch, "claim_capacity")
    one, two = at_once(lambda: pg_lic.resolve(ambiguity_id, "treat_as_new"),
                       lambda: pg_lic.resolve(ambiguity_id, "treat_as_new"))
    assert len(before) == 2
    assert sorted([one.status_code, two.status_code]) == [200, 409], (one.text, two.text)
    assert_error(one if one.status_code == 409 else two, 409, "ambiguity_already_resolved")
    assert len(active_bindings(pool["pool_id"])) == 2          # the original PC, plus exactly one claimed
    assert len(log_rows(pool["account_id"], "ambiguity_resolved")) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_identical_resolutions_replay_one_outcome(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    _, _, _, ambiguity_id = raise_activation_ambiguity(pg_lic, pool["pool_id"])
    _seams(monkeypatch, "claim_capacity")
    request_id = str(uuid.uuid4())
    one, two = at_once(lambda: pg_lic.resolve(ambiguity_id, "treat_as_new", request_id=request_id),
                       lambda: pg_lic.resolve(ambiguity_id, "treat_as_new", request_id=request_id))
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    assert one.json()["resolved_binding_id"] == two.json()["resolved_binding_id"]
    assert len(active_bindings(pool["pool_id"])) == 2


# ---------------------------------------------------------------------------
# J8 — a request owns its request_id before its lock is released
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_j8_a_retry_waits_for_the_stored_pool_full_and_never_claims(pg_lic, make_pool, monkeypatch, iteration):
    """
    Resolve R decides pool_full and is held just before its commit. A seat is
    released, and a retry of R arrives. The retry must wait for R's pool lock
    and then replay pool_full; it must never claim the freed seat.
    """
    from services import licence_history

    pool = make_pool(1, owner_identifier=pg_lic.owner)
    first, _, _, ambiguity_id = raise_activation_ambiguity(pg_lic, pool["pool_id"])
    held, go = threading.Event(), threading.Event()
    original = licence_history.record

    def record(cur, entry, **kwargs):
        result = original(cur, entry, **kwargs)
        if kwargs.get("outcome") == "refused" and not held.is_set():
            held.set()
            assert go.wait(20)
        return result

    monkeypatch.setattr(licence_history, "record", record)
    request_id = str(uuid.uuid4())

    def retry_after_release():
        assert held.wait(20)
        assert pg_lic.release(first["binding_id"]).status_code == 200
        results = []
        thread = threading.Thread(target=lambda: results.append(
            pg_lic.resolve(ambiguity_id, "treat_as_new", request_id=request_id)))
        thread.start()
        import time
        deadline = time.monotonic() + 20
        while lock_waiters() < 1:              # the retry is blocked on R's pool lock
            assert time.monotonic() < deadline, "the retry never blocked on the pool lock"
            time.sleep(0.02)
        go.set()
        thread.join(30)
        return results[0]

    original_call, retry = at_once(
        lambda: pg_lic.resolve(ambiguity_id, "treat_as_new", request_id=request_id), retry_after_release)
    assert_error(original_call, 409, "pool_full")
    assert_error(retry, 409, "pool_full")
    assert active_bindings(pool["pool_id"]) == []              # the freed seat was never claimed
    assert ambiguity(ambiguity_id)["status"] == "open"


# ---------------------------------------------------------------------------
# J9 — release serialises on the binding even when it is already released
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_j9_two_releases_of_a_released_binding_one_request_id_two_bodies(pg_lic, make_pool, monkeypatch,
                                                                        iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first = pg_lic.activated(pool["pool_id"])
    pg_lic.released(first["binding_id"])
    before, _ = _seams(monkeypatch, "_release_locked")
    request_id = str(uuid.uuid4())
    one, two = at_once(
        lambda: pg_lic.release(first["binding_id"], request_id=request_id, revoke_reason="retired"),
        lambda: pg_lic.release(first["binding_id"], request_id=request_id, revoke_reason="support"))
    assert len(before) == 2
    assert sorted([one.status_code, two.status_code]) == [200, 409], (one.text, two.text)
    ok = one if one.status_code == 200 else two
    assert ok.json()["result"] == "already_released"
    assert_error(one if one.status_code == 409 else two, 409, "request_id_reused")
    assert len(query("SELECT id FROM licence_requests WHERE request_id = ?", (request_id,))) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_identical_releases_release_once(pg_lic, make_pool, monkeypatch, iteration):
    pool = make_pool(5, owner_identifier=pg_lic.owner)
    first = pg_lic.activated(pool["pool_id"])
    _seams(monkeypatch, "_release_locked")
    request_id = str(uuid.uuid4())
    one, two = at_once(lambda: pg_lic.release(first["binding_id"], request_id=request_id),
                       lambda: pg_lic.release(first["binding_id"], request_id=request_id))
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    assert one.json()["result"] == two.json()["result"] == "released"
    assert len(log_rows(pool["account_id"], "release")) == 1


# ===========================================================================
# The history fails closed (I4) — Postgres, and SQLite
# ===========================================================================

def _snapshot(pool_ids):
    marks = ",".join("?" for _ in pool_ids)
    return {
        "bindings": query(f"SELECT id, lease_serial, revoked_at, installation_id, current_lease "
                          f"FROM institution_seats WHERE seat_pool_id IN ({marks}) ORDER BY id", tuple(pool_ids)),
        "log": query(f"SELECT id FROM seat_activation_log WHERE seat_pool_id IN ({marks}) ORDER BY id",
                     tuple(pool_ids)),
        "ambiguities": query(f"SELECT id, status, resolution, consumed_at FROM licence_ambiguities "
                             f"WHERE seat_pool_id IN ({marks}) ORDER BY id", tuple(pool_ids)),
    }


@pytest.fixture(params=["postgres", "sqlite"])
def any_lic(request, licence_signing_key):
    """pg_lic on Postgres; on SQLite, the same calls on a fresh file."""
    if request.param == "postgres":
        return request.getfixturevalue("pg_lic")
    from fastapi.testclient import TestClient

    from app import app
    from licensing_harness import Licensing, register_user

    request.getfixturevalue("sqlite_database")
    tag = uuid.uuid4().hex[:12]
    owner, admin = f"owner-{tag}@school.example", f"admin-{tag}@exampartner.example"
    register_user(owner)
    register_user(admin, is_admin=True)
    return Licensing(TestClient(app), owner=owner, admin=admin)


def test_the_request_history_fails_closed_and_the_backend_keeps_working(any_lic, make_pool):
    lic = any_lic
    pool = make_pool(10, owner_identifier=lic.owner)
    pc = lic.activated(pool["pool_id"], make_signals(**PC_B))
    act_first, act_reinstalled, _, act_ambiguity = raise_activation_ambiguity(lic, pool["pool_id"])
    lic.resolved(act_ambiguity, "recognize_existing", act_first["binding_id"])
    ref_first, ref_presented, _, ref_ambiguity = raise_refresh_ambiguity(lic, pool["pool_id"])
    lic.resolved(ref_ambiguity, "recognize_existing", ref_first["binding_id"])
    open_pool = make_pool(10, owner_identifier=lic.owner)
    _, _, _, open_ambiguity = raise_activation_ambiguity(lic, open_pool["pool_id"], disk="WD-OPEN",
                                                         nic="B44506000EEE")
    pools = [pool["pool_id"], open_pool["pool_id"]]
    token = lic.token()
    new_pc = make_signals(uuid="12121212-3434-4565-8787-909090909090", serial="NEWPC1", board_serial="NEWPCB",
                          disk="DNEWPC", nic="B44506121212")

    calls = {
        "activate": lambda: lic.activate(pool["pool_id"], new_pc, token=token),
        "refresh": lambda: lic.refresh(pc["lease"], make_signals(**PC_B)),
        "activate collection": lambda: lic.activate(pool["pool_id"], act_reinstalled, ambiguity_id=act_ambiguity,
                                                    token=token),
        "refresh collection": lambda: lic.refresh(ref_first["lease"], ref_presented, ambiguity_id=ref_ambiguity),
        "release": lambda: lic.release(pc["binding_id"]),
        "resolve": lambda: lic.resolve(open_ambiguity, "treat_as_new"),
    }

    before = _snapshot(pools)
    execute("ALTER TABLE licence_requests RENAME TO licence_requests_hidden")
    try:
        for name, call in calls.items():
            assert_error(call(), 503, "licensing_unavailable"), name
        assert _snapshot(pools) == before
        health = lic.client.get("/health")
        assert health.status_code == 200 and health.json()["licensing"] == "ready"
        assert lic.client.get("/content/version").status_code == 200
    finally:
        execute("ALTER TABLE licence_requests_hidden RENAME TO licence_requests")

    for name, call in calls.items():
        response = call()
        assert response.status_code == 200, (name, response.text)
