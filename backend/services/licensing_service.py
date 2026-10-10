"""
services/licensing_service.py — Windows seat-pool licensing (Pilot V1).

This unit holds only claim_capacity(), the single place capacity is granted.
Activation, restore, release and refresh build on it in later units; see
docs/ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec.md §5 and §8.

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
import json
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException

from config import db_conn
from db import licensing_index_present
from services import licensing_time

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
    fingerprint_signals_json: Optional[str] = None,
    machine_label: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Grant one unit of capacity in `pool_id` by creating an active binding,
    and write its `activate` row to seat_activation_log in the same
    transaction — a binding never exists without its audit row.

    Refuses with, in this order:
      503  licensing unavailable: ux_seat_bindings_active_installation missing
           (checked twice — before the lock and inside the granting
           transaction; see (0) and (2) below)
      404  pool not found
      409  pool status is not 'active'
      402  account subscription not 'active', or expired
      409  no capacity available (pool at or over pool_size) — never evicts
      500  capacity invariant violated after insert (nothing is committed)
    Every refusal writes nothing.

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
    """
    if not machine_fingerprint_hash or not str(machine_fingerprint_hash).strip():
        raise HTTPException(status_code=400, detail="machine_fingerprint_hash is required")
    if fingerprint_confidence not in _FINGERPRINT_CONFIDENCE:
        raise HTTPException(status_code=400, detail="Invalid fingerprint_confidence")
    if not performed_by or not str(performed_by).strip():
        raise HTTPException(status_code=400, detail="performed_by is required")
    if actor_role not in _CLAIM_ACTOR_ROLES:
        raise HTTPException(status_code=400, detail="Invalid actor_role for a capacity claim")
    if fingerprint_signals_json is not None and not isinstance(fingerprint_signals_json, str):
        raise HTTPException(status_code=400, detail="fingerprint_signals_json must be a JSON string")

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
            raise HTTPException(status_code=503, detail="Licensing is unavailable")

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
            raise HTTPException(status_code=503, detail="Licensing is unavailable")

        cur.execute(
            "SELECT account_id, pool_size, status FROM seat_pools WHERE id = ?",
            (pool_id,),
        )
        pool = cur.fetchone()
        if not pool:
            raise HTTPException(status_code=404, detail="Seat pool not found")
        pool = _row_to_dict(pool)
        pool_size = int(pool["pool_size"])
        account_id = pool["account_id"]

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
            raise HTTPException(status_code=409, detail="Seat pool is not active")
        cur.execute(
            "SELECT subscription_status, subscription_expires_at FROM accounts WHERE id = ?",
            (account_id,),
        )
        account = cur.fetchone()
        account = _row_to_dict(account) if account else {}
        if account.get("subscription_status") != "active" or licensing_time.is_expired(
            account.get("subscription_expires_at")
        ):
            raise HTTPException(status_code=402, detail="Subscription is not active")

        # (4) Under the lock, the count cannot change underneath us.
        active = _count_active(cur, pool_id)
        # Slot-agnostic: which machines are active is irrelevant, only how
        # many. Over-capacity after a shrink (active > pool_size) is refused
        # by the same comparison; no machine is ever evicted here.
        if active >= pool_size:
            raise HTTPException(status_code=409, detail="No capacity available in this pool")

        # (5) Insert the binding and its audit row.
        binding_id = secrets.token_hex(16)
        installation_id = secrets.token_hex(16)
        label = machine_label.strip() if isinstance(machine_label, str) and machine_label.strip() else None
        cur.execute(
            "INSERT INTO institution_seats (id, seat_pool_id, account_id, "
            "machine_fingerprint_hash, fingerprint_signals_json, fingerprint_confidence, "
            "installation_id, machine_label, activated_at, lease_serial, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (binding_id, pool_id, account_id,
             machine_fingerprint_hash, fingerprint_signals_json, fingerprint_confidence,
             installation_id, label, stamp, 0,
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
             json.dumps({"fingerprint_confidence": fingerprint_confidence}), stamp),
        )

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
            raise HTTPException(status_code=500, detail="Capacity invariant violated")

        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "binding_id": binding_id,
        "installation_id": installation_id,
        "account_id": account_id,
        "seat_pool_id": pool_id,
        "active_count": recount,
        "pool_size": pool_size,
    }


def _count_active(cur, pool_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS active FROM institution_seats "
        "WHERE seat_pool_id = ? AND revoked_at IS NULL",
        (pool_id,),
    )
    return int(_row_to_dict(cur.fetchone())["active"])
