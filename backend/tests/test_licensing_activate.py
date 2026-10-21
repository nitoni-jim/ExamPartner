"""
tests/test_licensing_activate.py — POST /licence/activation-session and
POST /licence/activate through the real routes (contract §4–§5; brief
§4.F–§4.H; spec §12 tests 1, 2, 3, 7, 9, 10, 11, 12, 13, 26).

SQLite throughout. Every concurrency guarantee is tested against Postgres
in test_licensing_activate_pg.py instead: SQLite serialises writers and
would pass those tests whether or not the guards exist.
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import OTHER_USER, TEST_USER, make_degenerate_signals, make_signals

WIRE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
PASSWORD = "correct horse"
ADMIN = "boss@exampartner.example"
CLIENT = {"platform": "windows", "app_version": "1.0.0", "os_version": "10.0.22631"}

# A second strong PC, different in every identifying signal.
PC_B = dict(uuid="9E8D7C6B-5A49-3827-1605-F4E3D2C1B0A9", serial="ZZ9PLURAL", board_serial="/ZZ9PLURAL/BOARD/",
            disk="WD-BBBBBBBBBB", nic="B44506BBBBBB")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

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
        return [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
    finally:
        db.close()


def register_user(identifier, password=PASSWORD, is_admin=False):
    from routes.auth import _hash_pw

    salt = uuid.uuid4().hex
    _execute(
        "INSERT INTO users (identifier, salt, pw_hash, is_paid, is_admin) VALUES (?, ?, ?, ?, ?)",
        (identifier, salt, _hash_pw(password, salt), 0, 1 if is_admin else 0),
    )


class Licensing:
    """A TestClient plus the calls a Windows client makes."""

    def __init__(self, client):
        self.client = client

    def session(self, identifier=TEST_USER, password=PASSWORD, client=None):
        return self.client.post("/licence/activation-session",
                                json={"identifier": identifier, "password": password, "client": client or CLIENT})

    def token(self, identifier=TEST_USER):
        response = self.session(identifier)
        assert response.status_code == 200, response.text
        return response.json()["activation_token"]

    def activate(self, pool_id, signals=None, *, token=None, request_id=None, label="LAB-PC-07",
                 installation_id=None, ambiguity_id=None, client=None, headers=None):
        body = {
            "request_id": request_id or str(uuid.uuid4()),
            "pool_id": pool_id,
            "machine_label": label,
            "installation_id": installation_id,
            "ambiguity_id": ambiguity_id,
            "fingerprint_signals": signals if signals is not None else make_signals(),
            "client": client or CLIENT,
        }
        if headers is None:
            headers = {"Authorization": f"Bearer {token or self.token()}"}
        return self.client.post("/licence/activate", json=body, headers=headers)


@pytest.fixture
def lic(sqlite_database, licence_signing_key):
    from fastapi.testclient import TestClient

    from app import app

    register_user(TEST_USER)
    register_user(OTHER_USER)
    register_user(ADMIN, is_admin=True)
    # No `with`: the startup event would reload the signing key from config,
    # which is deliberately empty in tests. init_db() already ran in
    # sqlite_database, and licence_signing_key installed an in-process key.
    return Licensing(TestClient(app))


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert isinstance(detail["message"], str) and detail["message"]
    assert isinstance(detail["retryable"], bool)
    assert WIRE_TIME.fullmatch(detail["server_time"])
    return detail


def active_bindings(pool_id):
    return _query("SELECT * FROM institution_seats WHERE seat_pool_id = ? AND revoked_at IS NULL", (pool_id,))


def log_actions(account_id):
    return [r["action"] for r in _query(
        "SELECT action, created_at FROM seat_activation_log WHERE account_id = ? ORDER BY created_at", (account_id,))]


def ambiguities(pool_id):
    return _query("SELECT * FROM licence_ambiguities WHERE seat_pool_id = ?", (pool_id,))


# ---------------------------------------------------------------------------
# Spec §12 test 1 — activation into a pool with room returns a lease
# ---------------------------------------------------------------------------

def test_1_activation_returns_a_verifiable_lease(lic, make_pool, licence_signing_key):
    from services.licence_lease import verify_lease

    pool = make_pool(20)
    for n in range(18):
        lic.activate(pool["pool_id"], make_signals(uuid=None, serial=f"OTHER{n:03d}", board_serial=None,
                                                   disk=f"D{n}", nic=None))
    response = lic.activate(pool["pool_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "activated"
    assert len(active_bindings(pool["pool_id"])) == 19
    for key in ("binding_id", "installation_id"):
        assert re.fullmatch(r"[0-9a-f]{32}", body[key])
    for key in ("lease_expires_at", "server_time"):
        assert WIRE_TIME.fullmatch(body[key])
    assert (body["machine_label"], body["account_name"], body["pool_label"]) == ("LAB-PC-07", "Test School", "Lab")

    payload = verify_lease(body["lease"], licence_signing_key)
    assert payload["kid"] == 1
    assert payload["binding_id"] == body["binding_id"]
    assert payload["installation_id"] == body["installation_id"]
    assert payload["seat_pool_id"] == pool["pool_id"]
    assert payload["account_id"] == pool["account_id"]
    assert payload["lease_serial"] == 1
    assert payload["expires_at"] == body["lease_expires_at"]
    assert all(WIRE_TIME.fullmatch(payload[k]) for k in ("issued_at", "expires_at", "subscription_expires_at"))
    assert re.fullmatch(r"[0-9a-f]{64}", payload["machine_fingerprint_hash"])

    [row] = [b for b in active_bindings(pool["pool_id"]) if b["id"] == body["binding_id"]]
    assert row["fingerprint_confidence"] == "strong"
    assert row["current_lease"] == body["lease"]
    assert row["activation_result"] == "activated"


def test_lease_expiry_is_30_days_or_the_subscription_end_whichever_is_first(lic, make_pool, licence_signing_key):
    from services.licence_lease import verify_lease

    def expiry(sub_end):
        pool = make_pool(5, subscription_expires_at=sub_end.isoformat())
        body = lic.activate(pool["pool_id"]).json()
        payload = verify_lease(body["lease"], licence_signing_key)
        issued = datetime.strptime(payload["issued_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return issued, datetime.strptime(payload["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    long_end = datetime.now(timezone.utc) + timedelta(days=365)
    issued, expires = expiry(long_end)
    assert expires == issued + timedelta(days=30)

    short_end = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=654321)
    issued, expires = expiry(short_end)
    assert expires == short_end.replace(microsecond=0)  # truncated, never rounded past the paid term


# ---------------------------------------------------------------------------
# Spec §12 test 2 — repeat activation: same binding, no second seat
# ---------------------------------------------------------------------------

def test_2_replay_by_request_id_is_byte_identical(lic, make_pool):
    pool = make_pool(5)
    request_id = str(uuid.uuid4())
    first = lic.activate(pool["pool_id"], request_id=request_id).json()
    second = lic.activate(pool["pool_id"], request_id=request_id).json()
    assert second["lease"] == first["lease"]
    assert (second["binding_id"], second["installation_id"], second["result"]) == \
        (first["binding_id"], first["installation_id"], "activated")
    assert len(active_bindings(pool["pool_id"])) == 1
    assert log_actions(pool["account_id"]).count("activate") == 1


def test_2_degenerate_pc_replay_takes_no_second_seat_and_raises_no_ambiguity(lic, make_pool):
    pool = make_pool(5)
    request_id = str(uuid.uuid4())
    signals = make_degenerate_signals(disk="WD-DEGENERATE-1")
    first = lic.activate(pool["pool_id"], signals, request_id=request_id)
    second = lic.activate(pool["pool_id"], signals, request_id=request_id)
    assert first.status_code == second.status_code == 200, second.text
    assert second.json()["lease"] == first.json()["lease"]
    assert len(active_bindings(pool["pool_id"])) == 1
    assert ambiguities(pool["pool_id"]) == []


def test_2_strong_pc_reactivating_with_a_new_request_id_is_restored(lic, make_pool):
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"]).json()
    again = lic.activate(pool["pool_id"]).json()
    assert again["result"] == "restored"
    assert again["binding_id"] == first["binding_id"]
    assert len(active_bindings(pool["pool_id"])) == 1


def test_replay_with_no_lease_issues_it_then(lic, make_pool):
    """F4/F5: a crash between claim_capacity()'s commit and the lease leaves
    a binding with no lease; the client's retry is the recovery."""
    pool = make_pool(5)
    request_id = str(uuid.uuid4())
    first = lic.activate(pool["pool_id"], request_id=request_id).json()
    _execute("UPDATE institution_seats SET current_lease = NULL, lease_serial = 0 WHERE id = ?", (first["binding_id"],))
    # Unit 3a: the request history stores the response once the lease is
    # issued, so a real crash at that point leaves it empty too.
    _execute("UPDATE licence_requests SET response_json = NULL WHERE request_id = ?", (request_id,))
    again = lic.activate(pool["pool_id"], request_id=request_id)
    assert again.status_code == 200 and again.json()["binding_id"] == first["binding_id"]
    assert again.json()["lease"]
    assert len(active_bindings(pool["pool_id"])) == 1


