"""
routes/auth.py — authentication and device management routes for ExamPartner.

Device policy (per account):
  free user  : 1 active device
  paid user  : 2 active devices

Sprint 8 changes:
  - Login/register 403 now passes through the structured body from device_service
    (includes active devices, limit, can_remove_device, cooldown_message).
  - New POST /auth/devices/remove-preauth endpoint for login-screen device removal.
  - Login returns is_paid via time-aware is_paid_user() check.
"""
import hashlib
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse

from config import db_conn, logger
from models.schemas import AuthReq, AuthResp, LoginReq, RefreshReq, RegisterReq
from services.access_control import is_admin_identifier, is_paid_user
from services.auth_utils import get_current_user, make_token
from services.device_service import (
    list_devices,
    refresh_session,
    register_device,
    revoke_device,
    revoke_device_preauth,
)
from services.question_utils import row_get
from pydantic import BaseModel

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------------------
# Local request models
# ---------------------------------------------------------------------------

class PreAuthRemoveBody(BaseModel):
    """Request body for /auth/devices/remove-preauth."""
    identifier: str
    password:   str
    device_id:  str


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


def _verify_password(identifier: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Verify identifier + password. Returns the user row on success, None on failure.
    Used by both login and the pre-auth device removal endpoint.
    """
    db = db_conn()
    cur = db.cursor()
    cur.execute(
        "SELECT identifier, salt, pw_hash, is_admin FROM users WHERE identifier = ?",
        (identifier,),
    )
    row = cur.fetchone()
    db.close()

    if not row:
        return None
    salt    = row["salt"]    if hasattr(row, "keys") else row[1]
    pw_hash = row["pw_hash"] if hasattr(row, "keys") else row[2]
    if _hash_pw(password, salt) != pw_hash:
        return None
    return row


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@router.post("/auth/register", response_model=AuthResp)
def register(body: RegisterReq):
    identifier = body.identifier.strip().lower()
    if not identifier or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Invalid identifier/password")

    full_name = (body.full_name or "").strip() or None
    salt      = secrets.token_hex(16)
    pw_hash   = _hash_pw(body.password, salt)

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO users (identifier, salt, pw_hash, is_paid, full_name) VALUES (?, ?, ?, ?, ?)",
            (identifier, salt, pw_hash, False, full_name),
        )
        db.commit()
    except Exception as e:
        msg = (str(e) or "").lower()
        logger.exception("Register failed for identifier=%s", identifier)
        if "unique" in msg or "duplicate" in msg or "already exists" in msg:
            raise HTTPException(status_code=409, detail="User already exists")
        raise HTTPException(status_code=500, detail="Registration failed.")
    finally:
        db.close()

    try:
        register_device(
            identifier=identifier,
            device_id=body.device_id,
            device_name=body.device_name,
            platform=body.platform,
        )
    except HTTPException as exc:
        # New registrations start with 0 devices, so this should never fire.
        # If it does (race condition), surface the structured 403 body as-is.
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    token = make_token(identifier)
    return {
        "token":      token,
        "identifier": identifier,
        "is_paid":    is_paid_user({"sub": identifier}),
        "is_admin":   is_admin_identifier(identifier),
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=AuthResp)
def login(body: AuthReq):
    identifier = body.identifier.strip().lower()

    row = _verify_password(identifier, body.password)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    try:
        register_device(
            identifier=identifier,
            device_id=body.device_id,
            device_name=body.device_name,
            platform=body.platform,
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            # Pass the structured body through verbatim.
            # Android reads: detail, devices, device_limit, active_count,
            # can_remove_device, cooldown_message.
            return JSONResponse(status_code=403, content=exc.detail)
        raise

    token = make_token(identifier)

    # Use is_paid_user() (time-aware paid_until check) not raw is_paid column.
    is_paid_active = is_paid_user({"sub": identifier})
    is_admin = is_admin_identifier(identifier) or bool(
        row_get(row, "is_admin") or (row[3] if not hasattr(row, "keys") else False)
    )

    return {
        "token":      token,
        "identifier": identifier,
        "is_paid":    is_paid_active,
        "is_admin":   is_admin,
    }


# ---------------------------------------------------------------------------
# Pre-auth device removal (login-screen reinstall recovery)
# ---------------------------------------------------------------------------

@router.post("/auth/devices/remove-preauth")
def remove_device_preauth(body: PreAuthRemoveBody):
    """
    Remove an active device using credentials instead of a Bearer token.

    Used when the user cannot log in because their device limit is reached
    (e.g. after reinstall — old device_id is orphaned but still active).

    Request body:
      { "identifier": "...", "password": "...", "device_id": "..." }

    On success (200): { "ok": true, "removed": "<device_id>" }
    On 401: wrong credentials.
    On 404: device not found or not owned by this user.
    On 429: within 30-day removal cooldown — detail contains backend message.

    After a successful removal the app retries login, which will succeed
    because the device slot is now free.
    """
    identifier = (body.identifier or "").strip().lower()
    password   = body.password or ""
    device_id  = (body.device_id or "").strip()

    if not identifier or not password or not device_id:
        raise HTTPException(status_code=400, detail="identifier, password and device_id are required")

    row = _verify_password(identifier, password)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Raises 404 / 429 with backend detail string if blocked
    revoke_device_preauth(identifier=identifier, device_id=device_id)

    return {"ok": True, "removed": device_id}


# ---------------------------------------------------------------------------
# Refresh token
# ---------------------------------------------------------------------------

@router.post("/auth/refresh")
def refresh_token(
    body: RefreshReq,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    refresh_session(identifier=identifier, device_id=body.device_id)
    token = make_token(identifier)
    return {"ok": True, "token": token, "identifier": identifier}


# ---------------------------------------------------------------------------
# List devices (token-authenticated)
# ---------------------------------------------------------------------------

@router.get("/auth/devices")
def get_devices(user: Optional[Dict[str, Any]] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    current_device_id = user.get("device_id") or None
    return list_devices(identifier=identifier, current_device_id=current_device_id)


# ---------------------------------------------------------------------------
# Revoke device (token-authenticated)
# ---------------------------------------------------------------------------

@router.delete("/auth/devices/{device_id}")
def delete_device(
    device_id: str = Path(...),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    revoke_device(identifier=identifier, device_id=device_id)
    return {"ok": True, "revoked": device_id}


# ---------------------------------------------------------------------------
# Me
# ---------------------------------------------------------------------------

@router.get("/me")
def me(user: Optional[Dict[str, Any]] = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    db = db_conn()
    cur = db.cursor()
    cur.execute(
        "SELECT is_paid, paid_until, plan, is_founding, email, is_admin, full_name "
        "FROM users WHERE identifier = ?",
        (identifier,),
    )
    row = cur.fetchone()
    db.close()

    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    is_paid_active = is_paid_user({"sub": identifier})
    paid_until     = row_get(row, "paid_until")

    return {
        "identifier":     identifier,
        "full_name":      row_get(row, "full_name"),
        "is_paid":        bool(row_get(row, "is_paid")),
        "is_paid_active": bool(is_paid_active),
        "paid_until":     paid_until.isoformat() if paid_until else None,
        "plan":           row_get(row, "plan") or "free",
        "is_founding":    bool(row_get(row, "is_founding") or False),
        "email":          row_get(row, "email"),
        "is_admin":       is_admin_identifier(identifier) or bool(row_get(row, "is_admin") or False),
        "device_limit":   2 if is_paid_active else 1,
    }


# ---------------------------------------------------------------------------
# Update email
# ---------------------------------------------------------------------------

@router.post("/me/email")
def update_email(
    payload: Dict[str, str],
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    email = (payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email is required")

    db = db_conn()
    cur = db.cursor()
    cur.execute("UPDATE users SET email = ? WHERE identifier = ?", (email, identifier))
    db.commit()
    db.close()

    return {"ok": True, "email": email}
