"""
services/licence_history.py — the one authoritative 24-hour request history
(brief §9 R1, §4.I I4, §4.J; contract §9.2, §11).

Every state-changing licensing request — activation, restore, refresh,
ambiguity collection on either endpoint, release and ambiguity resolution,
and since Unit 3b rename, account editing and pool editing (brief §4.K K7) —
records itself in licence_requests. A request_id belongs to the ONE logical
request that first used it: the same operation with the same digest within
24 hours is a replay and gets the stored outcome back; anything else is
409 request_id_reused, whichever endpoint the earlier use was on.

How every operation uses it (the pre-code report, approved in §4.J):

1. lookup() EARLY, before any write and before any lock: a fast replay, and
   — because the history fails closed — a missing history refuses the
   request before anything at all is written.
2. lookup_locked() AGAIN under the operation's own lock (pool, ambiguity or
   binding row), as the LAST check before the write. Two concurrent retries
   of one request on one target serialise on that lock, and the second sees
   the first's row. There is no global lock (H4): one request_id sent to two
   different targets at the same instant is not guarded, and cannot exceed
   any pool's entitlement.
3. record() in the SAME transaction as the effect, committed while that lock
   is still held (J8). That includes every refusal stored for replay: never
   record an outcome after the lock that decided it has been released, or a
   concurrent retry can decide the same request a second, different way.

Fails CLOSED (I4, contract §11). Every statement here goes through
_execute(), which turns any database error into 503 licensing_unavailable.
The caller's try/finally then closes its connection without committing —
the only rollback _PGConn has (backend/CLAUDE.md), and what clears an
aborted Postgres transaction — so nothing the request did is kept. There is
NO fallback to the per-binding request columns: they are written and read
alongside the history, never instead of it.

Digests are computed once and stored, never recomputed (H5).
"""
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Mapping, NoReturn, Optional

from services import licensing_time
from services.licence_wire import licence_fail, raise_stored, wire_time

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)        # contract §9.2

OPERATIONS = ("activate", "refresh", "release", "resolve",
              "rename", "account_update", "pool_update")          # Unit 3b (K3, K5, K6)


@dataclass(frozen=True)
class Entry:
    """
    What one request is, for the history: its request_id, its operation and
    its digest. A digest covers the request's target (pool, binding or
    ambiguity), so "a different pool, binding or body" is simply a
    different digest. The ids are recorded for support and for the replay.
    """
    request_id: str
    operation: str
    request_fp: str
    account_id: Optional[str] = None
    seat_pool_id: Optional[str] = None
    binding_id: Optional[str] = None
    ambiguity_id: Optional[str] = None


def digest(operation: str, **parts: Any) -> str:
    """The history digest of one request: SHA-256 over its operation and the parts that identify it."""
    canonical = {"operation": operation, **{k: ("" if v is None else v) for k, v in parts.items()}}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def _is_database_error(exc: Exception) -> bool:
    import sqlite3

    if isinstance(exc, sqlite3.Error):
        return True
    try:
        import psycopg2

        if isinstance(exc, psycopg2.Error):
            return True
    except ImportError:
        pass
    return False


def unavailable(exc: Exception) -> NoReturn:
    logger.error(
        "LICENSING UNAVAILABLE for this request — the request history could not be read or "
        "written (%s: %s). Refused with 503; nothing was written. Other routes are unaffected.",
        type(exc).__name__, exc,
    )
    licence_fail("licensing_unavailable")


def _execute(cur, sql: str, params: tuple = (), fetch: bool = False) -> Optional[List[Any]]:
    try:
        cur.execute(sql, params)
        return cur.fetchall() if fetch else None
    except Exception as exc:
        if _is_database_error(exc):
            unavailable(exc)
        raise


def _row_to_dict(row) -> Dict[str, Any]:
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


# ---------------------------------------------------------------------------
# Lookup, record, replay
# ---------------------------------------------------------------------------