def test_replay_window_is_24_hours(lic, make_pool):
    pool = make_pool(5)
    request_id = str(uuid.uuid4())
    first = lic.activate(pool["pool_id"], request_id=request_id).json()
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _execute("UPDATE institution_seats SET activation_request_at = ? WHERE id = ?", (old, first["binding_id"]))
    # Unit 3a: the request history is authoritative (brief §4.I I4), so it
    # must be 25 hours old too for the request to count as new.
    _execute("UPDATE licence_requests SET created_at = ? WHERE request_id = ?", (old, request_id))
    again = lic.activate(pool["pool_id"], request_id=request_id).json()
    assert again["result"] == "restored"          # treated as a new request: a strong match restores
    assert again["installation_id"] != first["installation_id"]


# ---------------------------------------------------------------------------
# Spec §12 test 3 — pool full
# ---------------------------------------------------------------------------

def test_3_pool_full_never_evicts(lic, make_pool):
    pool = make_pool(1)
    first = lic.activate(pool["pool_id"]).json()
    detail = assert_error(lic.activate(pool["pool_id"], make_signals(**PC_B)), 409, "pool_full")
    assert (detail["pool_size"], detail["active"]) == (1, 1)
    assert [b["id"] for b in active_bindings(pool["pool_id"])] == [first["binding_id"]]


