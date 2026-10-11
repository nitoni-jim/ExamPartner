"""
services/licence_lease.py — the signed offline lease (contract §6) and the
signing key that makes it (brief §4.F F5, F6).

Two halves, deliberately separate:

- sign_lease() and verify_lease() are PURE. They take the key as an
  argument and read no configuration, so the contract §6.4 vectors test them
  directly with the published TEST key, and the Windows client's verifier
  can be checked against exactly the same bytes.

- load_signing_key() / configure_signing() turn config.LICENCE_SIGNING_KEY
  and LICENCE_SIGNING_KID into the key the routes sign with, once at
  startup. Anything wrong with them — absent, malformed, the TEST seed, a
  kid below 1 — makes licensing UNAVAILABLE (503 on every /licence/*
  endpoint, one ERROR in the boot log, /health "unavailable"). It never
  stops the backend starting: this backend also serves the live Android app
  (brief §4.E).

Format (contract §6.1): base64url-no-padding(payload JSON) "." base64url-no-
padding(64-byte Ed25519 signature), where the signature is over the ASCII
bytes of the payload segment AS TRANSMITTED. Never over decoded or re-
serialised JSON: the client verifies the exact string it stores and never
re-serialises, so the server must sign exactly what it sends.
"""
import base64
import binascii
import json
import logging
import re
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import config

logger = logging.getLogger(__name__)

LEASE_VERSION = 1
LEASE_TYP = "ep.licence.lease"

# The published TEST seed (contract §6.4). Anyone can recompute it, so a
# lease signed with it proves nothing. It is here only so that configuring
# it as LICENCE_SIGNING_KEY is refused — compared as decoded bytes, so
# whitespace or line endings pasted into Render cannot slip it past.
_TEST_SEED = base64.b64decode("vaqSxX9ClSwoMECP/J92vJWuEQYqyktbMlFU8YPu+Lg=")

_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")


class LeaseInvalid(Exception):
    """The lease is malformed, unsigned, signed by an unknown key, or not a v1 lease."""


class SigningKeyError(Exception):
    """LICENCE_SIGNING_KEY / LICENCE_SIGNING_KID cannot be used; the message says why."""


# ---------------------------------------------------------------------------
# Pure: serialise, sign, verify
# ---------------------------------------------------------------------------

