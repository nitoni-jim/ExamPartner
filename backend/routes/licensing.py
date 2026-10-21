"""
routes/licensing.py — Windows seat-pool licensing endpoints.

Client contract (docs/LICENCE_API_CONTRACT.md):
  POST /licence/activation-session   owner/admin credentials → 15-minute token
  POST /licence/activate             activation token → lease
  POST /licence/refresh              the signed lease → a renewed lease (Unit 3a)

School owner — licence session token or ordinary bearer token (contract
v1.1 §10A; brief §4.I I7, §4.K K1–K4):
  GET   /institutions/accounts/{account_id}/seat_pools
  GET   /institutions/accounts/{account_id}/machines
  GET   /institutions/accounts/{account_id}/history
  PATCH /institutions/seats/{binding_id}
  POST  /institutions/seats/{binding_id}/release

Pilot setup, ExamPartner admin only, NOT part of the client contract
(brief §4.F F11):
  POST  /institutions/accounts
  POST  /institutions/accounts/{account_id}/seat_pools
  PATCH /institutions/accounts/{account_id}/subscription

Account and pool administration, ExamPartner admin only, ordinary bearer
token only (Unit 3b, brief §4.K K5, K6):
  PATCH /institutions/accounts/{account_id}
  PATCH /institutions/seat_pools/{pool_id}

Support, ExamPartner admin only (Unit 3a, brief §4.I I5, I8; Unit 3b, K8, K9):
  GET   /support/licensing/ambiguities
  POST  /support/licensing/ambiguities/{ambiguity_id}/resolve
  GET   /support/licensing/signals
  POST  /support/licensing/test-clock

Request models declare TYPES only. Everything semantic — UUID v4, label
length, the fingerprint shape — is checked in the service, so that the
contract's error order holds (contract §5): FastAPI's own 422 for a body
that does not match the types comes first, then activation_session_invalid,
then client_upgrade_required, then invalid_request, and so on.

The activation token is read from the Authorization header INSIDE the route
rather than through a Depends() (brief §4.H H8). A dependency's 401 would be
raised while FastAPI is still resolving the request, before the body's 422,
which reverses the contract's order. The owner routes do the same through
_licence_caller(), and their query parameters are plain optional strings, so
a malformed `limit` is 400 invalid_request after the 401, never a 422.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

import config

from routes.auth import _verify_password
from services import licensing_service
from services.access_control import is_admin_user, require_admin
from services.auth_utils import get_current_user
from services.licence_session import verify_activation_token
from services.licence_wire import licence_fail

router = APIRouter(tags=["licensing"])


# ---------------------------------------------------------------------------
# Request models — types only (see module docstring)
# ---------------------------------------------------------------------------

class ClientInfo(BaseModel):
    platform: str
    app_version: str
    os_version: Optional[str] = None


class ActivationSessionBody(BaseModel):
    identifier: str
    password: str
    client: ClientInfo


class ActivateBody(BaseModel):
    request_id: str
    pool_id: str
    machine_label: str
    installation_id: Optional[str] = None
    ambiguity_id: Optional[str] = None
    fingerprint_signals: Dict[str, Any]
    client: ClientInfo


class RefreshBody(BaseModel):
    request_id: str
    lease: str
    ambiguity_id: Optional[str] = None
    fingerprint_signals: Dict[str, Any]
    client: ClientInfo


# Release and resolve declare every field optional and untyped (brief §4.J
# J6): a missing request_id must reach the service as 400 invalid_request
# AFTER the 401/403 checks (I5, I7), not be pre-empted by FastAPI's 422.
class ReleaseBody(BaseModel):
    request_id: Optional[Any] = None
    revoke_reason: Optional[Any] = None
    reason: Optional[Any] = None


class ResolveBody(BaseModel):
    request_id: Optional[Any] = None
    decision: Optional[Any] = None
    binding_id: Optional[Any] = None


class RenameBody(BaseModel):
    request_id: Optional[Any] = None
    machine_label: Optional[Any] = None


# The Unit 3b admin bodies likewise: every field optional and untyped, so
# that 401 → admin_required → invalid_request holds (brief §4.L L1).
class AccountEditBody(BaseModel):
    request_id: Optional[Any] = None
    name: Optional[Any] = None
    contact_email: Optional[Any] = None
    owner_identifier: Optional[Any] = None


class PoolEditBody(BaseModel):
    request_id: Optional[Any] = None
    pool_size: Optional[Any] = None
    label: Optional[Any] = None
    status: Optional[Any] = None


class TestClockBody(BaseModel):
    offset_seconds: Optional[Any] = None
    reset: Optional[Any] = None


class CreateAccountBody(BaseModel):
    name: str
    owner_identifier: str
    contact_email: Optional[str] = None
    account_type: Optional[str] = "institution"


class CreatePoolBody(BaseModel):
    pool_size: int
    label: Optional[str] = None


class SubscriptionBody(BaseModel):
    subscription_status: str
    subscription_expires_at: str


# ---------------------------------------------------------------------------
# Client contract
# ---------------------------------------------------------------------------

@router.post("/licence/activation-session")
def activation_session(body: ActivationSessionBody):
    # Order (brief §4.H H8): version → validation → licensing_unavailable →
    # credentials → no_licensing_access.
    licensing_service.check_client(body.client.model_dump())
    identifier = licensing_service.validate_session_input(body.identifier, body.password)
    licensing_service.require_licensing_available()
    # The password is verified exactly as /auth/login verifies it — but
    # register_device() is NOT called: a lab PC must not take one of the
    # owner's Android device slots (contract §11).
    if not _verify_password(identifier, body.password):
        licence_fail("invalid_credentials")
    return licensing_service.open_activation_session(identifier)


@router.post("/licence/activate")
def activate(body: ActivateBody, authorization: Optional[str] = Header(default=None)):
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    # An ordinary ExamPartner login token fails here: it is signed with a
    # different key over different bytes (services/licence_session.py).
    session = verify_activation_token(token)
    if session is None:
        licence_fail("activation_session_invalid")
    return licensing_service.activate(session["sub"], body.model_dump())


@router.post("/licence/refresh")
def refresh(body: RefreshBody):
    # No Authorization header, and get_current_user() is never called: the
    # signed lease in the body is the authorisation (contract §9, brief I1).
    return licensing_service.refresh(body.model_dump())


# ---------------------------------------------------------------------------
# School owner (brief §4.I I7, §4.K K1–K4; contract v1.1 §10A)
# ---------------------------------------------------------------------------

def _licence_caller(authorization: Optional[str]) -> Dict[str, str]:
    """
    The ONE caller check for the five owner routes (K1). Accepts a licence
    session token — the 15-minute token from /licence/activation-session,
    verified exactly as /licence/activate verifies it — or an ordinary
    ExamPartner bearer token through get_current_user(), the same check every
    bearer route uses. Anything else is 401 not_authenticated.

    Returns ONLY {"sub": identifier}. The session token's role is dropped
    here so nothing downstream can trust it: ownership and admin status are
    re-derived on every call (F7), so a changed owner or a removed admin
    takes effect on the next call.

    The scope rule runs the other way too, and is why this exists at all:
    get_current_user() returns None for a session token (its signature is
    over different bytes with a derived key, services/licence_session.py), so
    on every route that is not one of these five the token is anonymous. Do
    not "simplify" that by teaching get_current_user() about it.

    Called as the first statement of the route, after FastAPI has parsed
    the request, so its 401 never pre-empts the 422 (H8). A lease is neither
    token kind, so it is refused here too (spec §12 test 28).
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    session = verify_activation_token(token)
    if session is not None:
        return {"sub": session["sub"]}
    user = get_current_user(authorization)
    if user is not None and str(user.get("sub") or "").strip():
        return {"sub": str(user["sub"])}
    licence_fail("not_authenticated")


