"""
routes/licensing.py — Windows seat-pool licensing endpoints.

Client contract (docs/LICENCE_API_CONTRACT.md):
  POST /licence/activation-session   owner/admin credentials → 15-minute token
  POST /licence/activate             activation token → lease
  POST /licence/refresh              the signed lease → a renewed lease (Unit 3a)

School owner, ordinary bearer token (Unit 3a, brief §4.I I7):
  POST  /institutions/seats/{binding_id}/release

Pilot setup, ExamPartner admin only, NOT part of the client contract
(brief §4.F F11):
  POST  /institutions/accounts
  POST  /institutions/accounts/{account_id}/seat_pools
  PATCH /institutions/accounts/{account_id}/subscription

Support, ExamPartner admin only (Unit 3a, brief §4.I I5, I8):
  GET   /support/licensing/ambiguities
  POST  /support/licensing/ambiguities/{ambiguity_id}/resolve

Request models declare TYPES only. Everything semantic — UUID v4, label
length, the fingerprint shape — is checked in the service, so that the
contract's error order holds (contract §5): FastAPI's own 422 for a body
that does not match the types comes first, then activation_session_invalid,
then client_upgrade_required, then invalid_request, and so on.

The activation token is read from the Authorization header INSIDE the route
rather than through a Depends() (brief §4.H H8). A dependency's 401 would be
raised while FastAPI is still resolving the request, before the body's 422,
which reverses the contract's order.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

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
# School owner (brief §4.I I7)
# ---------------------------------------------------------------------------

@router.post("/institutions/seats/{binding_id}/release")
def release_seat(binding_id: str, body: ReleaseBody, user=Depends(get_current_user)):
    # An ordinary ExamPartner bearer token. A lease is not one: read_token()
    # refuses it, so it arrives here as no user at all (spec §12 test 28).
    if user is None:
        licence_fail("not_authenticated")
    return licensing_service.release_seat(user, binding_id, body.model_dump())


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
