"""
services/password_reset_service.py — password reset logic for ExamPartner.

Responsibilities:
  - Generate a user-friendly 6-digit numeric reset code.
  - Hash the code with SHA-256 before storing (raw code is never persisted).
  - Insert a single-use, time-limited row into password_reset_tokens.
  - Validate a submitted code: exists, not expired, not already used.
  - Mark a code as used (single-use enforcement).
  - Update the user's password using the same hashing as auth.py.
  - Look up a user's stored email for dispatch by email_service.

All functions raise FastAPI HTTPException on validation failures so that
routes stay thin and readable.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from config import db_conn, logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESET_CODE_DIGITS = 6          # 6-digit numeric code: 000000–999999
RESET_TTL_MINUTES = 30         # code expires after 30 minutes


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hash_code(raw_code: str) -> str:
    """SHA-256 hash of the raw reset code. Only this hash is stored in DB."""
    return hashlib.sha256(raw_code.encode("utf-8")).hexdigest()


def _hash_pw(password: str, salt: str) -> str:
    """
    Identical to the function in routes/auth.py.
    Duplicated here deliberately to keep password_reset_service.py
    self-contained and avoid circular imports.
    Formula: SHA-256(salt + ":" + password)
    """
    return hashlib.sha256((salt + ":" + password).encode("utf-8")).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Render a datetime as an ISO 8601 string with UTC offset."""
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string back to an aware datetime."""
    # Python 3.11+ handles 'Z'; fromisoformat handles '+00:00'.
    # Normalise 'Z' for older Python versions.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Step 1 — generate and store a reset code
# ---------------------------------------------------------------------------

def create_reset_code(identifier: str) -> Optional[str]:
    """
    Generate a 6-digit reset code for the given identifier, store its hash,
    and return the identifier's linked email address (if any).

    Returns:
        The user's email address if the account exists and has one,
        or None if the account does not exist or has no email.
        The caller must always return a generic response — never reveal
        whether None was returned.

    Side effects:
        Inserts a row into password_reset_tokens.
        Any previous unused tokens for this identifier are left intact
        (they will expire naturally). This avoids a timing side-channel
        where an attacker could probe account existence by observing
        whether old tokens get invalidated.
    """
    db = db_conn()
    try:
        cur = db.cursor()

        # Look up the user's email. Use a single query to avoid TOCTOU.
        cur.execute(
            "SELECT email FROM users WHERE identifier = ?",
            (identifier.strip().lower(),),
        )
        row = cur.fetchone()

        # If no account or no email, return None — caller uses generic response.
        if not row:
            return None
        email = row.get("email") if hasattr(row, "get") else row[0]
        if not email:
            return None

        # Generate raw code — 6 digits, zero-padded.
        raw_code = str(secrets.randbelow(10 ** RESET_CODE_DIGITS)).zfill(RESET_CODE_DIGITS)
        code_hash = _hash_code(raw_code)

        now = _now_utc()
        expires_at = now + timedelta(minutes=RESET_TTL_MINUTES)
        token_id = uuid.uuid4().hex

        cur.execute(
            """
            INSERT INTO password_reset_tokens
              (id, identifier, token_hash, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_id, identifier.strip().lower(), code_hash, _iso(expires_at)),
        )
        db.commit()

        logger.info(
            "password_reset: created reset token for identifier=%s expires=%s",
            identifier,
            _iso(expires_at),
        )

        # Return BOTH email and raw code to the caller (route) so it can
        # dispatch the email without this service knowing about email_service.
        # We use a small tuple; the route unpacks it.
        return email, raw_code  # type: ignore[return-value]

    except Exception as exc:
        logger.error(
            "password_reset: failed to create reset token for identifier=%s — %s",
            identifier,
            exc,
        )
        # Surface as 500 — something unexpected went wrong in the DB.
        raise HTTPException(status_code=500, detail="Could not process request. Please try again.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Step 2 — validate and consume a reset code, update password
# ---------------------------------------------------------------------------

def redeem_reset_code(identifier: str, raw_code: str, new_password: str) -> None:
    """
    Validate the submitted reset code and update the user's password.

    Raises HTTPException on any failure:
      400 — missing fields or password too short
      422 — code is invalid, expired, or already used
              (single error message — never reveal which condition applies)

    On success: password is updated and the token row is marked used.
    """
    identifier = (identifier or "").strip().lower()
    raw_code   = (raw_code or "").strip()

    if not identifier or not raw_code or not new_password:
        raise HTTPException(status_code=400, detail="identifier, code and new_password are required")

    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    code_hash = _hash_code(raw_code)
    now = _now_utc()

    db = db_conn()
    try:
        cur = db.cursor()

        # Single lookup: match on both identifier and hash to prevent
        # cross-account code submission.
        cur.execute(
            """
            SELECT id, expires_at, used_at
            FROM password_reset_tokens
            WHERE identifier = ? AND token_hash = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (identifier, code_hash),
        )
        row = cur.fetchone()

        _invalid_msg = "Reset code is invalid or has expired. Please request a new one."

        if not row:
            # No matching token — invalid code or wrong identifier.
            raise HTTPException(status_code=422, detail=_invalid_msg)

        token_id  = row.get("id")         if hasattr(row, "get") else row[0]
        expires_at_str = row.get("expires_at") if hasattr(row, "get") else row[1]
        used_at   = row.get("used_at")    if hasattr(row, "get") else row[2]

        # Already used — single-use enforcement.
        if used_at is not None:
            raise HTTPException(status_code=422, detail=_invalid_msg)

        # Expired.
        expires_at = _parse_iso(expires_at_str)
        # Make naive datetimes timezone-aware for comparison safety.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now > expires_at:
            raise HTTPException(status_code=422, detail=_invalid_msg)

        # All checks passed — update password.
        new_salt    = secrets.token_hex(16)
        new_pw_hash = _hash_pw(new_password, new_salt)

        cur.execute(
            "UPDATE users SET salt = ?, pw_hash = ? WHERE identifier = ?",
            (new_salt, new_pw_hash, identifier),
        )

        # Mark token as used (single-use — prevents replay).
        cur.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ?",
            (_iso(now), token_id),
        )

        db.commit()

        logger.info(
            "password_reset: password updated for identifier=%s token=%s",
            identifier,
            token_id,
        )

    except HTTPException:
        raise  # re-raise validation errors as-is
    except Exception as exc:
        logger.error(
            "password_reset: unexpected error for identifier=%s — %s",
            identifier,
            exc,
        )
        raise HTTPException(status_code=500, detail="Could not process request. Please try again.")
    finally:
        db.close()
