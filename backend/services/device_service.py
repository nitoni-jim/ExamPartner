"""
services/device_service.py — device registration and policy enforcement.

Device limits (per account, not global):
  free user  : 1 active device
  paid user  : 2 active devices

An active device is one where revoked_at IS NULL.

Device limit UX (Sprint 8):
  When login is blocked, the 403 body includes:
    - active device list
    - device_limit and active_count
    - can_remove_device (false if within 30-day cooldown)
    - cooldown_message (backend text, shown verbatim in app)

  A pre-auth removal endpoint allows removing a device using credentials
  (not a token) so reinstall recovery works from the login screen itself.

revoke_reason values:
  manual               — user explicitly removed the device (triggers 30-day cooldown)
  reinstall_heuristic  — auto-revoked because same platform+name, new device_id after reinstall
  stale                — auto-revoked because not seen in STALE_DEVICE_DAYS days

Only 'manual' revokes count toward the 30-day removal cooldown.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from config import db_conn
from services.access_control import is_admin_identifier, is_paid_user

DEVICE_LIMIT_FREE = 1
DEVICE_LIMIT_PAID = 2
DEVICE_REMOVAL_COOLDOWN_DAYS = 30

# Devices not seen in this many days are considered orphaned (app uninstalled
# or reinstalled). They are silently revoked before the limit check so the
# user never hits a false device-limit error after a normal reinstall.
# 90 days is generous — a truly active device calls /auth/refresh far more often.
STALE_DEVICE_DAYS = 90


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_device_limit(identifier: str) -> int:
    paid = is_paid_user({"sub": identifier})
    return DEVICE_LIMIT_PAID if paid else DEVICE_LIMIT_FREE


def _get_user_id(cur, identifier: str) -> Optional[str]:
    cur.execute("SELECT id FROM users WHERE identifier = ?", (identifier,))
    row = cur.fetchone()
    if not row:
        return None
    return str(row["id"] if hasattr(row, "keys") else row[0])


# ---------------------------------------------------------------------------
# Cooldown — only manual revokes count
# ---------------------------------------------------------------------------

def _get_cooldown_message(cur, user_id: str, identifier: str) -> Optional[str]:
    """
    Returns cooldown message string if blocked, None if removal is allowed.

    Only rows with revoke_reason = 'manual' count toward the 30-day cooldown.
    Auto-revokes (reinstall_heuristic, stale) do not block manual removals.
    """
    if is_admin_identifier(identifier):
        return None

    cur.execute(
        """
        SELECT MAX(revoked_at) AS last_revoked
        FROM user_devices
        WHERE user_id = ?
          AND revoke_reason = 'manual'
        """,
        (user_id,),
    )
    row = cur.fetchone()
    last_revoked = row.get("last_revoked") if hasattr(row, "get") else row[0]
    if not last_revoked:
        return None

    try:
        if isinstance(last_revoked, str):
            dt = datetime.fromisoformat(last_revoked.replace("Z", "+00:00"))
        else:
            dt = last_revoked
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    cooldown_until = dt + timedelta(days=DEVICE_REMOVAL_COOLDOWN_DAYS)
    if datetime.now(timezone.utc) < cooldown_until:
        return f"You can remove a device again on {cooldown_until.strftime('%d %B %Y')}."
    return None


def _check_removal_cooldown(cur, user_id: str, identifier: str) -> None:
    msg = _get_cooldown_message(cur, user_id, identifier)
    if msg:
        raise HTTPException(status_code=429, detail=msg)


# ---------------------------------------------------------------------------
# Core queries
# ---------------------------------------------------------------------------

def _revoke_stale_devices(cur, user_id: str) -> None:
    """
    Silently revoke active devices for this user that haven't been seen in
    STALE_DEVICE_DAYS days. Called before the device limit check in
    register_device so a reinstall never incorrectly blocks re-login.

    The 30-day manual removal cooldown does NOT apply here — this is an
    automatic server-side cleanup, not a user-initiated removal action.
    revoke_reason is set to 'stale' so the cooldown query ignores these rows.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=STALE_DEVICE_DAYS)
    ).isoformat()
    cur.execute(
        """
        UPDATE user_devices
        SET revoked_at    = ?,
            revoke_reason = 'stale'
        WHERE user_id = ?
          AND revoked_at IS NULL
          AND last_seen_at < ?
        """,
        (_now_iso(), user_id, cutoff),
    )


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
    """Active device record for this specific (user_id, device_id) pair."""
    cur.execute(
        "SELECT id, device_id, device_name, platform, created_at, last_seen_at "
        "FROM user_devices WHERE user_id = ? AND device_id = ? AND revoked_at IS NULL",
        (user_id, device_id),
    )
    return cur.fetchone()