@router.get("/institutions/accounts/{account_id}/seat_pools")
def owner_pools(account_id: str, authorization: Optional[str] = Header(default=None)):
    caller = _licence_caller(authorization)
    return licensing_service.list_owner_pools(caller, account_id)


@router.get("/institutions/accounts/{account_id}/machines")
def owner_machines(account_id: str, pool_id: Optional[str] = None,
                   authorization: Optional[str] = Header(default=None)):
    caller = _licence_caller(authorization)
    return licensing_service.list_owner_machines(caller, account_id, pool_id=pool_id)


@router.get("/institutions/accounts/{account_id}/history")
def owner_history(account_id: str, pool_id: Optional[str] = None, binding_id: Optional[str] = None,
                  limit: Optional[str] = None, cursor: Optional[str] = None,
                  authorization: Optional[str] = Header(default=None)):
    caller = _licence_caller(authorization)
    return licensing_service.list_owner_history(caller, account_id, pool_id=pool_id, binding_id=binding_id,
                                                limit=limit, cursor=cursor)


@router.patch("/institutions/seats/{binding_id}")
def rename_seat(binding_id: str, body: RenameBody, authorization: Optional[str] = Header(default=None)):
    caller = _licence_caller(authorization)
    return licensing_service.rename_seat(caller, binding_id, body.model_dump())