# ---------------------------------------------------------------------------
# Spec §12 test 7 — reformat, strong match → restored, no new seat (F10, H6)
# ---------------------------------------------------------------------------

def test_7_reformatted_pc_is_restored_with_a_new_installation_id(lic, make_pool, licence_signing_key):
    from services.licence_lease import verify_lease

    pool = make_pool(5)
    first = lic.activate(pool["pool_id"]).json()
    # Reformat: new Windows install (new MachineGuid) plus a RAM upgrade —
    # identity unchanged.
    reinstalled = make_signals(guid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", total_physical_memory_bytes=16 * 2**30)
    body = lic.activate(pool["pool_id"], reinstalled, label="TYPED-AT-PC").json()
    assert body["result"] == "restored"
    assert body["binding_id"] == first["binding_id"]
    assert body["installation_id"] != first["installation_id"]
    assert len(active_bindings(pool["pool_id"])) == 1
    payload = verify_lease(body["lease"], licence_signing_key)
    assert payload["lease_serial"] == 2 and payload["installation_id"] == body["installation_id"]
    assert log_actions(pool["account_id"]) == ["activate", "restore"]
    [row] = active_bindings(pool["pool_id"])
    assert json.loads(row["fingerprint_signals_json"])["windows_machine_guid"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_restore_keeps_the_owners_label(lic, make_pool):
    """H6: a technician's typing at a reinstalled PC does not undo a rename."""
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"], label="LAB-PC-07").json()
    _execute("UPDATE institution_seats SET machine_label = ? WHERE id = ?", ("Front row, left", first["binding_id"]))
    body = lic.activate(pool["pool_id"], label="WHATEVER").json()
    assert body["result"] == "restored" and body["machine_label"] == "Front row, left"
    _execute("UPDATE institution_seats SET machine_label = NULL WHERE id = ?", (first["binding_id"],))
    assert lic.activate(pool["pool_id"], label="NEW-NAME").json()["machine_label"] == "NEW-NAME"


def test_restore_refuses_when_the_index_is_missing(lic, make_pool):
    """The §4.E standing rule: restore changes installation_id, so it checks
    the index live and writes nothing without it."""
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"]).json()
    _execute("DROP INDEX ux_seat_bindings_active_installation")
    assert_error(lic.activate(pool["pool_id"]), 503, "licensing_unavailable")
    [row] = active_bindings(pool["pool_id"])
    assert row["installation_id"] == first["installation_id"] and row["lease_serial"] == 1
    assert log_actions(pool["account_id"]) == ["activate"]


# ---------------------------------------------------------------------------
# Spec §12 tests 9, 10, 11 — revoked, cloned image, reused installation_id
# ---------------------------------------------------------------------------

def test_9_revoked_binding_is_never_restored(lic, make_pool):
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"]).json()
    _execute("UPDATE institution_seats SET revoked_at = ?, revoke_reason = 'manual' WHERE id = ?",
             (datetime.now(timezone.utc).isoformat(), first["binding_id"]))
    body = lic.activate(pool["pool_id"]).json()
    assert body["result"] == "activated"
    assert body["binding_id"] != first["binding_id"]
    assert [b["id"] for b in active_bindings(pool["pool_id"])] == [body["binding_id"]]


def test_10_cloned_image_on_different_hardware_gets_its_own_seat(lic, make_pool):
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"]).json()
    # Same Windows image, so the same MachineGuid; every hardware signal differs.
    body = lic.activate(pool["pool_id"], make_signals(**PC_B)).json()
    assert body["result"] == "activated" and body["binding_id"] != first["binding_id"]
    assert len(active_bindings(pool["pool_id"])) == 2


def test_11_reused_installation_id_is_flagged_and_not_merged(lic, make_pool):
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"]).json()
    body = lic.activate(pool["pool_id"], make_signals(**PC_B), installation_id=first["installation_id"]).json()
    assert body["result"] == "activated" and body["binding_id"] != first["binding_id"]
    original = [b for b in active_bindings(pool["pool_id"]) if b["id"] == first["binding_id"]][0]
    assert original["installation_id"] == first["installation_id"]     # untouched
    [flag] = _query("SELECT * FROM seat_activation_log WHERE action = 'clone_flagged' AND account_id = ?",
                    (pool["account_id"],))
    assert flag["binding_id"] is None
    assert json.loads(flag["detail_json"])["matched_binding_id"] == first["binding_id"]


def test_clone_flag_is_logged_against_the_activating_school(lic, make_pool):
    """H7: school B presenting school A's installation_id must not put B's PC
    into A's owner-facing history."""
    school_a = make_pool(5, owner_identifier=TEST_USER)
    school_b = make_pool(5, owner_identifier=OTHER_USER)
    first = lic.activate(school_a["pool_id"]).json()
    response = lic.activate(school_b["pool_id"], make_signals(**PC_B), installation_id=first["installation_id"],
                            token=lic.token(OTHER_USER))
    assert response.status_code == 200, response.text
    assert "clone_flagged" not in log_actions(school_a["account_id"])
    assert "clone_flagged" in log_actions(school_b["account_id"])


def test_installation_id_of_a_strong_match_is_not_a_clone(lic, make_pool):
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"]).json()
    body = lic.activate(pool["pool_id"], installation_id=first["installation_id"]).json()
    assert body["result"] == "restored"
    assert "clone_flagged" not in log_actions(pool["account_id"])


# ---------------------------------------------------------------------------
# Spec §12 tests 12, 13 — degenerate identity (F2, H1)
# ---------------------------------------------------------------------------

def test_12_two_all_zero_uuid_pcs_with_different_disks_both_activate(lic, make_pool):
    pool = make_pool(5)
    shared_guid = "11111111-2222-3333-4444-555555555555"  # one unsysprepped image (H1)
    a = lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA", guid=shared_guid))
    b = lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-BBBB", guid=shared_guid))
    assert a.status_code == b.status_code == 200, b.text
    assert a.json()["result"] == b.json()["result"] == "activated"
    assert sorted(r["fingerprint_confidence"] for r in active_bindings(pool["pool_id"])) == ["degenerate", "degenerate"]


def test_13_reinstalled_degenerate_pc_goes_to_ambiguity(lic, make_pool):
    pool = make_pool(5)
    first = lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA")).json()
    reinstalled = make_degenerate_signals(disk="WD-AAAA", guid="99999999-8888-7777-6666-555555555555")
    detail = assert_error(lic.activate(pool["pool_id"], reinstalled), 409, "ambiguity_pending")
    assert detail["retryable"] is False
    [row] = ambiguities(pool["pool_id"])
    assert row["id"] == detail["ambiguity_id"]
    assert (row["status"], row["origin"]) == ("open", "activate")
    assert json.loads(row["candidate_binding_ids_json"]) == [first["binding_id"]]
    assert row["request_fp"]
    assert len(active_bindings(pool["pool_id"])) == 1                    # no seat claimed
    assert log_actions(pool["account_id"]) == ["activate", "ambiguous_flagged"]


def test_13_continuity_on_the_network_card_alone_also_routes_to_ambiguity(lic, make_pool):
    pool = make_pool(5)
    lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA", nic="B44506CCCCCC"))
    # Disk replaced AND reinstalled, same onboard NIC.
    assert_error(lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-NEW", nic="B44506CCCCCC")),
                 409, "ambiguity_pending")


def test_continuity_matches_a_strong_binding_too(lic, make_pool):
    """F2: ANY active binding — a PC first activated strong whose firmware
    later stops reporting identifiers still has the same disk."""
    pool = make_pool(5)
    lic.activate(pool["pool_id"], make_signals(disk="WD-AAAA"))
    assert_error(lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA")), 409, "ambiguity_pending")


def test_ambiguity_replay_returns_the_same_row(lic, make_pool):
    pool = make_pool(5)
    lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA"))
    request_id = str(uuid.uuid4())
    reinstalled = make_degenerate_signals(disk="WD-AAAA", guid="99999999-8888-7777-6666-555555555555")
    one = assert_error(lic.activate(pool["pool_id"], reinstalled, request_id=request_id), 409, "ambiguity_pending")
    two = assert_error(lic.activate(pool["pool_id"], reinstalled, request_id=request_id), 409, "ambiguity_pending")
    assert one["ambiguity_id"] == two["ambiguity_id"]
    assert len(ambiguities(pool["pool_id"])) == 1


def test_collecting_an_ambiguity_reports_its_state(lic, make_pool):
    """F3: Unit 2 resolves nothing, so a resend with ambiguity_id gets the
    row's state back — pending, or expired after 7 days."""
    pool = make_pool(5)
    lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA"))
    reinstalled = make_degenerate_signals(disk="WD-AAAA", guid="99999999-8888-7777-6666-555555555555")
    raised = assert_error(lic.activate(pool["pool_id"], reinstalled), 409, "ambiguity_pending")["ambiguity_id"]

    again = assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=raised), 409, "ambiguity_pending")
    assert again["ambiguity_id"] == raised
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id="f" * 32), 409, "ambiguity_mismatch")
    _execute("UPDATE licence_ambiguities SET created_at = ? WHERE id = ?",
             ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), raised))
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=raised), 409, "ambiguity_expired")
    assert len(ambiguities(pool["pool_id"])) == 1