def touch_device(cur, user_id: str, device_id: str,
                 device_name: Optional[str] = None,
                 platform: Optional[str] = None) -> None:
    """
    Update last_seen_at on an existing active device row.
    Also refreshes device_name and platform if provided, so a reinstall
    that preserved device_id still gets up-to-date metadata.
    Uses COALESCE so a None incoming value never overwrites a stored value.
    """
    cur.execute(
        """
        UPDATE user_devices
        SET last_seen_at = ?,
            device_name  = COALESCE(?, device_name),
            platform     = COALESCE(?, platform)
        WHERE user_id = ? AND device_id = ? AND revoked_at IS NULL
        """,
        (
            _now_iso(),
            (device_name or "").strip() or None,
            (platform or "").strip().lower() or None,
            user_id,
            device_id,
        ),
    )


def _to_str(val) -> Optional[str]:
    """Convert datetime or string timestamp to ISO string. Returns None if blank."""
    if val is None:
        return None
    if isinstance(val, str):
        return val or None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _rows_to_device_list(rows) -> List[Dict[str, Any]]:
    def _v(row, key, idx):
        try:
            return row[key] if hasattr(row, "keys") else row[idx]
        except Exception:
            return None

    result = []
    for r in rows:
        result.append({
            "device_id":         _v(r, "device_id", 0),
            "device_name":       _v(r, "device_name", 1),
            "platform":          _v(r, "platform", 2),
            "created_at":        _to_str(_v(r, "created_at", 3)),
            "last_seen_at":      _to_str(_v(r, "last_seen_at", 4)),
            "is_current_device": False,
        })
    return result


def _fetch_active_devices(cur, user_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        "SELECT device_id, device_name, platform, created_at, last_seen_at "
        "FROM user_devices WHERE user_id = ? AND revoked_at IS NULL "
        "ORDER BY last_seen_at DESC",
        (user_id,),
    )
    return _rows_to_device_list(cur.fetchall())


# ---------------------------------------------------------------------------
# Reinstall heuristic
# ---------------------------------------------------------------------------

def _find_reinstall_candidate(
    cur,
    user_id: str,
    incoming_device_id: str,
    platform: Optional[str],
    device_name: Optional[str],
) -> Optional[Any]:
    """
    Returns the single active device row that looks like the same physical
    phone after a reinstall, or None if the match is absent or ambiguous.

    Conditions (ALL must hold):
      - same platform (case-insensitive, already normalised by caller)
      - same device_name (exact match)
      - different device_id (not the incoming one)
      - revoked_at IS NULL (still active)
      - exactly one such row exists — two or more means ambiguous, do not revoke

    Both platform and device_name must be non-empty. An empty device_name
    (e.g. the backend stored NULL) means we cannot reliably identify the
    physical phone, so we do not auto-revoke.
    """
    norm_platform    = (platform or "").strip().lower()
    norm_device_name = (device_name or "").strip()

    if not norm_platform or not norm_device_name:
        return None

    cur.execute(
        """
        SELECT id, device_id, device_name, platform, created_at, last_seen_at
        FROM user_devices
        WHERE user_id     = ?
          AND platform    = ?
          AND device_name = ?
          AND device_id  != ?
          AND revoked_at IS NULL
        """,
        (user_id, norm_platform, norm_device_name, incoming_device_id),
    )
    rows = cur.fetchall()

    # Exactly one match required — two or more is ambiguous (two phones of the
    # same model), so we fall through to the normal 403 device-limit response.
    if len(rows) != 1:
        return None
    return rows[0]


