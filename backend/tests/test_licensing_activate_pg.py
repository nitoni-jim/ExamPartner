"""
tests/test_licensing_activate_pg.py — the activation race guards, against
Postgres, through the real routes (brief §4.F F4, §4.H H2).

Each test sends the SAME activation twice at once — one request_id, one
body, as a client retrying after a lost response does — and asserts the
outcome is one activation, not two.

Why a barrier, and where. Two HTTP requests started together usually do NOT
race: one finishes its reads before the other starts, the second sees the
first's result as an ordinary replay, and the test passes whether or not the
guard exists. So each test wraps the function that does the guarded write in
a threading.Barrier(2): both requests must have passed the replay check
(F3 step 1) and arrived at the write before either may perform it. That
makes the race real every time, and makes each guard load-bearing in its
test — removing the guard makes the test fail (checked by hand, as spec
test 6 is for the capacity lock; see the PR description).

| Guard                                   | Wrapped function          |
|-----------------------------------------|---------------------------|
| claim_capacity()'s in-lock request check | claim_capacity            |
| create_or_get_ambiguity()'s pool lock    | create_or_get_ambiguity   |
| _restore()'s binding lock and re-check   | _restore                  |
| _ensure_lease()'s issue-if-absent        | _sign_for_binding         |

Postgres only: SQLite serialises writers and passes all four regardless.
"""
import threading
import uuid

import pytest

from conftest import make_degenerate_signals, make_signals

CLIENT = {"platform": "windows", "app_version": "1.0.0", "os_version": "10.0.22631"}
ITERATIONS = 3


def _execute(sql, params=()):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        db.commit()
    finally:
        db.close()


def _query(sql, params=()):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        db.close()


@pytest.fixture
def owner(pg_database, licence_signing_key):
    """A registered owner unique to this test: ep_test is shared and kept."""
    from routes.auth import _hash_pw

    identifier = f"owner-{uuid.uuid4().hex[:12]}@school.example"
    _execute("INSERT INTO users (identifier, salt, pw_hash, is_paid) VALUES (?, ?, ?, ?)",
             (identifier, "s", _hash_pw("pw", "s"), False))
    return identifier


def _token(identifier):
    from services.licence_session import issue_activation_token

    return issue_activation_token(identifier, "institution_owner")[0]


def _body(pool_id, signals, request_id):
    return {"request_id": request_id, "pool_id": pool_id, "machine_label": "LAB-PC-07",
            "installation_id": None, "ambiguity_id": None, "fingerprint_signals": signals, "client": CLIENT}


def _post(body, token):
    from fastapi.testclient import TestClient

    from app import app

    return TestClient(app).post("/licence/activate", json=body, headers={"Authorization": f"Bearer {token}"})


def _barrier_around(monkeypatch, module, name, *, before=None):
    """Replace module.name with a wrapper that waits for BOTH requests."""
    original = getattr(module, name)
    barrier = threading.Barrier(2, timeout=20)
    arrivals = []
    lock = threading.Lock()

    def wrapper(*args, **kwargs):
        with lock:
            index = len(arrivals)
            arrivals.append(threading.get_ident())
        if before is not None:
            args, kwargs = before(index, args, kwargs)
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return arrivals


def _twice_at_once(body, token):
    results = [None, None]

    def run(i):
        try:
            results[i] = _post(body, token)
        except Exception as exc:  # recorded and asserted on, never swallowed
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "a request hung"
    for r in results:
        assert not isinstance(r, Exception), repr(r)
    return results


# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_identical_new_activations_create_one_binding(owner, make_pool, monkeypatch, iteration):
    from services import licensing_service

    pool = make_pool(5, owner_identifier=owner)
    arrivals = _barrier_around(monkeypatch, licensing_service, "claim_capacity")
    one, two = _twice_at_once(_body(pool["pool_id"], make_signals(), str(uuid.uuid4())), _token(owner))

    assert len(arrivals) == 2, "both requests must reach the claim for the race to be real"
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    a, b = one.json(), two.json()
    assert (a["binding_id"], a["installation_id"], a["lease"]) == (b["binding_id"], b["installation_id"], b["lease"])
    assert len(_query("SELECT id FROM institution_seats WHERE seat_pool_id = ? AND revoked_at IS NULL",
                      (pool["pool_id"],))) == 1
    assert len(_query("SELECT id FROM seat_activation_log WHERE seat_pool_id = ? AND action = 'activate'",
                      (pool["pool_id"],))) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_identical_ambiguous_activations_create_one_ambiguity(owner, make_pool, monkeypatch, iteration):
    from services import licensing_service

    pool = make_pool(5, owner_identifier=owner)
    token = _token(owner)
    first = _post(_body(pool["pool_id"], make_degenerate_signals(disk=f"WD-{iteration}"), str(uuid.uuid4())), token)
    assert first.status_code == 200, first.text

    arrivals = _barrier_around(monkeypatch, licensing_service, "create_or_get_ambiguity")
    reinstalled = make_degenerate_signals(disk=f"WD-{iteration}", guid=str(uuid.uuid4()))
    one, two = _twice_at_once(_body(pool["pool_id"], reinstalled, str(uuid.uuid4())), token)

    assert len(arrivals) == 2
    assert one.status_code == two.status_code == 409, (one.text, two.text)
    assert one.json()["detail"]["code"] == two.json()["detail"]["code"] == "ambiguity_pending"
    assert one.json()["detail"]["ambiguity_id"] == two.json()["detail"]["ambiguity_id"]
    assert len(_query("SELECT id FROM licence_ambiguities WHERE seat_pool_id = ?", (pool["pool_id"],))) == 1
    assert len(_query("SELECT id FROM seat_activation_log WHERE seat_pool_id = ? AND action = 'ambiguous_flagged'",
                      (pool["pool_id"],))) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_identical_restores_issue_one_installation_id(owner, make_pool, monkeypatch, iteration):
    from services import licensing_service

    pool = make_pool(5, owner_identifier=owner)
    token = _token(owner)
    first = _post(_body(pool["pool_id"], make_signals(), str(uuid.uuid4())), token)
    assert first.status_code == 200, first.text

    arrivals = _barrier_around(monkeypatch, licensing_service, "_restore")
    one, two = _twice_at_once(_body(pool["pool_id"], make_signals(guid=str(uuid.uuid4())), str(uuid.uuid4())), token)

    assert len(arrivals) == 2
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    a, b = one.json(), two.json()
    assert a["result"] == b["result"] == "restored"
    # Without the binding lock and re-check, each request would write its own
    # installation_id and one PC would hold an ID the server had overwritten.
    assert (a["installation_id"], a["lease"]) == (b["installation_id"], b["lease"])
    [row] = _query("SELECT installation_id, lease_serial FROM institution_seats WHERE seat_pool_id = ? "
                   "AND revoked_at IS NULL", (pool["pool_id"],))
    assert row["installation_id"] == a["installation_id"] and row["lease_serial"] == 2
    assert len(_query("SELECT id FROM seat_activation_log WHERE seat_pool_id = ? AND action = 'restore'",
                      (pool["pool_id"],))) == 1


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_two_identical_activations_receive_one_lease(owner, make_pool, monkeypatch, iteration):
    """_ensure_lease() issues only if absent. The two signers are given
    clocks two seconds apart, so their leases differ and a lost write would
    show up as two different lease strings."""
    from datetime import timedelta

    from services import licensing_service

    pool = make_pool(5, owner_identifier=owner)

    def skew(index, args, kwargs):
        binding, subscription_end, serial, now = args
        return (binding, subscription_end, serial, now + timedelta(seconds=2 * index)), kwargs

    arrivals = _barrier_around(monkeypatch, licensing_service, "_sign_for_binding", before=skew)
    one, two = _twice_at_once(_body(pool["pool_id"], make_signals(), str(uuid.uuid4())), _token(owner))

    assert len(arrivals) == 2, "both requests must reach the signer for the race to be real"
    assert one.status_code == two.status_code == 200, (one.text, two.text)
    assert one.json()["lease"] == two.json()["lease"]
    [row] = _query("SELECT current_lease, lease_serial FROM institution_seats WHERE seat_pool_id = ? "
                   "AND revoked_at IS NULL", (pool["pool_id"],))
    assert row["current_lease"] == one.json()["lease"] and row["lease_serial"] == 1