def _raise_ambiguity(lic, pool_id, request_id=None):
    """A degenerate PC, then its reinstall: returns (reinstalled signals, raising request_id, ambiguity_id)."""
    lic.activate(pool_id, make_degenerate_signals(disk="WD-AAAA", nic="B44506AAAAAA"))
    reinstalled = make_degenerate_signals(disk="WD-AAAA", nic="B44506AAAAAA", guid="99999999-8888-7777-6666-555555555555")
    request_id = request_id or str(uuid.uuid4())
    raised = assert_error(lic.activate(pool_id, reinstalled, request_id=request_id), 409, "ambiguity_pending")
    return reinstalled, request_id, raised["ambiguity_id"]


def test_collection_by_the_raising_pc_with_a_new_request_id_gets_the_same_pending_ambiguity(lic, make_pool):
    pool = make_pool(5)
    reinstalled, _, raised = _raise_ambiguity(lic, pool["pool_id"])
    for _ in range(2):
        again = assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=raised,
                                          request_id=str(uuid.uuid4())), 409, "ambiguity_pending")
        assert again["ambiguity_id"] == raised
    assert len(ambiguities(pool["pool_id"])) == 1
    assert len(active_bindings(pool["pool_id"])) == 1


def test_collection_by_a_different_pc_in_the_same_pool_is_ambiguity_mismatch(lic, make_pool):
    """Contract §9.3: the PC presenting the ambiguity_id is not the one that
    raised it. Same pool, same owner, valid id — wrong machine."""
    pool = make_pool(5)
    _, _, raised = _raise_ambiguity(lic, pool["pool_id"])
    for other_pc in (make_signals(**PC_B), make_degenerate_signals(disk="WD-SOMEONE-ELSE")):
        detail = assert_error(lic.activate(pool["pool_id"], other_pc, ambiguity_id=raised), 409, "ambiguity_mismatch")
        assert "ambiguity_id" not in detail          # nothing about the row is revealed
    assert len(ambiguities(pool["pool_id"])) == 1
    assert len(active_bindings(pool["pool_id"])) == 1


