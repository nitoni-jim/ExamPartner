"""
services/licensing_service.py — Windows seat-pool licensing (Pilot V1).

Unit 1 built claim_capacity(), the single place capacity is granted. Unit 2
adds activation on top of it: the activation session, the decision between
restore, ambiguity and a new seat (brief §4.F F3), restore itself, lease
issue, and create_or_get_ambiguity(). Unit 3a adds refresh, release,
ambiguity resolution and collection, the admin ambiguity list, and the one
request history every state-changing request records itself in
(services/licence_history.py). Unit 3b adds the school owner's reads and
rename under the licence session token, account and pool editing, the
signals review list and the test clock. See
docs/ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec.md §5 and §8,
docs/LICENCE_API_CONTRACT.md, and docs/CLOUD_SESSION_BRIEF.md §4.F–§4.J.

Capacity is a COUNT of active bindings (institution_seats rows with
revoked_at IS NULL) against seat_pools.pool_size. It is enforced here, in
code, and not by the schema: a CHECK constraint was tested and rejects the
pool shrink itself, and over-capacity after a shrink is a supported state
(spec §3.3, §5.4). So correctness rests on two things this file must keep:

1. claim_capacity() is the ONLY path that creates an active binding.
   Activation, ambiguity resolution that results in a new machine, and any
   future path (Deployment Sessions included) must call it rather than
   INSERT into institution_seats directly. A path that bypasses it silently
   removes the capacity guarantee for every customer.
2. The pool-row lock at the top of claim_capacity() survives future edits.
"""
import base64
import binascii
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from fastapi import HTTPException

import config
from config import db_conn
from db import licensing_index_present, licensing_status
from services import fingerprint_service, licence_history, licence_lease, licence_session, licensing_time
from services.access_control import is_admin_user, require_account_owner
from services.licence_wire import (licence_detail, licence_fail, raise_stored, truncate_seconds, wire_time,
                                   wire_time_or_none)

logger = logging.getLogger(__name__)

# Who may cause capacity to be granted. A Windows client holding a lease may
# refresh only; it cannot activate unaided (spec §9), so 'client' is refused.
_CLAIM_ACTOR_ROLES = {"institution_owner", "exampartner_admin"}

# Provisional until the fingerprint survey (spec §8.2); recorded at claim time.
_FINGERPRINT_CONFIDENCE = {"strong", "weak", "degenerate"}


