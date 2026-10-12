"""
routes/licensing.py — Windows seat-pool licensing endpoints.

Client contract (docs/LICENCE_API_CONTRACT.md):
  POST /licence/activation-session   owner/admin credentials → 15-minute token
  POST /licence/activate             activation token → lease
(POST /licence/refresh is Unit 3.)

Pilot setup, ExamPartner admin only, NOT part of the client contract
(brief §4.F F11):
  POST  /institutions/accounts
  POST  /institutions/accounts/{account_id}/seat_pools
  PATCH /institutions/accounts/{account_id}/subscription

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
from services.access_control import require_admin
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
