"""
services/licence_session.py — the 15-minute activation-only token
(contract §4, §11; brief §4.F F7).

A technician signs in on a lab PC with the institution owner's (or an
ExamPartner admin's) password and gets this token instead of an account
token. It authorises POST /licence/activate and nothing else.

The two token kinds must be useless to each other's verifiers, and the
reason is not obvious from auth_utils: read_token() checks only the
signature and expiry, not a type. A token signed the ordinary way would be
accepted by get_current_user() as a full login on every Android route. So
this token is signed with a key DERIVED for this purpose only —
HMAC(JWT_SECRET, "exampartner-activation-v1") — and over the ASCII bytes of
the payload segment, where make_token() signs the decoded JSON with
JWT_SECRET itself. Neither verifier can validate the other's signature.
Tests prove both directions.

The `role` inside the token is INFORMATIONAL. Ownership and admin status
are re-checked at activate time with require_account_owner(), so a changed
owner or a removed admin takes effect immediately, not 15 minutes later.

Expiry uses the real clock, not licensing_time.now(): the licensing test
clock must never extend or cut short a credential.
"""
import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from typing import Any, Dict, Optional, Tuple

import config

TOKEN_TYP = "ep.activation"
TOKEN_TTL_SECONDS = 15 * 60
ROLES = ("institution_owner", "exampartner_admin")

_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")


def _signing_key() -> bytes:
    # Read at call time, like the rest of the licensing config, so a test
    # can change JWT_SECRET and see the effect.
    return hmac.new(config.JWT_SECRET.encode("utf-8"), b"exampartner-activation-v1", hashlib.sha256).digest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(segment: str) -> str:
    return _b64url(hmac.new(_signing_key(), segment.encode("ascii"), hashlib.sha256).digest())


def issue_activation_token(identifier: str, role: str, now: Optional[int] = None) -> Tuple[str, int]:
    """Return (token, exp) where exp is a Unix time 15 minutes after issue."""
    if role not in ROLES:
        raise ValueError(f"unknown activation role {role!r}")
    iat = int(time.time()) if now is None else int(now)
    exp = iat + TOKEN_TTL_SECONDS
    payload = {"typ": TOKEN_TYP, "sub": identifier, "role": role, "iat": iat, "exp": exp}
    segment = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{segment}.{_sign(segment)}", exp


def verify_activation_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """The payload of a valid, unexpired activation token, else None."""
    if not isinstance(token, str) or token.count(".") != 1:
        return None
    segment, signature = token.split(".")
    if not _SEGMENT.fullmatch(segment) or not _SEGMENT.fullmatch(signature):
        return None
    if not hmac.compare_digest(_sign(segment), signature):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("typ") != TOKEN_TYP:
        return None
    sub, role, exp = payload.get("sub"), payload.get("role"), payload.get("exp")
    if not isinstance(sub, str) or not sub.strip() or role not in ROLES:
        return None
    if not isinstance(exp, int) or isinstance(exp, bool) or exp <= int(time.time()):
        return None
    return payload