def _row_to_dict(row) -> Dict[str, Any]:
    """
    Normalizes a DB row (sqlite3.Row or psycopg2 RealDictRow) into a plain
    dict. Copied from paper_rules_service.py, per backend/CLAUDE.md.
    """
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def claim_capacity(
    pool_id: str,
    *,
    machine_fingerprint_hash: str,
    fingerprint_confidence: str,
    performed_by: str,
    actor_role: str,
    request_id: str,
    request_fp: str,
    history: "licence_history.Entry",
    fingerprint_signals_json: Optional[str] = None,
    machine_label: Optional[str] = None,
    resolves: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Grant one unit of capacity in `pool_id` by creating an active binding,
    and write its `activate` row to seat_activation_log in the same
    transaction — a binding never exists without its audit row.

    Refuses, through the contract §9.3 error body (brief F8), in this order:
      400  invalid_request — a caller passed something malformed
      503  licensing_unavailable — ux_seat_bindings_active_installation
           missing (checked twice — before the lock and inside the granting
           transaction; see (0) and (2) below)
      409  request_id_reused — this request_id already granted a binding in
           this pool within 24 hours, for a different reading
      403  pool_access_denied — pool not found (the contract returns the same
           code for a pool that does not exist)
      409  pool_inactive
      402  subscription_inactive — not 'active', or expired
      409  pool_full — at or over pool_size; never evicts
      500  internal_error — capacity invariant violated after insert
    Every refusal of an activation writes nothing. A resolve's refusal is
    stored for replay — its history row only (J8; see `resolves` below).

    Idempotent per request (brief F4): request_id and request_fp are stored
    on the binding in the same insert. If an active binding in this pool
    already carries the same request_id and request_fp within 24 hours, it
    is returned with "replayed": True and nothing is inserted. That check
    runs UNDER the pool lock, so two concurrent retries of one activation
    produce exactly one binding: the second blocks on the lock and then
    finds the first's committed row.

    The new binding's installation_id is ALWAYS generated here; there is
    deliberately no parameter for it (brief §4.D). A client-presented
    installation ID is evidence for the clone check in activation (spec
    §8.4 step 4), never the identity of a new binding — accepting one here
    would let a cloned machine's ID collide with, or be adopted by, a new
    binding.

    account_id is read from the pool row, never taken from the caller.

    Authorisation (owner or admin of the pool's account) is the CALLER's
    job, before calling this. performed_by and actor_role are required and
    are recorded on the audit row.

    The request history (brief §4.I I4). `history` is REQUIRED, so no
    capacity-grant path can skip recording itself: it is checked under the
    pool lock as the last thing before the insert, and the new binding's
    outcome is recorded in the same transaction. A request already recorded
    returns {"replayed": True, "history": row} and the caller replays it.

    Resolving an ambiguity as treat_as_new (brief §4.I I5, §4.J). With
    `resolves` = {"ambiguity_id", "admin"}, the resolution happens INSIDE
    this transaction, so a binding exists if and only if its resolution
    does: pool lock, then the ambiguity-row lock (pool → ambiguity, J6),
    the ambiguity's state, entitlement, capacity, the binding, the
    resolution and its history row, one commit. A refusal of a resolve is
    STORED for replay, and committed while the pool lock is still held
    (J8): only its history row, with last_claim_at put back as it was.
    An activation's refusal is still not stored — it is decided afresh on
    retry, and writes nothing.
    """
    if not machine_fingerprint_hash or not str(machine_fingerprint_hash).strip():
        licence_fail("invalid_request", "machine_fingerprint_hash is required", field="machine_fingerprint_hash")
    if fingerprint_confidence not in _FINGERPRINT_CONFIDENCE:
        licence_fail("invalid_request", "Invalid fingerprint_confidence", field="fingerprint_confidence")
    if not performed_by or not str(performed_by).strip():
        licence_fail("invalid_request", "performed_by is required", field="performed_by")
    if actor_role not in _CLAIM_ACTOR_ROLES:
        licence_fail("invalid_request", "Invalid actor_role for a capacity claim", field="actor_role")
    if fingerprint_signals_json is not None and not isinstance(fingerprint_signals_json, str):
        licence_fail("invalid_request", "fingerprint_signals_json must be a JSON string", field="fingerprint_signals_json")
    if not isinstance(request_id, str) or not request_id.strip():
        licence_fail("invalid_request", "request_id is required", field="request_id")
    if not isinstance(request_fp, str) or not request_fp.strip():
        licence_fail("invalid_request", "request_fp is required", field="request_fp")
    if not isinstance(history, licence_history.Entry):
        licence_fail("invalid_request", "history is required", field="history")

    # Closing without committing is the only rollback _PGConn offers, so
    # every refusal below simply raises: the finally closes the connection,
    # which discards the lock write and releases the row lock. A refused
    # claim therefore does NOT advance last_claim_at, which keeps that column
    # meaning "when this pool last consumed capacity".
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()

        # (0) Early licensing check, BEFORE the lock. There are two checks of
        # the same index, (0) and (2), and both are needed:
        #
        # - This one makes a broken or missing licensing schema fail as a
        #   clean 503. If the licensing step in init_db() failed before it
        #   created the tables, the UPDATE in (1) is the first statement to
        #   touch them and would surface as a database error — a 500 that
        #   reads like a server bug rather than "licensing is unavailable".
        #   The catalog query needs no licensing table, so it answers first.
        #
        # - It does not replace (2). On SQLite a SELECT does not open the
        #   write transaction, so (0) runs outside it; only (2), after the
        #   lock, verifies the index inside the transaction that grants the
        #   seat (brief §4.E). Do not delete either as a duplicate.
        #
        # On Postgres this SELECT does open the transaction, which is
        # harmless: under READ COMMITTED each statement takes a fresh
        # snapshot, so the COUNT after the lock still sees every committed
        # claim. Test 4 runs with this check in place.
        if not licensing_index_present(cur):
            licence_fail("licensing_unavailable")

        # (1) LOAD-BEARING — DO NOT REMOVE, REORDER OR "OPTIMISE AWAY".
        #
        # This looks like a bookkeeping write. It is the entire concurrency
        # guarantee. The UPDATE takes a row lock on the pool, held until this
        # transaction ends; a concurrent claim blocks here, and under READ
        # COMMITTED its COUNT below then takes a fresh snapshot that includes
        # our committed insert. Without it, 24 simultaneous claims against a
        # 15-seat pool on Postgres 16 grant all 24, every time (spec §5.1,
        # §12 tests 4 and 6). SQLite serialises writers anyway, which is why
        # only the Postgres test can catch its removal.
        #
        # It is written as a meaningful update — last_claim_at records when
        # the pool last consumed capacity — so that deleting it visibly
        # breaks something rather than silently removing the lock.
        cur.execute(
            "UPDATE seat_pools SET last_claim_at = ? WHERE id = ?",
            (stamp, pool_id),
        )

        # (2) The licensing-critical index, checked LIVE, in this
        # transaction, on every claim (brief §4.E) — the check that actually
        # guards the grant; (0) above exists only so a missing schema is a
        # 503 rather than a 500. init_db() no longer stops the backend when
        # that index is missing — it records licensing as unavailable and
        # carries on — so this is where the one-active-binding-per-
        # installation guarantee is actually enforced. It is deliberately not
        # read from db.licensing_status(): a startup flag can go stale, and
        # does not exist at all if init_db() never ran in this process. Two
        # catalog queries per claim, counting (0); claims are rare.
        if not licensing_index_present(cur):
            licence_fail("licensing_unavailable")

        # (2a) Resolving an ambiguity: take its row lock now, AFTER the pool
        # lock (pool → ambiguity, brief §4.J J6), and read it under the lock.
        ambiguity = _lock_ambiguity(cur, resolves["ambiguity_id"], stamp) if resolves is not None else None
        if resolves is not None and ambiguity is None:
            licence_fail("ambiguity_not_found")

        # (2b) The same activation already granted? (brief F4.) Under the
        # lock, so a concurrent retry of this request blocks above and then
        # sees the first one's committed binding here, instead of inserting
        # a second. Before entitlement and capacity on purpose: when the
        # first retry took the pool's last seat, the second must find that
        # binding, not be told the pool is full.
        #
        # The request history is that check now (brief §4.I I4), and the
        # LAST read before any write; the binding's own request columns are
        # still read beside it, for requests made before Unit 3a deployed.
        existing = _active_binding_for_request(cur, request_id, pool_id=pool_id)
        owner = licence_history.lookup_locked(cur, history)
        if owner is not None:
            return {"ok": True, "replayed": True, "history": owner}
        if existing is not None:
            if existing["activation_request_fp"] != request_fp:
                licence_fail("request_id_reused")
            return {
                "ok": True,
                "replayed": True,
                "binding_id": existing["id"],
                "installation_id": existing["installation_id"],
                "account_id": existing["account_id"],
                "seat_pool_id": pool_id,
            }

        def refuse(code: str, **extra: Any):
            # An activation's refusal is not stored: it writes nothing, and a
            # retry decides afresh. A resolve's IS stored (I5), and per J8 it
            # is committed here, under the pool lock that decided it — never
            # after the lock is released, or a concurrent retry could decide
            # the same request_id a second, different way. It commits only
            # its history row: last_claim_at is put back to its previous
            # value, so the refusal leaves no business trace.
            if resolves is None:
                licence_fail(code, **extra)
            status, detail = licence_detail(code, **extra)
            _restore_last_claim_at(cur, pool_id)
            licence_history.record(cur, history, outcome="refused", status_code=status, response=detail)
            db.commit()
            raise_stored(status, detail)

        cur.execute(
            "SELECT account_id, pool_size, status FROM seat_pools WHERE id = ?",
            (pool_id,),
        )
        pool = cur.fetchone()
        if not pool:
            licence_fail("pool_access_denied")
        pool = _row_to_dict(pool)
        pool_size = int(pool["pool_size"])
        account_id = pool["account_id"]

        # (2c) The ambiguity being resolved must still be open, and young
        # enough (I5). Under both locks, and before capacity, so a second,
        # concurrent treat_as_new is told it lost (already_resolved), not
        # that the pool is full.
        if ambiguity is not None:
            if ambiguity.get("seat_pool_id") != pool_id:
                licence_fail("internal_error", "The ambiguity does not belong to this pool")
            state_refusal = _resolution_state_refusal(ambiguity)
            if state_refusal is not None:
                refuse(state_refusal[0], **state_refusal[1])

        # (3) Entitlement, re-checked in this transaction (brief §4.D) and
        # BEFORE capacity (brief §4.E), so a suspended or lapsed school is
        # told its entitlement is the problem rather than to free a seat.
        # Still after the lock and in the same transaction, so the capacity
        # guarantee is unchanged. Activation may check both earlier for a
        # fast rejection; this is what makes EVERY capacity-grant path
        # enforce the same boundary. It is NOT race-free against a
        # concurrent subscription change: the subscription lives on the
        # accounts row, which the pool-row lock above does not serialise.
        if pool.get("status") != "active":
            refuse("pool_inactive")
        cur.execute(
            "SELECT subscription_status, subscription_expires_at FROM accounts WHERE id = ?",
            (account_id,),
        )
        account = cur.fetchone()
        account = _row_to_dict(account) if account else {}
        if account.get("subscription_status") != "active" or licensing_time.is_expired(
            account.get("subscription_expires_at")
        ):
            refuse(
                "subscription_inactive",
                subscription_expires_at=wire_time_or_none(account.get("subscription_expires_at")),
            )

        # (4) Under the lock, the count cannot change underneath us.
        active = _count_active(cur, pool_id)
        # Slot-agnostic: which machines are active is irrelevant, only how
        # many. Over-capacity after a shrink (active > pool_size) is refused
        # by the same comparison; no machine is ever evicted here.
        if active >= pool_size:
            refuse("pool_full", pool_size=pool_size, active=active)

        # (5) Insert the binding and its audit row.
        binding_id = secrets.token_hex(16)
        installation_id = secrets.token_hex(16)
        label = machine_label.strip() if isinstance(machine_label, str) and machine_label.strip() else None
        cur.execute(
            "INSERT INTO institution_seats (id, seat_pool_id, account_id, "
            "machine_fingerprint_hash, fingerprint_signals_json, fingerprint_confidence, "
            "installation_id, machine_label, activated_at, lease_serial, "
            "activation_request_id, activation_request_at, activation_request_fp, activation_result, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (binding_id, pool_id, account_id,
             machine_fingerprint_hash, fingerprint_signals_json, fingerprint_confidence,
             installation_id, label, stamp, 0,
             request_id, stamp, request_fp, "activated",
             stamp, stamp),
        )
        cur.execute(
            "INSERT INTO seat_activation_log (id, account_id, seat_pool_id, binding_id, "
            "action, installation_id, machine_fingerprint_hash, machine_label, "
            "fingerprint_signals_json, performed_by, actor_role, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(16), account_id, pool_id, binding_id,
             "activate", installation_id, machine_fingerprint_hash, label,
             fingerprint_signals_json, str(performed_by).strip(), actor_role,
             json.dumps({"fingerprint_confidence": fingerprint_confidence,
                         **({"ambiguity_id": resolves["ambiguity_id"]} if resolves is not None else {})},
                        sort_keys=True), stamp),
        )

        # (5b) The resolution, in the same transaction as its binding (I5):
        # a binding never exists without its resolution, nor the reverse.
        # "AND status = 'open'" is a second guard behind the ambiguity lock.
        response = None
        if ambiguity is not None:
            cur.execute(
                "UPDATE licence_ambiguities SET status = 'resolved', resolution = 'treat_as_new', "
                "resolved_binding_id = ?, resolved_by = ?, resolved_at = ? WHERE id = ? AND status = 'open'",
                (binding_id, resolves["admin"], stamp, ambiguity["id"]),
            )
            _log_resolution(cur, ambiguity, decision="treat_as_new", binding_id=binding_id,
                            admin=resolves["admin"], stamp=stamp)
            response = _resolution_body(ambiguity["id"], "treat_as_new", binding_id, stamp)
            history_row = licence_history.record(cur, history, outcome="resolved", response=response,
                                                 binding_id=binding_id)
        else:
            # The activation's outcome is owned now, under the pool lock; its
            # response is filled in once the lease is issued (F5).
            history_row = licence_history.record(cur, history, outcome="activated", binding_id=binding_id)

        # (6) Invariant re-check, same transaction. Cheap insurance against a
        # logic error above: abort rather than oversubscribe. Raising leaves
        # the transaction uncommitted, and the finally discards it.
        #
        # It is NOT a substitute for the lock in (1). Measured with (1)
        # removed, 24 barrier-synchronised claims on a 15-seat pool, five
        # runs: this check aborted between 0 and 6 claims per run and the
        # pool still ended at 18–24 active. Each transaction's recount sees
        # only its own insert plus whatever had committed, not concurrent
        # uncommitted inserts, so under a real race it mostly passes.
        recount = _count_active(cur, pool_id)
        if recount > pool_size:
            licence_fail("internal_error", "Capacity invariant violated")

        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "replayed": False,
        "binding_id": binding_id,
        "installation_id": installation_id,
        "account_id": account_id,
        "seat_pool_id": pool_id,
        "active_count": recount,
        "pool_size": pool_size,
        "history_row_id": history_row,
        "response": response,
    }


def _restore_last_claim_at(cur, pool_id: str) -> None:
    """
    Put seat_pools.last_claim_at back to what it was before this transaction's
    lock write, for a refusal that is committed (brief §4.J J8). The lock write
    is the load-bearing statement at the top of claim_capacity() and stays as
    it is; this undoes its VALUE, under the same lock, so a stored refusal
    leaves no business trace.

    The previous value is not read before the lock — another claim could
    commit between that read and the lock, and this would then erase its
    timestamp. It is recomputed under the lock instead: claim_capacity() is
    the only writer of last_claim_at, and it always writes the same stamp
    as the new binding's activated_at. So the previous value is exactly the
    latest activated_at in the pool, revoked bindings included, or NULL for
    a pool that never granted a seat. The raw stored value is written back,
    so it is identical on both engines.
    """
    cur.execute("SELECT activated_at FROM institution_seats WHERE seat_pool_id = ?", (pool_id,))
    latest, latest_raw = None, None
    for row in (_row_to_dict(r) for r in cur.fetchall()):
        when = licensing_time.to_datetime(row.get("activated_at"))
        if when is not None and (latest is None or when > latest):
            latest, latest_raw = when, row.get("activated_at")
    cur.execute("UPDATE seat_pools SET last_claim_at = ? WHERE id = ?", (latest_raw, pool_id))


def _count_active(cur, pool_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS active FROM institution_seats "
        "WHERE seat_pool_id = ? AND revoked_at IS NULL",
        (pool_id,),
    )
    return int(_row_to_dict(cur.fetchone())["active"])


# ===========================================================================
# Unit 2 — activation (contract §4–§5, brief §4.F–§4.H)
# ===========================================================================
#
# Lock discipline (brief §4.H H3, extended by §4.J J6 and §4.L L5). Four
# row locks exist in this file, and a transaction that takes more than one
# takes them in THIS order, never another — opposite orders in two
# transactions can deadlock:
#
#   0. the ACCOUNT row: _update_account_locked() (updated_at) — Unit 3b's
#      account editing, which takes no other lock. Any future path that needs
#      it with another takes it first;
#   1. the POOL row: claim_capacity() (last_claim_at),
#      create_or_get_ambiguity() (last_ambiguity_at) and, since Unit 3b,
#      _update_pool_locked() (updated_at);
#   2. an AMBIGUITY row: _lock_ambiguity() (last_locked_at) — resolve and
#      collection;
#   3. a BINDING row: _restore(), _issue_lease_if_absent(), _lock_binding()
#      — restore, lease issue, refresh, collection, release, rename.
#
# Who holds more than one: an activation-raised treat_as_new resolve
# (pool → ambiguity, inside claim_capacity()); a collection (ambiguity →
# binding). Refresh holds only the binding; a refresh that turns into an
# ambiguity creates it through create_or_get_ambiguity() in a transaction of
# its own, holding no binding lock. Pool and binding are never held together.
# The request history (licence_requests) is insert-only, with a non-unique
# index, so writing it waits on no other row.

REPLAY_WINDOW = timedelta(hours=24)       # contract §9.2
AMBIGUITY_TTL = timedelta(days=7)         # brief §4.C
LEASE_TTL = timedelta(days=30)            # spec §8.6 step 6

_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")


def _within_window(stamp: Any, window: timedelta, now: Optional[datetime] = None) -> bool:
    when = licensing_time.to_datetime(stamp)
    if when is None:
        return False
    return when > (now or licensing_time.now()) - window


def _active_binding_for_request(cur, request_id: str, pool_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    The active binding that last recorded this request_id within 24 hours,
    in pool_id if given, else in any pool. Older than 24 hours is treated as
    a new request (F4). Revoked bindings never answer: release always beats
    recognition, and a replay must not hand out a released activation.
    """
    if pool_id is None:
        cur.execute(
            "SELECT * FROM institution_seats WHERE activation_request_id = ? AND revoked_at IS NULL",
            (request_id,),
        )
    else:
        cur.execute(
            "SELECT * FROM institution_seats WHERE activation_request_id = ? "
            "AND seat_pool_id = ? AND revoked_at IS NULL",
            (request_id, pool_id),
        )
    rows = [_row_to_dict(r) for r in cur.fetchall()]
    now = licensing_time.now()
    rows = [r for r in rows if _within_window(r.get("activation_request_at"), REPLAY_WINDOW, now)]
    if not rows:
        return None
    # Two active bindings carrying one request_id can only come from a broken
    # client sending one ID to two pools at the same instant — not guarded,
    # by decision (H4): it cannot exceed any pool's entitlement, because
    # claim_capacity() protects each pool independently. Answer with the most
    # recent; its digest decides replay or request_id_reused.
    rows.sort(key=lambda r: str(licensing_time.to_datetime(r.get("activation_request_at"))))
    return rows[-1]


# ---------------------------------------------------------------------------
# Gates shared by both endpoints (contract §4, §5 order)
# ---------------------------------------------------------------------------

def _parse_version(text: Any) -> Optional[tuple]:
    if not isinstance(text, str):
        return None
    m = _VERSION.fullmatch(text.strip())
    return tuple(int(g) for g in m.groups()) if m else None


# The client version gate (F9), validated ONCE at startup as licensing
# configuration, like the signing key. A malformed MIN_WINDOWS_APP_VERSION
# fails CLOSED: one ERROR, licensing unavailable (503, /health
# "unavailable"), the rest of the backend unaffected (brief §4.E). It is
# never silently treated as "no minimum": that would switch off the one
# lever for stopping a broken client, and nobody would notice. In memory,
# so neither /health nor a request reads the environment or the database.
_client_gate: Dict[str, Any] = {"minimum": None, "text": None,
                                "reason": "configure_client_gate() has not run in this process"}


def configure_client_gate() -> bool:
    """Validate config.MIN_WINDOWS_APP_VERSION. Never raises; returns whether it is usable."""
    raw = config.MIN_WINDOWS_APP_VERSION
    text = raw.strip() if isinstance(raw, str) else ""
    minimum = _parse_version(text)
    if minimum is None:
        _client_gate.update(minimum=None, text=None,
                            reason=f"MIN_WINDOWS_APP_VERSION {raw!r} is not MAJOR.MINOR.PATCH")
        logger.error(
            "LICENSING UNAVAILABLE — MIN_WINDOWS_APP_VERSION %r is not MAJOR.MINOR.PATCH. Every "
            "/licence/* endpoint returns 503; the rest of the backend is unaffected.",
            raw,
        )
        return False
    _client_gate.update(minimum=minimum, text=text, reason=None)
    return True


def client_gate_status() -> Dict[str, Any]:
    return {"ready": _client_gate["minimum"] is not None, "reason": _client_gate["reason"]}


def check_client(client: Mapping[str, Any]) -> None:
    """
    Version first (426), then the client object's own shape (400). A
    malformed app_version cannot be compared, so it is invalid_request (F9).

    When the configured minimum is unusable, no 426 is decided here — there
    is no minimum to compare against. The request still gets its shape
    checks, and then require_licensing_available() refuses it with 503, so
    the contract's precedence (426 → 400 → ... → 503) holds either way.
    """
    version = _parse_version(client.get("app_version"))
    if version is None:
        licence_fail("invalid_request", "client.app_version must be MAJOR.MINOR.PATCH", field="client.app_version")
    minimum = _client_gate["minimum"]
    if minimum is not None and version < minimum:
        licence_fail("client_upgrade_required", min_app_version=_client_gate["text"])
    if str(client.get("platform") or "").strip().lower() != "windows":
        licence_fail("invalid_request", "client.platform must be windows", field="client.platform")


def licensing_config_ready() -> bool:
    """Schema ready at startup, signing key loaded, client gate valid. Memory only."""
    return (licensing_status()["ready"]
            and licence_lease.signing_key() is not None
            and _client_gate["minimum"] is not None)


def require_licensing_available() -> None:
    """
    503 unless the licensing schema was ready at startup, a signing key is
    loaded (F6), AND the client version gate is valid (F9). All read from
    memory — no database query — like /health. Binding writes additionally
    check the index live (§4.E).
    """
    if not licensing_config_ready():
        licence_fail("licensing_unavailable")


def _canonical_request_id(value: Any) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        licence_fail("invalid_request", "request_id must be a UUID v4", field="request_id")
    if parsed.version != 4:
        licence_fail("invalid_request", "request_id must be a UUID v4", field="request_id")
    return str(parsed)


def _optional_id(body: Mapping[str, Any], name: str) -> Optional[str]:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        licence_fail("invalid_request", f"{name} must be a string or null", field=name)
    value = value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Activation session (contract §4)
# ---------------------------------------------------------------------------

def validate_session_input(identifier: Any, password: Any) -> str:
    if not isinstance(identifier, str) or not identifier.strip():
        licence_fail("invalid_request", "identifier is required", field="identifier")
    if not isinstance(password, str) or not password:
        licence_fail("invalid_request", "password is required", field="password")
    # Lowercased and trimmed exactly as /auth/login does (F7).
    return identifier.strip().lower()


def _pool_summaries(cur, owner_identifier: Optional[str]) -> List[Dict[str, Any]]:
    sql = (
        "SELECT p.id AS pool_id, p.label, p.account_id, p.pool_size, p.status AS pool_status, "
        "a.name AS account_name, a.subscription_status, a.subscription_expires_at "
        "FROM seat_pools p JOIN accounts a ON a.id = p.account_id"
    )
    params: tuple = ()
    if owner_identifier is not None:
        sql += " WHERE lower(trim(a.owner_identifier)) = ?"
        params = (owner_identifier,)
    cur.execute(sql, params)
    pools = [_row_to_dict(r) for r in cur.fetchall()]
    cur.execute(
        "SELECT seat_pool_id, COUNT(*) AS active FROM institution_seats "
        "WHERE revoked_at IS NULL GROUP BY seat_pool_id"
    )
    counts = {r["seat_pool_id"]: int(r["active"]) for r in (_row_to_dict(x) for x in cur.fetchall())}
    out = []
    for p in sorted(pools, key=lambda x: (str(x.get("account_name") or ""), str(x.get("label") or ""), x["pool_id"])):
        size = int(p["pool_size"])
        active = counts.get(p["pool_id"], 0)
        out.append({
            "pool_id": p["pool_id"],
            "label": p.get("label"),
            "account_id": p["account_id"],
            "account_name": p.get("account_name"),
            "pool_size": size,
            "active": active,
            "available": max(0, size - active),
            "over_capacity": active > size,
            "pool_status": p.get("pool_status"),
            "subscription_status": p.get("subscription_status"),
            "subscription_expires_at": wire_time_or_none(p.get("subscription_expires_at")),
        })
    return out


def open_activation_session(identifier: str) -> Dict[str, Any]:
    """
    Called AFTER the password has been verified. Decides the caller's role,
    lists the pools they may activate into, and issues the 15-minute token.
    Never registers a device: that is /auth/login's job, and doing it here
    would spend the owner's Android device allowance on a lab PC (contract §11).
    """
    is_admin = is_admin_user({"sub": identifier})
    db = db_conn()
    try:
        cur = db.cursor()
        if is_admin:
            pools = _pool_summaries(cur, None)
        else:
            cur.execute(
                "SELECT COUNT(*) AS n FROM accounts WHERE lower(trim(owner_identifier)) = ?",
                (identifier,),
            )
            if int(_row_to_dict(cur.fetchone())["n"]) == 0:
                licence_fail("no_licensing_access")
            pools = _pool_summaries(cur, identifier)
    finally:
        db.close()

    role = "exampartner_admin" if is_admin else "institution_owner"
    token, exp = licence_session.issue_activation_token(identifier, role)
    return {
        "activation_token": token,
        "expires_at": wire_time(datetime.fromtimestamp(exp, tz=timezone.utc)),
        "actor_role": role,
        "pools": pools,
        "server_time": wire_time(),
    }


# ---------------------------------------------------------------------------
# Leases (brief F5)
# ---------------------------------------------------------------------------

def _sign_for_binding(binding: Mapping[str, Any], subscription_expires_at: Any, serial: int, now: datetime) -> Dict[str, Any]:
    key = licence_lease.signing_key()
    if key is None:
        licence_fail("licensing_unavailable")
    private_key, kid = key
    issued = truncate_seconds(now)
    subscription_end = truncate_seconds(licensing_time.to_datetime(subscription_expires_at))
    expires = min(issued + LEASE_TTL, subscription_end)
    payload = {
        "v": licence_lease.LEASE_VERSION,
        "typ": licence_lease.LEASE_TYP,
        "kid": kid,
        "binding_id": binding["id"],
        "installation_id": binding["installation_id"],
        "account_id": binding["account_id"],
        "seat_pool_id": binding["seat_pool_id"],
        "machine_fingerprint_hash": binding["machine_fingerprint_hash"],
        "lease_serial": serial,
        "issued_at": wire_time(issued),
        "expires_at": wire_time(expires),
        "subscription_expires_at": wire_time(subscription_end),
    }
    return {
        "lease": licence_lease.sign_lease(payload, private_key),
        "serial": serial,
        "issued": issued.isoformat(),
        "expires": expires.isoformat(),
    }


def _subscription_expires_at(cur, account_id: str) -> Any:
    cur.execute("SELECT subscription_expires_at FROM accounts WHERE id = ?", (account_id,))
    row = cur.fetchone()
    return _row_to_dict(row)["subscription_expires_at"] if row else None


def _ensure_lease(binding_id: str) -> Dict[str, Any]:
    """
    Make sure a binding created by claim_capacity() has a lease, and return
    the binding row. claim_capacity() commits the binding first and the
    lease is issued here, in a second transaction (F5); if the process dies
    in between, the client's retry is a replay that lands here and finds no
    lease, and this issues it then. Recovery is the replay path.

    Issue-if-absent: the UPDATE only writes when current_lease is still
    NULL. Two concurrent retries of one activation both arrive here for the
    same binding; on Postgres the second UPDATE blocks on the first's row
    lock, re-evaluates its WHERE after the commit, matches nothing, and both
    then read and return the SAME stored lease. Locks the binding row only
    (H3).
    """
    db = db_conn()
    try:
        cur = db.cursor()
        binding = _issue_lease_if_absent(cur, binding_id)
        if binding is None:
            licence_fail("internal_error", "The activation changed while it was being issued. Try again.")
        db.commit()
        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
        binding = _row_to_dict(cur.fetchone())
    finally:
        db.close()
    if not binding.get("current_lease") or binding.get("revoked_at") is not None:
        licence_fail("internal_error", "The activation changed while it was being issued. Try again.")
    return binding


def _issue_lease_if_absent(cur, binding_id: str) -> Optional[Dict[str, Any]]:
    """
    _ensure_lease()'s issue-if-absent, in the CALLER's transaction, so a
    collection can issue the lease and mark its ambiguity consumed in one
    commit (brief §4.I I6). Returns the binding row as it now stands in this
    transaction, or None if the binding is gone or revoked. Locks the
    binding row (its UPDATE), and only when it actually issues.
    """
    cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
    row = cur.fetchone()
    if not row or _row_to_dict(row).get("revoked_at") is not None:
        return None
    binding = _row_to_dict(row)
    if binding.get("current_lease"):
        return binding
    issued = _sign_for_binding(
        binding, _subscription_expires_at(cur, binding["account_id"]),
        int(binding.get("lease_serial") or 0) + 1, licensing_time.now(),
    )
    cur.execute(
        "UPDATE institution_seats SET current_lease = ?, lease_serial = ?, lease_issued_at = ?, "
        "lease_expires_at = ?, updated_at = ? "
        "WHERE id = ? AND revoked_at IS NULL AND current_lease IS NULL",
        (issued["lease"], issued["serial"], issued["issued"], issued["expires"],
         licensing_time.now_iso(), binding_id),
    )
    cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
    binding = _row_to_dict(cur.fetchone())
    return binding if binding.get("revoked_at") is None else None


def _activate_body(binding: Mapping[str, Any], account_name: Any, pool_label: Any) -> Dict[str, Any]:
    """The contract §5 response WITHOUT server_time — what the request history stores (J5)."""
    return {
        "result": binding.get("activation_result") or "activated",
        "lease": binding["current_lease"],
        "binding_id": binding["id"],
        "installation_id": binding["installation_id"],
        "lease_expires_at": wire_time(binding["lease_expires_at"]),
        "machine_label": binding.get("machine_label"),
        "account_name": account_name,
        "pool_label": pool_label,
    }


def _activate_response(binding: Mapping[str, Any], account_name: Any, pool_label: Any) -> Dict[str, Any]:
    return {**_activate_body(binding, account_name, pool_label), "server_time": wire_time()}


def _early_lookup(entry: "licence_history.Entry") -> Optional[Dict[str, Any]]:
    """
    The FIRST of the two history checks (licence_history docstring): before
    any lock and before any write, so a replay is fast and — because the
    history fails closed — a missing history refuses the request before
    anything at all is written.
    """
    db = db_conn()
    try:
        return licence_history.lookup(db.cursor(), entry)
    finally:
        db.close()


def _replay_activation(owner: Mapping[str, Any], account_name: Any, pool_label: Any) -> Dict[str, Any]:
    """
    Replay a recorded activation request (brief §4.I I4, §4.J J2, J5, J10).

    - A stored refusal — above all the 409 ambiguity_pending an activation
      that raised an ambiguity got — is returned exactly as stored, with the
      same ambiguity_id, whatever has happened to the ambiguity since (J10).
    - A binding the request activated or restored that has since been
      RELEASED → 403 lease_revoked: release beats a replay, so a released
      seat never receives a lease (J2, contract §9.3).
    - Otherwise the stored response, identical except server_time (J5). If
      the lease was never issued — a crash between claim_capacity()'s commit
      and _ensure_lease() (F5) — it is issued now and the response stored,
      and every concurrent replay converges on that one stored body.
    """
    if int(owner.get("status_code") or 200) >= 400:
        licence_history.replay(owner)
    binding_id = owner.get("binding_id")
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT revoked_at FROM institution_seats WHERE id = ?", (binding_id,))
        row = cur.fetchone()
    finally:
        db.close()
    if not row or _row_to_dict(row).get("revoked_at") is not None:
        licence_fail("lease_revoked")
    if licence_history.stored_response(owner) is None:
        binding = _ensure_lease(binding_id)
        db = db_conn()
        try:
            cur = db.cursor()
            licence_history.fill_response(cur, owner["id"], _activate_body(binding, account_name, pool_label))
            db.commit()
            owner = licence_history.read_row(cur, owner["id"])
        finally:
            db.close()
    return licence_history.replay(owner)


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _log(cur, *, account_id, seat_pool_id, binding_id, action, installation_id, fingerprint_hash,
         machine_label, signals_json, performed_by, actor_role, detail, stamp) -> None:
    cur.execute(
        "INSERT INTO seat_activation_log (id, account_id, seat_pool_id, binding_id, "
        "action, installation_id, machine_fingerprint_hash, machine_label, "
        "fingerprint_signals_json, performed_by, actor_role, detail_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (secrets.token_hex(16), account_id, seat_pool_id, binding_id,
         action, installation_id, fingerprint_hash, machine_label,
         signals_json, performed_by, actor_role, json.dumps(detail, sort_keys=True), stamp),
    )


def _log_clone_flag(*, account_id, pool_id, presented_installation_id, matched_binding_id, match,
                    fingerprint_hash, machine_label, signals_json, performed_by, actor_role) -> None:
    """
    clone_flagged is logged against the account of the pool being ACTIVATED
    INTO, never the matched binding's account (H7): the presented ID may
    belong to another school, and its owner-facing history must not show this
    school's PC. binding_id is NULL on the row for the same reason; the
    matched binding goes in detail_json, where ExamPartner support sees it.
    """
    db = db_conn()
    try:
        cur = db.cursor()
        _log(cur, account_id=account_id, seat_pool_id=pool_id, binding_id=None, action="clone_flagged",
             installation_id=presented_installation_id, fingerprint_hash=fingerprint_hash,
             machine_label=machine_label, signals_json=signals_json, performed_by=performed_by,
             actor_role=actor_role,
             detail={"matched_binding_id": matched_binding_id, "match": match,
                     "presented_installation_id": presented_installation_id},
             stamp=licensing_time.now_iso())
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Ambiguity (brief F3, H2)
# ---------------------------------------------------------------------------

def create_or_get_ambiguity(
    pool_id: str,
    *,
    origin: str,
    request_id: str,
    request_fp: str,
    presented_signals_json: Optional[str],
    presented_fingerprint_hash: Optional[str],
    presented_installation_id: Optional[str],
    machine_label: Optional[str],
    candidate_binding_ids: List[str],
    performed_by: str,
    actor_role: str,
    history: "licence_history.Entry",
) -> Dict[str, Any]:
    """
    The ONLY way a licence_ambiguities row is created — activation-raised
    and refresh-raised alike. Returns {"ambiguity_id", "replay"}: "replay"
    is the request-history row when this request was already recorded
    (under the lock), and the caller then replays it instead.

    The request history (brief §4.I I4): checked under the pool lock as the
    last read before the insert, and the outcome — 409 ambiguity_pending
    with this ambiguity_id — recorded in the same transaction, so a replay
    of the raising request returns exactly that, whatever later happens to
    the ambiguity (J10).

    Invariant (F4, H2): two concurrent calls with the same request_id and
    the same body that both decide "ambiguity" create exactly ONE row and
    both get the SAME id. The pool lock in claim_capacity() does not cover
    this — no binding is created — so this takes its own lock on the same
    pool row, through a different meaningful write. There is deliberately no
    unique index on request_id (H2): it would be one more correctness-
    critical index under §4.E, and it would refuse a request_id reused after
    24 hours, which F4 says is a new request.
    """
    if origin not in ("activate", "refresh"):
        licence_fail("invalid_request", "Invalid ambiguity origin", field="origin")
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()

        # (1) LOAD-BEARING — DO NOT REMOVE, REORDER OR "OPTIMISE AWAY".
        #
        # Same role as claim_capacity()'s last_claim_at write: the UPDATE
        # takes the pool-row lock, held until this transaction ends. A
        # concurrent identical request blocks here, and under READ
        # COMMITTED its lookup below then sees the row this one committed,
        # instead of inserting a second. Without it, two barrier-
        # synchronised identical requests on Postgres both find nothing and
        # both insert (test_licensing_activate_pg.py). SQLite serialises
        # writers anyway, which is why only the Postgres test can catch it.
        #
        # Written as a meaningful update — last_ambiguity_at records when the
        # pool last raised an ambiguity — so deleting it visibly breaks
        # something rather than silently removing the lock.
        cur.execute(
            "UPDATE seat_pools SET last_ambiguity_at = ? WHERE id = ?",
            (stamp, pool_id),
        )

        cur.execute("SELECT account_id FROM seat_pools WHERE id = ?", (pool_id,))
        pool = cur.fetchone()
        if not pool:
            licence_fail("pool_access_denied")
        account_id = _row_to_dict(pool)["account_id"]

        # (2) Under the lock: the same request already raised one? The
        # ambiguity's own request columns first (requests from before Unit 3a
        # deployed have no history row), then the request history, LAST,
        # immediately before the insert — that is where the race tests hold a
        # request, so removing the lock above makes both insert every run
        # (brief §9 R2).
        cur.execute(
            "SELECT id, request_fp, created_at FROM licence_ambiguities "
            "WHERE seat_pool_id = ? AND request_id = ? AND origin = ?",
            (pool_id, request_id, origin),
        )
        now = licensing_time.now()
        raised = [row for row in (_row_to_dict(r) for r in cur.fetchall())
                  if _within_window(row.get("created_at"), REPLAY_WINDOW, now)]
        owner = licence_history.lookup_locked(cur, history)
        if owner is not None:
            return {"ambiguity_id": owner.get("ambiguity_id"), "replay": owner}
        for row in raised:
            if row.get("request_fp") != request_fp:
                licence_fail("request_id_reused")
            return {"ambiguity_id": row["id"], "replay": None}

        # (3) Exactly one row, its audit row and its history row, in the same
        # transaction.
        ambiguity_id = secrets.token_hex(16)
        cur.execute(
            "INSERT INTO licence_ambiguities (id, account_id, seat_pool_id, presented_signals_json, "
            "presented_fingerprint_hash, presented_installation_id, machine_label, "
            "candidate_binding_ids_json, status, origin, request_id, request_fp, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ambiguity_id, account_id, pool_id, presented_signals_json,
             presented_fingerprint_hash, presented_installation_id, machine_label,
             json.dumps(sorted(candidate_binding_ids)), "open", origin, request_id, request_fp, stamp),
        )
        _log(cur, account_id=account_id, seat_pool_id=pool_id, binding_id=None, action="ambiguous_flagged",
             installation_id=presented_installation_id, fingerprint_hash=presented_fingerprint_hash,
             machine_label=machine_label, signals_json=presented_signals_json, performed_by=performed_by,
             actor_role=actor_role,
             detail={"ambiguity_id": ambiguity_id, "origin": origin,
                     "candidate_binding_ids": sorted(candidate_binding_ids)},
             stamp=stamp)
        status, detail = licence_detail("ambiguity_pending", ambiguity_id=ambiguity_id)
        licence_history.record(cur, history, outcome="ambiguity", status_code=status, response=detail,
                               ambiguity_id=ambiguity_id)
        db.commit()
        return {"ambiguity_id": ambiguity_id, "replay": None}
    finally:
        db.close()