@router.post("/institutions/seats/{binding_id}/release")
def release_seat(binding_id: str, body: ReleaseBody, authorization: Optional[str] = Header(default=None)):
    # Either token kind (K4); release itself is exactly as Unit 3a built it.
    caller = _licence_caller(authorization)
    return licensing_service.release_seat(caller, binding_id, body.model_dump())


# ---------------------------------------------------------------------------
# Account and pool administration — ExamPartner admin (brief §4.K K5, K6)
# ---------------------------------------------------------------------------

def _require_admin_coded(user: Optional[Dict[str, Any]]) -> str:
    """
    401 not_authenticated → 403 admin_required, in the contract §9.3 body, as
    the resolve route does (J4, L1). An ordinary bearer token only: a
    licence session token arrives here as no user (get_current_user()
    returns None for it), so it is 401 like no token at all (L2).
    """
    if user is None:
        licence_fail("not_authenticated")
    if not is_admin_user(user):
        licence_fail("admin_required")
    return str(user.get("sub") or "").strip().lower()


@router.patch("/institutions/accounts/{account_id}")
def edit_account(account_id: str, body: AccountEditBody, user=Depends(get_current_user)):
    admin = _require_admin_coded(user)
    return licensing_service.update_account(admin, account_id, body.model_dump())


@router.patch("/institutions/seat_pools/{pool_id}")
def edit_seat_pool(pool_id: str, body: PoolEditBody, user=Depends(get_current_user)):
    admin = _require_admin_coded(user)
    return licensing_service.update_pool(admin, pool_id, body.model_dump())


# ---------------------------------------------------------------------------
# Pilot setup — ExamPartner admin (brief F11)
# ---------------------------------------------------------------------------

@router.post("/institutions/accounts")
def create_account(body: CreateAccountBody, user=Depends(get_current_user)):
    require_admin(user)
    return licensing_service.create_account(
        name=body.name, owner_identifier=body.owner_identifier,
        contact_email=body.contact_email, account_type=body.account_type,
    )


@router.post("/institutions/accounts/{account_id}/seat_pools")
def create_seat_pool(account_id: str, body: CreatePoolBody, user=Depends(get_current_user)):
    require_admin(user)
    return licensing_service.create_seat_pool(account_id, pool_size=body.pool_size, label=body.label)


@router.patch("/institutions/accounts/{account_id}/subscription")
def set_subscription(account_id: str, body: SubscriptionBody, user=Depends(get_current_user)):
    admin = require_admin(user)
    return licensing_service.set_subscription(
        account_id, subscription_status=body.subscription_status,
        subscription_expires_at=body.subscription_expires_at, performed_by=admin,
    )


# ---------------------------------------------------------------------------
# Support — ExamPartner admin (brief §4.I I5, I8)
# ---------------------------------------------------------------------------

@router.get("/support/licensing/ambiguities")
def list_ambiguities(status: Optional[str] = "open", user=Depends(get_current_user)):
    # Plain require_admin(), like the pilot setup endpoints (brief §4.J J4).
    require_admin(user)
    return licensing_service.list_ambiguities(status)


@router.post("/support/licensing/ambiguities/{ambiguity_id}/resolve")
def resolve_ambiguity(ambiguity_id: str, body: ResolveBody, user=Depends(get_current_user)):
    # The contract §9.3 body here, unlike the list (I5, J4).
    if user is None:
        licence_fail("not_authenticated")
    if not is_admin_user(user):
        licence_fail("admin_required")
    admin = str(user.get("sub") or "").strip().lower()
    return licensing_service.resolve_ambiguity(admin, ambiguity_id, body.model_dump())


@router.get("/support/licensing/signals")
def list_signals(action: Optional[str] = None, account_id: Optional[str] = None, since: Optional[str] = None,
                 limit: Optional[str] = None, cursor: Optional[str] = None, user=Depends(get_current_user)):
    _require_admin_coded(user)
    return licensing_service.list_signals(action=action, account_id=account_id, since=since, limit=limit,
                                          cursor=cursor)


@router.post("/support/licensing/test-clock")
def test_clock(body: TestClockBody, user=Depends(get_current_user)):
    # Order (K9): 401 → admin_required → test_clock_disabled → the body.
    # ALLOW_TEST_CLOCK is read at call time; it stays absent on Render.
    _require_admin_coded(user)
    if not config.ALLOW_TEST_CLOCK:
        licence_fail("test_clock_disabled")
    return licensing_service.set_test_clock(body.model_dump())