def test_collection_reusing_the_raising_request_id_is_request_id_reused(lic, make_pool):
    """Contract §10: collection uses a NEW request_id."""
    pool = make_pool(5)
    reinstalled, raising_request_id, raised = _raise_ambiguity(lic, pool["pool_id"])
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=raised, request_id=raising_request_id),
                 409, "request_id_reused")
    assert len(ambiguities(pool["pool_id"])) == 1


def test_collection_of_an_expired_ambiguity_by_its_pc(lic, make_pool):
    pool = make_pool(5)
    reinstalled, _, raised = _raise_ambiguity(lic, pool["pool_id"])
    _execute("UPDATE licence_ambiguities SET created_at = ? WHERE id = ?",
             ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), raised))
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=raised), 409, "ambiguity_expired")
    # …and a different PC still learns only that it is the wrong PC.
    assert_error(lic.activate(pool["pool_id"], make_signals(**PC_B), ambiguity_id=raised), 409, "ambiguity_mismatch")


def test_weak_match_routes_to_ambiguity(lic, make_pool):
    pool = make_pool(5)
    lic.activate(pool["pool_id"])
    # Same UUID, every serial different: a weak match.
    assert_error(lic.activate(pool["pool_id"], make_signals(serial="NEWSERIAL1", board_serial="NEWBOARD1")),
                 409, "ambiguity_pending")


def test_a_batch_sharing_a_real_board_serial_raises_one_ambiguity_per_pool(lic, make_pool):
    """H8 / the pre-code report: boards from one batch carrying the same real
    (non-placeholder) serial. The second PC's shared serial is still usable —
    only one binding carries it — so it is a weak match and goes to review.
    Once an administrator admits it as new, two bindings carry the serial,
    it stops being usable, and the rest of the batch activates normally."""
    from services import fingerprint_service as fs
    from services.licence_history import Entry
    from services.licensing_service import claim_capacity

    pool = make_pool(10)

    def pc(n):
        return make_signals(uuid=f"{n:08X}-0000-4000-8000-00000000000{n}", serial=f"SYS-{n}",
                            board_serial="MB-BATCH-77", bios_serial=f"SYS-{n}", disk=f"WD-{n}", nic=None)

    assert lic.activate(pool["pool_id"], pc(1)).json()["result"] == "activated"
    assert_error(lic.activate(pool["pool_id"], pc(2)), 409, "ambiguity_pending")

    # An administrator's "treat as new" (Unit 3) claims capacity for PC 2.
    reading = fs.read(pc(2))
    claim_capacity(pool["pool_id"], machine_fingerprint_hash=fs.identity_hash(reading),
                   fingerprint_confidence="strong", performed_by=ADMIN, actor_role="exampartner_admin",
                   request_id=str(uuid.uuid4()), request_fp="admitted", fingerprint_signals_json=json.dumps(pc(2)),
                   history=Entry(str(uuid.uuid4()), "resolve", "admitted"))

    for n in (3, 4, 5):
        assert lic.activate(pool["pool_id"], pc(n)).json()["result"] == "activated"
    assert len(ambiguities(pool["pool_id"])) == 1
    assert len(active_bindings(pool["pool_id"])) == 5


# ---------------------------------------------------------------------------
# Spec §12 test 26 and the activation token (contract §11, F7)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer "}, {"Authorization": "Bearer nonsense"},
                                     {"Authorization": "Basic abc"}])