def _lock_ambiguity(cur, ambiguity_id: str, stamp: str) -> Optional[Dict[str, Any]]:
    """
    Take the AMBIGUITY-row lock and read the row under it (brief §4.J).

    LOAD-BEARING — DO NOT REMOVE, REORDER OR "OPTIMISE AWAY". This looks like
    a bookkeeping write. It is what makes exactly one resolution per
    ambiguity and exactly one collection per resolution (I5, I6): a second,
    concurrent resolve or collection blocks here, and under READ COMMITTED
    then reads the row the first one committed — resolved, or consumed —
    instead of acting on the state it saw before. Same shape and purpose as
    claim_capacity()'s last_claim_at write. Without it, two barrier-
    synchronised collections on Postgres both restore, and two resolves
    both resolve (test_licensing_unit3a_pg.py). SQLite serialises writers
    anyway, which is why only the Postgres tests can catch its removal.

    last_locked_at is bookkeeping, not state ("when a resolve or collection
    last held this row"), on purpose: a stored refusal commits this write
    too, and J8 allows a refusal to leave no business trace.

    Lock order: after the pool, before a binding (J6).
    """
    cur.execute("UPDATE licence_ambiguities SET last_locked_at = ? WHERE id = ?", (stamp, ambiguity_id))
    cur.execute("SELECT * FROM licence_ambiguities WHERE id = ?", (ambiguity_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def _lock_binding(cur, binding_id: str, stamp: str) -> Optional[Dict[str, Any]]:
    """
    Take the BINDING-row lock and read the row under it: refresh issue (I3),
    collection, release (I7). The write matches the row WHATEVER its
    revoked_at (brief §4.J J9), so a release of an already released binding
    still serialises, and two concurrent requests on one binding always
    meet here. A refresh then re-checks revoked under it, so a release that
    committed while the refresh was deciding wins.

    LOAD-BEARING, like the pool and ambiguity locks: without it two
    refreshes issue the same serial, and two releases of a released binding
    under one request_id both answer already_released.
    """
    cur.execute("UPDATE institution_seats SET updated_at = ? WHERE id = ?", (stamp, binding_id))
    cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def _ambiguity_state(row: Mapping[str, Any]) -> str:
    """open | expired | resolved | consumed. An open row 7 days old IS expired, whatever its column says."""
    status = row.get("status")
    if status == "open" and not _within_window(row.get("created_at"), AMBIGUITY_TTL):
        return "expired"
    return status


def _resolution_fields(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {"resolution": row.get("resolution"), "resolved_binding_id": row.get("resolved_binding_id"),
            "resolved_at": wire_time_or_none(row.get("resolved_at"))}


def _resolution_state_refusal(row: Mapping[str, Any]) -> Optional[tuple]:
    """
    Why this ambiguity cannot be resolved now, as (code, extra), or None if
    it can (brief §4.I I5): only an OPEN ambiguity younger than 7 days.
    """
    state = _ambiguity_state(row)
    if state == "expired":
        return "ambiguity_expired", {}
    if state == "resolved":
        return "ambiguity_already_resolved", _resolution_fields(row)
    if state == "consumed":
        return "ambiguity_consumed", _resolution_fields(row)
    return None


def _resolution_body(ambiguity_id: str, decision: str, binding_id: Optional[str], stamp: Any) -> Dict[str, Any]:
    """The resolve response WITHOUT server_time — what the request history stores (J5)."""
    return {"ok": True, "ambiguity_id": ambiguity_id, "status": "resolved", "resolution": decision,
            "resolved_binding_id": binding_id, "resolved_at": wire_time(stamp)}


def _log_resolution(cur, ambiguity: Mapping[str, Any], *, decision: str, binding_id: Optional[str],
                    admin: str, stamp: str) -> None:
    _log(cur, account_id=ambiguity["account_id"], seat_pool_id=ambiguity["seat_pool_id"], binding_id=binding_id,
         action="ambiguity_resolved", installation_id=ambiguity.get("presented_installation_id"),
         fingerprint_hash=ambiguity.get("presented_fingerprint_hash"), machine_label=ambiguity.get("machine_label"),
         signals_json=ambiguity.get("presented_signals_json"), performed_by=admin, actor_role="exampartner_admin",
         detail={"ambiguity_id": ambiguity["id"], "decision": decision, "binding_id": binding_id,
                 "origin": ambiguity.get("origin")},
         stamp=stamp)


def _ambiguity_outcome(row: Mapping[str, Any]) -> None:
    """Raise the state of an ambiguity this request owns (F3). Unit 2 resolves nothing."""
    status = row.get("status")
    if status == "consumed":
        licence_fail("ambiguity_consumed")
    if status == "expired" or (status == "open" and not _within_window(row.get("created_at"), AMBIGUITY_TTL)):
        licence_fail("ambiguity_expired")
    licence_fail("ambiguity_pending", ambiguity_id=row["id"])


def _check_collector(row: Optional[Mapping[str, Any]], *, origin: str, pool_id: str, request_id: str,
                     request_fp: str) -> Dict[str, Any]:
    """
    Unit 2's identity checks for a request carrying ambiguity_id (contract
    §10), kept exactly and in their order (brief §4.I I6):

    1. Not an ambiguity of THIS pool raised by THIS kind of request →
       ambiguity_mismatch, the same answer whether it belongs to another
       pool or does not exist.
    2. The request reuses the ambiguity's ORIGINAL raising request_id →
       request_id_reused. Contract §10 requires collection under a NEW
       request_id; resending the raising one is a client bug, and must not
       be mistaken for a replay of the raising request.
    3. The current request's identity digest differs from the one stored
       when the ambiguity was raised → ambiguity_mismatch (contract §9.3: the
       PC presenting the id is not the one that raised it). The stored
       digest is compared, never recomputed (H5).

    Identity is settled before the state is revealed, so a different PC
    holding someone else's ambiguity_id learns nothing about it.
    """
    if not row or row.get("seat_pool_id") != pool_id or row.get("origin") != origin:
        licence_fail("ambiguity_mismatch")
    if row.get("request_id") == request_id:
        licence_fail("request_id_reused")
    if row.get("request_fp") != request_fp:
        licence_fail("ambiguity_mismatch")
    return dict(row)


def _unresolved_outcome(row: Mapping[str, Any]) -> None:
    """Raise the answer for an ambiguity that is not (or no longer) collectable: pending, expired or consumed."""
    state = _ambiguity_state(row)
    if state == "consumed":
        licence_fail("ambiguity_consumed")
    if state == "expired":
        licence_fail("ambiguity_expired")
    if state == "open":
        licence_fail("ambiguity_pending", ambiguity_id=row["id"])


def _collect_on_activate(ambiguity_id: str, *, pool: Mapping[str, Any], request_id: str, request_fp: str,
                         history: "licence_history.Entry", reading: "fingerprint_service.Reading",
                         signals_json: str, label: str, caller: str, actor_role: str) -> Dict[str, Any]:
    """
    POST /licence/activate with ambiguity_id: collecting an activation-raised
    ambiguity (contract §10, brief §4.I I6). In order: Unit 2's identity
    checks; the request history (a replay returns its original result — or
    lease_revoked, J2); then the state:

    | state                          | answer                                      |
    |--------------------------------|---------------------------------------------|
    | open, under 7 days             | 409 ambiguity_pending                       |
    | open 7 days or more, expired   | 409 ambiguity_expired                       |
    | consumed                       | 409 ambiguity_consumed                      |
    | resolved, its binding revoked  | 403 lease_revoked                           |
    | resolved recognize_existing    | restore onto it, 200 result "restored"      |
    | resolved treat_as_new          | issue its lease, 200 result "activated"     |

    Only the successes are recorded in the history. A request answered
    pending owns nothing, so its retry after the admin resolves collects.
    """
    pool_id = pool["id"]
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM licence_ambiguities WHERE id = ?", (ambiguity_id,))
        row = cur.fetchone()
    finally:
        db.close()
    row = _check_collector(_row_to_dict(row) if row else None, origin="activate", pool_id=pool_id,
                           request_id=request_id, request_fp=request_fp)

    owner = _early_lookup(history)
    if owner is not None:
        return _replay_activation(owner, pool.get("account_name"), pool.get("label"))

    if _ambiguity_state(row) != "resolved":
        _unresolved_outcome(row)
    target = row.get("resolved_binding_id")
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT revoked_at FROM institution_seats WHERE id = ?", (target,))
        bound = cur.fetchone()
    finally:
        db.close()
    if not target or not bound or _row_to_dict(bound).get("revoked_at") is not None:
        licence_fail("lease_revoked")
    return _collect_resolved(row, target=target, pool=pool, reading=reading, signals_json=signals_json,
                             request_id=request_id, request_fp=request_fp, history=history, label=label,
                             caller=caller, actor_role=actor_role)


def _collect_resolved(ambiguity: Mapping[str, Any], *, target: str, pool: Mapping[str, Any],
                      reading: "fingerprint_service.Reading", signals_json: str, request_id: str,
                      request_fp: str, history: "licence_history.Entry", label: str, caller: str,
                      actor_role: str) -> Dict[str, Any]:
    """
    Apply a resolved activation-raised ambiguity, marking it consumed in the
    SAME transaction that issues the lease (single use, I6). Both kinds lock
    the ambiguity row first and then the binding (ambiguity → binding, J6);
    neither touches the pool lock. Two concurrent collections serialise on
    the ambiguity lock: the second finds it consumed — and replays if it is
    the same logical request (I4), or gets ambiguity_consumed if not.
    """
    if ambiguity.get("resolution") == "recognize_existing":
        body = _restore(target, reading=reading, signals_json=signals_json, request_id=request_id,
                        request_fp=request_fp, machine_label=label, performed_by=caller, actor_role=actor_role,
                        history=history, account_name=pool.get("account_name"), pool_label=pool.get("label"),
                        consume_ambiguity_id=ambiguity["id"])
        return {**body, "server_time": wire_time()}

    # treat_as_new: claim_capacity() created the binding at resolution; its
    # lease is issued now that the PC is back.
    replay_owner, body = None, None
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()
        locked = _lock_ambiguity(cur, ambiguity["id"], stamp)
        bound = _lock_binding(cur, target, stamp)
        owner = licence_history.lookup_locked(cur, history)
        if owner is not None:
            replay_owner = owner
        elif not locked or locked.get("status") != "resolved":
            licence_fail("ambiguity_consumed")
        elif not bound or bound.get("revoked_at") is not None:
            licence_fail("lease_revoked")
        else:
            binding = _issue_lease_if_absent(cur, target)
            if binding is None:
                licence_fail("lease_revoked")
            cur.execute(
                "UPDATE licence_ambiguities SET status = 'consumed', consumed_at = ? "
                "WHERE id = ? AND status = 'resolved'",
                (stamp, ambiguity["id"]),
            )
            body = _activate_body(binding, pool.get("account_name"), pool.get("label"))
            licence_history.record(cur, history, outcome="activated", response=body, binding_id=target)
            db.commit()
    finally:
        db.close()
    if replay_owner is not None:
        return _replay_activation(replay_owner, pool.get("account_name"), pool.get("label"))
    return {**body, "server_time": wire_time()}


# ---------------------------------------------------------------------------
# Restore (brief F10)
# ---------------------------------------------------------------------------

def _restore(binding_id: str, *, reading: "fingerprint_service.Reading", signals_json: str,
             request_id: str, request_fp: str, machine_label: str,
             performed_by: str, actor_role: str, history: "licence_history.Entry",
             account_name: Any, pool_label: Any, consume_ambiguity_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Recognised reinstall (case D): the same activation, a new installation,
    no capacity. One transaction, locking the BINDING row (H3) — and, when
    collecting a resolved recognize_existing ambiguity (I6), the AMBIGUITY
    row first (ambiguity → binding, J6), which is marked consumed in the
    same commit. Returns the contract §5 response without server_time, as
    the request history stores it (J5).
    """
    replay_owner, body, revoked = None, None, False
    db = db_conn()
    try:
        cur = db.cursor()
        now = licensing_time.now()
        stamp = now.isoformat()

        locked = _lock_ambiguity(cur, consume_ambiguity_id, stamp) if consume_ambiguity_id else None

        # Restore changes installation_id, so it is a binding write under the
        # §4.E standing rule: the index is checked live. Twice, for the same
        # reasons as claim_capacity()'s (0) and (2): before the lock so a
        # missing schema is a 503 rather than a database error, and after it
        # so the check runs inside the transaction that writes.
        if not licensing_index_present(cur):
            licence_fail("licensing_unavailable")

        # Binding-row lock, held to commit: two concurrent retries of one
        # restore serialise here, and the second then sees the first's
        # request columns below and replays instead of issuing a second
        # installation_id (which would orphan the first response's).
        cur.execute(
            "UPDATE institution_seats SET updated_at = ? WHERE id = ? AND revoked_at IS NULL",
            (stamp, binding_id),
        )
        if not licensing_index_present(cur):
            licence_fail("licensing_unavailable")

        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
        row = cur.fetchone()
        binding = _row_to_dict(row) if row else None
        # The request history, the last read before the write (I4).
        owner = licence_history.lookup_locked(cur, history)

        if owner is not None:
            replay_owner = owner
        elif not binding or binding.get("revoked_at") is not None:
            if consume_ambiguity_id:
                revoked = True          # a collection: release beats the resolution (I6)
            else:
                # Released between the decision and here. Retryable: the next
                # attempt will not see it as a candidate (release beats recognition).
                licence_fail("internal_error", "This computer's activation changed during the request. Try again.")
        elif consume_ambiguity_id and (not locked or locked.get("status") != "resolved"):
            licence_fail("ambiguity_consumed")
        elif binding.get("activation_request_id") == request_id and _within_window(
            binding.get("activation_request_at"), REPLAY_WINDOW, now
        ):
            # A restore recorded on the binding but not in the history: one
            # made before Unit 3a deployed. Unit 2's replay, unchanged.
            if binding.get("activation_request_fp") != request_fp:
                licence_fail("request_id_reused")
            body = _activate_body(binding, account_name, pool_label)  # the close discards the lock write
        else:
            previous = {"installation_id": binding["installation_id"],
                        "machine_fingerprint_hash": binding["machine_fingerprint_hash"]}
            installation_id = secrets.token_hex(16)
            # H6: the binding's existing label stands — the owner may have renamed
            # it. The technician's label is used only if the binding has none.
            label = binding.get("machine_label") or machine_label
            new_hash = fingerprint_service.identity_hash(reading)
            # Ratchet the stored identity forward on the strong match (spec
            # §8.6.1), so accumulated maintenance does not drift the binding away
            # from its machine one recognised change at a time.
            cur.execute(
                "UPDATE institution_seats SET installation_id = ?, last_seen_at = ?, machine_label = ?, "
                "machine_fingerprint_hash = ?, fingerprint_signals_json = ?, "
                "activation_request_id = ?, activation_request_at = ?, activation_request_fp = ?, "
                "activation_result = ?, updated_at = ? WHERE id = ?",
                (installation_id, stamp, label, new_hash, signals_json,
                 request_id, stamp, request_fp, "restored", stamp, binding_id),
            )
            binding.update(installation_id=installation_id, machine_label=label, machine_fingerprint_hash=new_hash)

            issued = _sign_for_binding(
                binding, _subscription_expires_at(cur, binding["account_id"]),
                int(binding.get("lease_serial") or 0) + 1, now,
            )
            cur.execute(
                "UPDATE institution_seats SET current_lease = ?, lease_serial = ?, lease_issued_at = ?, "
                "lease_expires_at = ? WHERE id = ?",
                (issued["lease"], issued["serial"], issued["issued"], issued["expires"], binding_id),
            )
            detail = {"previous_installation_id": previous["installation_id"],
                      "previous_fingerprint_hash": previous["machine_fingerprint_hash"],
                      "lease_serial": issued["serial"]}
            if consume_ambiguity_id:
                cur.execute(
                    "UPDATE licence_ambiguities SET status = 'consumed', consumed_at = ? "
                    "WHERE id = ? AND status = 'resolved'",
                    (stamp, consume_ambiguity_id),
                )
                detail["ambiguity_id"] = consume_ambiguity_id
            _log(cur, account_id=binding["account_id"], seat_pool_id=binding["seat_pool_id"], binding_id=binding_id,
                 action="restore", installation_id=installation_id, fingerprint_hash=new_hash,
                 machine_label=label, signals_json=signals_json, performed_by=performed_by, actor_role=actor_role,
                 detail=detail, stamp=stamp)

            cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
            body = _activate_body(_row_to_dict(cur.fetchone()), account_name, pool_label)
            licence_history.record(cur, history, outcome="restored", response=body, binding_id=binding_id)
            db.commit()
    finally:
        db.close()
    if revoked:
        licence_fail("lease_revoked")
    if replay_owner is not None:
        return _without_time(_replay_activation(replay_owner, account_name, pool_label))
    return body


def _without_time(body: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in body.items() if k != "server_time"}


# ---------------------------------------------------------------------------
# POST /licence/activate (contract §5, brief F3)
# ---------------------------------------------------------------------------

def _active_pool_bindings(cur, pool_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        "SELECT id, seat_pool_id, account_id, installation_id, fingerprint_signals_json "
        "FROM institution_seats WHERE seat_pool_id = ? AND revoked_at IS NULL",
        (pool_id,),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def activate(caller: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Everything after the activation token has been verified, in contract §5
    order: version → validation → fingerprint_unreadable →
    licensing_unavailable → pool_access_denied → pool_inactive →
    subscription_inactive → ambiguity_* → pool_full.
    """
    check_client(body.get("client") or {})

    request_id = _canonical_request_id(body.get("request_id"))
    pool_id = body.get("pool_id")
    if not isinstance(pool_id, str) or not pool_id.strip():
        licence_fail("invalid_request", "pool_id is required", field="pool_id")
    pool_id = pool_id.strip()
    label = body.get("machine_label")
    if not isinstance(label, str) or not (1 <= len(label.strip()) <= 64):
        licence_fail("invalid_request", "machine_label must be 1–64 characters", field="machine_label")
    label = label.strip()
    presented_installation_id = _optional_id(body, "installation_id")
    ambiguity_id = _optional_id(body, "ambiguity_id")
    signals = body.get("fingerprint_signals")
    try:
        fingerprint_service.validate_signals(signals)
    except fingerprint_service.SignalsInvalid as exc:
        licence_fail("invalid_request", str(exc), field=exc.field)

    unreadable = fingerprint_service.unreadable_classes(signals)
    if unreadable:
        licence_fail("fingerprint_unreadable", classes=unreadable)

    require_licensing_available()

    # Pool access. A pool that does not exist and a pool the caller does not
    # own get the same answer (contract §9.3), so the code cannot be used to
    # discover other schools' pool ids. Ownership is re-checked NOW, against
    # the pool's own account — the token's role is informational (F7).
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT p.id, p.account_id, p.label, p.status, a.name AS account_name, "
            "a.subscription_status, a.subscription_expires_at "
            "FROM seat_pools p JOIN accounts a ON a.id = p.account_id WHERE p.id = ?",
            (pool_id,),
        )
        pool = cur.fetchone()
    finally:
        db.close()
    if not pool:
        licence_fail("pool_access_denied")
    pool = _row_to_dict(pool)
    try:
        require_account_owner({"sub": caller}, pool["account_id"])
    except HTTPException:
        licence_fail("pool_access_denied")
    actor_role = "exampartner_admin" if is_admin_user({"sub": caller}) else "institution_owner"

    if pool.get("status") != "active":
        licence_fail("pool_inactive")
    if pool.get("subscription_status") != "active" or licensing_time.is_expired(pool.get("subscription_expires_at")):
        licence_fail("subscription_inactive",
                     subscription_expires_at=wire_time_or_none(pool.get("subscription_expires_at")))

    reading = fingerprint_service.read(signals)
    request_fp = fingerprint_service.request_digest(pool_id, reading)
    signals_json = json.dumps(signals, sort_keys=True, separators=(",", ":"))
    fingerprint_hash = fingerprint_service.identity_hash(reading)

    # The request history entry (brief §4.I I4). Its digest covers the pool,
    # the normalised reading and the ambiguity_id, so a collection and a
    # plain activation can never share a request_id.
    history = licence_history.Entry(
        request_id, "activate",
        licence_history.digest("activate", request_fp=request_fp, ambiguity_id=ambiguity_id),
        account_id=pool["account_id"], seat_pool_id=pool_id, ambiguity_id=ambiguity_id,
    )
    account_name, pool_label = pool.get("account_name"), pool.get("label")

    # Collecting an ambiguity (contract §10). After the digest exists,
    # because the collection must prove it comes from the PC that raised it.
    if ambiguity_id is not None:
        return _collect_on_activate(ambiguity_id, pool=pool, request_id=request_id, request_fp=request_fp,
                                    history=history, reading=reading, signals_json=signals_json, label=label,
                                    caller=caller, actor_role=actor_role)

    # F3 step 1 — replay. The request history first (I4): it remembers
    # every activation, restore and collection for 24 hours, including ones
    # a later request has overtaken (brief §9 R1). Then Unit 2's per-binding
    # and per-ambiguity columns, which answer for requests made before Unit
    # 3a deployed. Looked up across ALL pools, so a request_id reused
    # sequentially in a different pool is refused (H4): the digest includes
    # the pool id, so it can never match there.
    owner = _early_lookup(history)
    if owner is not None:
        return _replay_activation(owner, account_name, pool_label)
    db = db_conn()
    try:
        cur = db.cursor()
        replayed = _active_binding_for_request(cur, request_id)
        cur.execute(
            "SELECT * FROM licence_ambiguities WHERE request_id = ? AND origin = 'activate'",
            (request_id,),
        )
        raised = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    if replayed is not None:
        if replayed.get("activation_request_fp") != request_fp:
            licence_fail("request_id_reused")
        binding = _ensure_lease(replayed["id"])
        return _activate_response(binding, pool.get("account_name"), pool.get("label"))
    for row in raised:
        if _within_window(row.get("created_at"), REPLAY_WINDOW):
            if row.get("request_fp") != request_fp:
                licence_fail("request_id_reused")
            _ambiguity_outcome(row)

    # F3 steps 2–5 — decide, on the pool's ACTIVE bindings only. Revoked
    # bindings are never candidates: release always beats recognition (rule F).
    db = db_conn()
    try:
        cur = db.cursor()
        bindings = _active_pool_bindings(cur, pool_id)
        presented = None
        if presented_installation_id:
            cur.execute(
                "SELECT id, seat_pool_id, account_id, installation_id, fingerprint_signals_json "
                "FROM institution_seats WHERE installation_id = ? AND revoked_at IS NULL",
                (presented_installation_id,),
            )
            row = cur.fetchone()
            presented = _row_to_dict(row) if row else None
            other_pool = (
                _active_pool_bindings(cur, presented["seat_pool_id"])
                if presented and presented["seat_pool_id"] != pool_id else None
            )
    finally:
        db.close()

    readings = {b["id"]: fingerprint_service.read_stored(b.get("fingerprint_signals_json")) for b in bindings}
    index = fingerprint_service.PoolIndex(readings.values())
    quality = fingerprint_service.identity_quality(reading, index)

    # Step 2 — clone check. A presented installation_id is evidence, never
    # identity: if its binding is not a strong match for this reading, log
    # it and decide on the fingerprint alone.
    if presented is not None:
        if other_pool is None:
            match = fingerprint_service.match_strength(reading, readings[presented["id"]], index)
        else:
            other_index = fingerprint_service.PoolIndex(
                fingerprint_service.read_stored(b.get("fingerprint_signals_json")) for b in other_pool)
            match = fingerprint_service.match_strength(
                reading, fingerprint_service.read_stored(presented.get("fingerprint_signals_json")), other_index)
        if match != "strong":
            _log_clone_flag(account_id=pool["account_id"], pool_id=pool_id,
                            presented_installation_id=presented_installation_id,
                            matched_binding_id=presented["id"], match=match,
                            fingerprint_hash=fingerprint_hash, machine_label=label,
                            signals_json=signals_json, performed_by=caller, actor_role=actor_role)

    strengths = {bid: fingerprint_service.match_strength(reading, r, index) for bid, r in readings.items()}
    strong = sorted(b for b, s in strengths.items() if s == "strong")
    weak = sorted(b for b, s in strengths.items() if s == "weak")

    candidates: List[str] = []
    if len(strong) == 1:                                   # step 3 — case D
        body = _restore(strong[0], reading=reading, signals_json=signals_json,
                        request_id=request_id, request_fp=request_fp, machine_label=label,
                        performed_by=caller, actor_role=actor_role, history=history,
                        account_name=account_name, pool_label=pool_label)
        return {**body, "server_time": wire_time()}
    if len(strong) > 1:                                    # step 3 — more than one
        candidates = strong
    elif weak:                                             # step 4
        candidates = weak
    elif quality in ("weak", "degenerate"):                # step 5 (F2, H1)
        candidates = sorted(bid for bid, r in readings.items()
                            if fingerprint_service.activation_continuity_match(reading, r))

    if candidates:
        raised = create_or_get_ambiguity(
            pool_id, origin="activate", request_id=request_id, request_fp=request_fp,
            presented_signals_json=signals_json, presented_fingerprint_hash=fingerprint_hash,
            presented_installation_id=presented_installation_id, machine_label=label,
            candidate_binding_ids=candidates, performed_by=caller, actor_role=actor_role,
            history=history,
        )
        if raised["replay"] is not None:
            return _replay_activation(raised["replay"], account_name, pool_label)
        licence_fail("ambiguity_pending", ambiguity_id=raised["ambiguity_id"])

    # Step 6 — a new machine: case G, or pool_full (case H) from inside the claim.
    claimed = claim_capacity(
        pool_id,
        machine_fingerprint_hash=fingerprint_hash,
        fingerprint_confidence=quality,
        performed_by=caller,
        actor_role=actor_role,
        request_id=request_id,
        request_fp=request_fp,
        history=history,
        fingerprint_signals_json=signals_json,
        machine_label=label,
    )
    if claimed.get("history") is not None:
        return _replay_activation(claimed["history"], account_name, pool_label)
    binding = _ensure_lease(claimed["binding_id"])
    if claimed.get("replayed"):
        # Found by the binding's own request columns, not the history: a
        # request from before Unit 3a deployed. Unit 2's replay, unchanged.
        return _activate_response(binding, account_name, pool_label)
    # Store the response now that the lease exists (F5). If two fillers
    # race, both return the body that was stored first — identical anyway.
    db = db_conn()
    try:
        cur = db.cursor()
        licence_history.fill_response(cur, claimed["history_row_id"],
                                      _activate_body(binding, account_name, pool_label))
        db.commit()
        row = licence_history.read_row(cur, claimed["history_row_id"])
    finally:
        db.close()
    return licence_history.replay(row)


# ===========================================================================
# Unit 3a — POST /licence/refresh (contract §9, brief §4.I I1–I4, §4.J)
# ===========================================================================
#
# No Authorization header: the signed lease is the authorisation, and the
# presenting machine is verified against the binding on every refresh, so a
# copied lease does not refresh on another PC (spec §8.6, §8.6.1). Never
# calls get_current_user(). It claims no capacity, creates no binding and
# never changes installation_id, so the §4.E live index check does not apply.

REFRESH_LOG_GAP = timedelta(days=7)       # brief §4.I I3 step 5


def _verify_presented_lease(lease: str) -> Dict[str, Any]:
    """
    I1 step 2: verify with the public key of the CONFIGURED kid only. Any
    other kid — kid 0, the published TEST key, included — is lease_invalid.
    Key rotation is deferred (spec §8.7).

    A lease that fails is logged as an application WARNING only, never to
    seat_activation_log (J3): it has no trustworthy account, and logging it
    against the account its payload claims would let anyone write rows into
    any school's owner-visible history. The warning names the claimed
    binding_id and kid, and never the lease string.
    """
    key = licence_lease.signing_key()
    if key is None:
        licence_fail("licensing_unavailable")
    private_key, kid = key
    try:
        return licence_lease.verify_lease(lease, {kid: private_key.public_key()})
    except licence_lease.LeaseInvalid as exc:
        claimed: Dict[str, Any] = {}
        try:
            segment = lease.split(".")[0]
            decoded = json.loads(licence_lease._b64url_decode(segment).decode("utf-8"))
            claimed = decoded if isinstance(decoded, dict) else {}
        except Exception:
            pass
        logger.warning("lease_invalid on /licence/refresh (%s): claimed binding_id=%r kid=%r",
                       exc, claimed.get("binding_id"), claimed.get("kid"))
        licence_fail("lease_invalid")


def _log_event(*, account_id: str, seat_pool_id: Optional[str], binding_id: Optional[str], action: str,
               installation_id: Optional[str], fingerprint_hash: Optional[str], machine_label: Optional[str],
               signals_json: Optional[str], detail: Mapping[str, Any], performed_by: str = "client",
               actor_role: str = "client") -> None:
    """One audit row in its own transaction: a denial or a flag that issues nothing."""
    db = db_conn()
    try:
        cur = db.cursor()
        _log(cur, account_id=account_id, seat_pool_id=seat_pool_id, binding_id=binding_id, action=action,
             installation_id=installation_id, fingerprint_hash=fingerprint_hash, machine_label=machine_label,
             signals_json=signals_json, performed_by=performed_by, actor_role=actor_role, detail=detail,
             stamp=licensing_time.now_iso())
        db.commit()
    finally:
        db.close()


def _deny_refresh(code: str, *, binding: Mapping[str, Any], payload: Mapping[str, Any], request_id: str,
                  reason: Optional[str] = None, **extra: Any) -> None:
    """Log refresh_denied against the binding's own account, then refuse (I1: every denial is logged)."""
    _log_event(account_id=binding["account_id"], seat_pool_id=binding.get("seat_pool_id"),
               binding_id=binding["id"], action="refresh_denied", installation_id=payload.get("installation_id"),
               fingerprint_hash=None, machine_label=binding.get("machine_label"), signals_json=None,
               detail={"code": code, "reason": reason or code, "request_id": request_id,
                       "presented_serial": payload.get("lease_serial")})
    licence_fail(code, **extra)


def _resolve_lease_binding(payload: Mapping[str, Any], request_id: str) -> Dict[str, Any]:
    """
    I1 step 3. Release beats everything after this point.

    - Not found → lease_revoked, logged against the lease's own account: the
      lease is signed by us, so that account is trustworthy.
    - A correctly signed lease whose account or pool disagrees with its
      binding is a server defect: ERROR, refresh_denied against the
      BINDING's account (J3), lease_invalid.
    - Revoked → lease_revoked.
    """
    binding_id = payload.get("binding_id")
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id if isinstance(binding_id, str) else "",))
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        if isinstance(payload.get("account_id"), str):
            _log_event(account_id=payload["account_id"], seat_pool_id=payload.get("seat_pool_id"),
                       binding_id=None, action="refresh_denied", installation_id=payload.get("installation_id"),
                       fingerprint_hash=None, machine_label=None, signals_json=None,
                       detail={"code": "lease_revoked", "reason": "binding_not_found",
                               "claimed_binding_id": binding_id, "request_id": request_id})
        licence_fail("lease_revoked")
    binding = _row_to_dict(row)
    if payload.get("account_id") != binding["account_id"] or payload.get("seat_pool_id") != binding["seat_pool_id"]:
        logger.error(
            "LICENSING DEFECT — a correctly signed lease disagrees with its binding %s: lease account/pool "
            "%r/%r, binding %r/%r. Refused as lease_invalid.",
            binding["id"], payload.get("account_id"), payload.get("seat_pool_id"),
            binding["account_id"], binding["seat_pool_id"],
        )
        _deny_refresh("lease_invalid", binding=binding, payload=payload, request_id=request_id,
                      reason="lease_disagrees_with_binding")
    if binding.get("revoked_at") is not None:
        _deny_refresh("lease_revoked", binding=binding, payload=payload, request_id=request_id)
    return binding


def _replay_refresh(owner: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Replay a recorded refresh (I4, contract §9.2). A refresh that raised an
    ambiguity returns its stored ambiguity_pending (J10's rule, the same for
    refresh). A refresh that issued a lease returns the binding's CURRENT
    lease unchanged — even when a later refresh under another request_id has
    issued a newer one, because issuing again is the one thing a replay must
    never do. A released binding is lease_revoked: release beats a replay.
    """
    if int(owner.get("status_code") or 200) >= 400:
        licence_history.replay(owner)
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (owner.get("binding_id"),))
        row = cur.fetchone()
    finally:
        db.close()
    binding = _row_to_dict(row) if row else None
    if not binding or binding.get("revoked_at") is not None or not binding.get("current_lease"):
        licence_fail("lease_revoked")
    return _refresh_response(binding["current_lease"], binding["lease_expires_at"])


def _refresh_response(lease: str, expires: Any) -> Dict[str, Any]:
    return {"result": "refreshed", "lease": lease, "lease_expires_at": wire_time(expires), "server_time": wire_time()}


def refresh(body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    POST /licence/refresh, in the order of brief §4.I I1, after FastAPI's 422:

     1. client_upgrade_required → invalid_request → fingerprint_unreadable →
        licensing_unavailable;
     2. the lease verifies with the configured kid → else lease_invalid;
     3. its binding exists and agrees with it → else lease_revoked /
        lease_invalid; revoked → lease_revoked;
     4. the lease's installation is the binding's current one → else
        machine_mismatch (installation_superseded);
     5. the request history: a replay returns the current lease;
     6. pool active, subscription active → else pool_inactive /
        subscription_inactive;
     7. collection, if the body carries ambiguity_id;
     8. machine verification (I2) → refresh, machine_mismatch, or ambiguity;
     9. a stale presented serial is flagged, never denied;
    10. issue (I3).

    An EXPIRED presented lease is never a bar at any step (spec §8.6 step 5):
    a PC offline for 40 days recovers by itself. Steps 1–4 do not consult the
    request history (J6), so they keep their own codes when it is missing.
    """
    check_client(body.get("client") or {})
    request_id = _canonical_request_id(body.get("request_id"))
    lease = body.get("lease")
    if not isinstance(lease, str) or not lease.strip():
        licence_fail("invalid_request", "lease is required", field="lease")
    ambiguity_id = _optional_id(body, "ambiguity_id")
    signals = body.get("fingerprint_signals")
    try:
        fingerprint_service.validate_signals(signals)
    except fingerprint_service.SignalsInvalid as exc:
        licence_fail("invalid_request", str(exc), field=exc.field)
    unreadable = fingerprint_service.unreadable_classes(signals)
    if unreadable:
        licence_fail("fingerprint_unreadable", classes=unreadable)
    require_licensing_available()

    payload = _verify_presented_lease(lease)                      # step 2
    binding = _resolve_lease_binding(payload, request_id)         # step 3
    if payload.get("installation_id") != binding.get("installation_id"):   # step 4
        _deny_refresh("machine_mismatch", binding=binding, payload=payload, request_id=request_id,
                      reason="installation_superseded")

    reading = fingerprint_service.read(signals)
    identity_fp = fingerprint_service.refresh_digest(binding["id"], reading)
    signals_json = json.dumps(signals, sort_keys=True, separators=(",", ":"))
    history = licence_history.Entry(
        request_id, "refresh",
        licence_history.digest("refresh", request_fp=identity_fp, ambiguity_id=ambiguity_id),
        account_id=binding["account_id"], seat_pool_id=binding["seat_pool_id"], binding_id=binding["id"],
        ambiguity_id=ambiguity_id,
    )

    owner = _early_lookup(history)                                # step 5
    if owner is not None:
        return _replay_refresh(owner)

    db = db_conn()                                                # step 6
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT p.status AS pool_status, a.subscription_status, a.subscription_expires_at "
            "FROM seat_pools p JOIN accounts a ON a.id = p.account_id WHERE p.id = ?",
            (binding["seat_pool_id"],),
        )
        entitlement = _row_to_dict(cur.fetchone() or {})
    finally:
        db.close()
    if entitlement.get("pool_status") != "active":
        _deny_refresh("pool_inactive", binding=binding, payload=payload, request_id=request_id)
    if entitlement.get("subscription_status") != "active" or licensing_time.is_expired(
        entitlement.get("subscription_expires_at")
    ):
        _deny_refresh("subscription_inactive", binding=binding, payload=payload, request_id=request_id,
                      subscription_expires_at=wire_time_or_none(entitlement.get("subscription_expires_at")))

    if ambiguity_id is not None:                                  # step 7
        return _collect_on_refresh(ambiguity_id, binding=binding, payload=payload, request_id=request_id,
                                   identity_fp=identity_fp, history=history, reading=reading,
                                   signals_json=signals_json)

    # Step 8 — machine verification against the binding's STORED snapshot,
    # never the lease (spec §8.6.1): the binding is the mutable current
    # truth, the lease a point-in-time copy that may predate maintenance.
    db = db_conn()
    try:
        cur = db.cursor()
        pool_bindings = _active_pool_bindings(cur, binding["seat_pool_id"])
    finally:
        db.close()
    snapshot = fingerprint_service.read_stored(binding.get("fingerprint_signals_json"))
    index = fingerprint_service.PoolIndex(
        fingerprint_service.read_stored(b.get("fingerprint_signals_json")) for b in pool_bindings)
    match = fingerprint_service.match_strength(reading, snapshot, index)
    fingerprint_hash = fingerprint_service.identity_hash(reading)

    if match == "mismatch":
        # A different physical machine. Deny THIS machine; the activation is
        # left untouched, even if installation continuity also holds — a
        # motherboard replacement is resolved by the school releasing the
        # old activation (I2, contract §9.3).
        _log_event(account_id=binding["account_id"], seat_pool_id=binding["seat_pool_id"],
                   binding_id=binding["id"], action="clone_flagged", installation_id=payload.get("installation_id"),
                   fingerprint_hash=fingerprint_hash, machine_label=binding.get("machine_label"),
                   signals_json=signals_json,
                   detail={"origin": "refresh", "match": match, "request_id": request_id,
                           "presented_serial": payload.get("lease_serial")})
        licence_fail("machine_mismatch")
    if match != "strong" and not fingerprint_service.refresh_continuity(reading, snapshot):
        # Weak or no identity evidence, and installation continuity broken
        # (I2): a human decides. The ambiguity is created in a transaction of
        # its own — this request holds no binding lock — so pool and binding
        # locks are never held together (H3).
        raised = create_or_get_ambiguity(
            binding["seat_pool_id"], origin="refresh", request_id=request_id, request_fp=identity_fp,
            presented_signals_json=signals_json, presented_fingerprint_hash=fingerprint_hash,
            presented_installation_id=payload.get("installation_id"), machine_label=binding.get("machine_label"),
            candidate_binding_ids=[binding["id"]], performed_by="client", actor_role="client", history=history,
        )
        if raised["replay"] is not None:
            return _replay_refresh(raised["replay"])
        licence_fail("ambiguity_pending", ambiguity_id=raised["ambiguity_id"])

    return _issue_refresh(binding["id"], payload=payload, reading=reading, signals_json=signals_json,
                          history=history, request_id=request_id)


def _collect_on_refresh(ambiguity_id: str, *, binding: Mapping[str, Any], payload: Mapping[str, Any],
                        request_id: str, identity_fp: str, history: "licence_history.Entry",
                        reading: "fingerprint_service.Reading", signals_json: str) -> Dict[str, Any]:
    """
    POST /licence/refresh with ambiguity_id: collecting a refresh-raised
    ambiguity (contract §10, brief §4.I I6). The lease has already verified
    (I1 steps 2–4). Unit 2's identity checks — the binding's pool, origin
    refresh, not the raising request_id, the same identity digest — and the
    lease's binding must be the ambiguity's candidate; otherwise
    ambiguity_mismatch. Then by state:

    - open → ambiguity_pending; expired → ambiguity_expired; consumed →
      ambiguity_consumed (a replay was already answered at step 5);
    - resolved recognize_existing → refresh through I3, ratcheting the
      presented reading in, consumed in the same transaction → "refreshed";
    - resolved treat_as_new → machine_mismatch: ExamPartner decided this is
      a different machine, so it activates as one. Never consumed (J6), and
      a refresh can never hand a PC a new binding.
    """
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM licence_ambiguities WHERE id = ?", (ambiguity_id,))
        row = cur.fetchone()
    finally:
        db.close()
    row = _check_collector(_row_to_dict(row) if row else None, origin="refresh",
                           pool_id=binding["seat_pool_id"], request_id=request_id, request_fp=identity_fp)
    try:
        candidates = json.loads(row.get("candidate_binding_ids_json") or "[]")
    except ValueError:
        candidates = []
    if binding["id"] not in candidates:
        licence_fail("ambiguity_mismatch")
    if _ambiguity_state(row) != "resolved":
        _unresolved_outcome(row)
    if row.get("resolution") != "recognize_existing":
        _deny_refresh("machine_mismatch", binding=binding, payload=payload, request_id=request_id,
                      reason="ambiguity_treat_as_new")
    return _issue_refresh(binding["id"], payload=payload, reading=reading, signals_json=signals_json,
                          history=history, request_id=request_id, consume_ambiguity_id=row["id"])


def _issue_refresh(binding_id: str, *, payload: Mapping[str, Any], reading: "fingerprint_service.Reading",
                   signals_json: str, history: "licence_history.Entry", request_id: str,
                   consume_ambiguity_id: Optional[str] = None) -> Dict[str, Any]:
    """
    I3 — issue a refreshed lease. One transaction, locking the BINDING row —
    and, when collecting, the AMBIGUITY row first (ambiguity → binding, J6).
    Never the pool.

    Under the lock, the binding is re-read and re-checked, in I1's order:
    revoked → lease_revoked; installation moved → machine_mismatch; the
    request history → replay. A release or restore that committed while
    this request was deciding wins. Then:

    - the ratchet: if the NORMALISED reading differs from the stored
      snapshot's (J6), the presented hash and raw signals are written onto
      the binding — on a strong match (spec §8.6.1) and on a continuity
      refresh (§4.A) alike. fingerprint_confidence is NOT changed: it records
      quality at claim time (spec §3.4);
    - a stale presented serial is logged and refreshes anyway (spec §8.6.1).
      lease_serial is read under the lock, so the second of two concurrent
      refreshes from one lease is flagged too (J6);
    - lease_serial + 1, expires = min(now + 30 days, subscription end), sign;
    - log `refresh` only when the presented lease had expired, when
      last_seen_at was null or older than 7 days, or when the binding
      ratcheted (spec §3.5). Otherwise last_seen_at moves silently.
    """
    replay_owner, response, denial = None, None, None
    db = db_conn()
    try:
        cur = db.cursor()
        now = licensing_time.now()
        stamp = now.isoformat()
        locked = _lock_ambiguity(cur, consume_ambiguity_id, stamp) if consume_ambiguity_id else None
        row = _lock_binding(cur, binding_id, stamp)
        owner = licence_history.lookup_locked(cur, history)       # the last read before the write

        if not row or row.get("revoked_at") is not None:
            denial = ("lease_revoked", None)
        elif row.get("installation_id") != payload.get("installation_id"):
            denial = ("machine_mismatch", "installation_superseded")
        elif owner is not None:
            replay_owner = owner
        elif consume_ambiguity_id and (not locked or locked.get("status") != "resolved"):
            licence_fail("ambiguity_consumed")
        else:
            binding = dict(row)
            snapshot = fingerprint_service.read_stored(binding.get("fingerprint_signals_json"))
            ratchet = snapshot.normalised() != reading.normalised()
            previous_hash = binding["machine_fingerprint_hash"]
            if ratchet:
                binding["machine_fingerprint_hash"] = fingerprint_service.identity_hash(reading)
                binding["fingerprint_signals_json"] = signals_json

            presented_serial = payload.get("lease_serial")
            current_serial = int(binding.get("lease_serial") or 0)
            if isinstance(presented_serial, int) and presented_serial < current_serial:
                _log(cur, account_id=binding["account_id"], seat_pool_id=binding["seat_pool_id"],
                     binding_id=binding_id, action="stale_serial_flagged",
                     installation_id=binding["installation_id"],
                     fingerprint_hash=fingerprint_service.identity_hash(reading),
                     machine_label=binding.get("machine_label"), signals_json=signals_json,
                     performed_by="client", actor_role="client",
                     detail={"presented_serial": presented_serial, "current_serial": current_serial,
                             "request_id": request_id},
                     stamp=stamp)

            issued = _sign_for_binding(binding, _subscription_expires_at(cur, binding["account_id"]),
                                       current_serial + 1, now)
            cur.execute(
                "UPDATE institution_seats SET current_lease = ?, lease_serial = ?, lease_issued_at = ?, "
                "lease_expires_at = ?, last_seen_at = ?, machine_fingerprint_hash = ?, "
                "fingerprint_signals_json = ?, updated_at = ? WHERE id = ?",
                (issued["lease"], issued["serial"], issued["issued"], issued["expires"], stamp,
                 binding["machine_fingerprint_hash"], binding["fingerprint_signals_json"], stamp, binding_id),
            )
            if consume_ambiguity_id:
                cur.execute(
                    "UPDATE licence_ambiguities SET status = 'consumed', consumed_at = ? "
                    "WHERE id = ? AND status = 'resolved'",
                    (stamp, consume_ambiguity_id),
                )

            presented_expired = licensing_time.is_expired(payload.get("expires_at"), at=now)
            last_seen = licensing_time.to_datetime(row.get("last_seen_at"))
            gap = last_seen is None or last_seen <= now - REFRESH_LOG_GAP
            if presented_expired or gap or ratchet or consume_ambiguity_id:
                detail = {"lease_serial": issued["serial"], "presented_lease_expired": presented_expired,
                          "ratcheted": ratchet, "request_id": request_id}
                if ratchet:
                    detail["previous_fingerprint_hash"] = previous_hash
                if consume_ambiguity_id:
                    detail["ambiguity_id"] = consume_ambiguity_id
                _log(cur, account_id=binding["account_id"], seat_pool_id=binding["seat_pool_id"],
                     binding_id=binding_id, action="refresh", installation_id=binding["installation_id"],
                     fingerprint_hash=binding["machine_fingerprint_hash"],
                     machine_label=binding.get("machine_label"),
                     signals_json=signals_json if ratchet else None, performed_by="client", actor_role="client",
                     detail=detail, stamp=stamp)

            # The replay of a refresh returns the binding's CURRENT lease
            # (contract §9.2), so the history records the outcome, not a body.
            licence_history.record(cur, history, outcome="refreshed", binding_id=binding_id)
            db.commit()
            response = _refresh_response(issued["lease"], issued["expires"])
    finally:
        db.close()

    if denial is not None:
        # Logged after the lock is released: an audit row of a denial is not
        # an outcome stored for replay, so J8 does not apply to it.
        _deny_refresh(denial[0], binding=row or {"id": binding_id, "account_id": payload.get("account_id"),
                                                 "seat_pool_id": payload.get("seat_pool_id")},
                      payload=payload, request_id=request_id, reason=denial[1])
    if replay_owner is not None:
        return _replay_refresh(replay_owner)
    return response


# ===========================================================================
# Unit 3a — ambiguity resolution (brief §4.I I5, §4.J)
# ===========================================================================
#
# POST /support/licensing/ambiguities/{id}/resolve, ExamPartner admin only.
# The route has already refused 401 not_authenticated and 403
# admin_required (J4). A resolution is never changed once made; to undo
# one, the school releases the binding.

_DECISIONS = ("recognize_existing", "treat_as_new")


def resolve_ambiguity(admin: str, ambiguity_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Order (I5): 400 invalid_request → 503 licensing_unavailable → 404
    ambiguity_not_found → the request history → the state checks.

    - recognize_existing records the decision only; the restore or the
      refresh happens when the PC collects (I6), so a PC that never comes
      back changes nothing.
    - treat_as_new, activation-raised, claims capacity NOW, through
      claim_capacity() — the single chokepoint — so a full pool is reported
      to the admin rather than discovered later by the technician. The claim
      and the resolution are one transaction (claim_capacity(resolves=...)).
    - treat_as_new, refresh-raised, claims nothing: the PC presenting the
      lease is not the binding's machine, and collection answers
      machine_mismatch, sending it through ordinary activation.

    Every outcome, success or refusal, is stored for replay — committed under
    the lock that decided it (J8).
    """
    request_id = _canonical_request_id(body.get("request_id"))
    decision = body.get("decision")
    if decision not in _DECISIONS:
        licence_fail("invalid_request", "decision must be recognize_existing or treat_as_new", field="decision")
    binding_id = body.get("binding_id")
    if decision == "recognize_existing":
        if not isinstance(binding_id, str) or not binding_id.strip():
            licence_fail("invalid_request", "binding_id is required for recognize_existing", field="binding_id")
        binding_id = binding_id.strip()
    elif binding_id is not None:
        licence_fail("invalid_request", "binding_id is only for recognize_existing", field="binding_id")
    require_licensing_available()

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM licence_ambiguities WHERE id = ?", (ambiguity_id,))
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        licence_fail("ambiguity_not_found")
    ambiguity = _row_to_dict(row)

    history = licence_history.Entry(
        request_id, "resolve",
        licence_history.digest("resolve", ambiguity_id=ambiguity_id, decision=decision, binding_id=binding_id),
        account_id=ambiguity["account_id"], seat_pool_id=ambiguity["seat_pool_id"], binding_id=binding_id,
        ambiguity_id=ambiguity_id,
    )
    owner = _early_lookup(history)
    if owner is not None:
        return licence_history.replay(owner)

    if decision == "treat_as_new" and ambiguity.get("origin") == "activate":
        # The new binding's identity quality, from the presented reading
        # against the pool as it is now (I5).
        db = db_conn()
        try:
            cur = db.cursor()
            pool_bindings = _active_pool_bindings(cur, ambiguity["seat_pool_id"])
        finally:
            db.close()
        presented = fingerprint_service.read_stored(ambiguity.get("presented_signals_json"))
        index = fingerprint_service.PoolIndex(
            fingerprint_service.read_stored(b.get("fingerprint_signals_json")) for b in pool_bindings)
        claimed = claim_capacity(
            ambiguity["seat_pool_id"],
            machine_fingerprint_hash=ambiguity.get("presented_fingerprint_hash")
            or fingerprint_service.identity_hash(presented),
            fingerprint_confidence=fingerprint_service.identity_quality(presented, index),
            performed_by=admin,
            actor_role="exampartner_admin",
            request_id=request_id,
            request_fp=history.request_fp,
            history=history,
            fingerprint_signals_json=ambiguity.get("presented_signals_json"),
            machine_label=ambiguity.get("machine_label"),
            resolves={"ambiguity_id": ambiguity_id, "admin": admin},
        )
        if claimed.get("history") is not None:
            return licence_history.replay(claimed["history"])
        if claimed.get("response") is None:
            # claim_capacity() found a binding by its request columns but no
            # history row — impossible for a resolve, whose binding and
            # history row are written together.
            licence_fail("internal_error", "The resolution could not be confirmed. Try again.")
        return {**claimed["response"], "server_time": wire_time()}

    return _resolve_without_claim(ambiguity_id, decision=decision, binding_id=binding_id, admin=admin,
                                  history=history)


def _resolve_without_claim(ambiguity_id: str, *, decision: str, binding_id: Optional[str], admin: str,
                           history: "licence_history.Entry") -> Dict[str, Any]:
    """
    recognize_existing, and a refresh-raised treat_as_new: record the
    decision only. One transaction on the AMBIGUITY-row lock; no pool lock,
    no binding lock — recognize_existing reads its candidate without one
    (J6), and a release that lands before the commit is caught at
    collection as lease_revoked.
    """
    replay_owner, response = None, None
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()
        ambiguity = _lock_ambiguity(cur, ambiguity_id, stamp)
        candidate = None
        if binding_id is not None:
            cur.execute("SELECT id, revoked_at FROM institution_seats WHERE id = ?", (binding_id,))
            found = cur.fetchone()
            candidate = _row_to_dict(found) if found else None
        owner = licence_history.lookup_locked(cur, history)        # the last read before the write

        def refuse(code: str, **extra: Any):
            # Stored for replay, committed under the ambiguity lock (J8). The
            # lock write is bookkeeping (last_locked_at), so the history row
            # is the only business trace.
            status, detail = licence_detail(code, **extra)
            licence_history.record(cur, history, outcome="refused", status_code=status, response=detail)
            db.commit()
            raise_stored(status, detail)

        if owner is not None:
            replay_owner = owner
        else:
            if ambiguity is None:
                licence_fail("ambiguity_not_found")
            state_refusal = _resolution_state_refusal(ambiguity)
            if state_refusal is not None:
                refuse(state_refusal[0], **state_refusal[1])
            if decision == "recognize_existing":
                try:
                    candidates = json.loads(ambiguity.get("candidate_binding_ids_json") or "[]")
                except ValueError:
                    candidates = []
                # A released candidate can never be recognised (rule F).
                if binding_id not in candidates or candidate is None or candidate.get("revoked_at") is not None:
                    refuse("ambiguity_candidate_invalid", candidate_binding_ids=candidates)
            resolved_binding = binding_id if decision == "recognize_existing" else None
            cur.execute(
                "UPDATE licence_ambiguities SET status = 'resolved', resolution = ?, resolved_binding_id = ?, "
                "resolved_by = ?, resolved_at = ? WHERE id = ? AND status = 'open'",
                (decision, resolved_binding, admin, stamp, ambiguity_id),
            )
            _log_resolution(cur, ambiguity, decision=decision, binding_id=resolved_binding, admin=admin,
                            stamp=stamp)
            response = _resolution_body(ambiguity_id, decision, resolved_binding, stamp)
            licence_history.record(cur, history, outcome="resolved", response=response,
                                   binding_id=resolved_binding)
            db.commit()
    finally:
        db.close()
    if replay_owner is not None:
        return licence_history.replay(replay_owner)
    return {**response, "server_time": wire_time()}


# ===========================================================================
# Unit 3a — release (brief §4.I I7, §4.J J9; spec §8.5)
# ===========================================================================

_REVOKE_REASONS = ("manual", "replacement", "hardware_failure", "retired", "renewal_retire", "support", "other")
_REASON_MAX = 500


def release_seat(user: Mapping[str, Any], binding_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    POST /institutions/seats/{binding_id}/release. The route has already
    refused 401 not_authenticated. Then: 400 invalid_request → 503
    licensing_unavailable → 403 binding_access_denied → the request history
    → the effect.

    Authorisation resolves the binding first and checks the caller owns ITS
    account (spec §9). An unknown binding and another school's binding get
    the same 403, so the code reveals nothing about which ids exist. It runs
    before the replay check, so a replay never shows one owner another
    school's response.

    Neither reason field ever gates the release (spec §8.5). It must not
    need the old installation to be reachable: the dead PC is the case
    schools actually hit.

    Kept in this one function, so Unit 3b can add the licence session token
    as a second way to authorise without restructuring the route (I9).
    """
    request_id = _canonical_request_id(body.get("request_id"))
    revoke_reason = body.get("revoke_reason")
    if revoke_reason is None:
        revoke_reason = "manual"
    if revoke_reason not in _REVOKE_REASONS:
        licence_fail("invalid_request", "Unknown revoke_reason", field="revoke_reason")
    reason = body.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            licence_fail("invalid_request", "reason must be a string", field="reason")
        if len(reason) > _REASON_MAX:
            licence_fail("invalid_request", f"reason is at most {_REASON_MAX} characters", field="reason")
        reason = reason.strip() or None
    require_licensing_available()

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        licence_fail("binding_access_denied")
    binding = _row_to_dict(row)
    try:
        caller = require_account_owner(user, binding["account_id"])
    except HTTPException:
        licence_fail("binding_access_denied")
    actor_role = "exampartner_admin" if is_admin_user({"sub": caller}) else "institution_owner"

    history = licence_history.Entry(
        request_id, "release",
        licence_history.digest("release", binding_id=binding_id, revoke_reason=revoke_reason, reason=reason),
        account_id=binding["account_id"], seat_pool_id=binding["seat_pool_id"], binding_id=binding_id,
    )
    owner = _early_lookup(history)
    if owner is not None:
        return licence_history.replay(owner)
    return _release_locked(binding_id, caller=caller, actor_role=actor_role, revoke_reason=revoke_reason,
                           reason=reason, history=history)


def _release_locked(binding_id: str, *, caller: str, actor_role: str, revoke_reason: str,
                    reason: Optional[str], history: "licence_history.Entry") -> Dict[str, Any]:
    """
    The effect, in one transaction locking the BINDING row only (I7) — and
    taking that lock whether or not the binding is already released (J9).
    Otherwise two concurrent releases of a released binding with one
    request_id and two bodies could both find no history row, both record
    one, and both answer already_released, where one must be
    request_id_reused.

    A new request_id on a released binding answers already_released with the
    original released_at, records itself, and writes nothing else.

    The pool counts are read in this transaction, after the revoke and
    before the commit, so they include it — and are stored with the
    response under the lock (J8). Reading them after the commit would mean
    recording the outcome after the lock was released, which J8 forbids.
    """
    replay_owner, response = None, None
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()
        row = _lock_binding(cur, binding_id, stamp)
        owner = licence_history.lookup_locked(cur, history)        # the last read before the write
        if owner is not None:
            replay_owner = owner
        else:
            if row is None:
                licence_fail("binding_access_denied")
            if row.get("revoked_at") is not None:
                result, released_at = "already_released", row["revoked_at"]
            else:
                result, released_at = "released", stamp
                cur.execute(
                    "UPDATE institution_seats SET revoked_at = ?, revoke_reason = ?, revoked_by = ?, "
                    "updated_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (stamp, revoke_reason, caller, stamp, binding_id),
                )
                cur.execute(
                    "INSERT INTO seat_activation_log (id, account_id, seat_pool_id, binding_id, "
                    "action, installation_id, machine_fingerprint_hash, machine_label, "
                    "fingerprint_signals_json, performed_by, actor_role, reason, detail_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (secrets.token_hex(16), row["account_id"], row["seat_pool_id"], binding_id,
                     "release", row.get("installation_id"), row.get("machine_fingerprint_hash"),
                     row.get("machine_label"), row.get("fingerprint_signals_json"), caller, actor_role, reason,
                     json.dumps({"revoke_reason": revoke_reason, "request_id": history.request_id},
                                sort_keys=True),
                     stamp),
                )
            cur.execute("SELECT pool_size FROM seat_pools WHERE id = ?", (row["seat_pool_id"],))
            pool_size = int(_row_to_dict(cur.fetchone())["pool_size"])
            active = _count_active(cur, row["seat_pool_id"])
            response = {
                "result": result,
                "binding_id": binding_id,
                "released_at": wire_time(released_at),
                "seat_pool_id": row["seat_pool_id"],
                "pool_size": pool_size,
                "active": active,
                "available": max(0, pool_size - active),
                "over_capacity": active > pool_size,
            }
            licence_history.record(cur, history, outcome=result, response=response, binding_id=binding_id)
            db.commit()
    finally:
        db.close()
    if replay_owner is not None:
        return licence_history.replay(replay_owner)
    return {**response, "server_time": wire_time()}


# ===========================================================================
# Unit 3a — GET /support/licensing/ambiguities (brief §4.I I8)
# ===========================================================================

_AMBIGUITY_STATES = ("open", "resolved", "consumed", "expired")


def list_ambiguities(status: Optional[str] = "open") -> Dict[str, Any]:
    """
    Everything Nitoni needs to decide an ambiguity by PowerShell: the school,
    the pool, the presented reading NORMALISED, and each candidate with its
    match strength against that reading. Filtered by the EFFECTIVE state, so
    an open ambiguity 7 days old is listed as expired. Plain {"ok": true}
    body, like the pilot setup endpoints (F11): not part of the client
    contract.
    """
    status = (status or "open").strip().lower()
    if status not in _AMBIGUITY_STATES:
        raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(_AMBIGUITY_STATES)}")

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT l.*, a.name AS account_name, p.label AS pool_label FROM licence_ambiguities l "
            "LEFT JOIN accounts a ON a.id = l.account_id LEFT JOIN seat_pools p ON p.id = l.seat_pool_id"
        )
        rows = [r for r in (_row_to_dict(x) for x in cur.fetchall()) if _ambiguity_state(r) == status]
        rows.sort(key=lambda r: licensing_time.to_datetime(r.get("created_at")) or licensing_time.now())
        pools: Dict[str, List[Dict[str, Any]]] = {}
        listed = []
        for row in rows:
            pool_id = row.get("seat_pool_id")
            if pool_id not in pools:
                pools[pool_id] = _active_pool_bindings(cur, pool_id)
            index = fingerprint_service.PoolIndex(
                fingerprint_service.read_stored(b.get("fingerprint_signals_json")) for b in pools[pool_id])
            presented = fingerprint_service.read_stored(row.get("presented_signals_json"))
            try:
                candidate_ids = json.loads(row.get("candidate_binding_ids_json") or "[]")
            except ValueError:
                candidate_ids = []
            candidates = []
            for candidate_id in candidate_ids:
                cur.execute("SELECT * FROM institution_seats WHERE id = ?", (candidate_id,))
                found = cur.fetchone()
                if not found:
                    candidates.append({"binding_id": candidate_id, "active": False})
                    continue
                candidate = _row_to_dict(found)
                candidates.append({
                    "binding_id": candidate_id,
                    "machine_label": candidate.get("machine_label"),
                    "last_seen_at": wire_time_or_none(candidate.get("last_seen_at")),
                    "fingerprint_confidence": candidate.get("fingerprint_confidence"),
                    "active": candidate.get("revoked_at") is None,
                    "match_strength": fingerprint_service.match_strength(
                        presented, fingerprint_service.read_stored(candidate.get("fingerprint_signals_json")),
                        index),
                })
            created = licensing_time.to_datetime(row.get("created_at"))
            listed.append({
                "ambiguity_id": row["id"],
                "account_id": row.get("account_id"),
                "account_name": row.get("account_name"),
                "seat_pool_id": pool_id,
                "pool_label": row.get("pool_label"),
                "origin": row.get("origin"),
                "machine_label": row.get("machine_label"),
                "created_at": wire_time_or_none(created),
                "expires_at": wire_time_or_none(created + AMBIGUITY_TTL) if created else None,
                "status": status,
                **_resolution_fields(row),
                "presented": {
                    "smbios_uuid": presented.uuid,
                    "serials": sorted(presented.serials),
                    "system_disk": presented.activation_disk,
                    "onboard_addresses": sorted(presented.activation_nics),
                    "machine_guid": presented.machine_guid,
                },
                "candidates": candidates,
            })
    finally:
        db.close()
    return {"ok": True, "status": status, "ambiguities": listed, "server_time": wire_time()}


