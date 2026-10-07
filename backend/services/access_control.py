"""
services/access_control.py — access control helpers for ExamPartner.

Centralised here so routes/questions.py, routes/cbt.py, and any future
route modules all use the same logic without duplication.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from config import ADMIN_IDENTIFIERS, db_conn
from services.licensing_time import to_datetime


def is_admin_identifier(identifier: Optional[str]) -> bool:
    normalized = (identifier or "").strip().lower()
    return bool(normalized and normalized in ADMIN_IDENTIFIERS)


def is_admin_user(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False

    identifier = (user.get("sub") or "").strip().lower()
    if is_admin_identifier(identifier):
        return True
    if not identifier:
        return False

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT is_admin FROM users WHERE identifier = ?", (identifier,))
        row = cur.fetchone()
    finally:
        db.close()

    if not row:
        return False
    val = row.get("is_admin") if hasattr(row, "get") else row[0]
    return bool(val)


def is_paid_user(user: Optional[Dict[str, Any]]) -> bool:
    """
    Paid access check.
    - If paid_until exists and is in the future => active
    - Else fallback to legacy is_paid (for older accounts)
    """
    if not user:
        return False
    identifier = user.get("sub")
    if not identifier:
        return False
    if is_admin_identifier(identifier):
        return True

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT is_paid, paid_until FROM users WHERE identifier = ?",
            (identifier,),
        )
        row = cur.fetchone()
    finally:
        db.close()

    if not row:
        return False

    paid_until = row.get("paid_until") if hasattr(row, "get") else row[1]
    if paid_until is not None:
        # paid_until is a datetime on Postgres and an ISO str on SQLite, where
        # comparing it raw against a datetime raised TypeError. to_datetime()
        # normalises both. The comparison deliberately uses the REAL clock,
        # not licensing_time.now(): the licensing test clock must never move
        # a paying student's access.
        now = datetime.now(timezone.utc)
        return to_datetime(paid_until) > now

    # legacy fallback
    is_paid = row.get("is_paid") if hasattr(row, "get") else row[0]
    return bool(is_paid)


def get_free_year_for_subject(
    db,
    exam: Optional[str],
    subject: Optional[str],
) -> Optional[int]:
    """
    Returns the oldest available year for a given exam+subject combination.
    This is the only year free users may access.
    """
    where: List[str] = ["year IS NOT NULL"]
    params: List[Any] = []

    if exam:
        where.append("exam = ?")
        params.append(exam)
    if subject:
        where.append("subject = ?")
        params.append(subject)

    where_sql = "WHERE " + " AND ".join(where)
    cur = db.cursor()
    cur.execute(
        f"SELECT MIN(year) AS oldest FROM questions {where_sql}",
        tuple(params) if params else None,
    )
    row = cur.fetchone()
    if not row:
        return None
    val = row.get("oldest") if hasattr(row, "get") else row[0]
    return int(val) if val is not None else None


def require_admin(user: Optional[Dict[str, Any]]) -> str:
    """Raise 403 if not admin. Returns identifier string."""
    if not user or not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return str(user.get("sub") or "").strip().lower()


def require_account_owner(user: Optional[Dict[str, Any]], account_id: Optional[str]) -> str:
    """
    Seat-pool licensing: raise 403 unless the caller is an ExamPartner admin
    or the primary owner of `account_id` (accounts.owner_identifier).
    Returns the caller's identifier.

    The caller must pass an account_id it RESOLVED from the object being
    acted on (the pool's or binding's own account_id column), never one taken
    from the request body — a client-supplied id must not imply authority.

    An unknown account is 403, not 404: answering 404 to a non-owner would
    tell them which account ids exist. Both sides are compared stripped and
    lowercased, because registration lowercases identifiers
    (routes/auth.py) and an owner_identifier typed by an admin may not be.
    """
    if not user:
        raise HTTPException(status_code=403, detail="Account owner access required")
    if is_admin_user(user):
        return str(user.get("sub") or "").strip().lower()

    identifier = str(user.get("sub") or "").strip().lower()
    if not identifier or not account_id:
        raise HTTPException(status_code=403, detail="Account owner access required")

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute("SELECT owner_identifier FROM accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
    finally:
        db.close()

    owner = (row.get("owner_identifier") if hasattr(row, "get") else row[0]) if row else None
    if not owner or str(owner).strip().lower() != identifier:
        raise HTTPException(status_code=403, detail="Account owner access required")
    return identifier
