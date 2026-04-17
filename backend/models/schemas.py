"""
models/schemas.py — Pydantic request/response models for ExamPartner.
"""
from typing import Optional
from pydantic import BaseModel


class LoginReq(BaseModel):
    identifier: str
    password: str


class RegisterReq(BaseModel):
    identifier: str
    password: str
    full_name: Optional[str] = None


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