def lookup(cur, entry: Entry) -> Optional[Dict[str, Any]]:
    """
    The row that owns entry.request_id within 24 hours, if it is this same
    logical request — or None if the request_id is new. Raises 409
    request_id_reused if it belongs to a different one (another operation,
    target or body), and 503 if the history cannot be read.
    """
    rows = [_row_to_dict(r) for r in _execute(
        cur, "SELECT * FROM licence_requests WHERE request_id = ?", (entry.request_id,), fetch=True)]
    now = licensing_time.now()
    recent = []
    for row in rows:
        when = licensing_time.to_datetime(row.get("created_at"))
        if when is not None and when > now - WINDOW:
            recent.append((when, row))
    if not recent:
        return None
    # The FIRST use owns the request_id. More than one row inside the window
    # can only come from one request_id sent to two targets at the same
    # instant, which is not guarded (H4); the earliest still decides.
    recent.sort(key=lambda pair: pair[0])
    owner = recent[0][1]
    if owner.get("operation") != entry.operation or owner.get("request_fp") != entry.request_fp:
        licence_fail("request_id_reused")
    return owner


def lookup_locked(cur, entry: Entry) -> Optional[Dict[str, Any]]:
    """
    lookup(), called UNDER the operation's own lock as the last check before
    it writes. The same query; a separate name so the code says which of the
    two checks this is, and so the Postgres race tests can hold a request
    exactly between this check and its write (licensing_harness.pause_after).
    """
    return lookup(cur, entry)


def record(cur, entry: Entry, *, outcome: str, status_code: int = 200,
           response: Optional[Mapping[str, Any]] = None, binding_id: Optional[str] = None,
           ambiguity_id: Optional[str] = None) -> str:
    """
    Record this request's outcome, in the caller's transaction, while the
    caller still holds the lock that decided it (J8). `response` is the body
    (or, for a refusal, the error detail) a replay returns, WITHOUT
    server_time — a replay adds the time of its own response (J5).
    """
    row_id = secrets.token_hex(16)
    body = None if response is None else json.dumps(_without_server_time(response), sort_keys=True,
                                                    separators=(",", ":"))
    _execute(
        cur,
        "INSERT INTO licence_requests (id, request_id, operation, account_id, seat_pool_id, binding_id, "
        "ambiguity_id, request_fp, outcome, status_code, response_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, entry.request_id, entry.operation, entry.account_id, entry.seat_pool_id,
         binding_id or entry.binding_id, ambiguity_id or entry.ambiguity_id, entry.request_fp,
         outcome, status_code, body, licensing_time.now_iso()),
    )
    return row_id


def fill_response(cur, row_id: str, response: Mapping[str, Any]) -> None:
    """
    Store the response of an activation whose lease is issued in a second
    transaction (F5): claim_capacity() records the outcome — this binding,
    activated — under the pool lock, and the lease is added here once issued.
    Only while still empty, so concurrent fillers converge on one body.
    """
    body = json.dumps(_without_server_time(response), sort_keys=True, separators=(",", ":"))
    _execute(cur, "UPDATE licence_requests SET response_json = ? WHERE id = ? AND response_json IS NULL",
             (body, row_id))


def read_row(cur, row_id: str) -> Optional[Dict[str, Any]]:
    rows = _execute(cur, "SELECT * FROM licence_requests WHERE id = ?", (row_id,), fetch=True)
    return _row_to_dict(rows[0]) if rows else None


def stored_response(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    body = row.get("response_json")
    return json.loads(body) if body else None


def replay(row: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Return the stored body with the server_time of THIS response (J5), or
    raise the stored refusal the same way. Every other field — the lease
    string above all — is exactly as stored.
    """
    body = stored_response(row) or {}
    status = int(row.get("status_code") or 200)
    if status >= 400:
        raise_stored(status, body)
    body["server_time"] = wire_time()
    return body


def _without_server_time(body: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in body.items() if k != "server_time"}
