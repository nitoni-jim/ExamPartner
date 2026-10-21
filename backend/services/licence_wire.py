"""
services/licence_wire.py — the wire format shared by every /licence/* route
and claim_capacity(): the error body and the timestamp format.

docs/LICENCE_API_CONTRACT.md fixes both, and the Windows client is built
against that page by a different implementer who cannot see this code. Two
helpers, used everywhere, so the shape cannot drift between routes:

- licence_error() raises HTTPException with the contract §9.3 body. FastAPI
  renders HTTPException(detail={...}) as {"detail": {...}}, which is exactly
  the contract's shape, so no custom exception handler is needed.
- wire_time() renders a timestamp as RFC 3339, UTC, whole seconds, literal Z
  (contract §2). licensing_time.now_iso() produces "+00:00" with
  microseconds; that format must never reach the wire.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, NoReturn, Optional, Tuple, Union

from fastapi import HTTPException

from services import licensing_time


def truncate_seconds(value: datetime) -> datetime:
    """Aware UTC datetime with microseconds dropped (floor, never round up)."""
    return licensing_time.to_datetime(value).replace(microsecond=0)


def wire_time(value: Union[str, datetime, None] = None) -> str:
    """
    `2026-10-08T09:00:00Z`. With no argument, the licensing clock's now —
    which is what every response's server_time uses.
    """
    dt = licensing_time.now() if value is None else licensing_time.to_datetime(value)
    return truncate_seconds(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wire_time_or_none(value: Union[str, datetime, None]) -> Optional[str]:
    return None if value is None or value == "" else wire_time(value)


def licence_error(status: int, code: str, message: str, retryable: bool, **extra: Any) -> NoReturn:
    """
    Raise the contract §9.3 error. `extra` carries the per-code fields the
    contract lists (pool_size and active for pool_full, field for
    invalid_request, and so on). server_time is always added last, so a
    caller cannot accidentally override it.
    """
    detail = {"code": code, "message": message, "retryable": retryable}
    detail.update(extra)
    detail["server_time"] = wire_time()
    raise HTTPException(status_code=status, detail=detail)


# Contract §9.3: code → (HTTP status, message safe to show, retryable).
# Messages are the contract's own wording where it gives one. One table, so
# a code can never be sent with the wrong status or retry flag.
#
# ambiguity_pending's "retryable" column in the contract reads "later", not
# yes or no. It is sent as false: the client must NOT retry it with backoff;
# it stores the ambiguity_id and offers "Check again" (contract §10).
LICENCE_ERRORS = {
    "invalid_request": (400, "Something went wrong — contact ExamPartner.", False),
    "fingerprint_unreadable": (400, "Windows could not report this PC's hardware. Restart the PC, check the Windows Management Instrumentation service, and try again.", False),
    "invalid_credentials": (401, "Email or password is incorrect.", False),
    "activation_session_invalid": (401, "Your sign-in has expired. Sign in again to activate.", False),
    "subscription_inactive": (402, "This school's ExamPartner subscription has ended. Contact the school administrator.", False),
    "no_licensing_access": (403, "This account does not manage any ExamPartner computers.", False),
    "pool_access_denied": (403, "This account cannot activate computers in that lab.", False),
    "pool_inactive": (409, "This lab's licence pool is suspended. Contact ExamPartner.", False),
    "pool_full": (409, "All seats in this lab are in use. Release a computer that is no longer used, or add seats.", False),
    "ambiguity_pending": (409, "ExamPartner needs to confirm this computer's identity. This usually takes up to one working day.", False),
    "ambiguity_expired": (409, "This identity check went unresolved for 7 days. Start again.", False),
    "ambiguity_consumed": (409, "This identity check has already been collected.", False),
    "ambiguity_mismatch": (409, "This identity check belongs to a different computer. Start again.", False),
    "request_id_reused": (409, "This request ID was already used for a different request.", False),
    # Unit 3a — refresh (contract §9.3).
    "lease_invalid": (403, "This licence could not be verified. Activate this computer again.", False),
    "lease_revoked": (403, "This computer's licence was released by the school. An administrator must activate it again, which uses a seat.", False),
    "machine_mismatch": (403, "This licence belongs to a different computer.", False),
    # Unit 3a — release and the admin resolve route (brief §4.I I5, I7,
    # §4.J J4). Not yet in the contract: Unit 3b writes them into v1.1's
    # §9.3 exactly as they are here.
    "not_authenticated": (401, "Sign in to continue.", False),
    "admin_required": (403, "ExamPartner admin access required.", False),
    "binding_access_denied": (403, "This account cannot manage that computer.", False),
    "ambiguity_not_found": (404, "No such identity check.", False),
    "ambiguity_already_resolved": (409, "This identity check has already been resolved.", False),
    "ambiguity_candidate_invalid": (409, "That activation is not an active candidate for this identity check.", False),
    "client_upgrade_required": (426, "Update ExamPartner to continue.", False),
    "internal_error": (500, "Something went wrong. Try again.", True),
    "licensing_unavailable": (503, "Licensing is temporarily unavailable. Try again later.", True),
}


def licence_fail(code: str, message: Optional[str] = None, **extra: Any) -> NoReturn:
    """licence_error() with the status, message and retry flag from LICENCE_ERRORS."""
    status, default_message, retryable = LICENCE_ERRORS[code]
    licence_error(status, code, message or default_message, retryable, **extra)


def licence_detail(code: str, message: Optional[str] = None, **extra: Any) -> Tuple[int, Dict[str, Any]]:
    """
    The (status, detail) licence_fail() would raise, WITHOUT server_time and
    without raising — for an outcome the request history stores so a replay
    can return it (brief §4.J J5, J8). raise_stored() raises it later.
    """
    status, default_message, retryable = LICENCE_ERRORS[code]
    detail = {"code": code, "message": message or default_message, "retryable": retryable}
    detail.update(extra)
    return status, detail


def raise_stored(status: int, detail: Mapping[str, Any]) -> NoReturn:
    """Raise a stored error detail as it was, with the server_time of THIS response (J5)."""
    detail = dict(detail)
    detail.pop("server_time", None)
    detail["server_time"] = wire_time()
    raise HTTPException(status_code=status, detail=detail)
