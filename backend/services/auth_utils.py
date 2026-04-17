"""
services/auth_utils.py — JWT-ish token helpers for ExamPartner.
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from fastapi import Header

from config import JWT_SECRET, JWT_TTL_SECONDS


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _sign(data: bytes, secret: str) -> str:
    return _b64url(hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest())


def make_token(sub: str, ttl_seconds: int = JWT_TTL_SECONDS) -> str:
    payload = {"sub": sub, "exp": int(time.time()) + ttl_seconds}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = _sign(raw, JWT_SECRET)
    return f"{_b64url(raw)}.{sig}"


def read_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        b64, sig = token.split(".", 1)
        raw = base64.urlsafe_b64decode(b64 + "==")
        if _sign(raw, JWT_SECRET) != sig:
            return None
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> Optional[Dict[str, Any]]:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return read_token(token)
