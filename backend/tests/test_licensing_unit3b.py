"""
tests/test_licensing_unit3b.py — Unit 3b on SQLite: a school manages its own
computers (brief §4.K, §4.L, §5; contract v1.1 §10A).

- the licence session token on the five owner routes, and nowhere else (K1, L2);
- the owner reads: pools, machines, history and its keyset cursor (K2);
- rename (K3) and release under the session token (K4);
- account editing (K5) and pool editing (K6, L4), including spec §12 tests 5,
  29 and 30 through the pool-editing route, and suspension;
- the signals review list (K8) and the test-clock endpoint (K9).

The race guards for rename, pool editing and account editing are in
test_licensing_unit3b_pg.py: SQLite serialises writers and would pass them
regardless.
"""
import base64
import json
import secrets
import uuid
from datetime import timedelta

import pytest

from conftest import OTHER_USER, TEST_USER, make_signals
from licensing_harness import (  # noqa: F401  (lic and clock are fixtures)
    ADMIN, PC_B, WIRE_TIME, active_bindings, assert_error, bearer, binding, clock, execute, history, lease_payload,
    lic, log_rows, pc, query, raise_activation_ambiguity, register_user, without_server_time,
)

EVENT_KEYS = {"event_id", "at", "action", "binding_id", "pool_id", "machine_label", "actor", "actor_identifier",
              "revoke_reason", "reason"}


def _school(lic, make_pool, size=5, owner=TEST_USER):
    return make_pool(size, owner_identifier=owner)


def _expired_session(identifier, role="institution_owner"):
    import time

    from services.licence_session import issue_activation_token

    token, _ = issue_activation_token(identifier, role, now=int(time.time()) - 16 * 60)
    return {"Authorization": f"Bearer {token}"}


def _counts():
    return {table: query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            for table in ("accounts", "seat_pools", "institution_seats", "seat_activation_log",
                          "licence_ambiguities", "licence_requests")}


# ===========================================================================
# K1 — the licence session token
# ===========================================================================

def test_session_token_is_accepted_on_all_five_owner_routes_for_owner_and_admin(lic, make_pool):
    pool = _school(lic, make_pool)
    for who in (TEST_USER, ADMIN):
        pc_body = lic.activated(pool["pool_id"], pc(1 if who == TEST_USER else 2))
        headers = lic.session(who)
        account_id = pool["account_id"]
        assert lic.pools(account_id, headers=headers).status_code == 200
        assert lic.machines(account_id, headers=headers).status_code == 200
        assert lic.owner_history(account_id, headers=headers).status_code == 200
        renamed = lic.rename(pc_body["binding_id"], f"RENAMED-{who}", headers=headers)
        assert renamed.status_code == 200 and renamed.json()["result"] == "renamed", renamed.text
        released = lic.release(pc_body["binding_id"], headers=headers)
        assert released.status_code == 200 and released.json()["result"] == "released", released.text
    # Who acted is recorded from the RE-DERIVED role, never the token's.
    roles = {r["performed_by"]: r["actor_role"] for r in log_rows(pool["account_id"], "release")}
    assert roles == {TEST_USER: "institution_owner", ADMIN: "exampartner_admin"}


def test_ordinary_bearer_token_still_works_on_owner_routes_and_is_refused_at_activate(lic, make_pool):
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    account_id = pool["account_id"]
    assert lic.pools(account_id).status_code == 200
    assert lic.machines(account_id).status_code == 200
    assert lic.owner_history(account_id).status_code == 200
    assert lic.rename(body["binding_id"], "BEARER").status_code == 200
    assert lic.release(body["binding_id"]).status_code == 200
    refused = lic.client.post("/licence/activate", headers=bearer(TEST_USER), json={
        "request_id": str(uuid.uuid4()), "pool_id": pool["pool_id"], "machine_label": "X",
        "fingerprint_signals": make_signals(**PC_B), "client": {"platform": "windows", "app_version": "1.0.0"}})
    assert_error(refused, 401, "activation_session_invalid")
    assert len(active_bindings(pool["pool_id"])) == 0


