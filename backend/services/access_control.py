"""
services/access_control.py — access control helpers for ExamPartner.

Centralised here so routes/questions.py, routes/cbt.py, and any future
route modules all use the same logic without duplication.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from config import ADMIN_IDENTIFIERS, db_conn


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
        now = datetime.now(timezone.utc)
        return paid_until > now

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