def serialise_payload(payload: Mapping[str, Any]) -> bytes:
    """
    Contract §6.3, exactly. Sorted keys and fixed separators make the bytes
    deterministic, and Ed25519 is deterministic, so re-issuing an unchanged
    lease is byte-identical — which is what makes an idempotent replay
    (contract §9.2) exact. Do not change these arguments.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode("ascii")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def sign_lease(payload: Mapping[str, Any], private_key: Ed25519PrivateKey) -> str:
    segment = _b64url_encode(serialise_payload(payload))
    signature = private_key.sign(segment.encode("ascii"))
    return f"{segment}.{_b64url_encode(signature)}"


def _as_public_key(value: Union[bytes, Ed25519PublicKey]) -> Ed25519PublicKey:
    if isinstance(value, Ed25519PublicKey):
        return value
    return Ed25519PublicKey.from_public_bytes(bytes(value))


def verify_lease(lease: str, public_keys_by_kid: Mapping[int, Union[bytes, Ed25519PublicKey]]) -> Dict[str, Any]:
    """
    Verify per contract §6.1, in its order, and return the payload. Raises
    LeaseInvalid for anything else. Not called by an endpoint until Unit 3's
    refresh; written now so the §6.4 vectors cover both directions.
    """
    # 1. Exactly two non-empty base64url segments, no padding.
    if not isinstance(lease, str) or lease.count(".") != 1:
        raise LeaseInvalid("not two segments")
    segment, signature_segment = lease.split(".")
    if not _SEGMENT.fullmatch(segment) or not _SEGMENT.fullmatch(signature_segment):
        raise LeaseInvalid("segments are not unpadded base64url")

    # 2. Read only far enough to find kid, and select its key.
    try:
        payload = json.loads(_b64url_decode(segment).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise LeaseInvalid("payload is not base64url JSON")
    if not isinstance(payload, dict):
        raise LeaseInvalid("payload is not an object")
    kid = payload.get("kid")
    if not isinstance(kid, int) or isinstance(kid, bool) or kid not in public_keys_by_kid:
        raise LeaseInvalid("unknown kid")

    # 3. Verify over the ASCII bytes of the segment exactly as transmitted.
    try:
        signature = _b64url_decode(signature_segment)
        _as_public_key(public_keys_by_kid[kid]).verify(signature, segment.encode("ascii"))
    except (InvalidSignature, binascii.Error, ValueError):
        raise LeaseInvalid("bad signature")

    # 4. Only now trust it — and only as a v1 lease.
    version = payload.get("v")
    if isinstance(version, bool) or version != LEASE_VERSION or payload.get("typ") != LEASE_TYP:
        raise LeaseInvalid("not a v1 lease")
    return payload


# ---------------------------------------------------------------------------
# The signing key — configuration, once at startup
# ---------------------------------------------------------------------------

def load_signing_key(key_b64: Optional[str], kid_raw: Optional[str]) -> Tuple[Ed25519PrivateKey, int]:
    """Parse and validate the configured key. Raises SigningKeyError with a reason."""
    text = (key_b64 or "").strip()
    if not text:
        raise SigningKeyError("LICENCE_SIGNING_KEY is not set")
    try:
        seed = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise SigningKeyError("LICENCE_SIGNING_KEY is not valid base64")
    if len(seed) != 32:
        raise SigningKeyError(f"LICENCE_SIGNING_KEY must decode to 32 bytes, got {len(seed)}")
    if seed == _TEST_SEED:
        raise SigningKeyError("LICENCE_SIGNING_KEY is the published TEST seed (contract §6.4)")

    kid_text = (kid_raw or "").strip()
    if not kid_text:
        raise SigningKeyError("LICENCE_SIGNING_KID is not set")
    if not re.fullmatch(r"-?[0-9]+", kid_text):
        raise SigningKeyError("LICENCE_SIGNING_KID must be an integer")
    kid = int(kid_text)
    if kid < 1:
        raise SigningKeyError("LICENCE_SIGNING_KID must be at least 1 (0 is the TEST key)")

    return Ed25519PrivateKey.from_private_bytes(seed), kid


# What configure_signing() found, read by every /licence/* request and by
# /health. In memory: neither reads the database or the environment per
# request.
_signing: Dict[str, Any] = {"key": None, "kid": None, "reason": "configure_signing() has not run in this process"}


def use_signing_key(private_key: Optional[Ed25519PrivateKey], kid: Optional[int], reason: Optional[str] = None) -> None:
    """Install (or, with None, remove) the signing key. Startup and tests only."""
    _signing.update(key=private_key, kid=kid, reason=None if private_key is not None else (reason or "no signing key"))


def configure_signing() -> bool:
    """
    Load the key from config. On failure log ONE ERROR and leave licensing
    unavailable; never raise. Returns whether a key is loaded.
    """
    try:
        key, kid = load_signing_key(config.LICENCE_SIGNING_KEY, config.LICENCE_SIGNING_KID)
    except SigningKeyError as exc:
        use_signing_key(None, None, reason=str(exc))
        logger.error(
            "LICENSING UNAVAILABLE — lease signing key not usable: %s. Every "
            "/licence/* endpoint returns 503; the rest of the backend is unaffected.",
            exc,
        )
        return False
    use_signing_key(key, kid)
    return True


def signing_key() -> Optional[Tuple[Ed25519PrivateKey, int]]:
    if _signing["key"] is None:
        return None
    return _signing["key"], _signing["kid"]


def signing_status() -> Dict[str, Any]:
    return {"ready": _signing["key"] is not None, "reason": _signing["reason"]}