def test_26_no_valid_activation_session_is_401_and_consumes_nothing(lic, make_pool, headers):
    pool = make_pool(5)
    assert_error(lic.activate(pool["pool_id"], headers=headers), 401, "activation_session_invalid")
    assert active_bindings(pool["pool_id"]) == []


def test_an_expired_activation_token_is_refused(lic, make_pool):
    import time

    from services.licence_session import issue_activation_token

    pool = make_pool(5)
    token, _ = issue_activation_token(TEST_USER, "institution_owner", now=int(time.time()) - 16 * 60)
    assert_error(lic.activate(pool["pool_id"], token=token), 401, "activation_session_invalid")


def test_an_ordinary_user_token_is_refused_by_activate(lic, make_pool):
    from services.auth_utils import make_token

    pool = make_pool(5)
    assert_error(lic.activate(pool["pool_id"], token=make_token(TEST_USER)), 401, "activation_session_invalid")


def test_an_activation_token_is_refused_by_get_current_user(lic, make_pool):
    from services.auth_utils import get_current_user

    make_pool(5)
    token = lic.token()
    assert get_current_user(authorization=f"Bearer {token}") is None
    # …and on a real Android route, it is no login at all.
    assert lic.client.get("/auth/devices", headers={"Authorization": f"Bearer {token}"}).status_code in (401, 403)