def _revoke_reinstall_candidate(cur, candidate_row) -> None:
    """
    Revoke the old device row identified as the reinstall candidate.
    Sets revoke_reason = 'reinstall_heuristic' so the cooldown query ignores it.
    """
    def _v(row, key, idx):
        try:
            return row[key] if hasattr(row, "keys") else row[idx]
        except Exception:
            return None

    row_id = _v(candidate_row, "id", 0)
    cur.execute(
        """
        UPDATE user_devices
        SET revoked_at    = ?,
            revoke_reason = 'reinstall_heuristic'
        WHERE id = ?
          AND revoked_at IS NULL
        """,
        (_now_iso(), row_id),
    )


# ---------------------------------------------------------------------------
# Insert helper
# ---------------------------------------------------------------------------

def _insert_device(
    cur,
    user_id: str,
    device_id: str,
    device_name: Optional[str],
    platform: Optional[str],
) -> None:
    """
    Insert a new active device row.

    The ON CONFLICT clause acts as a final safety net against any race that
    slips past the earlier find_device check (e.g. concurrent login requests).
    The partial unique index ux_user_devices_active_user_device on
    (user_id, device_id) WHERE revoked_at IS NULL backs this at the DB level.

    On conflict: update metadata rather than failing, so the row is always
    in the most up-to-date state after this call.
    """
    record_id = secrets.token_hex(16)
    now       = _now_iso()
    cur.execute(
        """
        INSERT INTO user_devices
          (id, user_id, device_id, device_name, platform,
           created_at, last_seen_at, revoked_at, revoke_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT (user_id, device_id) WHERE revoked_at IS NULL
        DO UPDATE SET
          last_seen_at = EXCLUDED.last_seen_at,
          device_name  = COALESCE(EXCLUDED.device_name, user_devices.device_name),
          platform     = COALESCE(EXCLUDED.platform,    user_devices.platform)
        """,
        (
            record_id,
            user_id,
            device_id,
            device_name,
            platform,
            now,
            now,
        ),
    )


# ---------------------------------------------------------------------------
# Register (login / register flow)
# ---------------------------------------------------------------------------

