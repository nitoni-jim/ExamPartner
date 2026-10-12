"""
services/licensing_service.py — Windows seat-pool licensing (Pilot V1).

Unit 1 built claim_capacity(), the single place capacity is granted. Unit 2
adds activation on top of it: the activation session, the decision between
restore, ambiguity and a new seat (brief §4.F F3), restore itself, lease
issue, and create_or_get_ambiguity(). Release, refresh and ambiguity
resolution are Unit 3. See
docs/ExamPartner_Windows_SeatPool_PilotV1_Implementation_Spec.md §5 and §8,
docs/LICENCE_API_CONTRACT.md, and docs/CLOUD_SESSION_BRIEF.md §4.F–§4.H.

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
from services import fingerprint_service, licence_lease, licence_session, licensing_time
from services.access_control import is_admin_user, require_account_owner
from services.licence_wire import licence_fail, truncate_seconds, wire_time, wire_time_or_none

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
    fingerprint_signals_json: Optional[str] = None,
    machine_label: Optional[str] = None,
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
    Every refusal writes nothing.

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

        # (2b) The same activation already granted? (brief F4.) Under the
        # lock, so a concurrent retry of this request blocks above and then
        # sees the first one's committed binding here, instead of inserting
        # a second. Before entitlement and capacity on purpose: when the
        # first retry took the pool's last seat, the second must find that
        # binding, not be told the pool is full.
        existing = _active_binding_for_request(cur, request_id, pool_id=pool_id)
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
            licence_fail("pool_inactive")
        cur.execute(
            "SELECT subscription_status, subscription_expires_at FROM accounts WHERE id = ?",
            (account_id,),
        )
        account = cur.fetchone()
        account = _row_to_dict(account) if account else {}
        if account.get("subscription_status") != "active" or licensing_time.is_expired(
            account.get("subscription_expires_at")
        ):
            licence_fail(
                "subscription_inactive",
                subscription_expires_at=wire_time_or_none(account.get("subscription_expires_at")),
            )

        # (4) Under the lock, the count cannot change underneath us.
        active = _count_active(cur, pool_id)
        # Slot-agnostic: which machines are active is irrelevant, only how
        # many. Over-capacity after a shrink (active > pool_size) is refused
        # by the same comparison; no machine is ever evicted here.
        if active >= pool_size:
            licence_fail("pool_full", pool_size=pool_size, active=active)

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
    }


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
# Lock discipline (brief §4.H H3). Two row locks exist in this file:
#   - the POOL row: claim_capacity() (last_claim_at) and
#     create_or_get_ambiguity() (last_ambiguity_at);
#   - a BINDING row: _restore() and _ensure_lease().
# No transaction here holds both. If a future path ever needs both — Unit 3's
# refresh that turns into an ambiguity is the first candidate — it takes the
# POOL lock first. Taking them in opposite orders in two transactions can
# deadlock.

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
        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
        row = cur.fetchone()
        if not row or _row_to_dict(row).get("revoked_at") is not None:
            licence_fail("internal_error", "The activation changed while it was being issued. Try again.")
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
        db.commit()
        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
        binding = _row_to_dict(cur.fetchone())
    finally:
        db.close()
    if not binding.get("current_lease") or binding.get("revoked_at") is not None:
        licence_fail("internal_error", "The activation changed while it was being issued. Try again.")
    return binding


def _activate_response(binding: Mapping[str, Any], account_name: Any, pool_label: Any) -> Dict[str, Any]:
    return {
        "result": binding.get("activation_result") or "activated",
        "lease": binding["current_lease"],
        "binding_id": binding["id"],
        "installation_id": binding["installation_id"],
        "lease_expires_at": wire_time(binding["lease_expires_at"]),
        "machine_label": binding.get("machine_label"),
        "account_name": account_name,
        "pool_label": pool_label,
        "server_time": wire_time(),
    }


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
) -> str:
    """
    The ONLY way a licence_ambiguities row is created — activation now, and
    Unit 3's refresh-raised ambiguities too. Returns the ambiguity_id.

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

        # (2) Under the lock: the same request already raised one?
        cur.execute(
            "SELECT id, request_fp, created_at FROM licence_ambiguities "
            "WHERE seat_pool_id = ? AND request_id = ? AND origin = ?",
            (pool_id, request_id, origin),
        )
        now = licensing_time.now()
        for row in (_row_to_dict(r) for r in cur.fetchall()):
            if not _within_window(row.get("created_at"), REPLAY_WINDOW, now):
                continue
            if row.get("request_fp") != request_fp:
                licence_fail("request_id_reused")
            return row["id"]

        # (3) Exactly one row, and its audit row, in the same transaction.
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
        db.commit()
        return ambiguity_id
    finally:
        db.close()


def _ambiguity_outcome(row: Mapping[str, Any]) -> None:
    """Raise the state of an ambiguity this request owns (F3). Unit 2 resolves nothing."""
    status = row.get("status")
    if status == "consumed":
        licence_fail("ambiguity_consumed")
    if status == "expired" or (status == "open" and not _within_window(row.get("created_at"), AMBIGUITY_TTL)):
        licence_fail("ambiguity_expired")
    licence_fail("ambiguity_pending", ambiguity_id=row["id"])