def test_session_token_grants_nothing_outside_the_owner_routes(lic, make_pool):
    """L2: each route keeps its EXISTING refusal, and nothing protected changes."""
    pool = _school(lic, make_pool)
    first, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    headers = lic.session(ADMIN)                    # even an ADMIN's session token
    before = _counts()
    pool_row = query("SELECT * FROM seat_pools WHERE id = ?", (pool["pool_id"],))[0]
    account_row = query("SELECT * FROM accounts WHERE id = ?", (pool["account_id"],))[0]

    # 401 not_authenticated: the coded admin and support routes.
    assert_error(lic.resolve(ambiguity_id, "recognize_existing", first["binding_id"], headers=headers),
                 401, "not_authenticated")
    assert_error(lic.edit_account(pool["account_id"], name="Hijacked", headers=headers), 401, "not_authenticated")
    assert_error(lic.edit_pool(pool["pool_id"], pool_size=999, headers=headers), 401, "not_authenticated")
    assert_error(lic.signals(headers=headers), 401, "not_authenticated")
    assert_error(lic.test_clock({"offset_seconds": 3600}, headers=headers), 401, "not_authenticated")

    # 403 "Admin access required": F11's setup routes, the I8 list, routes/admin.py.
    for response in (
        lic.client.post("/institutions/accounts", headers=headers,
                        json={"name": "X", "owner_identifier": TEST_USER}),
        lic.client.post(f"/institutions/accounts/{pool['account_id']}/seat_pools", headers=headers,
                        json={"pool_size": 5}),
        lic.client.patch(f"/institutions/accounts/{pool['account_id']}/subscription", headers=headers,
                         json={"subscription_status": "expired", "subscription_expires_at": "2020-01-01T00:00:00Z"}),
        lic.client.get("/support/licensing/ambiguities", headers=headers),
        lic.client.get("/admin/questions", headers=headers),
        lic.client.get("/admin/feedback", headers=headers),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Admin access required"

    # The unauthenticated answer on ordinary routes that use get_current_user().
    for path in ("/me", "/auth/devices", "/cbt/papers?subject=Physics", "/cbt/questions?subject=Physics",
                 "/cbt/theory-questions?subject=Physics&paper=2"):
        response = lic.client.get(path, headers=headers)
        assert response.status_code == 401, (path, response.text)

    assert _counts() == before
    assert query("SELECT * FROM seat_pools WHERE id = ?", (pool["pool_id"],))[0] == pool_row
    assert query("SELECT * FROM accounts WHERE id = ?", (pool["account_id"],))[0] == account_row
    assert query("SELECT status FROM licence_ambiguities WHERE id = ?", (ambiguity_id,))[0]["status"] == "open"


def test_get_current_user_still_returns_none_for_a_session_token(lic, make_pool):
    from services.auth_utils import get_current_user

    _school(lic, make_pool)

    assert get_current_user(lic.session(TEST_USER)["Authorization"]) is None


@pytest.mark.parametrize("headers", ["expired_session", "expired_bearer", "garbage", "none", "basic"])
def test_expired_or_missing_token_is_not_authenticated_on_every_owner_route(lic, make_pool, headers):
    from services.auth_utils import make_token

    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    headers = {
        "expired_session": _expired_session(TEST_USER),
        "expired_bearer": {"Authorization": f"Bearer {make_token(TEST_USER, ttl_seconds=-60)}"},
        "garbage": {"Authorization": "Bearer not-a-token"},
        "none": {},
        "basic": {"Authorization": "Basic dXNlcjpwYXNz"},
    }[headers]
    account_id = pool["account_id"]
    for response in (lic.pools(account_id, headers=headers), lic.machines(account_id, headers=headers),
                     lic.owner_history(account_id, headers=headers),
                     lic.rename(body["binding_id"], "NOPE", headers=headers),
                     lic.release(body["binding_id"], headers=headers)):
        assert_error(response, 401, "not_authenticated")
    assert binding(body["binding_id"])["revoked_at"] is None
    assert binding(body["binding_id"])["machine_label"] == "LAB-PC-07"


def test_401_comes_before_invalid_request_and_a_bad_query_is_not_a_422(lic, make_pool):
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    # No token and a broken body: 401 first (contract §10A order).
    assert_error(lic.rename(body["binding_id"], None, headers={}, body={"request_id": 7}), 401, "not_authenticated")
    assert_error(lic.owner_history(pool["account_id"], headers={}, limit="abc"), 401, "not_authenticated")
    # Signed in, the same mistakes are invalid_request, never FastAPI's 422.
    assert_error(lic.rename(body["binding_id"], None, body={"request_id": 7}), 400, "invalid_request")
    assert_error(lic.owner_history(pool["account_id"], limit="abc"), 400, "invalid_request")


def test_a_changed_owner_takes_effect_on_the_old_owners_next_call(lic, make_pool):
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    new_owner = "new.owner@school.example"
    register_user(new_owner)
    old_session = lic.session(TEST_USER)
    assert lic.pools(pool["account_id"], headers=old_session).status_code == 200

    edited = lic.edit_account(pool["account_id"], owner_identifier=" New.Owner@School.example ")
    assert edited.status_code == 200, edited.text
    assert edited.json()["account"]["owner_identifier"] == new_owner

    # The old owner's still-valid token: refused on the very next call.
    assert_error(lic.pools(pool["account_id"], headers=old_session), 403, "account_access_denied")
    assert_error(lic.machines(pool["account_id"], headers=old_session), 403, "account_access_denied")
    assert_error(lic.owner_history(pool["account_id"], headers=old_session), 403, "account_access_denied")
    assert_error(lic.rename(body["binding_id"], "OLD", headers=old_session), 403, "binding_access_denied")
    assert_error(lic.release(body["binding_id"], headers=old_session), 403, "binding_access_denied")
    assert binding(body["binding_id"])["revoked_at"] is None

    new_session = lic.session(new_owner)
    assert lic.pools(pool["account_id"], headers=new_session).status_code == 200
    assert lic.rename(body["binding_id"], "NEW", headers=new_session).json()["result"] == "renamed"
    assert lic.release(body["binding_id"], headers=new_session).json()["result"] == "released"


# ===========================================================================
# K2 — owner reads
# ===========================================================================

def test_another_schools_or_a_missing_account_pool_or_computer_get_the_identical_403(lic, make_pool):
    mine = _school(lic, make_pool)
    theirs = _school(lic, make_pool, owner=OTHER_USER)
    their_pc = lic.activated(theirs["pool_id"], token=lic.token(OTHER_USER))
    my_pc = lic.activated(mine["pool_id"], pc(3))
    missing = secrets.token_hex(16)

    def same(responses, status, code):
        details = [without_server_time({"detail": assert_error(r, status, code)}) for r in responses]
        assert all(d == details[0] for d in details), details

    for read in (lic.pools, lic.machines, lic.owner_history):
        same([read(theirs["account_id"]), read(missing)], 403, "account_access_denied")
    same([lic.machines(mine["account_id"], pool_id=theirs["pool_id"]),
          lic.machines(mine["account_id"], pool_id=missing),
          lic.owner_history(mine["account_id"], pool_id=theirs["pool_id"]),
          lic.owner_history(mine["account_id"], pool_id=missing)], 403, "account_access_denied")
    same([lic.owner_history(mine["account_id"], binding_id=their_pc["binding_id"]),
          lic.owner_history(mine["account_id"], binding_id=missing),
          lic.rename(their_pc["binding_id"], "MINE"), lic.rename(missing, "MINE"),
          lic.release(their_pc["binding_id"]), lic.release(missing)], 403, "binding_access_denied")
    # An admin passes on any EXISTING account, but a missing one is still the same 403.
    admin = bearer(ADMIN)
    assert lic.pools(theirs["account_id"], headers=admin).status_code == 200
    assert_error(lic.pools(missing, headers=admin), 403, "account_access_denied")
    assert binding(their_pc["binding_id"])["revoked_at"] is None
    assert lic.owner_history(mine["account_id"], binding_id=my_pc["binding_id"]).status_code == 200


def test_pools_report_counts_as_claims_see_them(lic, make_pool, clock):
    pool = _school(lic, make_pool, size=2)
    a = lic.activated(pool["pool_id"], pc(1))
    lic.activated(pool["pool_id"], pc(2))
    lic.released(a["binding_id"])
    _, _, _, ambiguity_id = raise_activation_ambiguity(lic, pool["pool_id"])
    response = lic.pools(pool["account_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == pool["account_id"] and body["account_name"] == "Test School"
    assert body["subscription_status"] == "active" and WIRE_TIME.fullmatch(body["subscription_expires_at"])
    assert WIRE_TIME.fullmatch(body["server_time"])
    (row,) = body["pools"]
    assert row == {"pool_id": pool["pool_id"], "label": "Lab", "pool_size": 2, "active": 2, "available": 0,
                   "over_capacity": False, "pool_status": "active", "pending_identity_checks": 1}


def test_pending_identity_checks_counts_only_effectively_open_ambiguities(lic, make_pool, clock):
    pool = _school(lic, make_pool)
    raise_activation_ambiguity(lic, pool["pool_id"])
    first, _, _, resolved = raise_activation_ambiguity(lic, pool["pool_id"], disk="WD-BBBB", nic="B44506BBBBBB")
    lic.resolved(resolved, "recognize_existing", first["binding_id"])
    assert lic.pools(pool["account_id"]).json()["pools"][0]["pending_identity_checks"] == 1
    clock(days=7, seconds=1)
    # Still 'open' in the database, but past 7 days it is effectively expired (K0).
    assert query("SELECT COUNT(*) AS n FROM licence_ambiguities WHERE seat_pool_id = ? AND status = 'open'",
                 (pool["pool_id"],))[0]["n"] == 1
    assert lic.pools(pool["account_id"]).json()["pools"][0]["pending_identity_checks"] == 0


def test_machines_lists_active_computers_sorted_case_insensitively(lic, make_pool):
    first = _school(lic, make_pool)
    execute("INSERT INTO seat_pools (id, account_id, pool_size, label, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", ("b" * 32, first["account_id"], 5, "annex", "active",
                                              "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
    labels = {}
    for n, (pool_id, label) in enumerate([(first["pool_id"], "pc-b"), (first["pool_id"], "PC-A"),
                                          ("b" * 32, "Z"), (first["pool_id"], "pc-c")], start=1):
        labels[label] = lic.activated(pool_id, pc(n), label=label)
    lic.released(labels["pc-c"]["binding_id"])
    body = lic.machines(first["account_id"]).json()
    assert [(m["pool_label"], m["machine_label"]) for m in body["machines"]] == [
        ("annex", "Z"), ("Lab", "PC-A"), ("Lab", "pc-b")]
    machine = body["machines"][1]
    assert set(machine) == {"binding_id", "pool_id", "pool_label", "machine_label", "activated_at", "last_seen_at",
                            "lease_expires_at", "fingerprint_confidence"}
    assert machine["binding_id"] == labels["PC-A"]["binding_id"]
    assert machine["fingerprint_confidence"] == "strong" and machine["last_seen_at"] is None
    assert WIRE_TIME.fullmatch(machine["activated_at"]) and WIRE_TIME.fullmatch(machine["lease_expires_at"])
    filtered = lic.machines(first["account_id"], pool_id="b" * 32).json()["machines"]
    assert [m["machine_label"] for m in filtered] == ["Z"]


def test_history_hides_exampartners_events_and_carries_no_readings(lic, make_pool):
    mine = _school(lic, make_pool)
    theirs = _school(lic, make_pool, owner=OTHER_USER)
    their_pc = lic.activated(theirs["pool_id"], token=lic.token(OTHER_USER))
    # H7: a clone of THEIR installation presented in MY pool is logged against
    # MY account, naming THEIR binding in detail_json.
    lic.activated(mine["pool_id"], pc(5), installation_id=their_pc["installation_id"])
    clone = log_rows(mine["account_id"], "clone_flagged")
    assert clone and their_pc["binding_id"] in clone[0]["detail_json"]
    # A stale serial, a rename, a release, a refusal, an account change and a pool change.
    mine_pc = lic.activated(mine["pool_id"], pc(6))
    lic.refreshed(mine_pc["lease"], pc(6))
    lic.refreshed(mine_pc["lease"], pc(6))
    assert log_rows(mine["account_id"], "stale_serial_flagged")
    lic.rename(mine_pc["binding_id"], "RENAMED")
    lic.released(mine_pc["binding_id"], revoke_reason="hardware_failure", reason="Motherboard failed")
    assert_error(lic.refresh(mine_pc["lease"], pc(6)), 403, "lease_revoked")
    lic.edit_account(mine["account_id"], name="New Name")
    lic.edit_pool(mine["pool_id"], pool_size=9, status="suspended")
    assert log_rows(mine["account_id"], "account_update")

    events = lic.owner_history(mine["account_id"], limit="200").json()["events"]
    actions = {e["action"] for e in events}
    # The first refresh logs `refresh` (last_seen_at was null, I3 step 5).
    assert actions == {"activate", "refresh", "rename", "release", "refresh_denied", "resize", "pool_status"}
    assert not actions & {"clone_flagged", "stale_serial_flagged", "account_update"}
    hidden = {mine_pc["installation_id"], their_pc["installation_id"], their_pc["binding_id"],
              binding(mine_pc["binding_id"])["machine_fingerprint_hash"], ADMIN}
    for event in events:
        assert set(event) == EVENT_KEYS, event
        assert not hidden & {str(v) for v in event.values()}, event
        assert WIRE_TIME.fullmatch(event["at"])
        if event["actor"] != "owner":
            assert event["actor_identifier"] is None
    release = next(e for e in events if e["action"] == "release")
    assert release == {**release, "actor": "owner", "actor_identifier": TEST_USER, "revoke_reason": "hardware_failure",
                       "reason": "Motherboard failed", "machine_label": "RENAMED", "binding_id": mine_pc["binding_id"]}
    assert next(e for e in events if e["action"] == "refresh_denied")["actor"] == "computer"
    resize = next(e for e in events if e["action"] == "resize")
    assert resize["actor"] == "exampartner" and resize["binding_id"] is None and resize["pool_id"] == mine["pool_id"]
    assert all(e["revoke_reason"] is None and e["reason"] is None for e in events if e["action"] != "release")


def _log(account_id, at, action="rename", pool_id=None, binding_id=None, event_id=None):
    event_id = event_id or secrets.token_hex(16)
    execute("INSERT INTO seat_activation_log (id, account_id, seat_pool_id, binding_id, action, machine_label, "
            "performed_by, actor_role, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, account_id, pool_id, binding_id, action, "PC", TEST_USER, "institution_owner", at))
    return event_id


def test_history_keyset_pagination_returns_every_row_below_the_boundary_exactly_once(lic, make_pool):
    pool = _school(lic, make_pool)
    account_id = pool["account_id"]
    base = "2026-10-01T09:00:{:02d}.000000+00:00"
    # Ten events, two of them sharing one created_at: the id breaks the tie.
    existing = [_log(account_id, base.format(s)) for s in range(9)]
    existing.append(_log(account_id, base.format(4), event_id="f" * 32))
    existing.append(_log(account_id, base.format(4), event_id="0" * 32))
    _log(account_id, base.format(30), action="clone_flagged")          # never shown

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": "3"} if cursor is None else {"limit": "3", "cursor": cursor}
        body = lic.owner_history(account_id, **params).json()
        seen.extend(e["event_id"] for e in body["events"])
        pages += 1
        if pages == 1:
            # Written between pages: NEWER events sort above the boundary and
            # are never inserted into this traversal; an event that commits
            # late with an OLDER time sorts below it and appears exactly once.
            newer = [_log(account_id, "2026-10-02T09:00:00+00:00") for _ in range(2)]
            late_old = _log(account_id, "2026-10-01T08:59:59+00:00")
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen))
    assert set(seen) == set(existing) | {late_old}
    assert not set(newer) & set(seen)
    assert seen[-1] == late_old
    # Newest first, with the tie on 09:00:04 ordered by id, descending.
    tie = [e for e in seen if e in ("f" * 32, "0" * 32)]
    assert tie == ["f" * 32, "0" * 32]
    # A fresh traversal starts at the top and includes the newer events.
    assert set(newer) <= {e["event_id"] for e in lic.owner_history(account_id, limit="200").json()["events"]}


@pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "201"}, {"limit": "-1"}, {"limit": "1.5"},
                                    {"limit": ""}, {"cursor": "not base64!"}, {"cursor": "e30"},
                                    {"cursor": base64.urlsafe_b64encode(b'{"t":"yesterday","id":"x"}').decode()}])
def test_history_rejects_a_malformed_limit_or_cursor(lic, make_pool, params):
    pool = _school(lic, make_pool)
    detail = assert_error(lic.owner_history(pool["account_id"], **params), 400, "invalid_request")
    assert detail["field"] == next(iter(params))


def test_history_filters_by_pool_and_binding(lic, make_pool):
    pool = _school(lic, make_pool)
    a = lic.activated(pool["pool_id"], pc(1))
    b = lic.activated(pool["pool_id"], pc(2))
    lic.rename(a["binding_id"], "A2")
    by_binding = lic.owner_history(pool["account_id"], binding_id=a["binding_id"]).json()["events"]
    assert {e["binding_id"] for e in by_binding} == {a["binding_id"]}
    assert [e["action"] for e in by_binding] == ["rename", "activate"]
    by_pool = lic.owner_history(pool["account_id"], pool_id=pool["pool_id"]).json()["events"]
    assert {e["binding_id"] for e in by_pool} == {a["binding_id"], b["binding_id"]}


# ===========================================================================
# K3 — rename
# ===========================================================================

def test_rename_renames_logs_and_replays(lic, make_pool):
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    request_id = str(uuid.uuid4())
    first = lic.rename(body["binding_id"], "  LAB-PC-99  ", request_id=request_id)
    assert first.status_code == 200, first.text
    assert without_server_time(first.json()) == {"result": "renamed", "binding_id": body["binding_id"],
                                                 "machine_label": "LAB-PC-99"}
    assert WIRE_TIME.fullmatch(first.json()["server_time"])
    assert binding(body["binding_id"])["machine_label"] == "LAB-PC-99"
    (row,) = log_rows(pool["account_id"], "rename")
    assert row["machine_label"] == "LAB-PC-99" and row["performed_by"] == TEST_USER
    assert json.loads(row["detail_json"])["from"] == "LAB-PC-07"
    assert json.loads(row["detail_json"])["to"] == "LAB-PC-99"

    # A retry replays the stored outcome; the computer has since been renamed again.
    lic.rename(body["binding_id"], "LATER")
    retry = lic.rename(body["binding_id"], "LAB-PC-99", request_id=request_id)
    assert without_server_time(retry.json()) == without_server_time(first.json())
    assert binding(body["binding_id"])["machine_label"] == "LATER"
    assert len(log_rows(pool["account_id"], "rename")) == 2
    assert len(history(request_id)) == 1
    assert_error(lic.rename(body["binding_id"], "OTHER", request_id=request_id), 409, "request_id_reused")


def test_rename_unchanged_logs_nothing_and_duplicate_labels_are_allowed(lic, make_pool):
    pool = _school(lic, make_pool)
    a = lic.activated(pool["pool_id"], pc(1), label="SAME")
    b = lic.activated(pool["pool_id"], pc(2), label="OTHER")
    unchanged = lic.rename(a["binding_id"], "SAME")
    assert unchanged.json()["result"] == "unchanged"
    assert log_rows(pool["account_id"], "rename") == []
    assert lic.rename(b["binding_id"], "SAME").json()["result"] == "renamed"


def test_rename_of_a_released_computer_is_binding_released_and_stored(lic, make_pool):
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    lic.released(body["binding_id"])
    request_id = str(uuid.uuid4())
    first = assert_error(lic.rename(body["binding_id"], "GHOST", request_id=request_id), 409, "binding_released")
    again = assert_error(lic.rename(body["binding_id"], "GHOST", request_id=request_id), 409, "binding_released")
    assert {k: v for k, v in first.items() if k != "server_time"} == {k: v for k, v in again.items()
                                                                       if k != "server_time"}
    assert binding(body["binding_id"])["machine_label"] == "LAB-PC-07"
    assert log_rows(pool["account_id"], "rename") == []


def test_rename_request_id_rules(lic, make_pool):
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    other = lic.activated(pool["pool_id"], pc(2))
    release_id = str(uuid.uuid4())
    lic.released(other["binding_id"], request_id=release_id)
    assert_error(lic.rename(body["binding_id"], "X", request_id=release_id), 409, "request_id_reused")
    for bad in ({}, {"machine_label": "X"}, {"request_id": "not-a-uuid", "machine_label": "X"},
                {"request_id": str(uuid.uuid1()), "machine_label": "X"}):
        assert assert_error(lic.rename(body["binding_id"], None, body=bad), 400, "invalid_request")["field"] == \
            "request_id"
    for label in (None, "", "   ", "x" * 65, 7):
        detail = assert_error(lic.rename(body["binding_id"], None,
                                         body={"request_id": str(uuid.uuid4()), "machine_label": label}),
                              400, "invalid_request")
        assert detail["field"] == "machine_label"
    assert binding(body["binding_id"])["machine_label"] == "LAB-PC-07"


# ===========================================================================
# K4 — release under the licence session token
# ===========================================================================

def test_release_under_a_session_token_behaves_exactly_as_under_a_bearer_token(lic, make_pool):
    pool = _school(lic, make_pool)
    a = lic.activated(pool["pool_id"], pc(1))
    b = lic.activated(pool["pool_id"], pc(2))
    request_a, request_b = str(uuid.uuid4()), str(uuid.uuid4())
    by_bearer = lic.release(a["binding_id"], request_id=request_a, revoke_reason="retired", reason="old")
    by_session = lic.release(b["binding_id"], request_id=request_b, revoke_reason="retired", reason="old",
                             headers=lic.session())
    assert by_bearer.status_code == by_session.status_code == 200
    one, two = without_server_time(by_bearer.json()), without_server_time(by_session.json())
    assert one["result"] == two["result"] == "released"
    assert {k: v for k, v in one.items() if k not in ("binding_id", "released_at", "active", "available")} == \
        {k: v for k, v in two.items() if k not in ("binding_id", "released_at", "active", "available")}
    rows = {r["binding_id"]: r for r in log_rows(pool["account_id"], "release")}
    for key in ("performed_by", "actor_role", "reason"):
        assert rows[a["binding_id"]][key] == rows[b["binding_id"]][key]
    # The same request replayed under the OTHER token type: the stored response.
    replay = lic.release(a["binding_id"], request_id=request_a, revoke_reason="retired", reason="old",
                         headers=lic.session())
    assert without_server_time(replay.json()) == one
    assert len(log_rows(pool["account_id"], "release")) == 2


# ===========================================================================
# K5 — account editing
# ===========================================================================

def test_account_edit_updates_logs_and_hides_from_owner_history(lic, make_pool):
    pool = _school(lic, make_pool)
    response = lic.edit_account(pool["account_id"], name="  Renamed School ", contact_email="it@school.example")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and WIRE_TIME.fullmatch(body["server_time"])
    assert body["account"]["name"] == "Renamed School"
    assert body["account"]["contact_email"] == "it@school.example"
    assert body["account"]["owner_identifier"] == TEST_USER
    (row,) = log_rows(pool["account_id"], "account_update")
    detail = json.loads(row["detail_json"])
    assert detail["from"] == {"name": "Test School", "contact_email": None}
    assert detail["to"] == {"name": "Renamed School", "contact_email": "it@school.example"}
    assert row["actor_role"] == "exampartner_admin" and row["performed_by"] == ADMIN
    actions = {e["action"] for e in lic.owner_history(pool["account_id"]).json()["events"]}
    assert "account_update" not in actions


def test_account_edit_refuses_an_unregistered_owner_and_records_nothing(lic, make_pool):
    pool = _school(lic, make_pool)
    request_id = str(uuid.uuid4())
    detail = assert_error(lic.edit_account(pool["account_id"], request_id=request_id,
                                           owner_identifier="nobody@nowhere.example"), 400, "invalid_request")
    assert detail["field"] == "owner_identifier"
    assert history(request_id) == []
    assert log_rows(pool["account_id"], "account_update") == []
    assert query("SELECT owner_identifier FROM accounts WHERE id = ?",
                 (pool["account_id"],))[0]["owner_identifier"] == TEST_USER


def test_account_edit_replay_keeps_the_other_admins_value(lic, make_pool):
    pool = _school(lic, make_pool)
    second_admin = "second.admin@exampartner.example"
    register_user(second_admin, is_admin=True)
    request_id = str(uuid.uuid4())
    first = lic.edit_account(pool["account_id"], request_id=request_id, name="Admin A")
    assert first.status_code == 200
    assert lic.edit_account(pool["account_id"], name="Admin B", as_user=second_admin).status_code == 200
    retry = lic.edit_account(pool["account_id"], request_id=request_id, name="Admin A")
    assert retry.status_code == 200
    assert without_server_time(retry.json()) == without_server_time(first.json())
    assert retry.json()["server_time"] and WIRE_TIME.fullmatch(retry.json()["server_time"])
    assert query("SELECT name FROM accounts WHERE id = ?", (pool["account_id"],))[0]["name"] == "Admin B"
    rows = log_rows(pool["account_id"], "account_update")
    assert [r["performed_by"] for r in rows] == [ADMIN, second_admin]
    assert_error(lic.edit_account(pool["account_id"], request_id=request_id, name="Other"), 409, "request_id_reused")


def test_account_edit_validation_and_errors(lic, make_pool):
    pool = _school(lic, make_pool)
    assert_error(lic.edit_account(pool["account_id"], body={"name": "X"}), 400, "invalid_request")
    assert assert_error(lic.edit_account(pool["account_id"]), 400, "invalid_request")["field"] == "body"
    for field, value in (("name", ""), ("name", 7), ("contact_email", "x" * 201), ("owner_identifier", [])):
        assert assert_error(lic.edit_account(pool["account_id"], **{field: value}), 400,
                            "invalid_request")["field"] == field
    assert_error(lic.edit_account(secrets.token_hex(16), name="X"), 404, "account_not_found")
    assert_error(lic.edit_account(pool["account_id"], name="X", headers={}), 401, "not_authenticated")
    assert_error(lic.edit_account(pool["account_id"], name="X", headers=bearer(TEST_USER)), 403, "admin_required")
    # A release's request_id reused for an account edit.
    body = lic.activated(pool["pool_id"])
    release_id = str(uuid.uuid4())
    lic.released(body["binding_id"], request_id=release_id)
    assert_error(lic.edit_account(pool["account_id"], request_id=release_id, name="X"), 409, "request_id_reused")


def test_account_edit_that_changes_nothing_records_its_request_and_logs_nothing(lic, make_pool):
    pool = _school(lic, make_pool)
    request_id = str(uuid.uuid4())
    response = lic.edit_account(pool["account_id"], request_id=request_id, name="Test School")
    assert response.status_code == 200 and response.json()["account"]["name"] == "Test School"
    assert len(history(request_id)) == 1
    assert log_rows(pool["account_id"], "account_update") == []


# ===========================================================================
# K6 — pool editing, suspension, spec §12 tests 5, 29, 30
# ===========================================================================

def _fill(lic, pool_id, count, start=1):
    token = lic.token()
    return [lic.activated(pool_id, pc(n), token=token, label=f"PC-{n}") for n in range(start, start + count)]


def test_spec_29_grow_20_to_30(lic, make_pool):
    pool = _school(lic, make_pool, size=20)
    machines = _fill(lic, pool["pool_id"], 20)
    assert_error(lic.activate(pool["pool_id"], pc(99)), 409, "pool_full")
    response = lic.edit_pool(pool["pool_id"], pool_size=30)
    assert response.status_code == 200, response.text
    assert without_server_time(response.json()) == {
        "ok": True, "pool_id": pool["pool_id"], "pool_size": 30, "label": "Lab", "pool_status": "active",
        "active": 20, "available": 10, "over_capacity": False}
    assert WIRE_TIME.fullmatch(response.json()["server_time"])
    assert all(binding(m["binding_id"])["revoked_at"] is None for m in machines)
    _fill(lic, pool["pool_id"], 10, start=21)
    assert_error(lic.activate(pool["pool_id"], pc(99)), 409, "pool_full")
    assert len(active_bindings(pool["pool_id"])) == 30
    (row,) = log_rows(pool["account_id"], "resize")
    assert json.loads(row["detail_json"])["from"] == {"pool_size": 20}
    assert json.loads(row["detail_json"])["to"] == {"pool_size": 30}


def test_spec_30_shrink_20_to_15_is_over_capacity_until_the_school_retires_three(lic, make_pool):
    pool = _school(lic, make_pool, size=20)
    machines = _fill(lic, pool["pool_id"], 18)
    response = lic.edit_pool(pool["pool_id"], pool_size=15).json()
    assert (response["active"], response["available"], response["over_capacity"]) == (18, 0, True)
    assert len(active_bindings(pool["pool_id"])) == 18                  # no machine killed
    assert_error(lic.activate(pool["pool_id"], pc(99)), 409, "pool_full")
    for machine in machines[5:8]:                                     # whichever the school chooses
        lic.released(machine["binding_id"], revoke_reason="renewal_retire")
    (summary,) = lic.pools(pool["account_id"]).json()["pools"]
    assert (summary["active"], summary["available"], summary["over_capacity"]) == (15, 0, False)


def test_spec_5_shrink_through_the_route_then_release_three_then_a_fourth(lic, make_pool):
    pool = _school(lic, make_pool, size=20)
    machines = _fill(lic, pool["pool_id"], 18)
    lic.edit_pool(pool["pool_id"], pool_size=15)
    for machine in machines[:3]:                                      # the first ones added, as in spec §5.4
        lic.released(machine["binding_id"])
    assert len(active_bindings(pool["pool_id"])) == 15
    assert_error(lic.activate(pool["pool_id"], pc(99)), 409, "pool_full")
    lic.released(machines[10]["binding_id"])
    lic.activated(pool["pool_id"], pc(99))
    assert_error(lic.activate(pool["pool_id"], pc(100)), 409, "pool_full")
    assert len(active_bindings(pool["pool_id"])) == 15


def test_suspension_stops_activation_and_refresh_and_reactivation_restores_both(lic, make_pool):
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    issued = binding(body["binding_id"])
    suspended = lic.edit_pool(pool["pool_id"], status="suspended")
    assert suspended.json()["pool_status"] == "suspended"
    assert_error(lic.activate(pool["pool_id"], make_signals(**PC_B)), 409, "pool_inactive")
    assert_error(lic.refresh(body["lease"]), 409, "pool_inactive")
    held = binding(body["binding_id"])
    assert (held["current_lease"], held["lease_serial"], held["lease_expires_at"]) == \
        (issued["current_lease"], issued["lease_serial"], issued["lease_expires_at"])
    (row,) = log_rows(pool["account_id"], "pool_status")
    assert json.loads(row["detail_json"])["from"] == "active" and json.loads(row["detail_json"])["to"] == "suspended"
    assert log_rows(pool["account_id"], "resize") == []

    lic.edit_pool(pool["pool_id"], status="active")
    assert lic.refresh(body["lease"]).status_code == 200
    assert lic.activate(pool["pool_id"], make_signals(**PC_B)).status_code == 200
    assert [json.loads(r["detail_json"])["to"] for r in log_rows(pool["account_id"], "pool_status")] == \
        ["suspended", "active"]


def test_pool_edit_changing_status_and_size_logs_both_rows(lic, make_pool):
    pool = _school(lic, make_pool)
    response = lic.edit_pool(pool["pool_id"], pool_size=7, label="Annex", status="suspended")
    assert response.status_code == 200, response.text
    (resize,) = log_rows(pool["account_id"], "resize")
    (status,) = log_rows(pool["account_id"], "pool_status")
    assert json.loads(resize["detail_json"])["from"] == {"pool_size": 5, "label": "Lab"}
    assert json.loads(resize["detail_json"])["to"] == {"pool_size": 7, "label": "Annex"}
    assert json.loads(status["detail_json"])["to"] == "suspended"
    assert resize["created_at"] == status["created_at"]


def test_pool_edit_lost_response_replay_keeps_the_later_size(lic, make_pool):
    pool = _school(lic, make_pool, size=10)
    second_admin = "second.admin@exampartner.example"
    register_user(second_admin, is_admin=True)
    request_id = str(uuid.uuid4())
    first = lic.edit_pool(pool["pool_id"], request_id=request_id, pool_size=20)
    lic.edit_pool(pool["pool_id"], pool_size=25, as_user=second_admin)
    retry = lic.edit_pool(pool["pool_id"], request_id=request_id, pool_size=20)
    assert retry.status_code == 200
    assert without_server_time(retry.json()) == without_server_time(first.json())
    assert retry.json()["pool_size"] == 20
    assert query("SELECT pool_size FROM seat_pools WHERE id = ?", (pool["pool_id"],))[0]["pool_size"] == 25
    rows = log_rows(pool["account_id"], "resize")
    assert [r["performed_by"] for r in rows] == [ADMIN, second_admin]
    assert_error(lic.edit_pool(pool["pool_id"], request_id=request_id, pool_size=21), 409, "request_id_reused")


def test_pool_edit_validation_and_errors(lic, make_pool):
    pool = _school(lic, make_pool)
    assert_error(lic.edit_pool(pool["pool_id"], body={"pool_size": 3}), 400, "invalid_request")
    assert assert_error(lic.edit_pool(pool["pool_id"]), 400, "invalid_request")["field"] == "body"
    for field, value in (("pool_size", 0), ("pool_size", 10001), ("pool_size", "5"), ("pool_size", True),
                         ("pool_size", 2.5), ("label", ""), ("label", "x" * 65), ("status", "expired"),
                         ("status", 3)):
        assert assert_error(lic.edit_pool(pool["pool_id"], **{field: value}), 400,
                            "invalid_request")["field"] == field
    assert_error(lic.edit_pool(secrets.token_hex(16), pool_size=3), 404, "pool_not_found")
    assert_error(lic.edit_pool(pool["pool_id"], pool_size=3, headers={}), 401, "not_authenticated")
    assert_error(lic.edit_pool(pool["pool_id"], pool_size=3, headers=bearer(TEST_USER)), 403, "admin_required")
    assert lic.edit_pool(pool["pool_id"], pool_size=10000).json()["pool_size"] == 10000
    noop_id = str(uuid.uuid4())
    noop = lic.edit_pool(pool["pool_id"], request_id=noop_id, pool_size=10000, status="active")
    assert noop.status_code == 200 and len(history(noop_id)) == 1
    assert len(log_rows(pool["account_id"], "resize")) == 1 and log_rows(pool["account_id"], "pool_status") == []


# ===========================================================================
# K8 — signals review
# ===========================================================================

def test_signals_list_clone_stale_serial_and_refresh_denial(lic, make_pool):
    mine = _school(lic, make_pool)
    theirs = _school(lic, make_pool, owner=OTHER_USER)
    their_pc = lic.activated(theirs["pool_id"], token=lic.token(OTHER_USER))
    lic.activated(mine["pool_id"], pc(5), installation_id=their_pc["installation_id"])
    mine_pc = lic.activated(mine["pool_id"], pc(6))
    lic.refreshed(mine_pc["lease"], pc(6))
    lic.refreshed(mine_pc["lease"], pc(6))
    lic.released(mine_pc["binding_id"])
    assert_error(lic.refresh(mine_pc["lease"], pc(6)), 403, "lease_revoked")

    response = lic.signals()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and WIRE_TIME.fullmatch(body["server_time"]) and body["next_cursor"] is None
    by_action = {e["action"]: e for e in body["events"]}
    assert set(by_action) == {"clone_flagged", "stale_serial_flagged", "refresh_denied"}
    clone = by_action["clone_flagged"]
    assert clone["account_id"] == mine["account_id"] and clone["account_name"] == "Test School"
    assert clone["pool_label"] == "Lab"
    assert clone["detail"]["matched_binding_id"] == their_pc["binding_id"]
    assert clone["fingerprint_signals"]["system_serial"] == pc(5)["system_serial"]
    assert "detail_json" not in clone and "fingerprint_signals_json" not in clone
    assert by_action["stale_serial_flagged"]["binding_id"] == mine_pc["binding_id"]
    assert by_action["refresh_denied"]["detail"]

    only = lic.signals(action="clone_flagged", account_id=mine["account_id"]).json()["events"]
    assert [e["action"] for e in only] == ["clone_flagged"]
    assert lic.signals(account_id=theirs["account_id"]).json()["events"] == []
    assert lic.signals(since="2999-01-01T00:00:00Z").json()["events"] == []
    first_page = lic.signals(limit="2").json()
    assert len(first_page["events"]) == 2 and first_page["next_cursor"]
    rest = lic.signals(limit="2", cursor=first_page["next_cursor"]).json()
    assert {e["event_id"] for e in first_page["events"] + rest["events"]} == \
        {e["event_id"] for e in body["events"]}


def test_signals_refusals(lic, make_pool):
    assert_error(lic.signals(headers=bearer(TEST_USER)), 403, "admin_required")
    assert_error(lic.signals(headers={}), 401, "not_authenticated")
    assert assert_error(lic.signals(action="release"), 400, "invalid_request")["field"] == "action"
    assert assert_error(lic.signals(since="last week"), 400, "invalid_request")["field"] == "since"
    assert assert_error(lic.signals(limit="0"), 400, "invalid_request")["field"] == "limit"
    assert_error(lic.signals(account_id=secrets.token_hex(16)), 404, "account_not_found")


# ===========================================================================
# K9 — the test clock
# ===========================================================================

@pytest.fixture
def clock_reset():
    from services import licensing_time

    yield
    licensing_time.reset_clock()


def test_test_clock_is_disabled_unless_allowed_even_for_an_admin(lic, monkeypatch, clock_reset):
    import config
    from services import licensing_time

    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", False)
    assert_error(lic.test_clock({"offset_seconds": 3600}), 403, "test_clock_disabled")
    assert_error(lic.test_clock({"offset_seconds": 3600}, headers={}), 401, "not_authenticated")
    assert_error(lic.test_clock({"offset_seconds": 3600}, headers=bearer(TEST_USER)), 403, "admin_required")
    # Refused before anything was set: no offset is waiting to take effect.
    assert licensing_time._clock_offset == timedelta(0)


def test_test_clock_offset_moves_refresh_expiry_and_reset_restores_it(lic, make_pool, monkeypatch, clock_reset):
    import config
    from services import licensing_time

    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", True)
    pool = _school(lic, make_pool)
    body = lic.activated(pool["pool_id"])
    issued = licensing_time.to_datetime(lease_payload(body["lease"])["expires_at"])

    shifted = lic.test_clock({"offset_seconds": 40 * 86400})
    assert shifted.status_code == 200, shifted.text
    assert shifted.json()["ok"] is True and shifted.json()["offset_seconds"] == 40 * 86400
    server_time = licensing_time.to_datetime(shifted.json()["server_time"])
    assert server_time > issued                                     # the lease is now expired
    # Spec test 16: the expired lease still refreshes, on the shifted clock.
    renewed = lic.refreshed(body["lease"])
    expires = licensing_time.to_datetime(renewed["lease_expires_at"])
    assert abs(expires - (server_time + timedelta(days=30))) < timedelta(minutes=1)

    reset = lic.test_clock({"reset": True})
    assert reset.status_code == 200 and reset.json()["offset_seconds"] == 0
    assert licensing_time.to_datetime(reset.json()["server_time"]) < issued


@pytest.mark.parametrize("payload", [{}, {"offset_seconds": "60"}, {"offset_seconds": 1.5}, {"offset_seconds": True},
                                     {"offset_seconds": 11 * 366 * 86400}, {"offset_seconds": -11 * 366 * 86400},
                                     {"reset": False}, {"reset": True, "offset_seconds": 5}])
def test_test_clock_rejects_a_malformed_body(lic, monkeypatch, clock_reset, payload):
    import config

    monkeypatch.setattr(config, "ALLOW_TEST_CLOCK", True)
    assert_error(lic.test_clock(payload), 400, "invalid_request")