def test_422_comes_before_401(lic):
    """Contract §5: FastAPI's 422 for a body of the wrong shape comes first."""
    response = lic.client.post("/licence/activate", json={"pool_id": "x"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Activation session (contract §4)
# ---------------------------------------------------------------------------

def test_session_lists_the_owners_pools_and_registers_no_device(lic, make_pool):
    mine = make_pool(20, owner_identifier=TEST_USER)
    make_pool(10, owner_identifier=OTHER_USER)
    for n in range(3):
        lic.activate(mine["pool_id"], make_signals(uuid=None, serial=f"X{n}", board_serial=None, disk=f"D{n}", nic=None))
    devices_before = _query("SELECT COUNT(*) AS n FROM user_devices")[0]["n"]

    response = lic.session(identifier="  Test.Student@Example.com ")   # trimmed and lowercased, as /auth/login does
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["actor_role"] == "institution_owner"
    assert WIRE_TIME.fullmatch(body["expires_at"]) and WIRE_TIME.fullmatch(body["server_time"])
    issued = datetime.strptime(body["server_time"], "%Y-%m-%dT%H:%M:%SZ")
    expires = datetime.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert timedelta(minutes=14) <= expires - issued <= timedelta(minutes=15, seconds=1)
    [pool] = body["pools"]
    assert pool["pool_id"] == mine["pool_id"]
    assert (pool["pool_size"], pool["active"], pool["available"], pool["over_capacity"]) == (20, 3, 17, False)
    assert (pool["pool_status"], pool["subscription_status"]) == ("active", "active")
    assert WIRE_TIME.fullmatch(pool["subscription_expires_at"])
    assert _query("SELECT COUNT(*) AS n FROM user_devices")[0]["n"] == devices_before


def test_session_lists_suspended_and_lapsed_pools_with_their_state(lic, make_pool):
    make_pool(5, pool_status="suspended")
    make_pool(5, subscription_status="expired")
    states = sorted((p["pool_status"], p["subscription_status"]) for p in lic.session().json()["pools"])
    assert states == [("active", "expired"), ("suspended", "active")]


def test_admin_sees_every_pool(lic, make_pool):
    a = make_pool(5, owner_identifier=TEST_USER)
    b = make_pool(5, owner_identifier=OTHER_USER)
    body = lic.session(identifier=ADMIN).json()
    assert body["actor_role"] == "exampartner_admin"
    assert {p["pool_id"] for p in body["pools"]} >= {a["pool_id"], b["pool_id"]}


def test_owner_of_no_account_has_no_licensing_access(lic):
    assert_error(lic.session(identifier=OTHER_USER), 403, "no_licensing_access")


@pytest.mark.parametrize("identifier, password", [(TEST_USER, "wrong"), ("nobody@example.com", PASSWORD)])
def test_invalid_credentials(lic, make_pool, identifier, password):
    make_pool(5)
    assert_error(lic.session(identifier=identifier, password=password), 401, "invalid_credentials")


def test_session_error_order(lic, make_pool, monkeypatch):
    """H8: version → validation → licensing_unavailable → credentials."""
    import config
    from services import licence_lease, licensing_service

    make_pool(5)
    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", "2.0.0")
    assert licensing_service.configure_client_gate()   # startup configuration (the fixture restores it)
    assert_error(lic.session(password=""), 426, "client_upgrade_required")
    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", "0.0.0")
    assert licensing_service.configure_client_gate()
    assert_error(lic.session(password=""), 400, "invalid_request")
    licence_lease.use_signing_key(None, None)
    assert_error(lic.session(password="wrong"), 503, "licensing_unavailable")


# ---------------------------------------------------------------------------
# Pool access and error precedence (contract §5, §9.3)
# ---------------------------------------------------------------------------

def test_another_owners_pool_and_a_missing_pool_look_identical(lic, make_pool):
    make_pool(5, owner_identifier=TEST_USER)   # the caller owns a school, just not this one
    theirs = make_pool(5, owner_identifier=OTHER_USER)
    one = assert_error(lic.activate(theirs["pool_id"]), 403, "pool_access_denied")
    two = assert_error(lic.activate("0" * 32), 403, "pool_access_denied")
    one.pop("server_time"), two.pop("server_time")
    assert one == two
    assert active_bindings(theirs["pool_id"]) == []


def test_ownership_is_rechecked_at_activate_time(lic, make_pool):
    """F7: a token minted while the caller owned the account stops working
    the moment ownership changes, not 15 minutes later."""
    pool = make_pool(5)
    token = lic.token()
    _execute("UPDATE accounts SET owner_identifier = ? WHERE id = ?", (OTHER_USER, pool["account_id"]))
    assert_error(lic.activate(pool["pool_id"], token=token), 403, "pool_access_denied")


def test_admin_activation_is_recorded_as_admin(lic, make_pool):
    pool = make_pool(5, owner_identifier=OTHER_USER)
    body = lic.activate(pool["pool_id"], token=lic.token(ADMIN)).json()
    [row] = _query("SELECT performed_by, actor_role FROM seat_activation_log WHERE binding_id = ?", (body["binding_id"],))
    assert (row["performed_by"], row["actor_role"]) == (ADMIN, "exampartner_admin")


@pytest.mark.parametrize("pool_kwargs, status, code", [
    ({"pool_status": "suspended"}, 409, "pool_inactive"),
    ({"subscription_status": "expired"}, 402, "subscription_inactive"),
    ({"subscription_expires_at": "2020-01-01T00:00:00+00:00"}, 402, "subscription_inactive"),
])
def test_entitlement_outranks_capacity(lic, make_pool, pool_kwargs, status, code):
    """Precedence: a suspended full pool is pool_inactive and a lapsed full
    pool is subscription_inactive, never pool_full."""
    pool = make_pool(1, **pool_kwargs)
    _execute("INSERT INTO institution_seats (id, seat_pool_id, account_id, machine_fingerprint_hash, installation_id) "
             "VALUES (?, ?, ?, ?, ?)", ("existing", pool["pool_id"], pool["account_id"], "fp", "inst-existing"))
    detail = assert_error(lic.activate(pool["pool_id"]), status, code)
    if code == "subscription_inactive":
        assert "subscription_expires_at" in detail


def test_request_id_reused_with_a_different_body(lic, make_pool):
    pool = make_pool(5)
    request_id = str(uuid.uuid4())
    lic.activate(pool["pool_id"], request_id=request_id)
    assert_error(lic.activate(pool["pool_id"], make_signals(**PC_B), request_id=request_id), 409, "request_id_reused")
    assert len(active_bindings(pool["pool_id"])) == 1


def test_request_id_reused_sequentially_in_another_pool(lic, make_pool):
    """H4: the digest covers the pool id, and the lookup spans pools."""
    a, b = make_pool(5), make_pool(5)
    request_id = str(uuid.uuid4())
    lic.activate(a["pool_id"], request_id=request_id)
    assert_error(lic.activate(b["pool_id"], request_id=request_id), 409, "request_id_reused")
    assert active_bindings(b["pool_id"]) == []


def test_request_id_of_an_ambiguity_reused_with_a_different_body(lic, make_pool):
    pool = make_pool(5)
    lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA"))
    request_id = str(uuid.uuid4())
    assert_error(lic.activate(pool["pool_id"], make_degenerate_signals(disk="WD-AAAA", guid="99999999-8888-7777-6666-555555555555"),
                              request_id=request_id), 409, "ambiguity_pending")
    assert_error(lic.activate(pool["pool_id"], make_signals(**PC_B), request_id=request_id), 409, "request_id_reused")


# ---------------------------------------------------------------------------
# Validation, version and availability
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("change, field", [
    ({"request_id": "not-a-uuid"}, "request_id"),
    ({"request_id": str(uuid.uuid1())}, "request_id"),
    ({"machine_label": "   "}, "machine_label"),
    ({"machine_label": "x" * 65}, "machine_label"),
    ({"fingerprint_signals": {"schema": 2}}, "fingerprint_signals.schema"),
    ({"client": {"platform": "android", "app_version": "1.0.0"}}, "client.platform"),
    ({"client": {"platform": "windows", "app_version": "1.0"}}, "client.app_version"),
])
def test_invalid_request_names_the_field(lic, make_pool, change, field):
    pool = make_pool(5)
    body = {"request_id": str(uuid.uuid4()), "pool_id": pool["pool_id"], "machine_label": "LAB-PC-07",
            "fingerprint_signals": make_signals(), "client": CLIENT, **change}
    response = lic.client.post("/licence/activate", json=body, headers={"Authorization": f"Bearer {lic.token()}"})
    assert assert_error(response, 400, "invalid_request")["field"] == field


def test_fingerprint_unreadable(lic, make_pool):
    pool = make_pool(5)
    detail = assert_error(lic.activate(pool["pool_id"], make_signals(collection_errors=["Win32_BIOS", "MachineGuid"])),
                          400, "fingerprint_unreadable")
    assert detail["classes"] == ["Win32_BIOS"]


def test_426_below_the_minimum_version(lic, make_pool, monkeypatch):
    import config
    from services import licensing_service

    pool = make_pool(5)
    token = lic.token()
    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", "1.10.0")
    assert licensing_service.configure_client_gate()   # startup configuration (the fixture restores it)
    detail = assert_error(lic.activate(pool["pool_id"], token=token,
                                       client={"platform": "windows", "app_version": "1.9.12"}), 426,
                          "client_upgrade_required")
    assert detail["min_app_version"] == "1.10.0"
    ok = lic.activate(pool["pool_id"], token=token, client={"platform": "windows", "app_version": "1.10.0"})
    assert ok.status_code == 200


@pytest.mark.parametrize("key_case", ["absent", "test_seed", "kid_zero"])
def test_unusable_signing_key_makes_licensing_unavailable(lic, make_pool, monkeypatch, key_case):
    import base64
    import os

    import config
    from services import licence_lease
    from services.licence_session import issue_activation_token

    pool = make_pool(5)
    token, _ = issue_activation_token(TEST_USER, "institution_owner")
    key, kid = {
        "absent": ("", "1"),
        "test_seed": ("vaqSxX9ClSwoMECP/J92vJWuEQYqyktbMlFU8YPu+Lg=", "1"),
        "kid_zero": (base64.b64encode(os.urandom(32)).decode(), "0"),
    }[key_case]
    monkeypatch.setattr(config, "LICENCE_SIGNING_KEY", key)
    monkeypatch.setattr(config, "LICENCE_SIGNING_KID", kid)
    assert licence_lease.configure_signing() is False

    assert_error(lic.session(), 503, "licensing_unavailable")
    assert_error(lic.activate(pool["pool_id"], token=token), 503, "licensing_unavailable")
    health = lic.client.get("/health")
    assert health.status_code == 200 and health.json()["licensing"] == "unavailable"
    assert active_bindings(pool["pool_id"]) == []


def test_health_is_ready_with_schema_and_key(lic):
    assert lic.client.get("/health").json()["licensing"] == "ready"


# ---------------------------------------------------------------------------
# Pilot setup endpoints (F11)
# ---------------------------------------------------------------------------

def test_pilot_setup_end_to_end(lic):
    from services.auth_utils import make_token

    admin = {"Authorization": f"Bearer {make_token(ADMIN)}"}
    created = lic.client.post("/institutions/accounts", headers=admin,
                              json={"name": "Pilot School", "owner_identifier": "  Test.Student@Example.com "})
    assert created.status_code == 200, created.text
    account = created.json()["account"]
    assert account["owner_identifier"] == TEST_USER

    pool = lic.client.post(f"/institutions/accounts/{account['id']}/seat_pools", headers=admin,
                           json={"pool_size": 20, "label": "Computer Lab"})
    assert pool.status_code == 200, pool.text
    pool_id = pool.json()["seat_pool"]["id"]

    # No subscription yet: no entitlement.
    assert_error(lic.activate(pool_id), 402, "subscription_inactive")

    end = (datetime.now(timezone.utc) + timedelta(days=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sub = lic.client.patch(f"/institutions/accounts/{account['id']}/subscription", headers=admin,
                           json={"subscription_status": "active", "subscription_expires_at": end})
    assert sub.status_code == 200, sub.text
    assert sub.json()["subscription_expires_at"] == end
    assert log_actions(account["id"]) == ["renewal"]

    assert lic.activate(pool_id).json()["result"] == "activated"


def test_pilot_setup_is_admin_only(lic, make_pool):
    from services.auth_utils import make_token

    owner = {"Authorization": f"Bearer {make_token(TEST_USER)}"}
    pool = make_pool(5)
    assert lic.client.post("/institutions/accounts", headers=owner,
                           json={"name": "X", "owner_identifier": TEST_USER}).status_code == 403
    assert lic.client.post(f"/institutions/accounts/{pool['account_id']}/seat_pools", headers=owner,
                           json={"pool_size": 5}).status_code == 403
    assert lic.client.patch(f"/institutions/accounts/{pool['account_id']}/subscription", headers=owner,
                            json={"subscription_status": "active",
                                  "subscription_expires_at": "2030-01-01T00:00:00Z"}).status_code == 403


@pytest.mark.parametrize("body", [
    {"name": "X", "owner_identifier": "not.registered@example.com"},
    {"name": "  ", "owner_identifier": TEST_USER},
    {"name": "X", "owner_identifier": TEST_USER, "account_type": "reseller"},
])
def test_create_account_validation(lic, body):
    from services.auth_utils import make_token

    response = lic.client.post("/institutions/accounts", headers={"Authorization": f"Bearer {make_token(ADMIN)}"},
                               json=body)
    assert response.status_code == 400, response.text