# ===========================================================================
# Pilot setup — ExamPartner admin only (brief F11, spec §10.1)
# ===========================================================================
#
# Without these, the pilot school's account and pool could only be created
# by editing Neon by hand, which spec §10.4 forbids. They are NOT part of the
# client contract: plain {"ok": true, ...} bodies and ordinary HTTPException
# errors. Authorisation (require_admin) is the route's job; validation is
# here, per backend/CLAUDE.md, so a future bulk path cannot skip it.
# Account editing and pool resizing are Unit 3.

_SUBSCRIPTION_STATUSES = {"active", "expired", "suspended"}


def _clean_text(value: Any, name: str, *, required: bool, max_len: int = 200) -> Optional[str]:
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{name} is required")
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{name} must be a string")
    text = value.strip()
    if not text:
        if required:
            raise HTTPException(status_code=400, detail=f"{name} is required")
        return None
    if len(text) > max_len:
        raise HTTPException(status_code=400, detail=f"{name} is too long")
    return text


def create_account(*, name: Any, owner_identifier: Any, contact_email: Any = None,
                   account_type: Any = "institution") -> Dict[str, Any]:
    name = _clean_text(name, "name", required=True)
    owner = _clean_text(owner_identifier, "owner_identifier", required=True).lower()
    contact = _clean_text(contact_email, "contact_email", required=False)
    if (account_type or "institution") != "institution":
        # V1 creates only institutions; reseller scoping does not exist (spec §14).
        raise HTTPException(status_code=400, detail="account_type must be institution")

    db = db_conn()
    try:
        cur = db.cursor()
        # The owner must already be a registered user — the activation
        # session signs them in with their ordinary ExamPartner password.
        cur.execute("SELECT identifier FROM users WHERE identifier = ?", (owner,))
        if not cur.fetchone():
            raise HTTPException(status_code=400, detail="owner_identifier is not a registered user")
        account_id = secrets.token_hex(16)
        stamp = licensing_time.now_iso()
        cur.execute(
            "INSERT INTO accounts (id, name, account_type, contact_email, owner_identifier, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, name, "institution", contact, owner, stamp, stamp),
        )
        db.commit()
    finally:
        db.close()
    return {"ok": True, "account": {"id": account_id, "name": name, "account_type": "institution",
                                    "contact_email": contact, "owner_identifier": owner,
                                    "subscription_status": None, "subscription_expires_at": None}}


