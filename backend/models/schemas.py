"""
models/schemas.py — Pydantic request/response models for ExamPartner.
"""
from typing import List, Optional
from pydantic import BaseModel


class LoginReq(BaseModel):
    identifier: str
    password: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    platform: Optional[str] = None


class RegisterReq(BaseModel):
    identifier: str
    password: str
    full_name: Optional[str] = None
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    platform: Optional[str] = None


class RefreshReq(BaseModel):
    device_id: Optional[str] = None


# Backwards-compatible alias
AuthReq = LoginReq


class AuthResp(BaseModel):
    token: str
    identifier: str
    is_paid: bool
    is_admin: bool = False


class PlatformFeedbackReq(BaseModel):
    category: str
    message: str
    source_area: str = "footer"


class QuestionFeedbackReq(BaseModel):
    question_id: str
    category: str
    message: str
    source_area: Optional[str] = None


class StudyTopicsResponse(BaseModel):
    exam: str
    subject: str
    topics: List[str]


class StudySubtopicsResponse(BaseModel):
    exam: str
    subject: str
    topic: str
    subtopics: List[str]


class DeviceResp(BaseModel):
    device_id: str
    device_name: Optional[str] = None
    platform: Optional[str] = None
    created_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    is_current_device: bool = False


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

class ForgotPasswordReq(BaseModel):
    """
    Step 1 — request a reset code.
    identifier: the user's login identifier (email or phone).
    Always returns a generic 200. Never reveals whether the account exists
    or whether it has a linked email.
    """
    identifier: str


class ResetPasswordReq(BaseModel):
    """
    Step 2 — redeem the reset code and set a new password.
    identifier:   same login identifier used in ForgotPasswordReq.
    code:         the 6-digit numeric code sent to the user's email.
    new_password: minimum 4 characters (matches registration requirement).
    """
    identifier:   str
    code:         str
    new_password: str
