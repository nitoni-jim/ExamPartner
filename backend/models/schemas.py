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