def create_seat_pool(account_id: str, *, pool_size: Any, label: Any = None) -> Dict[str, Any]:
    if not isinstance(pool_size, int) or isinstance(pool_size, bool) or pool_size < 1:
        raise HTTPException(status_code=400, detail="pool_size must be a positive integer")
    label = _clean_text(label, "label", required=False, max_len=64)
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM accounts WHERE id = ?", (account_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Account not found")
        pool_id = secrets.token_hex(16)
        stamp = licensing_time.now_iso()
        cur.execute(
            "INSERT INTO seat_pools (id, account_id, pool_size, label, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pool_id, account_id, pool_size, label, "active", stamp, stamp),
        )
        db.commit()
    finally:
        db.close()
    return {"ok": True, "seat_pool": {"id": pool_id, "account_id": account_id, "pool_size": pool_size,
                                      "label": label, "status": "active"}}


def set_subscription(account_id: str, *, subscription_status: Any, subscription_expires_at: Any,
                     performed_by: str) -> Dict[str, Any]:
    if subscription_status not in _SUBSCRIPTION_STATUSES:
        raise HTTPException(status_code=400, detail="subscription_status must be active, expired or suspended")
    if not isinstance(subscription_expires_at, str) or not subscription_expires_at.strip():
        raise HTTPException(status_code=400, detail="subscription_expires_at is required")
    try:
        expires = licensing_time.to_datetime(subscription_expires_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="subscription_expires_at must be an ISO 8601 timestamp")

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT subscription_status, subscription_expires_at FROM accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found")
        before = _row_to_dict(row)
        stamp = licensing_time.now_iso()
        cur.execute(
            "UPDATE accounts SET subscription_status = ?, subscription_expires_at = ?, updated_at = ? WHERE id = ?",
            (subscription_status, expires.isoformat(), stamp, account_id),
        )
        _log(cur, account_id=account_id, seat_pool_id=None, binding_id=None, action="renewal",
             installation_id=None, fingerprint_hash=None, machine_label=None, signals_json=None,
             performed_by=performed_by, actor_role="exampartner_admin",
             detail={"from": {"subscription_status": before.get("subscription_status"),
                              "subscription_expires_at": wire_time_or_none(before.get("subscription_expires_at"))},
                     "to": {"subscription_status": subscription_status,
                            "subscription_expires_at": wire_time(expires)}},
             stamp=stamp)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "account_id": account_id, "subscription_status": subscription_status,
            "subscription_expires_at": wire_time(expires)}


