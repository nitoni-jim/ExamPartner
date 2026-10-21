"""
tests/test_licensing_refresh.py — POST /licence/refresh through the real
route (contract §9; brief §4.I I1–I4, §4.J; spec §8.6, §8.6.1 and §12
tests 15–25).

SQLite throughout. The concurrency guarantees are tested against Postgres in
test_licensing_unit3a_pg.py: SQLite serialises writers and would pass those
tests whether or not the guards exist.
"""
import json
import uuid

import pytest

from conftest import make_degenerate_signals, make_signals
from licensing_harness import (  # noqa: F401  (lic, clock are fixtures)
    PC_B, WIRE_TIME, assert_error, binding, clock, execute, history, lease_payload, lic, log_rows,
    raise_activation_ambiguity,
)


def _log_count(account_id, action):
    return len(log_rows(account_id, action))


# ---------------------------------------------------------------------------
# The ordinary refresh
# ---------------------------------------------------------------------------

def test_refresh_issues_the_next_serial_and_a_verifiable_lease(lic, make_pool, licence_signing_key):
    from services.licence_lease import verify_lease

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    body = lic.refreshed(first["lease"])

    assert body["result"] == "refreshed"
    assert WIRE_TIME.fullmatch(body["server_time"]) and WIRE_TIME.fullmatch(body["lease_expires_at"])
    old, new = lease_payload(first["lease"]), verify_lease(body["lease"], licence_signing_key)
    assert new["lease_serial"] == old["lease_serial"] + 1
    assert (new["binding_id"], new["installation_id"]) == (old["binding_id"], old["installation_id"])
    assert new["expires_at"] == body["lease_expires_at"]
    row = binding(first["binding_id"])
    assert row["current_lease"] == body["lease"] and row["lease_serial"] == new["lease_serial"]
    assert row["last_seen_at"] is not None


def test_a_routine_refresh_is_not_logged(lic, make_pool):
    """Spec §3.5: do not log every refresh. last_seen_at is fresh, nothing ratcheted, lease unexpired."""
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    second = lic.refreshed(first["lease"])
    lic.refreshed(second["lease"])
    # The first refresh logs (last_seen_at was never set); the second is routine.
    assert _log_count(pool["account_id"], "refresh") == 1


def test_refresh_takes_no_capacity_and_creates_no_binding(lic, make_pool):
    pool = make_pool(1)
    first = lic.activated(pool["pool_id"])
    lic.refreshed(first["lease"])
    from licensing_harness import all_bindings

    assert len(all_bindings(pool["pool_id"])) == 1


# ---------------------------------------------------------------------------
# I1 — the decision order
# ---------------------------------------------------------------------------

def test_refresh_needs_no_authorization_header_and_ignores_one(lic, make_pool, auth_headers):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    body = {"request_id": str(uuid.uuid4()), "lease": first["lease"], "ambiguity_id": None,
            "fingerprint_signals": make_signals(), "client": {"platform": "windows", "app_version": "1.0.0"}}
    assert lic.client.post("/licence/refresh", json=body, headers=auth_headers).status_code == 200


def test_426_comes_first(lic, make_pool, monkeypatch):
    import config
    from services import licensing_service

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", "2.0.0")
    licensing_service.configure_client_gate()
    try:
        assert_error(lic.refresh("garbage", {"schema": 2}), 426, "client_upgrade_required")
        assert_error(lic.refresh(first["lease"]), 426, "client_upgrade_required")
    finally:
        monkeypatch.setattr(config, "MIN_WINDOWS_APP_VERSION", "0.0.0")
        licensing_service.configure_client_gate()


@pytest.mark.parametrize("change, field", [
    ({"request_id": "not-a-uuid"}, "request_id"),
    ({"request_id": str(uuid.uuid1())}, "request_id"),
    ({"lease": ""}, "lease"),
    ({"fingerprint_signals": {"schema": 2}}, "fingerprint_signals.schema"),
    ({"ambiguity_id": 7}, "ambiguity_id"),
])
def test_invalid_request_names_the_field(lic, make_pool, change, field):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    body = {"request_id": str(uuid.uuid4()), "lease": first["lease"], "ambiguity_id": None,
            "fingerprint_signals": make_signals(), "client": {"platform": "windows", "app_version": "1.0.0"}}
    body.update(change)
    response = lic.client.post("/licence/refresh", json=body)
    if response.status_code == 422:      # a non-string ambiguity_id is a type error: FastAPI's 422 first
        assert field == "ambiguity_id"
        return
    assert assert_error(response, 400, "invalid_request")["field"] == field


