"""
tests/test_licensing_release_resolve.py — release, ambiguity resolution,
ambiguity collection on both endpoints, the admin ambiguity list, and the
one request history across all of them (brief §4.I I4–I8, §4.J, §9 R1;
contract §9.2, §10; spec §8.5 and §12 tests 8, 9, 14, 27, 28).

SQLite throughout; the races are in test_licensing_unit3a_pg.py.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import OTHER_USER, TEST_USER, make_degenerate_signals, make_signals
from licensing_harness import (  # noqa: F401  (lic is a fixture)
    ADMIN, PC_B, WIRE_TIME, active_bindings, all_bindings, ambiguities, ambiguity, assert_error, bearer, binding,
    execute, history, lease_payload, lic, log_rows, raise_activation_ambiguity, raise_refresh_ambiguity,
    without_server_time,
)


def _age_ambiguity(ambiguity_id, days=8):
    execute("UPDATE licence_ambiguities SET created_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), ambiguity_id))


# ===========================================================================
# Release (I7)
# ===========================================================================

def test_release_frees_capacity_immediately_and_reports_post_commit_counts(lic, make_pool):
    pool = make_pool(2)
    a = lic.activated(pool["pool_id"])
    lic.activated(pool["pool_id"], make_signals(**PC_B))
    body = lic.released(a["binding_id"], revoke_reason="hardware_failure", reason="PSU died")

    assert body["result"] == "released"
    assert body["binding_id"] == a["binding_id"] and body["seat_pool_id"] == pool["pool_id"]
    assert (body["pool_size"], body["active"], body["available"], body["over_capacity"]) == (2, 1, 1, False)
    assert WIRE_TIME.fullmatch(body["released_at"]) and WIRE_TIME.fullmatch(body["server_time"])
    row = binding(a["binding_id"])
    assert row["revoked_at"] is not None
    assert (row["revoke_reason"], row["revoked_by"]) == ("hardware_failure", TEST_USER)
    logged = log_rows(pool["account_id"], "release")
    assert len(logged) == 1 and logged[0]["reason"] == "PSU died"
    assert logged[0]["machine_fingerprint_hash"] == row["machine_fingerprint_hash"]
    assert logged[0]["actor_role"] == "institution_owner"


def test_8_dead_pc_released_replacement_activates_at_no_extra_cost(lic, make_pool):
    pool = make_pool(1)
    dead = lic.activated(pool["pool_id"])
    assert_error(lic.activate(pool["pool_id"], make_signals(**PC_B)), 409, "pool_full")
    lic.released(dead["binding_id"], revoke_reason="hardware_failure")
    replacement = lic.activated(pool["pool_id"], make_signals(**PC_B))
    assert replacement["result"] == "activated"
    assert [b["id"] for b in active_bindings(pool["pool_id"])] == [replacement["binding_id"]]


def test_9_released_pc_returning_is_never_restored(lic, make_pool):
    pool = make_pool(5)
    first = lic.activated(pool["pool_id"])
    lic.released(first["binding_id"])
    back = lic.activated(pool["pool_id"])
    assert back["result"] == "activated" and back["binding_id"] != first["binding_id"]


def test_release_defaults_revoke_reason_to_manual(lic, make_pool):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    lic.released(a["binding_id"])
    assert binding(a["binding_id"])["revoke_reason"] == "manual"


def test_admin_may_release_any_school_binding(lic, make_pool):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    lic.released(a["binding_id"], as_user=ADMIN)
    assert log_rows(pool["account_id"], "release")[0]["actor_role"] == "exampartner_admin"


def test_27_another_schools_binding_and_an_unknown_binding_look_identical(lic, make_pool):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    other = assert_error(lic.release(a["binding_id"], as_user=OTHER_USER), 403, "binding_access_denied")
    unknown = assert_error(lic.release("f" * 32, as_user=OTHER_USER), 403, "binding_access_denied")
    assert without_server_time({"detail": other}) == without_server_time({"detail": unknown})
    assert binding(a["binding_id"])["revoked_at"] is None


def test_28_a_lease_is_not_a_bearer_token(lic, make_pool):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    assert_error(lic.release(a["binding_id"], headers={"Authorization": f"Bearer {a['lease']}"}),
                 401, "not_authenticated")
    assert binding(a["binding_id"])["revoked_at"] is None


def test_release_without_a_token_is_not_authenticated(lic, make_pool):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    assert_error(lic.release(a["binding_id"], headers={}), 401, "not_authenticated")


@pytest.mark.parametrize("body", [
    {},
    {"request_id": None},
    {"request_id": "not-a-uuid"},
    {"request_id": "11111111-1111-1111-1111-111111111111"},           # not v4
    {"request_id": "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b9c", "revoke_reason": "bored"},
    {"request_id": "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b9c", "reason": "x" * 501},
    {"request_id": "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b9c", "reason": 7},
])
def test_release_validation_is_invalid_request(lic, make_pool, body):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    assert_error(lic.release(a["binding_id"], body=body), 400, "invalid_request")
    assert binding(a["binding_id"])["revoked_at"] is None


def test_release_order_401_before_400_before_403(lic, make_pool):
    pool = make_pool(5)
    a = lic.activated(pool["pool_id"])
    assert_error(lic.release(a["binding_id"], body={}, headers={}), 401, "not_authenticated")
    assert_error(lic.release(a["binding_id"], body={}, as_user=OTHER_USER), 400, "invalid_request")


def test_release_retried_replays_the_stored_response(lic, make_pool):
    pool = make_pool(3)
    a = lic.activated(pool["pool_id"])
    request_id = str(uuid.uuid4())
    one = lic.released(a["binding_id"], request_id=request_id, revoke_reason="retired")
    lic.activated(pool["pool_id"], make_signals(**PC_B))        # the counts move on
    two = lic.released(a["binding_id"], request_id=request_id, revoke_reason="retired")
    assert without_server_time(two) == without_server_time(one)
    assert len(log_rows(pool["account_id"], "release")) == 1


def test_a_new_request_on_a_released_binding_is_already_released(lic, make_pool):
    pool = make_pool(3)
    a = lic.activated(pool["pool_id"])
    first = lic.released(a["binding_id"])
    revoked_at = binding(a["binding_id"])["revoked_at"]
    again = lic.released(a["binding_id"], revoke_reason="support")
    assert again["result"] == "already_released"
    assert again["released_at"] == first["released_at"]
    row = binding(a["binding_id"])
    assert (row["revoked_at"], row["revoke_reason"]) == (revoked_at, "manual")
    assert len(log_rows(pool["account_id"], "release")) == 1


def test_release_request_id_with_a_different_revoke_reason_is_reused(lic, make_pool):
    pool = make_pool(3)
    a = lic.activated(pool["pool_id"])
    request_id = str(uuid.uuid4())
    lic.released(a["binding_id"], request_id=request_id)
    assert_error(lic.release(a["binding_id"], request_id=request_id, revoke_reason="support"),
                 409, "request_id_reused")


def test_an_activation_request_id_reused_for_a_release_is_reused(lic, make_pool):
    pool = make_pool(3)
    request_id = str(uuid.uuid4())
    a = lic.activated(pool["pool_id"], request_id=request_id)
    assert_error(lic.release(a["binding_id"], request_id=request_id), 409, "request_id_reused")
    assert binding(a["binding_id"])["revoked_at"] is None


def test_release_replay_never_shows_another_school_the_response(lic, make_pool):
    """Authorisation runs before the replay check (I7)."""
    pool = make_pool(3)
    a = lic.activated(pool["pool_id"])
    request_id = str(uuid.uuid4())
    lic.released(a["binding_id"], request_id=request_id)
    assert_error(lic.release(a["binding_id"], request_id=request_id, as_user=OTHER_USER), 403,
                 "binding_access_denied")


def test_release_reason_never_gates_the_release(lic, make_pool):
    pool = make_pool(3)
    a = lic.activated(pool["pool_id"])
    assert lic.released(a["binding_id"], revoke_reason="other", reason="")["result"] == "released"


def test_release_when_licensing_is_unavailable(lic, make_pool):
    from services import licence_lease

    pool = make_pool(3)
    a = lic.activated(pool["pool_id"])
    saved = dict(licence_lease._signing)
    licence_lease.use_signing_key(None, None, reason="test")
    try:
        assert_error(lic.release(a["binding_id"]), 503, "licensing_unavailable")
    finally:
        licence_lease._signing.update(saved)
    assert binding(a["binding_id"])["revoked_at"] is None


def test_released_over_capacity_counts(lic, make_pool):
    pool = make_pool(3)
    a = lic.activated(pool["pool_id"])
    lic.activated(pool["pool_id"], make_signals(**PC_B))
    lic.activated(pool["pool_id"], make_signals(uuid="11112222-3333-4444-5555-666677778888", serial="CCC",
                                                board_serial="CCCB", disk="DCCC", nic="B44506CCCCCC"))
    execute("UPDATE seat_pools SET pool_size = 1 WHERE id = ?", (pool["pool_id"],))
    body = lic.released(a["binding_id"], revoke_reason="renewal_retire")
    assert (body["pool_size"], body["active"], body["available"], body["over_capacity"]) == (1, 2, 0, True)


# ===========================================================================
# Resolution (I5) — authorisation, validation, order
# ===========================================================================

def test_resolve_needs_a_token_then_an_admin(lic, make_pool):
    pool = make_pool(5)
    _, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    assert_error(lic.resolve(ambiguity_id, "treat_as_new", headers={}), 401, "not_authenticated")
    assert_error(lic.resolve(ambiguity_id, "treat_as_new", as_user=TEST_USER), 403, "admin_required")
    assert_error(lic.resolve(ambiguity_id, "treat_as_new", body={}, as_user=TEST_USER), 403, "admin_required")
    assert ambiguity(ambiguity_id)["status"] == "open"


@pytest.mark.parametrize("body", [
    {},
    {"request_id": "not-a-uuid", "decision": "treat_as_new"},
    {"request_id": str(uuid.uuid4()), "decision": "maybe"},
    {"request_id": str(uuid.uuid4()), "decision": "recognize_existing"},                   # binding_id required
    {"request_id": str(uuid.uuid4()), "decision": "treat_as_new", "binding_id": "a" * 32},  # and refused otherwise
])
def test_resolve_validation(lic, make_pool, body):
    pool = make_pool(5)
    _, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    assert_error(lic.resolve(ambiguity_id, None, body=body), 400, "invalid_request")


def test_resolve_unknown_ambiguity_is_404(lic):
    assert_error(lic.resolve("d" * 32, "treat_as_new"), 404, "ambiguity_not_found")


# ===========================================================================
# Spec §12 test 14 and recognize_existing
# ===========================================================================

def test_14_recognize_existing_then_the_pc_collects_and_is_restored(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    body = lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    assert (body["ok"], body["ambiguity_id"], body["status"], body["resolution"], body["resolved_binding_id"]) == \
        (True, ambiguity_id, "resolved", "recognize_existing", first["binding_id"])
    assert WIRE_TIME.fullmatch(body["resolved_at"]) and WIRE_TIME.fullmatch(body["server_time"])
    resolved_log = log_rows(pool["account_id"], "ambiguity_resolved")
    assert len(resolved_log) == 1 and resolved_log[0]["actor_role"] == "exampartner_admin"
    assert json.loads(resolved_log[0]["detail_json"])["decision"] == "recognize_existing"

    collected = lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id)
    assert collected["result"] == "restored"
    assert collected["binding_id"] == first["binding_id"]
    assert collected["installation_id"] != first["installation_id"]
    assert len(active_bindings(pool["pool_id"])) == 1
    row = ambiguity(ambiguity_id)
    assert row["status"] == "consumed" and row["consumed_at"] is not None
    # The collected lease refreshes on the reinstalled PC.
    lic.refreshed(collected["lease"], reinstalled)


def test_recognize_existing_of_a_non_candidate_is_candidate_invalid(lic, make_pool):
    pool = make_pool(5)
    _, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    stranger = lic.activated(pool["pool_id"], make_signals(**PC_B))
    detail = assert_error(lic.resolve(ambiguity_id, "recognize_existing", stranger["binding_id"]),
                          409, "ambiguity_candidate_invalid")
    assert detail["candidate_binding_ids"] == json.loads(ambiguity(ambiguity_id)["candidate_binding_ids_json"])
    assert ambiguity(ambiguity_id)["status"] == "open"


def test_recognize_existing_of_a_candidate_released_before_resolution_is_candidate_invalid(lic, make_pool):
    pool = make_pool(5)
    first, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    lic.released(first["binding_id"])
    assert_error(lic.resolve(ambiguity_id, "recognize_existing", first["binding_id"]),
                 409, "ambiguity_candidate_invalid")
    assert ambiguity(ambiguity_id)["status"] == "open"


def test_candidate_released_after_resolution_collects_as_lease_revoked(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    lic.released(first["binding_id"])
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id), 403, "lease_revoked")
    assert ambiguity(ambiguity_id)["status"] == "resolved"


# ===========================================================================
# treat_as_new
# ===========================================================================

def test_treat_as_new_claims_at_resolution_and_the_pc_collects_activated(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    body = lic.resolved(ambiguity_id, "treat_as_new")
    new_id = body["resolved_binding_id"]
    assert new_id and new_id != first["binding_id"]
    assert len(active_bindings(pool["pool_id"])) == 2
    new = binding(new_id)
    assert new["current_lease"] is None                      # issued when the PC collects
    activate_log = [r for r in log_rows(pool["account_id"], "activate") if r["binding_id"] == new_id]
    assert len(activate_log) == 1
    assert (activate_log[0]["performed_by"], activate_log[0]["actor_role"]) == (ADMIN, "exampartner_admin")

    collected = lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id)
    assert (collected["result"], collected["binding_id"]) == ("activated", new_id)
    assert lease_payload(collected["lease"])["binding_id"] == new_id
    assert len(active_bindings(pool["pool_id"])) == 2
    assert ambiguity(ambiguity_id)["status"] == "consumed"


def test_treat_as_new_on_a_full_pool_is_pool_full_to_the_admin_and_stays_open(lic, make_pool):
    pool = make_pool(1)
    _, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    seats_claimed_at = query_last_claim_at(pool["pool_id"])
    detail = assert_error(lic.resolve(ambiguity_id, "treat_as_new"), 409, "pool_full")
    assert (detail["pool_size"], detail["active"]) == (1, 1)
    assert ambiguity(ambiguity_id)["status"] == "open"
    assert len(all_bindings(pool["pool_id"])) == 1
    # J8: the stored refusal commits only its history row — no business trace.
    assert query_last_claim_at(pool["pool_id"]) == seats_claimed_at
    assert log_rows(pool["account_id"], "ambiguity_resolved") == []


def query_last_claim_at(pool_id):
    from licensing_harness import query

    return query("SELECT last_claim_at FROM seat_pools WHERE id = ?", (pool_id,))[0]["last_claim_at"]


def test_a_stored_pool_full_replays_even_after_a_seat_is_freed(lic, make_pool):
    """I5: the same request_id replays the refusal it got; a new request_id decides afresh."""
    pool = make_pool(1)
    first, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    request_id = str(uuid.uuid4())
    one = assert_error(lic.resolve(ambiguity_id, "treat_as_new", request_id=request_id), 409, "pool_full")
    lic.released(first["binding_id"])
    two = assert_error(lic.resolve(ambiguity_id, "treat_as_new", request_id=request_id), 409, "pool_full")
    assert without_server_time({"detail": two}) == without_server_time({"detail": one})
    assert len(active_bindings(pool["pool_id"])) == 0
    assert lic.resolved(ambiguity_id, "treat_as_new")["resolved_binding_id"]


@pytest.mark.parametrize("change, status, code", [
    ("UPDATE seat_pools SET status = 'suspended' WHERE id = ?", 409, "pool_inactive"),
    ("UPDATE accounts SET subscription_status = 'expired' WHERE id = (SELECT account_id FROM seat_pools WHERE id = ?)",
     402, "subscription_inactive"),
])
def test_treat_as_new_entitlement_refusals_go_to_the_admin(lic, make_pool, change, status, code):
    pool = make_pool(5)
    _, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    execute(change, (pool["pool_id"],))
    assert_error(lic.resolve(ambiguity_id, "treat_as_new"), status, code)
    assert ambiguity(ambiguity_id)["status"] == "open"
    assert len(all_bindings(pool["pool_id"])) == 1


def test_treat_as_new_refresh_raised_claims_nothing_and_collects_machine_mismatch(lic, make_pool):
    pool = make_pool(5)
    first, presented, _, ambiguity_id = raise_refresh_ambiguity(lic, pool["pool_id"])
    body = lic.resolved(ambiguity_id, "treat_as_new")
    assert body["resolved_binding_id"] is None
    assert len(all_bindings(pool["pool_id"])) == 1
    for _ in range(2):          # never consumed, so every collection says the same (J6)
        assert_error(lic.refresh(first["lease"], presented, ambiguity_id=ambiguity_id), 403, "machine_mismatch")
    assert ambiguity(ambiguity_id)["status"] == "resolved"


# ===========================================================================
# Resolution state checks
# ===========================================================================

def test_a_second_resolution_is_already_resolved_with_the_first_ones_fields(lic, make_pool):
    pool = make_pool(5)
    first, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    winner = lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    for decision, binding_id in (("recognize_existing", first["binding_id"]), ("treat_as_new", None)):
        detail = assert_error(lic.resolve(ambiguity_id, decision, binding_id), 409, "ambiguity_already_resolved")
        assert (detail["resolution"], detail["resolved_binding_id"], detail["resolved_at"]) == \
            (winner["resolution"], winner["resolved_binding_id"], winner["resolved_at"])
    assert len(log_rows(pool["account_id"], "ambiguity_resolved")) == 1


def test_resolve_retried_replays_the_stored_outcome_and_changes_nothing(lic, make_pool):
    pool = make_pool(5)
    first, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    request_id = str(uuid.uuid4())
    one = lic.resolved(ambiguity_id, "treat_as_new", request_id=request_id)
    two = lic.resolved(ambiguity_id, "treat_as_new", request_id=request_id)
    assert without_server_time(two) == without_server_time(one)
    assert len(active_bindings(pool["pool_id"])) == 2
    assert len(log_rows(pool["account_id"], "ambiguity_resolved")) == 1


def test_resolving_a_consumed_ambiguity_is_ambiguity_consumed(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id)
    detail = assert_error(lic.resolve(ambiguity_id, "treat_as_new"), 409, "ambiguity_consumed")
    assert detail["resolution"] == "recognize_existing"


def test_an_ambiguity_older_than_7_days_cannot_be_resolved_and_collects_expired(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    _age_ambiguity(ambiguity_id)
    assert_error(lic.resolve(ambiguity_id, "recognize_existing", first["binding_id"]), 409, "ambiguity_expired")
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id), 409, "ambiguity_expired")


def test_a_refresh_request_id_reused_for_a_resolution_is_reused(lic, make_pool):
    pool = make_pool(5)
    first, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    request_id = str(uuid.uuid4())
    lic.refreshed(first["lease"], make_degenerate_signals(disk="WD-AAAA", nic="B44506AAAAAA"), request_id=request_id)
    assert_error(lic.resolve(ambiguity_id, "treat_as_new", request_id=request_id), 409, "request_id_reused")
    assert ambiguity(ambiguity_id)["status"] == "open"


# ===========================================================================
# Collection (I6)
# ===========================================================================

def test_collecting_twice_issues_one_lease_then_ambiguity_consumed(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    request_id = str(uuid.uuid4())
    one = lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=request_id)
    replay = lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=request_id)
    assert without_server_time(replay) == without_server_time(one)
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id), 409, "ambiguity_consumed")
    assert len(log_rows(pool["account_id"], "restore")) == 1


def test_a_replayed_collection_whose_binding_was_since_released_is_lease_revoked(lic, make_pool):
    """J2: release beats a replay."""
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    request_id = str(uuid.uuid4())
    lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=request_id)
    lic.released(first["binding_id"])
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=request_id),
                 403, "lease_revoked")


def test_refresh_raised_recognize_existing_collects_refreshed_and_ratchets(lic, make_pool):
    pool = make_pool(5)
    first, presented, _, ambiguity_id = raise_refresh_ambiguity(lic, pool["pool_id"])
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    body = lic.refreshed(first["lease"], presented, ambiguity_id=ambiguity_id)
    assert body["result"] == "refreshed"
    assert lease_payload(body["lease"])["lease_serial"] == lease_payload(first["lease"])["lease_serial"] + 1
    snapshot = json.loads(binding(first["binding_id"])["fingerprint_signals_json"])
    assert snapshot["windows_machine_guid"] == presented["windows_machine_guid"]
    assert ambiguity(ambiguity_id)["status"] == "consumed"
    # The ratchet took: the next ordinary refresh with that reading passes on continuity.
    lic.refreshed(body["lease"], presented)


def test_refresh_raised_collection_states(lic, make_pool):
    pool = make_pool(5)
    first, presented, raising_request_id, ambiguity_id = raise_refresh_ambiguity(lic, pool["pool_id"])
    again = assert_error(lic.refresh(first["lease"], presented, ambiguity_id=ambiguity_id), 409, "ambiguity_pending")
    assert again["ambiguity_id"] == ambiguity_id
    assert_error(lic.refresh(first["lease"], presented, ambiguity_id=ambiguity_id, request_id=raising_request_id),
                 409, "request_id_reused")
    assert_error(lic.refresh(first["lease"], make_degenerate_signals(disk="WD-RRRR", nic="B44506RRRRRR"),
                             ambiguity_id=ambiguity_id), 409, "ambiguity_mismatch")
    _age_ambiguity(ambiguity_id)
    assert_error(lic.refresh(first["lease"], presented, ambiguity_id=ambiguity_id), 409, "ambiguity_expired")


def test_refresh_collection_with_another_bindings_lease_is_ambiguity_mismatch(lic, make_pool):
    pool = make_pool(5)
    _, presented, _, ambiguity_id = raise_refresh_ambiguity(lic, pool["pool_id"])
    other = lic.activated(pool["pool_id"], make_signals(**PC_B))
    assert_error(lic.refresh(other["lease"], make_signals(**PC_B), ambiguity_id=ambiguity_id),
                 409, "ambiguity_mismatch")


def test_refresh_raised_raising_request_replays_its_pending(lic, make_pool):
    pool = make_pool(5)
    first, presented, raising_request_id, ambiguity_id = raise_refresh_ambiguity(lic, pool["pool_id"])
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    again = assert_error(lic.refresh(first["lease"], presented, request_id=raising_request_id),
                         409, "ambiguity_pending")
    assert again["ambiguity_id"] == ambiguity_id


# ===========================================================================
# J10 — the raising activation replays its original outcome
# ===========================================================================

def test_j10_raising_activation_replays_its_original_pending_whatever_happened_since(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, raising_request_id, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    original = lic.activate(pool["pool_id"], reinstalled, request_id=raising_request_id).json()
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    after_resolve = lic.activate(pool["pool_id"], reinstalled, request_id=raising_request_id)
    assert_error(after_resolve, 409, "ambiguity_pending")
    assert after_resolve.json()["detail"]["ambiguity_id"] == ambiguity_id
    assert without_server_time(after_resolve.json()) == without_server_time(original)
    lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id)      # collected
    _age_ambiguity(ambiguity_id)
    after_all = lic.activate(pool["pool_id"], reinstalled, request_id=raising_request_id)
    assert without_server_time(after_all.json()) == without_server_time(original)


# ===========================================================================
# Brief §9 R1 — one authoritative history
# ===========================================================================

def test_r1_1_collection_reusing_a_recent_activation_request_id_in_the_same_pool(lic, make_pool):
    pool = make_pool(5)
    used = str(uuid.uuid4())
    lic.activated(pool["pool_id"], make_signals(**PC_B), request_id=used)
    _, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=used),
                 409, "request_id_reused")


def test_r1_2_collection_reusing_a_request_id_from_another_pool(lic, make_pool):
    pool, other = make_pool(5), make_pool(5)
    used = str(uuid.uuid4())
    lic.activated(other["pool_id"], request_id=used)
    _, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=used),
                 409, "request_id_reused")


def test_r1_3_collection_reusing_another_ambiguitys_request_id(lic, make_pool):
    pool, other = make_pool(5), make_pool(5)
    _, _, other_raising, _ = raise_activation_ambiguity(lic, other["pool_id"], disk="WD-OTHER", nic="B44506000DDD")
    _, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=other_raising),
                 409, "request_id_reused")


def test_r1_4_a_new_collection_request_id_from_the_raising_pc_gets_the_same_outcome(lic, make_pool):
    pool = make_pool(5)
    _, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    detail = assert_error(lic.activate(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id), 409,
                          "ambiguity_pending")
    assert detail["ambiguity_id"] == ambiguity_id


def test_r1_5_activation_id_overtaken_by_a_restore_replays_its_original_outcome(lic, make_pool):
    pool = make_pool(5)
    r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
    first = lic.activated(pool["pool_id"], request_id=r1)
    restored = lic.activated(pool["pool_id"], request_id=r2)
    assert restored["result"] == "restored"
    replay = lic.activated(pool["pool_id"], request_id=r1)
    assert without_server_time(replay) == without_server_time(first)
    assert binding(first["binding_id"])["installation_id"] == restored["installation_id"]   # nothing changed
    assert len(log_rows(pool["account_id"], "restore")) == 1


def test_j2_a_replayed_activation_whose_binding_was_released_is_lease_revoked(lic, make_pool):
    pool = make_pool(5)
    request_id = str(uuid.uuid4())
    first = lic.activated(pool["pool_id"], request_id=request_id)
    lic.released(first["binding_id"])
    assert_error(lic.activate(pool["pool_id"], request_id=request_id), 403, "lease_revoked")
    assert active_bindings(pool["pool_id"]) == []


def test_every_operation_records_itself_in_the_history(lic, make_pool):
    pool = make_pool(5)
    ids = {name: str(uuid.uuid4()) for name in ("activate", "refresh", "release", "resolve", "collect")}
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    other = lic.activated(pool["pool_id"], make_signals(**PC_B), request_id=ids["activate"])
    lic.refreshed(other["lease"], make_signals(**PC_B), request_id=ids["refresh"])
    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"], request_id=ids["resolve"])
    lic.activated(pool["pool_id"], reinstalled, ambiguity_id=ambiguity_id, request_id=ids["collect"])
    lic.released(other["binding_id"], request_id=ids["release"])
    operations = {name: [r["operation"] for r in history(rid)] for name, rid in ids.items()}
    assert operations == {"activate": ["activate"], "refresh": ["refresh"], "release": ["release"],
                          "resolve": ["resolve"], "collect": ["activate"]}


# ===========================================================================
# I8 — the admin ambiguity list
# ===========================================================================

def test_admin_ambiguity_list(lic, make_pool):
    pool = make_pool(5)
    first, reinstalled, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    response = lic.list_ambiguities()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    [row] = [a for a in body["ambiguities"] if a["ambiguity_id"] == ambiguity_id]
    assert (row["account_id"], row["account_name"], row["seat_pool_id"], row["pool_label"]) == \
        (pool["account_id"], "Test School", pool["pool_id"], "Lab")
    assert (row["origin"], row["status"], row["machine_label"]) == ("activate", "open", "LAB-PC-07")
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    assert datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) - created == timedelta(days=7)
    presented = row["presented"]
    assert presented["smbios_uuid"] is None and presented["serials"] == []
    assert presented["system_disk"] == "WD-AAAA" and presented["onboard_addresses"] == ["B44506AAAAAA"]
    assert presented["machine_guid"] == "99999999888847778666555555555555"
    [candidate] = row["candidates"]
    assert candidate["binding_id"] == first["binding_id"]
    assert (candidate["active"], candidate["fingerprint_confidence"]) == (True, "degenerate")
    assert candidate["match_strength"] == "none" and "last_seen_at" in candidate

    lic.resolved(ambiguity_id, "recognize_existing", first["binding_id"])
    assert all(a["ambiguity_id"] != ambiguity_id for a in lic.list_ambiguities().json()["ambiguities"])
    resolved = lic.list_ambiguities("resolved").json()["ambiguities"]
    assert [a["ambiguity_id"] for a in resolved] == [ambiguity_id]


def test_admin_ambiguity_list_reports_expired_ones_as_expired(lic, make_pool):
    pool = make_pool(5)
    _, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    _age_ambiguity(ambiguity_id)
    assert all(a["ambiguity_id"] != ambiguity_id for a in lic.list_ambiguities().json()["ambiguities"])
    assert [a["ambiguity_id"] for a in lic.list_ambiguities("expired").json()["ambiguities"]] == [ambiguity_id]


def test_admin_ambiguity_list_is_admin_only(lic):
    assert lic.list_ambiguities(as_user=TEST_USER).status_code == 403
    assert lic.list_ambiguities("nonsense").status_code == 400