def _collect_ambiguity(ambiguity_id: str, pool_id: str, request_id: str, request_fp: str) -> None:
    """
    A request carrying ambiguity_id (contract §10). Resolving is Unit 3, so
    here the outcome is only ever the row's state — pending or expired —
    and ONLY for the PC that raised it. Checked in this order:

    1. Not an activation-raised ambiguity of THIS pool → ambiguity_mismatch,
       the same answer whether it belongs to another pool or does not exist.
    2. The request reuses the ambiguity's ORIGINAL raising request_id →
       request_id_reused. Contract §10 requires collection under a NEW
       request_id; resending the raising one is a client bug, and must not
       be mistaken for a replay of the raising request.
    3. The current request's digest differs from the one stored when the
       ambiguity was raised → ambiguity_mismatch (contract §9.3: the PC
       presenting the id is not the one that raised it). request_fp covers
       the pool and the normalised reading, so this is the identity check.
       The stored digest is compared, never recomputed (H5).

    Identity is settled before the state is revealed, so a different PC
    holding someone else's ambiguity_id learns nothing about it.
    """
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT * FROM licence_ambiguities WHERE id = ?", (ambiguity_id,))
        row = cur.fetchone()
    finally:
        db.close()
    row = _row_to_dict(row) if row else None
    if not row or row.get("seat_pool_id") != pool_id or row.get("origin") != "activate":
        licence_fail("ambiguity_mismatch")
    if row.get("request_id") == request_id:
        licence_fail("request_id_reused")
    if row.get("request_fp") != request_fp:
        licence_fail("ambiguity_mismatch")
    _ambiguity_outcome(row)


# ---------------------------------------------------------------------------
# Restore (brief F10)
# ---------------------------------------------------------------------------

def _restore(binding_id: str, *, reading: "fingerprint_service.Reading", signals_json: str,
             request_id: str, request_fp: str, machine_label: str,
             performed_by: str, actor_role: str) -> Dict[str, Any]:
    """
    Recognised reinstall (case D): the same activation, a new installation,
    no capacity. One transaction, locking the BINDING row only (H3).
    Returns the binding row as committed.
    """
    db = db_conn()
    try:
        cur = db.cursor()
        now = licensing_time.now()
        stamp = now.isoformat()

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
        if not binding or binding.get("revoked_at") is not None:
            # Released between the decision and here. Retryable: the next
            # attempt will not see it as a candidate (release beats recognition).
            licence_fail("internal_error", "This computer's activation changed during the request. Try again.")

        if binding.get("activation_request_id") == request_id and _within_window(
            binding.get("activation_request_at"), REPLAY_WINDOW, now
        ):
            if binding.get("activation_request_fp") != request_fp:
                licence_fail("request_id_reused")
            return binding  # already applied; the close discards the lock write

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
        _log(cur, account_id=binding["account_id"], seat_pool_id=binding["seat_pool_id"], binding_id=binding_id,
             action="restore", installation_id=installation_id, fingerprint_hash=new_hash,
             machine_label=label, signals_json=signals_json, performed_by=performed_by, actor_role=actor_role,
             detail={"previous_installation_id": previous["installation_id"],
                     "previous_fingerprint_hash": previous["machine_fingerprint_hash"],
                     "lease_serial": issued["serial"]},
             stamp=stamp)
        db.commit()

        cur.execute("SELECT * FROM institution_seats WHERE id = ?", (binding_id,))
        return _row_to_dict(cur.fetchone())
    finally:
        db.close()


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

    # Collecting an ambiguity (contract §10). After the digest exists,
    # because the collection must prove it comes from the PC that raised it.
    if ambiguity_id is not None:
        _collect_ambiguity(ambiguity_id, pool_id, request_id, request_fp)

    # F3 step 1 — replay. Looked up across ALL pools, so a request_id reused
    # sequentially in a different pool is refused (H4): the digest includes
    # the pool id, so it can never match there.
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
        binding = _restore(strong[0], reading=reading, signals_json=signals_json,
                           request_id=request_id, request_fp=request_fp, machine_label=label,
                           performed_by=caller, actor_role=actor_role)
        if not binding.get("current_lease"):
            binding = _ensure_lease(binding["id"])
        return _activate_response(binding, pool.get("account_name"), pool.get("label"))
    if len(strong) > 1:                                    # step 3 — more than one
        candidates = strong
    elif weak:                                             # step 4
        candidates = weak
    elif quality in ("weak", "degenerate"):                # step 5 (F2, H1)
        candidates = sorted(bid for bid, r in readings.items()
                            if fingerprint_service.activation_continuity_match(reading, r))

    if candidates:
        raised_id = create_or_get_ambiguity(
            pool_id, origin="activate", request_id=request_id, request_fp=request_fp,
            presented_signals_json=signals_json, presented_fingerprint_hash=fingerprint_hash,
            presented_installation_id=presented_installation_id, machine_label=label,
            candidate_binding_ids=candidates, performed_by=caller, actor_role=actor_role,
        )
        licence_fail("ambiguity_pending", ambiguity_id=raised_id)

    # Step 6 — a new machine: case G, or pool_full (case H) from inside the claim.
    claimed = claim_capacity(
        pool_id,
        machine_fingerprint_hash=fingerprint_hash,
        fingerprint_confidence=quality,
        performed_by=caller,
        actor_role=actor_role,
        request_id=request_id,
        request_fp=request_fp,
        fingerprint_signals_json=signals_json,
        machine_label=label,
    )
    binding = _ensure_lease(claimed["binding_id"])
    return _activate_response(binding, pool.get("account_name"), pool.get("label"))


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