def test_fingerprint_unreadable(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    detail = assert_error(lic.refresh(first["lease"], make_signals(collection_errors=["Win32_BIOS"])),
                          400, "fingerprint_unreadable")
    assert detail["classes"] == ["Win32_BIOS"]


def test_licensing_unavailable_before_the_lease_is_read(lic, make_pool):
    from services import licence_lease

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    saved = dict(licence_lease._signing)
    licence_lease.use_signing_key(None, None, reason="test")
    try:
        assert_error(lic.refresh(first["lease"]), 503, "licensing_unavailable")
        assert_error(lic.refresh("not a lease at all"), 503, "licensing_unavailable")
    finally:
        licence_lease._signing.update(saved)


def _resign(lease, private_key, kid, **changes):
    from services.licence_lease import sign_lease

    payload = lease_payload(lease)
    payload.update(kid=kid, **changes)
    return sign_lease(payload, private_key)


def test_kid_0_the_published_test_key_is_lease_invalid(lic, make_pool):
    """I1 step 2: only the configured kid verifies. The TEST key is public."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    test_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode("vaqSxX9ClSwoMECP/J92vJWuEQYqyktbMlFU8YPu+Lg="))
    assert_error(lic.refresh(_resign(first["lease"], test_key, 0)), 403, "lease_invalid")


@pytest.mark.parametrize("make_lease", [
    lambda lease: _resign(lease, __import__("cryptography.hazmat.primitives.asymmetric.ed25519",
                                            fromlist=["x"]).Ed25519PrivateKey.generate(), 2),
    lambda lease: _resign(lease, __import__("cryptography.hazmat.primitives.asymmetric.ed25519",
                                            fromlist=["x"]).Ed25519PrivateKey.generate(), 1),
    lambda lease: lease[:-4] + "AAAA",
    lambda lease: lease + "=",
    lambda lease: "only-one-segment",
])
def test_unknown_kid_wrong_signature_and_malformed_leases_are_lease_invalid(lic, make_pool, make_lease):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    assert_error(lic.refresh(make_lease(first["lease"])), 403, "lease_invalid")


def test_lease_invalid_is_never_written_to_the_activation_log(lic, make_pool, caplog):
    """J3: an unverified lease has no trustworthy account. Application warning only."""
    import logging

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    before = log_rows(pool["account_id"])
    forged = _resign(first["lease"], Ed25519PrivateKey.generate(), 1)
    with caplog.at_level(logging.WARNING):
        assert_error(lic.refresh(forged), 403, "lease_invalid")
    assert log_rows(pool["account_id"]) == before
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "lease_invalid" in r.getMessage()]
    assert warnings, "a lease_invalid refresh must leave an application warning"
    assert first["binding_id"] in warnings[0].getMessage()
    assert forged not in warnings[0].getMessage(), "the lease string is never logged"


def test_a_signed_lease_that_disagrees_with_its_binding_is_lease_invalid_and_an_error(lic, make_pool,
                                                                                         licence_signing_key, caplog):
    """I1 step 3 / J3: a server defect, logged at ERROR and against the binding's account."""
    import logging

    from services import licence_lease

    pool, other = make_pool(5), make_pool(5)
    first = lic.activated(pool["pool_id"])
    key, kid = licence_lease.signing_key()
    wrong = _resign(first["lease"], key, kid, seat_pool_id=other["pool_id"])
    with caplog.at_level(logging.ERROR):
        assert_error(lic.refresh(wrong), 403, "lease_invalid")
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    denied = log_rows(pool["account_id"], "refresh_denied")
    assert len(denied) == 1 and denied[0]["binding_id"] == first["binding_id"]
    assert log_rows(other["account_id"]) == []


def test_unknown_binding_is_lease_revoked(lic, make_pool):
    from services import licence_lease

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    key, kid = licence_lease.signing_key()
    assert_error(lic.refresh(_resign(first["lease"], key, kid, binding_id="e" * 32)), 403, "lease_revoked")


def test_a_released_binding_is_lease_revoked_ahead_of_every_later_check(lic, make_pool):
    """Release beats everything after I1 step 3: a suspended pool, a lapsed subscription, a stranger's hardware."""
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    lic.released(first["binding_id"])
    execute("UPDATE seat_pools SET status = 'suspended' WHERE id = ?", (pool["pool_id"],))
    execute("UPDATE accounts SET subscription_status = 'expired' WHERE id = ?", (pool["account_id"],))
    assert_error(lic.refresh(first["lease"], make_signals(**PC_B)), 403, "lease_revoked")
    assert len(log_rows(pool["account_id"], "refresh_denied")) == 1


def test_a_superseded_installation_is_machine_mismatch(lic, make_pool):
    """I1 step 4: the binding was restored onto a newer installation."""
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    restored = lic.activated(pool["pool_id"])
    assert restored["result"] == "restored"
    assert_error(lic.refresh(first["lease"]), 403, "machine_mismatch")
    denied = log_rows(pool["account_id"], "refresh_denied")
    assert len(denied) == 1
    assert json.loads(denied[0]["detail_json"])["reason"] == "installation_superseded"
    # The current installation still refreshes.
    lic.refreshed(restored["lease"])


@pytest.mark.parametrize("column, value, status, code", [
    ("pool", "suspended", 409, "pool_inactive"),
    ("subscription", "expired", 402, "subscription_inactive"),
])
def test_suspended_pool_and_lapsed_subscription(lic, make_pool, column, value, status, code):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    if column == "pool":
        execute("UPDATE seat_pools SET status = ? WHERE id = ?", (value, pool["pool_id"]))
    else:
        execute("UPDATE accounts SET subscription_status = ? WHERE id = ?", (value, pool["account_id"]))
    detail = assert_error(lic.refresh(first["lease"]), status, code)
    if code == "subscription_inactive":
        assert WIRE_TIME.fullmatch(detail["subscription_expires_at"])
    assert len(log_rows(pool["account_id"], "refresh_denied")) == 1
    assert binding(first["binding_id"])["lease_serial"] == lease_payload(first["lease"])["lease_serial"]


# ---------------------------------------------------------------------------
# I4 — replays and the request history
# ---------------------------------------------------------------------------

def test_refresh_retried_after_a_lost_response_returns_the_same_lease(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    request_id = str(uuid.uuid4())
    one = lic.refreshed(first["lease"], request_id=request_id)
    two = lic.refreshed(first["lease"], request_id=request_id)
    assert two["lease"] == one["lease"]
    assert binding(first["binding_id"])["lease_serial"] == lease_payload(one["lease"])["lease_serial"]
    assert log_rows(pool["account_id"], "stale_serial_flagged") == []
    assert len(history(request_id)) == 1


def test_refresh_retried_after_a_later_refresh_returns_the_current_lease_and_issues_nothing(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    early = str(uuid.uuid4())
    lic.refreshed(first["lease"], request_id=early)
    later = lic.refreshed(first["lease"])           # a different request_id, issued a newer lease
    again = lic.refreshed(first["lease"], request_id=early)
    assert again["lease"] == later["lease"]
    assert binding(first["binding_id"])["lease_serial"] == lease_payload(later["lease"])["lease_serial"]


def test_an_activation_request_id_reused_for_a_refresh_is_request_id_reused(lic, make_pool):
    pool = make_pool(5)
    request_id = str(uuid.uuid4())
    first = lic.activated(pool["pool_id"], request_id=request_id)
    assert_error(lic.refresh(first["lease"], request_id=request_id), 409, "request_id_reused")
    assert binding(first["binding_id"])["lease_serial"] == 1


def test_a_refresh_request_id_reused_with_a_different_reading_is_request_id_reused(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    request_id = str(uuid.uuid4())
    lic.refreshed(first["lease"], request_id=request_id)
    assert_error(lic.refresh(first["lease"], make_signals(disk="SOMETHING-ELSE"), request_id=request_id),
                 409, "request_id_reused")


def test_refresh_replay_window_is_24_hours(lic, make_pool):
    from datetime import datetime, timedelta, timezone

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    request_id = str(uuid.uuid4())
    one = lic.refreshed(first["lease"], request_id=request_id)
    execute("UPDATE licence_requests SET created_at = ? WHERE request_id = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(), request_id))
    two = lic.refreshed(first["lease"], request_id=request_id)
    assert lease_payload(two["lease"])["lease_serial"] == lease_payload(one["lease"])["lease_serial"] + 1


# ---------------------------------------------------------------------------
# Spec §12 tests 15–19 — lease and expiry, on the in-process clock (I10)
# ---------------------------------------------------------------------------

def test_15_refresh_after_29_days(lic, make_pool, clock):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    clock(days=29)
    lic.refreshed(first["lease"])


def test_16_a_40_day_old_expired_lease_self_recovers(lic, make_pool, clock):
    """The one that matters most: a school back from a long holiday is not locked out."""
    from licensing_harness import active_bindings

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    clock(days=40)
    body = lic.refreshed(first["lease"])
    new = lease_payload(body["lease"])
    assert new["binding_id"] == first["binding_id"]
    assert len(active_bindings(pool["pool_id"])) == 1
    assert ambiguity_free(pool["pool_id"])
    logged = log_rows(pool["account_id"], "refresh")
    assert logged and json.loads(logged[-1]["detail_json"])["presented_lease_expired"] is True


def ambiguity_free(pool_id):
    from licensing_harness import ambiguities

    return ambiguities(pool_id) == []


def test_17_expired_lease_of_a_released_activation_is_denied(lic, make_pool, clock):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    lic.released(first["binding_id"])
    clock(days=40)
    assert_error(lic.refresh(first["lease"]), 403, "lease_revoked")


def test_18_lapsed_subscription_is_denied_until_renewal(lic, make_pool, clock):
    from datetime import timedelta

    from licensing_harness import ADMIN, bearer
    from services import licensing_time

    pool = make_pool(5, subscription_expires_at=(licensing_time.now() + timedelta(days=20)).isoformat())
    first = lic.activated(pool["pool_id"])
    clock(days=40)
    assert_error(lic.refresh(first["lease"]), 402, "subscription_inactive")
    renewed_until = (licensing_time.now() + timedelta(days=365)).isoformat()
    response = lic.client.patch(f"/institutions/accounts/{pool['account_id']}/subscription",
                                json={"subscription_status": "active", "subscription_expires_at": renewed_until},
                                headers=bearer(ADMIN))
    assert response.status_code == 200, response.text
    body = lic.refreshed(first["lease"])
    assert lease_payload(body["lease"])["binding_id"] == first["binding_id"]


def test_lease_never_outlives_the_subscription(lic, make_pool):
    from datetime import timedelta

    from services import licensing_time

    end = (licensing_time.now() + timedelta(days=10)).replace(microsecond=0)
    pool = make_pool(5, subscription_expires_at=end.isoformat())
    first = lic.activated(pool["pool_id"])
    body = lic.refreshed(first["lease"])
    assert body["lease_expires_at"] == end.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_19_released_offline_pc_is_denied_and_its_seat_went_to_another(lic, make_pool, clock):
    pool = make_pool(1)
    pc_a = lic.activated(pool["pool_id"])
    lic.released(pc_a["binding_id"])
    pc_b = lic.activated(pool["pool_id"], make_signals(**PC_B))
    assert pc_b["result"] == "activated"
    clock(days=31)                               # PC-A ran offline to its lease's end
    assert_error(lic.refresh(pc_a["lease"]), 403, "lease_revoked")
    lic.refreshed(pc_b["lease"], make_signals(**PC_B))


# ---------------------------------------------------------------------------
# Spec §12 test 20 — stale serial: evidence, not a denial
# ---------------------------------------------------------------------------

def test_20_stale_serial_is_logged_not_denied(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    lic.refreshed(first["lease"])                 # the "first machine" advances to N+1
    body = lic.refreshed(first["lease"])          # the copy still presents N
    assert body["result"] == "refreshed"
    flagged = log_rows(pool["account_id"], "stale_serial_flagged")
    assert len(flagged) == 1
    detail = json.loads(flagged[0]["detail_json"])
    assert detail["presented_serial"] < detail["current_serial"]


# ---------------------------------------------------------------------------
# Spec §12 tests 21–25 — machine verification (§8.6.1, I2)
# ---------------------------------------------------------------------------

def test_21_copied_lease_on_a_different_machine_is_machine_mismatch(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    before = binding(first["binding_id"])
    assert_error(lic.refresh(first["lease"], make_signals(**PC_B)), 403, "machine_mismatch")
    flagged = log_rows(pool["account_id"], "clone_flagged")
    assert len(flagged) == 1 and flagged[0]["binding_id"] == first["binding_id"]
    after = binding(first["binding_id"])
    assert after["revoked_at"] is None
    for column in ("lease_serial", "current_lease", "machine_fingerprint_hash", "fingerprint_signals_json"):
        assert after[column] == before[column]


def test_mismatch_holds_even_when_continuity_also_holds(lic, make_pool):
    """I2: a different motherboard with the same disk, NIC and MachineGuid is still a mismatch."""
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    other_board = make_signals(uuid=PC_B["uuid"], serial=PC_B["serial"], board_serial=PC_B["board_serial"])
    assert_error(lic.refresh(first["lease"], other_board), 403, "machine_mismatch")


def test_22_maintenance_is_not_a_mismatch_and_the_binding_ratchets(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    original_hash = binding(first["binding_id"])["machine_fingerprint_hash"]
    maintained = make_signals(board_serial="/B5QMT32/NEWBOARDREAD/", disk="NEW-SSD-0001",
                              total_physical_memory_bytes=17179869184)
    lic.refreshed(first["lease"], maintained)
    after = binding(first["binding_id"])
    assert after["machine_fingerprint_hash"] != original_hash
    assert json.loads(after["fingerprint_signals_json"])["disk_serials"][0]["serial"] == "NEW-SSD-0001"
    ratcheted = log_rows(pool["account_id"], "refresh")[-1]
    assert json.loads(ratcheted["detail_json"])["previous_fingerprint_hash"] == original_hash


def test_23_ratchet_accumulation(lic, make_pool):
    from services import fingerprint_service

    pool = make_pool(5)
    original = make_signals(serial="SER-0", board_serial="BRD-0")
    first = lic.activated(pool["pool_id"], original)
    steps = [make_signals(serial="SER-0", board_serial="BRD-1"),
             make_signals(serial="SER-2", board_serial="BRD-1"),
             make_signals(serial="SER-2", board_serial="BRD-3")]
    lease = first["lease"]
    for signals in steps:
        lease = lic.refreshed(lease, signals)["lease"]
    # The third succeeded BECAUSE the binding ratcheted: against day one it is not strong.
    day_one, third = fingerprint_service.read(original), fingerprint_service.read(steps[-1])
    index = fingerprint_service.PoolIndex([day_one])
    assert fingerprint_service.match_strength(third, day_one, index) != "strong"


def test_raw_only_changes_neither_ratchet_nor_log(lic, make_pool):
    """J6: the ratchet compares NORMALISED readings — RAM and a random Wi-Fi address change nothing."""
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    second = lic.refreshed(first["lease"])
    snapshot = binding(first["binding_id"])["fingerprint_signals_json"]
    noisy = make_signals(total_physical_memory_bytes=1234, bios_version="9.9.9")
    noisy["mac_addresses"].append({"mac": "06:11:22:33:44:55", "permanent_address": None, "name": "Wi-Fi",
                                   "pnp_device_id": "PCI\\VEN_8086&DEV_WIFI", "net_connection_id": "Wi-Fi"})
    refreshes_before = len(log_rows(pool["account_id"], "refresh"))
    lic.refreshed(second["lease"], noisy)
    assert binding(first["binding_id"])["fingerprint_signals_json"] == snapshot
    assert len(log_rows(pool["account_id"], "refresh")) == refreshes_before


def test_24_degenerate_binding_continuity_broken_raises_an_ambiguity(lic, make_pool):
    from licensing_harness import ambiguities

    pool = make_pool(5)
    first = lic.activated(pool["pool_id"], make_degenerate_signals(disk="WD-1111", nic="B44506111111"))
    presented = make_degenerate_signals(disk="WD-1111", nic="B44506111111",
                                        guid="12345678-1234-4234-8234-123456789abc")
    detail = assert_error(lic.refresh(first["lease"], presented), 409, "ambiguity_pending")
    rows = ambiguities(pool["pool_id"])
    assert [r["id"] for r in rows] == [detail["ambiguity_id"]]
    assert rows[0]["origin"] == "refresh"
    assert json.loads(rows[0]["candidate_binding_ids_json"]) == [first["binding_id"]]
    assert len(log_rows(pool["account_id"], "ambiguous_flagged")) == 1
    after = binding(first["binding_id"])
    assert after["revoked_at"] is None and after["lease_serial"] == 1


def test_24_degenerate_binding_continuity_intact_refreshes_and_ratchets(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"], make_degenerate_signals(disk="WD-2222", nic="B44506222222"))
    new_nic = make_degenerate_signals(disk="WD-2222", nic="B44506999999")
    lic.refreshed(first["lease"], new_nic)
    snapshot = json.loads(binding(first["binding_id"])["fingerprint_signals_json"])
    assert snapshot["mac_addresses"][0]["permanent_address"] == "B44506999999"


def test_24_continuity_requires_the_machine_guid(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"], make_degenerate_signals(disk="WD-3333", guid=None))
    assert_error(lic.refresh(first["lease"], make_degenerate_signals(disk="WD-3333", guid=None)),
                 409, "ambiguity_pending")


def test_continuity_with_no_disk_or_nic_on_the_snapshot_relies_on_installation_and_guid(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"], make_degenerate_signals(disk=None, nic=None))
    lic.refreshed(first["lease"], make_degenerate_signals(disk=None, nic=None))


def test_i2_extension_strong_binding_whose_firmware_lost_its_serial_refreshes(lic, make_pool):
    """I2: a BIOS update resets the serial to a placeholder; continuity carries it, no ambiguity."""
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    assert binding(first["binding_id"])["fingerprint_confidence"] == "strong"
    placeholder = make_signals(serial="To Be Filled By O.E.M.", board_serial="Default string")
    lic.refreshed(first["lease"], placeholder)
    assert ambiguity_free(pool["pool_id"])
    assert binding(first["binding_id"])["fingerprint_confidence"] == "strong"   # recorded at claim, never changed


def test_copied_lease_on_a_cloned_image_pc_is_not_refreshed(lic, make_pool):
    """Same installation_id and MachineGuid (copied with the image), different disk and NIC."""
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"], make_degenerate_signals(disk="WD-ORIGINAL", nic="B44506000AAA"))
    clone = make_degenerate_signals(disk="WD-CLONE-PC", nic="B44506000BBB")
    response = lic.refresh(first["lease"], clone)
    assert response.status_code != 200
    assert binding(first["binding_id"])["lease_serial"] == 1


def test_disk_cloned_onto_a_new_disk_refreshes_ratchets_and_keeps_refreshing(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"], make_degenerate_signals(disk="WD-OLD-DISK", nic="B44506000CCC"))
    new_disk = make_degenerate_signals(disk="WD-NEW-DISK", nic="B44506000CCC")
    second = lic.refreshed(first["lease"], new_disk)
    snapshot = json.loads(binding(first["binding_id"])["fingerprint_signals_json"])
    assert snapshot["disk_serials"][0]["serial"] == "WD-NEW-DISK"
    lic.refreshed(second["lease"], new_disk)


def test_25_denials_are_distinguishable(lic, make_pool):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    mismatch = assert_error(lic.refresh(a["lease"], make_signals(**PC_B)), 403, "machine_mismatch")
    lic.released(a["binding_id"])
    revoked = assert_error(lic.refresh(a["lease"]), 403, "lease_revoked")
    assert mismatch["code"] != revoked["code"] and mismatch["message"] != revoked["message"]


def test_a_collection_of_an_activation_raised_ambiguity_on_refresh_is_ambiguity_mismatch(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    assert_error(lic.refresh(first["lease"], reinstalled, ambiguity_id=ambiguity_id), 409, "ambiguity_mismatch")