# ===========================================================================
# Unit 3b — a school manages its own computers (brief §4.K, §4.L; contract
# v1.1 §10A)
# ===========================================================================
#
# The five owner routes — pools, machines, history, rename, release — accept
# the licence session token OR an ordinary bearer token. The route reduces
# either one to {"sub": identifier} (routes/licensing.py, _licence_caller())
# and drops the token's role, so nothing here can trust it: ownership and
# admin status are re-derived on every call (K1, F7). A changed owner
# therefore loses access on their next call (K5, L5).
#
# Locks (H3, J6, L5). Rename takes the BINDING row (_lock_binding()), like
# release. Pool editing takes the POOL row — the same row lock claim_capacity()
# takes, so a resize and a claim serialise. Account editing takes the ACCOUNT
# row, alone. The full order is account → pool → ambiguity → binding; today no
# path holds the account lock together with any other, and a future one that
# does must be checked against that order.
#
# The admin routes here (account and pool editing, signals, the test clock)
# return plain {"ok": true, ...} bodies, like F11, but CODED errors through
# licence_error(), like the resolve route (L1).

_OWNER_HISTORY_ACTIONS = (                 # contract §10A.3 — an ALLOWLIST (K2)
    "activate", "restore", "release", "rename", "refresh", "refresh_denied",
    "ambiguous_flagged", "ambiguity_resolved", "resize", "pool_status", "renewal",
)
_ACTORS = {"institution_owner": "owner", "exampartner_admin": "exampartner", "client": "computer"}
_SIGNAL_ACTIONS = ("clone_flagged", "stale_serial_flagged", "refresh_denied")     # K8
_PAGE_DEFAULT, _PAGE_MAX = 50, 200
_LABEL_MAX = 64                            # contract §5 / §10A.4
_POOL_SIZE_MAX = 10000                     # K6
_TEST_CLOCK_MAX = timedelta(days=3660)     # ±10 years (L5)
_CURSOR = re.compile(r"[A-Za-z0-9_-]+")
_HEX_ID = re.compile(r"[0-9a-f]{32}")


