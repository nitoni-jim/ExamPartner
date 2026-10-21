"""
tests/licensing_harness.py — the calls a Windows client, a school owner and
an ExamPartner admin make, for the Unit 3a tests (refresh, release,
ambiguity resolution and collection; brief §4.I–§4.J).

Not a test module: pytest collects only test_*.py. Test modules import the
helpers and the `lic` / `clock` fixtures from here, which is how a fixture
defined outside conftest.py is shared.

test_licensing_activate.py keeps its own copy of the activation helpers
from Unit 2; it is deliberately left as it was, not refactored onto this.
"""
import base64
import json
import re
import uuid

import pytest

from conftest import OTHER_USER, TEST_USER, make_signals

WIRE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
PASSWORD = "correct horse"
ADMIN = "boss@exampartner.example"
CLIENT = {"platform": "windows", "app_version": "1.0.0", "os_version": "10.0.22631"}

# A second strong PC, different in every identifying signal.
PC_B = dict(uuid="9E8D7C6B-5A49-3827-1605-F4E3D2C1B0A9", serial="ZZ9PLURAL", board_serial="/ZZ9PLURAL/BOARD/",
            disk="WD-BBBBBBBBBB", nic="B44506BBBBBB", guid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def execute(sql, params=()):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        db.commit()
    finally:
        db.close()


def query(sql, params=()):
    from config import db_conn

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        return [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
    finally:
        db.close()


def register_user(identifier, password=PASSWORD, is_admin=False):
    from routes.auth import _hash_pw

    salt = uuid.uuid4().hex
    execute(
        "INSERT INTO users (identifier, salt, pw_hash, is_paid, is_admin) VALUES (?, ?, ?, ?, ?)",
        (identifier, salt, _hash_pw(password, salt), False, bool(is_admin)),
    )


def bearer(identifier):
    from services.auth_utils import make_token

    return {"Authorization": f"Bearer {make_token(identifier)}"}


def binding(binding_id):
    rows = query("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
    return rows[0] if rows else None


def active_bindings(pool_id):
    return query("SELECT * FROM institution_seats WHERE seat_pool_id = ? AND revoked_at IS NULL", (pool_id,))


def all_bindings(pool_id):
    return query("SELECT * FROM institution_seats WHERE seat_pool_id = ?", (pool_id,))


def log_rows(account_id, action=None):
    rows = query("SELECT * FROM seat_activation_log WHERE account_id = ? ORDER BY created_at", (account_id,))
    return [r for r in rows if action is None or r["action"] == action]


def log_actions(account_id):
    return [r["action"] for r in log_rows(account_id)]


def ambiguity(ambiguity_id):
    rows = query("SELECT * FROM licence_ambiguities WHERE id = ?", (ambiguity_id,))
    return rows[0] if rows else None


def ambiguities(pool_id):
    return query("SELECT * FROM licence_ambiguities WHERE seat_pool_id = ?", (pool_id,))


def history(request_id):
    return query("SELECT * FROM licence_requests WHERE request_id = ?", (request_id,))


def lease_payload(lease):
    segment = lease.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code, detail
    assert isinstance(detail["message"], str) and detail["message"]
    assert isinstance(detail["retryable"], bool)
    assert WIRE_TIME.fullmatch(detail["server_time"])
    return detail


def without_server_time(body):
    """A replay is identical to the original except server_time (J5)."""
    body = dict(body)
    if "detail" in body and isinstance(body["detail"], dict):
        body["detail"] = {k: v for k, v in body["detail"].items() if k != "server_time"}
    body.pop("server_time", None)
    return body


# ---------------------------------------------------------------------------
# The three callers
# ---------------------------------------------------------------------------

class Licensing:
    """A TestClient plus the calls the Windows client, an owner and an admin make."""

    def __init__(self, client, owner=TEST_USER, admin=None):
        self.client = client
        self.owner = owner                  # the school owner the calls act as by default
        self.admin = admin or ADMIN         # the ExamPartner admin

    # -- Windows client --------------------------------------------------

    def token(self, identifier=None):
        identifier = identifier or self.owner
        response = self.client.post("/licence/activation-session",
                                    json={"identifier": identifier, "password": PASSWORD, "client": CLIENT})
        assert response.status_code == 200, response.text
        return response.json()["activation_token"]

    def activate(self, pool_id, signals=None, *, token=None, request_id=None, label="LAB-PC-07",
                 installation_id=None, ambiguity_id=None, as_user=None):
        body = {
            "request_id": request_id or str(uuid.uuid4()),
            "pool_id": pool_id,
            "machine_label": label,
            "installation_id": installation_id,
            "ambiguity_id": ambiguity_id,
            "fingerprint_signals": signals if signals is not None else make_signals(),
            "client": CLIENT,
        }
        headers = {"Authorization": f"Bearer {token or self.token(as_user)}"}
        return self.client.post("/licence/activate", json=body, headers=headers)

    def activated(self, pool_id, signals=None, **kwargs):
        """Activate and return the 200 body."""
        response = self.activate(pool_id, signals, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def refresh(self, lease, signals=None, *, request_id=None, ambiguity_id=None, client=None):
        body = {
            "request_id": request_id or str(uuid.uuid4()),
            "lease": lease,
            "ambiguity_id": ambiguity_id,
            "fingerprint_signals": signals if signals is not None else make_signals(),
            "client": client or CLIENT,
        }
        return self.client.post("/licence/refresh", json=body)

    def refreshed(self, lease, signals=None, **kwargs):
        response = self.refresh(lease, signals, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    # -- School owner ----------------------------------------------------

    def release(self, binding_id, *, as_user=None, request_id="__new__", revoke_reason=None, reason=None,
                headers=None, body=None):
        if body is None:
            body = {"request_id": str(uuid.uuid4()) if request_id == "__new__" else request_id}
            if revoke_reason is not None:
                body["revoke_reason"] = revoke_reason
            if reason is not None:
                body["reason"] = reason
        if headers is None:
            headers = bearer(as_user or self.owner)
        return self.client.post(f"/institutions/seats/{binding_id}/release", json=body, headers=headers)

    def released(self, binding_id, **kwargs):
        response = self.release(binding_id, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    # -- ExamPartner admin -----------------------------------------------

    def resolve(self, ambiguity_id, decision, binding_id=None, *, request_id=None, as_user=None,
                headers=None, body=None):
        if body is None:
            body = {"request_id": request_id or str(uuid.uuid4()), "decision": decision}
            if binding_id is not None:
                body["binding_id"] = binding_id
        if headers is None:
            headers = bearer(as_user or self.admin)
        return self.client.post(f"/support/licensing/ambiguities/{ambiguity_id}/resolve", json=body,
                                headers=headers)

    def resolved(self, ambiguity_id, decision, binding_id=None, **kwargs):
        response = self.resolve(ambiguity_id, decision, binding_id, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def list_ambiguities(self, status=None, as_user=None):
        params = {} if status is None else {"status": status}
        return self.client.get("/support/licensing/ambiguities", params=params,
                               headers=bearer(as_user or self.admin))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def lic(sqlite_database, licence_signing_key):
    """SQLite, a signing key, an owner, a second owner and an admin."""
    from fastapi.testclient import TestClient

    from app import app

    register_user(TEST_USER)
    register_user(OTHER_USER)
    register_user(ADMIN, is_admin=True)
    # No `with`: the startup event would reload the signing key from config,
    # which is deliberately empty in tests (see test_licensing_activate.py).
    return Licensing(TestClient(app))


@pytest.fixture
def clock(monkeypatch):
    """Shift licensing time (I10): clock(days=40). ALLOW_TEST_CLOCK is patched on."""
    from datetime import timedelta

    import config
    from services import licensing_time

    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", True)

    def shift(**kwargs):
        licensing_time.set_clock_offset(timedelta(**kwargs))

    try:
        yield shift
    finally:
        licensing_time.reset_clock()


# ---------------------------------------------------------------------------
# Scenarios several modules need
# ---------------------------------------------------------------------------

def raise_activation_ambiguity(lic, pool_id, *, disk="WD-AAAA", nic="B44506AAAAAA", request_id=None):
    """A degenerate PC, then its reinstall. Returns (first body, reinstalled signals, raising request_id, ambiguity_id)."""
    from conftest import make_degenerate_signals

    first = lic.activated(pool_id, make_degenerate_signals(disk=disk, nic=nic))
    reinstalled = make_degenerate_signals(disk=disk, nic=nic, guid="99999999-8888-4777-8666-555555555555")
    request_id = request_id or str(uuid.uuid4())
    raised = assert_error(lic.activate(pool_id, reinstalled, request_id=request_id), 409, "ambiguity_pending")
    return first, reinstalled, request_id, raised["ambiguity_id"]


def raise_refresh_ambiguity(lic, pool_id, *, disk="WD-RRRR", nic="B44506RRRRRR"):
    """
    A degenerate PC whose continuity then breaks on refresh: the same lease
    and installation, but a different MachineGuid. Returns
    (activation body, presented signals, raising request_id, ambiguity_id).
    """
    from conftest import make_degenerate_signals

    first = lic.activated(pool_id, make_degenerate_signals(disk=disk, nic=nic))
    presented = make_degenerate_signals(disk=disk, nic=nic, guid="77777777-6666-4555-8444-333333333333")
    request_id = str(uuid.uuid4())
    raised = assert_error(lic.refresh(first["lease"], presented, request_id=request_id), 409, "ambiguity_pending")
    return first, presented, request_id, raised["ambiguity_id"]


# ---------------------------------------------------------------------------
# Postgres: identities unique to the test, and the race seams
# ---------------------------------------------------------------------------
#
# ep_test is shared and kept between runs, so a Postgres test cannot reuse
# TEST_USER: every test registers its own owner and admin.

@pytest.fixture
def pg_lic(pg_database, licence_signing_key):
    from fastapi.testclient import TestClient

    from app import app

    tag = uuid.uuid4().hex[:12]
    owner, admin = f"owner-{tag}@school.example", f"admin-{tag}@exampartner.example"
    register_user(owner)
    register_user(admin, is_admin=True)
    return Licensing(TestClient(app), owner=owner, admin=admin)


def barrier_before(monkeypatch, module, name, parties=2, timeout=20):
    """
    Replace module.name with a wrapper that waits for all `parties` callers
    before calling the original. Used to get every request PAST its early,
    unlocked history check before any of them takes a lock — otherwise the
    second request usually just replays the first and no race happens.
    Returns the list of arrivals.
    """
    import threading

    original = getattr(module, name)
    barrier = threading.Barrier(parties, timeout=timeout)
    arrivals = []
    lock = threading.Lock()

    def wrapper(*args, **kwargs):
        with lock:
            arrivals.append(threading.get_ident())
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return arrivals


def lock_waiters():
    """Backends in this database currently blocked on a lock (a row lock, here)."""
    rows = query("SELECT COUNT(*) AS n FROM pg_stat_activity "
                 "WHERE datname = current_database() AND wait_event_type = 'Lock'")
    return int(rows[0]["n"])


def pause_after(monkeypatch, module, name, parties=2, timeout=20):
    """
    Replace module.name with a wrapper that, AFTER the original returns,
    holds its caller until either every party has also returned from it, or
    every party that has not is blocked on a lock.

    Wrapped around the LAST check a request makes under its lock before it
    writes, this makes a race guard deterministic in both directions:

    - guard present: the second request is blocked on the lock, the seam sees
      it waiting, and the first proceeds and commits; the second then finds
      the first's result. Nothing is decided by timing.
    - guard removed: nothing blocks, so both requests reach the seam having
      checked and found nothing — and both write. The test fails every run.

    That is the shape R2 asks for (brief §9): a barrier BEFORE the guarded
    function, as Unit 2's ambiguity test had, lets one request finish its
    lookup and insert before the other looks, and passes without the lock
    some of the time. Returns the list of arrivals.
    """
    import threading
    import time

    original = getattr(module, name)
    arrivals = []
    lock = threading.Lock()

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        with lock:
            arrivals.append(threading.get_ident())
        deadline = time.monotonic() + timeout
        while True:
            with lock:
                missing = parties - len(arrivals)
            if missing <= 0 or lock_waiters() >= missing:
                return result
            if time.monotonic() > deadline:
                raise AssertionError(f"pause_after({name}): the other request neither arrived nor blocked")
            time.sleep(0.02)

    monkeypatch.setattr(module, name, wrapper)
    return arrivals


def at_once(*calls, timeout=60):
    """Run each zero-argument call in its own thread; return their results in order."""
    import threading

    results = [None] * len(calls)

    def run(i):
        try:
            results[i] = calls[i]()
        except Exception as exc:     # recorded and re-raised below, never swallowed
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    assert not any(t.is_alive() for t in threads), "a request hung"
    for r in results:
        if isinstance(r, Exception):
            raise r
    return results
