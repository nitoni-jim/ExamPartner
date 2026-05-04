"""
services/device_service.py — device registration and policy enforcement.

Device limits:
  free user  : 1 active device
  paid user  : 2 active devices

An active device is one where revoked_at IS NULL.
All enforcement is backend-side — the Android app cannot bypass this.
"""
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from config import db_conn
from services.access_control import is_paid_user


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

DEVICE_LIMIT_FREE = 1
DEVICE_LIMIT_PAID = 2

DEVICE_LIMIT_ERROR = (
    "This account has reached its device limit. "
    "Please remove an old device or contact support."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_device_limit(identifier: str) -> int:
    """Returns the device limit for this user based on paid status."""
    paid = is_paid_user({"sub": identifier})
    return DEVICE_LIMIT_PAID if paid else DEVICE_LIMIT_FREE


def _get_user_id(cur, identifier: str) -> Optional[str]:
    cur.execute("SELECT id FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    if not row:
        return None
    return str(row["id"] if hasattr(row, "keys") else row[0])


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def count_active_devices(cur, user_id: str) -> int:
    cur.execute(
        "SELECT COUNT(*) AS c FROM user_devices WHERE user_id = ? AND revoked_at IS NULL",
        (user_id,),
    )
    row = cur.fetchone()
    try:
        return int(row.get("c") if hasattr(row, "get") else row[0])
    except Exception:
        return int(row[0])


def find_device(cur, user_id: str, device_id: str) -> Optional[Any]:
    """Find an active (non-revoked) device record."""
    cur.execute(
        "SELECT id, device_id, device_name, platform, created_at, last_seen_at "
        "FROM user_devices WHERE user_id = ? AND device_id = ? AND revoked_at IS NULL",
        (user_id, device_id),
    )
    return cur.fetchone()


def touch_device(cur, user_id: str, device_id: str) -> None:
    """Update last_seen_at for an existing active device."""
    cur.execute(
        "UPDATE user_devices SET last_seen_at = ? WHERE user_id = ? AND device_id = ? AND revoked_at IS NULL",
        (_now_iso(), user_id, device_id),
    )


def register_device(
    identifier: str,
    device_id: Optional[str],
    device_name: Optional[str],
    platform: Optional[str],
) -> None:
    """
    Register a device for a user, enforcing the device limit.

    - If device already registered (active): refresh last_seen_at only.
    - If device is new and limit not exceeded: insert.
    - If device is new and limit exceeded: raise 403.
    - If no device_id provided: skip silently (web clients).
    """
    device_id = (device_id or "").strip()
    if not device_id:
        return  # web/anonymous callers — no device tracking needed

    db = db_conn()
    try:
        cur = db.cursor()
        user_id = _get_user_id(cur, identifier)
        if not user_id:
            return

        existing = find_device(cur, user_id, device_id)

        if existing:
            # Already registered — just refresh
            touch_device(cur, user_id, device_id)
            db.commit()
            return

        # New device — check limit
        active_count = count_active_devices(cur, user_id)
        limit = _get_device_limit(identifier)

        if active_count >= limit:
            raise HTTPException(status_code=403, detail=DEVICE_LIMIT_ERROR)

        # Insert new device
        record_id = secrets.token_hex(16)
        now = _now_iso()
        cur.execute(
            """
            INSERT INTO user_devices (id, user_id, device_id, device_name, platform, created_at, last_seen_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                record_id,
                user_id,
                device_id,
                (device_name or "").strip() or None,
                (platform or "").strip().lower() or None,
                now,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()


def list_devices(identifier: str, current_device_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Return all active devices for this user.
    Marks the current device with is_current_device: true.
    """
    db = db_conn()
    try:
        cur = db.cursor()
        user_id = _get_user_id(cur, identifier)
        if not user_id:
            return []

        cur.execute(
            """
            SELECT device_id, device_name, platform, created_at, last_seen_at
            FROM user_devices
            WHERE user_id = ? AND revoked_at IS NULL
            ORDER BY last_seen_at DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    finally:
        db.close()

    def _val(row, key, idx):
        try:
            return row[key] if hasattr(row, "keys") else row[idx]
        except Exception:
            return None

    result = []
    for row in rows:
        did = _val(row, "device_id", 0)
        result.append({
            "device_id":        did,
            "device_name":      _val(row, "device_name", 1),
            "platform":         _val(row, "platform", 2),
            "created_at":       _val(row, "created_at", 3),
            "last_seen_at":     _val(row, "last_seen_at", 4),
            "is_current_device": (did == current_device_id) if current_device_id else False,
        })
    return result


def revoke_device(identifier: str, device_id: str) -> None:
    """
    Revoke (soft-delete) a device belonging to the user.
    Raises 404 if device is not found or already revoked.
    Raises 403 if device belongs to another user.
    """
    device_id = (device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    db = db_conn()
    try:
        cur = db.cursor()
        user_id = _get_user_id(cur, identifier)
        if not user_id:
            raise HTTPException(status_code=404, detail="User not found")

        existing = find_device(cur, user_id, device_id)
        if not existing:
            # Check if it exists but belongs to another user
            cur.execute(
                "SELECT user_id FROM user_devices WHERE device_id = ? AND revoked_at IS NULL",
                (device_id,),
            )
            other = cur.fetchone()
            if other:
                raise HTTPException(status_code=403, detail="You can only remove your own devices")
            raise HTTPException(status_code=404, detail="Device not found or already removed")

        cur.execute(
            "UPDATE user_devices SET revoked_at = ? WHERE user_id = ? AND device_id = ? AND revoked_at IS NULL",
            (_now_iso(), user_id, device_id),
        )
        db.commit()
    finally:
        db.close()


def refresh_session(
    identifier: str,
    device_id: Optional[str],
) -> None:
    """
    Touch device last_seen_at on token refresh.
    No-op if device_id not provided.
    """
    device_id = (device_id or "").strip()
    if not device_id:
        return

    db = db_conn()
    try:
        cur = db.cursor()
        user_id = _get_user_id(cur, identifier)
        if not user_id:
            return
        touch_device(cur, user_id, device_id)
        db.commit()
    finally:
        db.close()