def register_device(
    identifier: str,
    device_id: Optional[str],
    device_name: Optional[str],
    platform: Optional[str],
) -> None:
    """
    Register or refresh a device for a user.

    Flow:
      1. If the active device already exists for (user_id, device_id): update
         last_seen_at / device_name / platform. No insert. Return.
      2. Silently revoke stale devices (not seen in STALE_DEVICE_DAYS days).
      3. Re-count active devices after stale cleanup.
      4. If under the limit: insert new row. Return.
      5. If at/over the limit: run the reinstall heuristic.
         If exactly one active row matches (same platform + device_name,
         different device_id): revoke it with reason 'reinstall_heuristic',
         insert new row, allow login.
      6. Otherwise: raise HTTP 403 with structured device-limit body.

    Device limit is enforced per user_id, not globally per device_id.
    The same physical phone used by two different accounts does not affect
    either account's limit.
    """
    device_id = (device_id or "").strip()
    if not device_id:
        return

    norm_platform    = (platform or "").strip().lower() or None
    norm_device_name = (device_name or "").strip() or None

    db = db_conn()
    try:
        cur = db.cursor()
        user_id = _get_user_id(cur, identifier)
        if not user_id:
            return

        # Step 1: idempotent refresh — same active device_id, just update metadata.
        if find_device(cur, user_id, device_id):
            touch_device(cur, user_id, device_id,
                         device_name=norm_device_name,
                         platform=norm_platform)
            db.commit()
            return

        # Step 2: silently revoke orphaned/stale devices (reason = 'stale').
        _revoke_stale_devices(cur, user_id)

        # Step 3: re-count after stale cleanup.
        active_count = count_active_devices(cur, user_id)
        limit        = _get_device_limit(identifier)

        # Step 4: under limit — insert normally.
        if active_count < limit:
            _insert_device(cur, user_id, device_id, norm_device_name, norm_platform)
            db.commit()
            return

        # Step 5: at/over limit — run reinstall heuristic before blocking.
        candidate = _find_reinstall_candidate(
            cur,
            user_id=user_id,
            incoming_device_id=device_id,
            platform=norm_platform,
            device_name=norm_device_name,
        )
        if candidate is not None:
            # Same physical phone after reinstall — revoke old row, register new one.
            _revoke_reinstall_candidate(cur, candidate)
            _insert_device(cur, user_id, device_id, norm_device_name, norm_platform)
            db.commit()
            return

        # Step 6: genuinely over limit or ambiguous — return structured 403.
        devices      = _fetch_active_devices(cur, user_id)
        cooldown_msg = _get_cooldown_message(cur, user_id, identifier)
        raise HTTPException(
            status_code=403,
            detail={
                "detail":            "This account has reached its device limit.",
                "devices":           devices,
                "device_limit":      limit,
                "active_count":      active_count,
                "can_remove_device": cooldown_msg is None,
                "cooldown_message":  cooldown_msg,
            },
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# List (token-authenticated — ProfileScreen)
# ---------------------------------------------------------------------------

def list_devices(
    identifier: str,
    current_device_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    db = db_conn()
    try:
        cur = db.cursor()
        user_id = _get_user_id(cur, identifier)
        if not user_id:
            return []

        cur.execute(
            "SELECT device_id, device_name, platform, created_at, last_seen_at "
            "FROM user_devices WHERE user_id = ? AND revoked_at IS NULL "
            "ORDER BY last_seen_at DESC",
            (user_id,),
        )
        rows = cur.fetchall()
    finally:
        db.close()

    def _v(row, key, idx):
        try:
            return row[key] if hasattr(row, "keys") else row[idx]
        except Exception:
            return None

    return [
        {
            "device_id":         (did := _v(r, "device_id", 0)),
            "device_name":       _v(r, "device_name", 1),
            "platform":          _v(r, "platform", 2),
            "created_at":        _to_str(_v(r, "created_at", 3)),
            "last_seen_at":      _to_str(_v(r, "last_seen_at", 4)),
            "is_current_device": (did == current_device_id) if current_device_id else False,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Revoke — token-authenticated (ProfileScreen → device management)
# ---------------------------------------------------------------------------

def revoke_device(identifier: str, device_id: str) -> None:
    """
    Revoke a device via Bearer token (from ProfileScreen).
    30-day cooldown applies. Admins bypass.
    Sets revoke_reason = 'manual' so the cooldown query counts this row.
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
            cur.execute(
                "SELECT user_id FROM user_devices WHERE device_id = ? AND revoked_at IS NULL",
                (device_id,),
            )
            if cur.fetchone():
                raise HTTPException(status_code=403, detail="You can only remove your own devices")
            raise HTTPException(status_code=404, detail="Device not found or already removed")

        _check_removal_cooldown(cur, user_id, identifier)

        cur.execute(
            """
            UPDATE user_devices
            SET revoked_at    = ?,
                revoke_reason = 'manual'
            WHERE user_id = ? AND device_id = ? AND revoked_at IS NULL
            """,
            (_now_iso(), user_id, device_id),
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Revoke — pre-auth (login screen — reinstall recovery)
# ---------------------------------------------------------------------------

def revoke_device_preauth(identifier: str, device_id: str) -> None:
    """
    Remove a device using credentials instead of a Bearer token.

    Used when the user cannot log in because their limit is reached (reinstall).
    Password verification is performed by the auth route before calling this.
    The 30-day cooldown applies identically to the token-authenticated path.
    Sets revoke_reason = 'manual' so the cooldown query counts this row.

    On success the caller retries login, which will succeed because the slot
    is now free.
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
            raise HTTPException(status_code=404, detail="Device not found or already removed")

        _check_removal_cooldown(cur, user_id, identifier)

        cur.execute(
            """
            UPDATE user_devices
            SET revoked_at    = ?,
                revoke_reason = 'manual'
            WHERE user_id = ? AND device_id = ? AND revoked_at IS NULL
            """,
            (_now_iso(), user_id, device_id),
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Refresh session (token refresh path)
# ---------------------------------------------------------------------------

def refresh_session(identifier: str, device_id: Optional[str]) -> None:
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
