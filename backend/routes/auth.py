"""
routes/auth.py — authentication and device management routes for ExamPartner.

Device policy (enforced backend-side):
  free user  : 1 active device
  paid user  : 2 active devices
"""
import hashlib
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path

from config import db_conn, logger
from models.schemas import AuthReq, AuthResp, LoginReq, RefreshReq, RegisterReq
from services.access_control import is_admin_identifier, is_admin_user, is_paid_user
from services.auth_utils import get_current_user, make_token
from services.device_service import (
    list_devices,
    refresh_session,
    register_device,
    revoke_device,
)
from services.question_utils import row_get

router = APIRouter(tags=["auth"])


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

@router.post("/auth/register", response_model=AuthResp)
def register(body: RegisterReq):
    identifier = body.identifier.strip().lower()
    if not identifier or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Invalid identifier/password")

    full_name = (body.full_name or "").strip() or None
    salt = secrets.token_hex(16)
    pw_hash = _hash_pw(body.password, salt)

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
        raise HTTPException(status_code=500, detail="Registration failed. Server DB error.")
    finally:
        db.close()

    # Register device — new accounts start with 0 devices so limit is never hit here
    register_device(
        identifier=identifier,
        device_id=body.device_id,
        device_name=body.device_name,
        platform=body.platform,
    )

    token = make_token(identifier)
    return {
        "token": token,
        "identifier": identifier,
        "is_paid": False,
        "is_admin": is_admin_identifier(identifier),
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=AuthResp)
def login(body: AuthReq):
    identifier = body.identifier.strip().lower()

    db = db_conn()
    cur = db.cursor()
    cur.execute(
        "SELECT identifier, salt, pw_hash, is_paid, is_admin FROM users WHERE identifier = ?",
        (identifier,),
    )
    row = cur.fetchone()
    db.close()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    salt = row["salt"] if hasattr(row, "keys") else row[1]
    pw_hash = row["pw_hash"] if hasattr(row, "keys") else row[2]
    if _hash_pw(body.password, salt) != pw_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Enforce device policy — raises 403 if limit exceeded
    register_device(
        identifier=identifier,
        device_id=body.device_id,
        device_name=body.device_name,
        platform=body.platform,
    )

    token = make_token(identifier)
    return {
        "token": token,
        "identifier": identifier,
        "is_paid": bool(row["is_paid"] if hasattr(row, "keys") else row[3]),
        "is_admin": is_admin_identifier(identifier) or bool(row_get(row, "is_admin") or False),
    }


# ---------------------------------------------------------------------------
# Refresh token
# ---------------------------------------------------------------------------

@router.post("/auth/refresh")
def refresh_token(
    body: RefreshReq,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Refresh an access token without requiring full re-login.
    The existing Bearer token must still be valid.
    Updates device last_seen_at if device_id is provided.
    Returns a fresh token with a new expiry.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Touch device last_seen_at
    refresh_session(identifier=identifier, device_id=body.device_id)

    # Issue fresh token
    token = make_token(identifier)
    return {"ok": True, "token": token, "identifier": identifier}


# ---------------------------------------------------------------------------
# List devices
# ---------------------------------------------------------------------------

@router.get("/auth/devices")
def get_devices(
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Return all active registered devices for the logged-in user.
    The current device is identified by the x-device-id header if provided.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # current_device_id comes from the token payload if the client included it
    current_device_id = user.get("device_id") or None

    devices = list_devices(identifier=identifier, current_device_id=current_device_id)
    return devices


# ---------------------------------------------------------------------------
# Delete / revoke device
# ---------------------------------------------------------------------------

@router.delete("/auth/devices/{device_id}")
def delete_device(
    device_id: str = Path(...),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Revoke (soft-delete) one of the user's registered devices.
    After revocation, a new device can register on next login.
    A user can only revoke their own devices.
    """
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
        "SELECT is_paid, paid_until, plan, is_founding, email, is_admin, full_name FROM users WHERE identifier = ?",
        (identifier,),
    )
    row = cur.fetchone()
    db.close()

    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    is_paid_active = is_paid_user({"sub": identifier})
    paid_until = row_get(row, "paid_until")

    return {
        "identifier": identifier,
        "full_name": row_get(row, "full_name"),
        "is_paid": bool(row_get(row, "is_paid")),
        "is_paid_active": bool(is_paid_active),
        "paid_until": paid_until.isoformat() if paid_until else None,
        "plan": row_get(row, "plan") or "free",
        "is_founding": bool(row_get(row, "is_founding") or False),
        "email": row_get(row, "email"),
        "is_admin": is_admin_identifier(identifier) or bool(row_get(row, "is_admin") or False),
        "device_limit": 2 if is_paid_active else 1,
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
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")

    db = db_conn()
    cur = db.cursor()
    cur.execute("UPDATE users SET email = ? WHERE identifier = ?", (email, identifier))
    db.commit()
    db.close()

    return {"ok": True, "email": email}