def _owner_access(caller: Mapping[str, Any], account_id: Optional[str], code: str) -> tuple:
    """
    Resolve, then check (spec §9): the caller must own `account_id` — an
    account RESOLVED from the object acted on, never one the request merely
    names — or be an ExamPartner admin. Returns (identifier, actor_role).

    Every refusal is the same 403 `code`, a missing account included, so
    nothing leaks (contract §10A). The existence check comes first on
    purpose: require_account_owner() lets an admin through for ANY id.
    """
    if not account_id:
        licence_fail(code)
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM accounts WHERE id = ?", (account_id,))
        found = cur.fetchone()
    finally:
        db.close()
    if not found:
        licence_fail(code)
    try:
        identifier = require_account_owner(caller, account_id)
    except HTTPException:
        licence_fail(code)
    return identifier, ("exampartner_admin" if is_admin_user({"sub": identifier}) else "institution_owner")


def _query_id(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        licence_fail("invalid_request", f"{name} must be a non-empty string", field=name)
    return value.strip()


def _parse_limit(value: Any) -> int:
    if value is None:
        return _PAGE_DEFAULT
    if not isinstance(value, str) or not value.isdigit() or not (1 <= int(value) <= _PAGE_MAX):
        licence_fail("invalid_request", f"limit must be an integer from 1 to {_PAGE_MAX}", field="limit")
    return int(value)


# ---------------------------------------------------------------------------
# Keyset pagination (K2) — in Python
# ---------------------------------------------------------------------------
#
# Newest first on (created_at, id), with an opaque cursor; never OFFSET,
# which skips or repeats rows while events arrive. The ordering and the
# cursor comparison are done HERE, not in SQL: created_at is TEXT on SQLite,
# and timestamps are compared in Python through licensing_time
# (backend/CLAUDE.md). SQL only narrows by equality. Each page therefore
# reads every matching row of the account — hundreds to low thousands for a
# school lab, since refreshes are logged only after a gap (spec §3.5). The
# same thing list_ambiguities() does.
#
# A page is the first `limit` rows whose key is STRICTLY below the cursor.
# Rows written after page 1 sort above the cursor and belong to a later,
# fresh traversal; a row that commits late with an older time sorts below
# it and appears once. No row can appear twice.
#
# The cursor is not signed: it only positions a read inside an account the
# caller is already allowed to see, so tampering with it widens nothing.

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _event_key(row: Mapping[str, Any]) -> tuple:
    return (licensing_time.to_datetime(row.get("created_at")) or _EPOCH, str(row.get("id") or ""))


def _encode_cursor(key: tuple) -> str:
    raw = json.dumps({"t": key[0].isoformat(), "id": key[1]}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def _decode_cursor(value: Any) -> Optional[tuple]:
    if value is None:
        return None

    def bad():
        licence_fail("invalid_request", "cursor is not one this server issued", field="cursor")

    if not isinstance(value, str) or not _CURSOR.fullmatch(value):
        bad()
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8"))
        when = licensing_time.to_datetime(data["t"]) if isinstance(data, dict) and isinstance(data.get("t"), str) \
            else None
    except (binascii.Error, ValueError, UnicodeDecodeError, KeyError, TypeError):
        bad()
    if when is None or not isinstance(data.get("id"), str) or not _HEX_ID.fullmatch(data["id"]):
        bad()
    return when, data["id"]


def _keyset_page(rows: List[Dict[str, Any]], limit: int, after: Optional[tuple]) -> tuple:
    """(the page, newest first, and next_cursor or None)."""
    ordered = sorted(rows, key=_event_key, reverse=True)
    if after is not None:
        ordered = [r for r in ordered if _event_key(r) < after]
    page = ordered[:limit]
    more = len(ordered) > limit
    return page, (_encode_cursor(_event_key(page[-1])) if more and page else None)


# ---------------------------------------------------------------------------
# Owner reads — contract §10A.1–§10A.3 (K2). Read-only: no history, no locks.
# ---------------------------------------------------------------------------

def list_owner_pools(caller: Mapping[str, Any], account_id: str) -> Dict[str, Any]:
    """GET /institutions/accounts/{account_id}/seat_pools (contract §10A.1)."""
    require_licensing_available()
    _owner_access(caller, account_id, "account_access_denied")
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        account = _row_to_dict(cur.fetchone())
        cur.execute("SELECT * FROM seat_pools WHERE account_id = ?", (account_id,))
        pools = [_row_to_dict(r) for r in cur.fetchall()]
        # Counted exactly as claim_capacity() counts (_count_active).
        cur.execute(
            "SELECT seat_pool_id, COUNT(*) AS active FROM institution_seats "
            "WHERE account_id = ? AND revoked_at IS NULL GROUP BY seat_pool_id",
            (account_id,),
        )
        counts = {r["seat_pool_id"]: int(r["active"]) for r in (_row_to_dict(x) for x in cur.fetchall())}
        cur.execute(
            "SELECT seat_pool_id, status, created_at FROM licence_ambiguities WHERE account_id = ? AND status = 'open'",
            (account_id,),
        )
        pending: Dict[str, int] = {}
        for row in (_row_to_dict(x) for x in cur.fetchall()):
            # The EFFECTIVE state (K0): open in the database but 7 days old is
            # expired, and no longer pending.
            if _ambiguity_state(row) == "open":
                pending[row["seat_pool_id"]] = pending.get(row["seat_pool_id"], 0) + 1
    finally:
        db.close()
    listed = []
    for pool in sorted(pools, key=lambda p: (str(p.get("label") or "").casefold(), p["id"])):
        size, active = int(pool["pool_size"]), counts.get(pool["id"], 0)
        listed.append({
            "pool_id": pool["id"],
            "label": pool.get("label"),
            "pool_size": size,
            "active": active,
            "available": max(0, size - active),
            "over_capacity": active > size,
            "pool_status": pool.get("status"),
            "pending_identity_checks": pending.get(pool["id"], 0),
        })
    return {
        "account_id": account_id,
        "account_name": account.get("name"),
        "subscription_status": account.get("subscription_status"),
        "subscription_expires_at": wire_time_or_none(account.get("subscription_expires_at")),
        "pools": listed,
        "server_time": wire_time(),
    }


def _pool_in_account(pool_id: str, account_id: str) -> None:
    """A pool_id filter must be a pool of THIS account; otherwise the same 403 as the account's (§10A.2–§10A.3)."""
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT account_id FROM seat_pools WHERE id = ?", (pool_id,))
        row = cur.fetchone()
    finally:
        db.close()
    if not row or _row_to_dict(row)["account_id"] != account_id:
        licence_fail("account_access_denied")


def list_owner_machines(caller: Mapping[str, Any], account_id: str, *, pool_id: Any = None) -> Dict[str, Any]:
    """GET /institutions/accounts/{account_id}/machines (contract §10A.2): active computers only."""
    pool_id = _query_id(pool_id, "pool_id")
    require_licensing_available()
    _owner_access(caller, account_id, "account_access_denied")
    if pool_id is not None:
        _pool_in_account(pool_id, account_id)
    sql = ("SELECT s.*, p.label AS pool_label FROM institution_seats s JOIN seat_pools p ON p.id = s.seat_pool_id "
           "WHERE s.account_id = ? AND s.revoked_at IS NULL")
    params: tuple = (account_id,)
    if pool_id is not None:
        sql += " AND s.seat_pool_id = ?"
        params += (pool_id,)
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    # pool_label, then machine_label, both case-insensitive, then binding_id
    # — labels may repeat, and the order must not change between calls (L5).
    rows.sort(key=lambda r: (str(r.get("pool_label") or "").casefold(),
                             str(r.get("machine_label") or "").casefold(), r["id"]))
    return {
        "account_id": account_id,
        "machines": [{
            "binding_id": r["id"],
            "pool_id": r["seat_pool_id"],
            "pool_label": r.get("pool_label"),
            "machine_label": r.get("machine_label"),
            "activated_at": wire_time_or_none(r.get("activated_at")),
            "last_seen_at": wire_time_or_none(r.get("last_seen_at")),
            "lease_expires_at": wire_time_or_none(r.get("lease_expires_at")),
            "fingerprint_confidence": r.get("fingerprint_confidence"),
        } for r in rows],
        "server_time": wire_time(),
    }


def _owner_event(row: Mapping[str, Any]) -> Dict[str, Any]:
    """
    One history event as the school sees it (contract §10A.3). Never the
    readings, hashes, installation IDs or detail_json; ExamPartner staff are
    never named. revoke_reason is read from the release row's detail_json,
    where Unit 3a stores it, and only that field leaves it.
    """
    action = row.get("action")
    actor = _ACTORS.get(row.get("actor_role"), "exampartner")
    revoke_reason = None
    if action == "release":
        try:
            revoke_reason = (json.loads(row.get("detail_json") or "{}") or {}).get("revoke_reason")
        except (ValueError, AttributeError):
            revoke_reason = None
    return {
        "event_id": row["id"],
        "at": wire_time(row.get("created_at")),
        "action": action,
        "binding_id": row.get("binding_id"),
        "pool_id": row.get("seat_pool_id"),
        "machine_label": row.get("machine_label"),
        "actor": actor,
        "actor_identifier": row.get("performed_by") if actor == "owner" else None,
        "revoke_reason": revoke_reason,
        "reason": row.get("reason") if action == "release" else None,
    }


def list_owner_history(caller: Mapping[str, Any], account_id: str, *, pool_id: Any = None, binding_id: Any = None,
                       limit: Any = None, cursor: Any = None) -> Dict[str, Any]:
    """GET /institutions/accounts/{account_id}/history (contract §10A.3), newest first."""
    pool_id = _query_id(pool_id, "pool_id")
    binding_id = _query_id(binding_id, "binding_id")
    page_size = _parse_limit(limit)
    after = _decode_cursor(cursor)
    require_licensing_available()
    _owner_access(caller, account_id, "account_access_denied")
    if pool_id is not None:
        _pool_in_account(pool_id, account_id)
    if binding_id is not None:
        db = db_conn()
        try:
            cur = db.cursor()
            cur.execute("SELECT account_id FROM institution_seats WHERE id = ?", (binding_id,))
            row = cur.fetchone()
        finally:
            db.close()
        if not row or _row_to_dict(row)["account_id"] != account_id:
            licence_fail("binding_access_denied")

    # The allowlist is the guarantee: clone_flagged (whose detail_json can
    # name ANOTHER school's binding, H7), stale_serial_flagged,
    # account_update, and any action added later never reach a school.
    marks = ", ".join("?" for _ in _OWNER_HISTORY_ACTIONS)
    sql = (f"SELECT id, seat_pool_id, binding_id, action, machine_label, performed_by, actor_role, reason, "
           f"detail_json, created_at FROM seat_activation_log WHERE account_id = ? AND action IN ({marks})")
    params: tuple = (account_id, *_OWNER_HISTORY_ACTIONS)
    if pool_id is not None:
        sql += " AND seat_pool_id = ?"
        params += (pool_id,)
    if binding_id is not None:
        sql += " AND binding_id = ?"
        params += (binding_id,)
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(sql, params)
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    page, next_cursor = _keyset_page(rows, page_size, after)
    return {"account_id": account_id, "events": [_owner_event(r) for r in page], "next_cursor": next_cursor,
            "server_time": wire_time()}


# ---------------------------------------------------------------------------
# Rename — PATCH /institutions/seats/{binding_id} (K3, contract §10A.4)
# ---------------------------------------------------------------------------

def _clean_label(value: Any) -> str:
    if not isinstance(value, str) or not (1 <= len(value.strip()) <= _LABEL_MAX):
        licence_fail("invalid_request", f"machine_label must be 1–{_LABEL_MAX} characters", field="machine_label")
    return value.strip()


def rename_seat(caller: Mapping[str, Any], binding_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    The route has already refused 401 not_authenticated. Then: 400
    invalid_request → 503 licensing_unavailable → 403 binding_access_denied
    → the request history → the effect, as release does (I7).

    Renaming changes only the label, never installation_id, so the §4.E live
    index check is not needed. Duplicate labels are allowed.
    """
    request_id = _canonical_request_id(body.get("request_id"))
    label = _clean_label(body.get("machine_label"))
    require_licensing_available()

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        licence_fail("binding_access_denied")
    seat = _row_to_dict(row)
    caller_id, actor_role = _owner_access(caller, seat["account_id"], "binding_access_denied")

    history = licence_history.Entry(
        request_id, "rename", licence_history.digest("rename", binding_id=binding_id, machine_label=label),
        account_id=seat["account_id"], seat_pool_id=seat["seat_pool_id"], binding_id=binding_id,
    )
    owner = _early_lookup(history)
    if owner is not None:
        return licence_history.replay(owner)
    return _rename_locked(binding_id, label=label, caller=caller_id, actor_role=actor_role, history=history)


def _rename_locked(binding_id: str, *, label: str, caller: str, actor_role: str,
                   history: "licence_history.Entry") -> Dict[str, Any]:
    """
    The effect, in one transaction locking the BINDING row only (H3), the
    same lock release takes — so a rename and a release of one computer
    serialise: either renamed then released, or binding_released, never a
    released computer renamed. Every outcome, the binding_released refusal
    included, is recorded and committed while the lock is held (J8).
    binding_released is stored because it is permanent: a released binding
    never becomes active again, so storing it and recomputing it agree.
    """
    replay_owner, response, refusal = None, None, None
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()
        row = _lock_binding(cur, binding_id, stamp)
        owner = licence_history.lookup_locked(cur, history)        # the last read before the write
        if owner is not None:
            replay_owner = owner
        else:
            if row is None:
                licence_fail("binding_access_denied")
            if row.get("revoked_at") is not None:
                refusal = licence_detail("binding_released")
                licence_history.record(cur, history, outcome="refused", status_code=refusal[0],
                                       response=refusal[1], binding_id=binding_id)
            else:
                previous = row.get("machine_label")
                result = "unchanged" if previous == label else "renamed"
                if result == "renamed":
                    cur.execute("UPDATE institution_seats SET machine_label = ?, updated_at = ? WHERE id = ?",
                                (label, stamp, binding_id))
                    _log(cur, account_id=row["account_id"], seat_pool_id=row["seat_pool_id"], binding_id=binding_id,
                         action="rename", installation_id=row.get("installation_id"),
                         fingerprint_hash=row.get("machine_fingerprint_hash"), machine_label=label,
                         signals_json=row.get("fingerprint_signals_json"), performed_by=caller,
                         actor_role=actor_role,
                         detail={"from": previous, "to": label, "request_id": history.request_id}, stamp=stamp)
                response = {"result": result, "binding_id": binding_id, "machine_label": label}
                licence_history.record(cur, history, outcome=result, response=response, binding_id=binding_id)
            db.commit()
    finally:
        db.close()
    if replay_owner is not None:
        return licence_history.replay(replay_owner)
    if refusal is not None:
        raise_stored(*refusal)
    return {**response, "server_time": wire_time()}


# ---------------------------------------------------------------------------
# Account editing — PATCH /institutions/accounts/{account_id} (K5)
# ---------------------------------------------------------------------------

_ACCOUNT_FIELDS = ("name", "contact_email", "owner_identifier")


def _edit_text(body: Mapping[str, Any], name: str, max_len: int) -> Optional[str]:
    """An optional text field of an edit: absent or null leaves it as it is (contract §2), never clears it."""
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        licence_fail("invalid_request", f"{name} must be a non-empty string", field=name)
    if len(value.strip()) > max_len:
        licence_fail("invalid_request", f"{name} is at most {max_len} characters", field=name)
    return value.strip()


def _account_body(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {"id": row["id"], "name": row.get("name"), "account_type": row.get("account_type"),
            "contact_email": row.get("contact_email"), "owner_identifier": row.get("owner_identifier"),
            "subscription_status": row.get("subscription_status"),
            "subscription_expires_at": wire_time_or_none(row.get("subscription_expires_at"))}


def update_account(admin: str, account_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    ExamPartner admin only; the route has refused 401 and 403. Then: 400
    invalid_request — including an owner_identifier that is not a registered
    user, checked before the lock and NOT stored (K5) → 503 → 404
    account_not_found → the request history → the effect.
    """
    request_id = _canonical_request_id(body.get("request_id"))
    fields = {"name": _edit_text(body, "name", 200), "contact_email": _edit_text(body, "contact_email", 200),
              "owner_identifier": _edit_text(body, "owner_identifier", 200)}
    if fields["owner_identifier"] is not None:
        fields["owner_identifier"] = fields["owner_identifier"].lower()       # as F11 and /auth/login do
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        licence_fail("invalid_request", "Give at least one of name, contact_email, owner_identifier", field="body")
    db = db_conn()
    try:
        cur = db.cursor()
        if "owner_identifier" in fields:
            # The owner signs in to the licence session with their ordinary
            # ExamPartner password, so they must already be a registered user.
            cur.execute("SELECT identifier FROM users WHERE identifier = ?", (fields["owner_identifier"],))
            if not cur.fetchone():
                licence_fail("invalid_request", "owner_identifier is not a registered user", field="owner_identifier")
    finally:
        db.close()
    require_licensing_available()
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM accounts WHERE id = ?", (account_id,))
        found = cur.fetchone()
    finally:
        db.close()
    if not found:
        licence_fail("account_not_found")

    history = licence_history.Entry(
        request_id, "account_update",
        licence_history.digest("account_update", account_id=account_id, **{k: fields.get(k) for k in _ACCOUNT_FIELDS}),
        account_id=account_id,
    )
    owner = _early_lookup(history)
    if owner is not None:
        return licence_history.replay(owner)
    return _update_account_locked(account_id, fields=fields, admin=admin, history=history)


def _update_account_locked(account_id: str, *, fields: Mapping[str, str], admin: str,
                           history: "licence_history.Entry") -> Dict[str, Any]:
    """
    One transaction, locking the ACCOUNT row and nothing else (K5, L5). The
    account lock sits outside pool → ambiguity → binding and is never held
    with another lock, so it cannot join a deadlock cycle; a future path that
    needs it with another takes it FIRST (account → pool → …).

    Every outcome is recorded while the lock is held (J8). There is no stored
    refusal: everything that can refuse runs before the lock, or is
    request_id_reused, which is decided afresh. A replay found under the
    lock closes without committing, so its lock write leaves nothing.
    """
    replay_owner, response = None, None
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()
        # LOAD-BEARING — DO NOT REMOVE, REORDER OR "OPTIMISE AWAY". This looks
        # like a bookkeeping write. It is the ROW LOCK that serialises two
        # edits of one account: without it two edits each read the same
        # "before", both log it as their `from`, and two retries of one
        # request_id with different bodies both apply
        # (test_licensing_unit3b_pg.py). SQLite serialises writers anyway,
        # which is why only the Postgres tests can catch its removal.
        cur.execute("UPDATE accounts SET updated_at = ? WHERE id = ?", (stamp, account_id))
        cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
        row = _row_to_dict(row) if row else None
        owner = licence_history.lookup_locked(cur, history)        # the last read before the write
        if owner is not None:
            replay_owner = owner
        else:
            if row is None:
                licence_fail("account_not_found")
            changed = {k: v for k, v in fields.items() if row.get(k) != v}
            if changed:
                cur.execute(
                    f"UPDATE accounts SET {', '.join(f'{k} = ?' for k in changed)}, updated_at = ? WHERE id = ?",
                    (*changed.values(), stamp, account_id),
                )
                _log(cur, account_id=account_id, seat_pool_id=None, binding_id=None, action="account_update",
                     installation_id=None, fingerprint_hash=None, machine_label=None, signals_json=None,
                     performed_by=admin, actor_role="exampartner_admin",
                     detail={"from": {k: row.get(k) for k in changed}, "to": dict(changed),
                             "request_id": history.request_id},
                     stamp=stamp)
                row.update(changed)
            response = {"ok": True, "account": _account_body(row)}
            licence_history.record(cur, history, outcome="updated" if changed else "unchanged", response=response)
            db.commit()
    finally:
        db.close()
    if replay_owner is not None:
        return licence_history.replay(replay_owner)
    return {**response, "server_time": wire_time()}


# ---------------------------------------------------------------------------
# Pool editing — PATCH /institutions/seat_pools/{pool_id} (K6, L4)
# ---------------------------------------------------------------------------

_POOL_STATUSES = ("active", "suspended")


def update_pool(admin: str, pool_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    ExamPartner admin only; the route has refused 401 and 403. Then: 400
    invalid_request → 503 → 404 pool_not_found → the request history → the
    effect. Never evicts (spec §5.4): a shrink below the active count leaves
    the pool over capacity, and claims are refused until releases bring it
    below.
    """
    request_id = _canonical_request_id(body.get("request_id"))
    fields: Dict[str, Any] = {}
    size = body.get("pool_size")
    if size is not None:
        if not isinstance(size, int) or isinstance(size, bool) or not (1 <= size <= _POOL_SIZE_MAX):
            licence_fail("invalid_request", f"pool_size must be an integer from 1 to {_POOL_SIZE_MAX}",
                         field="pool_size")
        fields["pool_size"] = size
    label = body.get("label")
    if label is not None:
        if not isinstance(label, str) or not (1 <= len(label.strip()) <= _LABEL_MAX):
            licence_fail("invalid_request", f"label must be 1–{_LABEL_MAX} characters", field="label")
        fields["label"] = label.strip()
    status = body.get("status")
    if status is not None:
        if status not in _POOL_STATUSES:
            licence_fail("invalid_request", "status must be active or suspended", field="status")
        fields["status"] = status
    if not fields:
        licence_fail("invalid_request", "Give at least one of pool_size, label, status", field="body")
    require_licensing_available()
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT account_id FROM seat_pools WHERE id = ?", (pool_id,))
        found = cur.fetchone()
    finally:
        db.close()
    if not found:
        licence_fail("pool_not_found")
    account_id = _row_to_dict(found)["account_id"]

    history = licence_history.Entry(
        request_id, "pool_update",
        licence_history.digest("pool_update", pool_id=pool_id, pool_size=fields.get("pool_size"),
                               label=fields.get("label"), status=fields.get("status")),
        account_id=account_id, seat_pool_id=pool_id,
    )
    owner = _early_lookup(history)
    if owner is not None:
        return licence_history.replay(owner)
    return _update_pool_locked(pool_id, fields=fields, admin=admin, history=history)


def _update_pool_locked(pool_id: str, *, fields: Mapping[str, Any], admin: str,
                        history: "licence_history.Entry") -> Dict[str, Any]:
    """
    One transaction, locking the POOL row and nothing else (H3: pool first).
    Recorded under the lock (J8); like account editing, no stored refusal.

    A status change logs `pool_status`; a size or label change logs `resize`;
    a request that changes both writes both rows, in this one transaction
    (L4). The counts are taken after the write, in this transaction (K0).
    """
    replay_owner, response = None, None
    db = db_conn()
    try:
        cur = db.cursor()
        stamp = licensing_time.now_iso()
        # LOAD-BEARING — DO NOT REMOVE, REORDER OR "OPTIMISE AWAY". This looks
        # like a bookkeeping write. It takes the same pool-row lock as
        # claim_capacity()'s last_claim_at write, so a resize and a claim
        # serialise (H3: pool first), and two edits of one pool serialise.
        # Note what it does NOT carry: "no claim beyond the size in force"
        # rests on claim_capacity() reading pool_size under ITS lock (L3). What
        # this lock guards is the edit itself: without it two edits each read
        # the same "before" and log it as their `from`, and two retries of one
        # request_id with different bodies both apply
        # (test_licensing_unit3b_pg.py). updated_at is bookkeeping, so the
        # write leaves no business trace — last_claim_at is not touched.
        cur.execute("UPDATE seat_pools SET updated_at = ? WHERE id = ?", (stamp, pool_id))
        cur.execute("SELECT * FROM seat_pools WHERE id = ?", (pool_id,))
        row = cur.fetchone()
        row = _row_to_dict(row) if row else None
        owner = licence_history.lookup_locked(cur, history)        # the last read before the write
        if owner is not None:
            replay_owner = owner
        else:
            if row is None:
                licence_fail("pool_not_found")
            changed = {k: v for k, v in fields.items() if row.get(k) != v}
            if changed:
                cur.execute(
                    f"UPDATE seat_pools SET {', '.join(f'{k} = ?' for k in changed)}, updated_at = ? WHERE id = ?",
                    (*changed.values(), stamp, pool_id),
                )
                resized = {k: changed[k] for k in ("pool_size", "label") if k in changed}
                common = dict(account_id=row["account_id"], seat_pool_id=pool_id, binding_id=None,
                              installation_id=None, fingerprint_hash=None, machine_label=None, signals_json=None,
                              performed_by=admin, actor_role="exampartner_admin", stamp=stamp)
                if resized:
                    _log(cur, action="resize",
                         detail={"from": {k: row.get(k) for k in resized}, "to": resized,
                                 "request_id": history.request_id}, **common)
                if "status" in changed:
                    _log(cur, action="pool_status",
                         detail={"from": row.get("status"), "to": changed["status"],
                                 "request_id": history.request_id}, **common)
                row.update(changed)
            size = int(row["pool_size"])
            active = _count_active(cur, pool_id)
            response = {"ok": True, "pool_id": pool_id, "pool_size": size, "label": row.get("label"),
                        "pool_status": row.get("status"), "active": active, "available": max(0, size - active),
                        "over_capacity": active > size}
            licence_history.record(cur, history, outcome="updated" if changed else "unchanged", response=response)
            db.commit()
    finally:
        db.close()
    if replay_owner is not None:
        return licence_history.replay(replay_owner)
    return {**response, "server_time": wire_time()}


# ---------------------------------------------------------------------------
# Signals review — GET /support/licensing/signals (K8)
# ---------------------------------------------------------------------------

def _parsed(text: Any) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def list_signals(*, action: Any = None, account_id: Any = None, since: Any = None, limit: Any = None,
                 cursor: Any = None) -> Dict[str, Any]:
    """
    Where clones, stale serials and refresh denials become decisions for a
    human (spec §10.4). Admin only — it shows every school, and H7's matched
    binding from another school, which is exactly what support needs.
    Every log column, with detail_json and fingerprint_signals_json parsed
    (L5). Same keyset pager as the owner history.
    """
    if action is not None and action not in _SIGNAL_ACTIONS:
        licence_fail("invalid_request", f"action must be one of {', '.join(_SIGNAL_ACTIONS)}", field="action")
    account_id = _query_id(account_id, "account_id")
    since_at = None
    if since is not None:
        try:
            since_at = licensing_time.to_datetime(since) if isinstance(since, str) else None
        except ValueError:
            since_at = None
        if since_at is None:
            licence_fail("invalid_request", "since must be an RFC 3339 timestamp", field="since")
    page_size = _parse_limit(limit)
    after = _decode_cursor(cursor)
    require_licensing_available()

    actions = _SIGNAL_ACTIONS if action is None else (action,)
    marks = ", ".join("?" for _ in actions)
    sql = (f"SELECT l.*, a.name AS account_name, p.label AS pool_label FROM seat_activation_log l "
           f"LEFT JOIN accounts a ON a.id = l.account_id LEFT JOIN seat_pools p ON p.id = l.seat_pool_id "
           f"WHERE l.action IN ({marks})")
    params: tuple = tuple(actions)
    db = db_conn()
    try:
        cur = db.cursor()
        if account_id is not None:
            cur.execute("SELECT id FROM accounts WHERE id = ?", (account_id,))
            if not cur.fetchone():
                licence_fail("account_not_found")
            sql += " AND l.account_id = ?"
            params += (account_id,)
        cur.execute(sql, params)
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        db.close()
    if since_at is not None:
        rows = [r for r in rows if (licensing_time.to_datetime(r.get("created_at")) or _EPOCH) >= since_at]
    page, next_cursor = _keyset_page(rows, page_size, after)
    events = [{
        "event_id": r["id"],
        "at": wire_time_or_none(r.get("created_at")),
        "action": r.get("action"),
        "account_id": r.get("account_id"),
        "account_name": r.get("account_name"),
        "seat_pool_id": r.get("seat_pool_id"),
        "pool_label": r.get("pool_label"),
        "binding_id": r.get("binding_id"),
        "installation_id": r.get("installation_id"),
        "machine_fingerprint_hash": r.get("machine_fingerprint_hash"),
        "machine_label": r.get("machine_label"),
        "fingerprint_signals": _parsed(r.get("fingerprint_signals_json")),
        "performed_by": r.get("performed_by"),
        "actor_role": r.get("actor_role"),
        "reason": r.get("reason"),
        "detail": _parsed(r.get("detail_json")),
    } for r in page]
    return {"ok": True, "events": events, "next_cursor": next_cursor, "server_time": wire_time()}


# ---------------------------------------------------------------------------
# The test clock — POST /support/licensing/test-clock (K9, spec §6.3)
# ---------------------------------------------------------------------------

def set_test_clock(body: Mapping[str, Any]) -> Dict[str, Any]:
    """
    The route has refused 401, admin_required and test_clock_disabled. The
    offset lives in PROCESS MEMORY (licensing_time), so it suits a single-
    process development server and nothing else; ALLOW_TEST_CLOCK stays
    absent on Render. It takes no request_id (K7) and does not check
    licensing availability (L5): it touches neither the database nor a key.
    """
    offset, reset = body.get("offset_seconds"), body.get("reset")
    if reset is not None:
        if reset is not True or offset is not None:
            licence_fail("invalid_request", 'Send {"reset": true} or {"offset_seconds": <integer>}', field="reset")
        seconds = 0
    else:
        if not isinstance(offset, int) or isinstance(offset, bool) \
                or abs(offset) > _TEST_CLOCK_MAX.total_seconds():
            licence_fail("invalid_request", "offset_seconds must be an integer within ±10 years",
                         field="offset_seconds")
        seconds = offset
    licensing_time.set_clock_offset(timedelta(seconds=seconds))
    return {"ok": True, "offset_seconds": seconds, "server_time": wire_time()}
