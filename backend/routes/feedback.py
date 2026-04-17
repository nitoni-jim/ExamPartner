"""
routes/feedback.py — user feedback routes for ExamPartner.
"""
import secrets
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from config import db_conn
from models.schemas import PlatformFeedbackReq, QuestionFeedbackReq
from services.auth_utils import get_current_user

router = APIRouter(tags=["feedback"])


def _require_value(value: Optional[str], field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    return normalized


@router.post("/feedback/platform")
def submit_platform_feedback(
    body: PlatformFeedbackReq,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    feedback_id = secrets.token_hex(16)
    category = _require_value(body.category, "category")
    message = _require_value(body.message, "message")
    source_area = (body.source_area or "footer").strip() or "footer"
    user_identifier = user.get("sub") if user else None

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO feedback (id, feedback_type, question_id, source_area, category, message, user_identifier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (feedback_id, "platform", None, source_area, category, message, user_identifier),
        )
        db.commit()
    finally:
        db.close()

    return {"ok": True, "id": feedback_id}


@router.post("/feedback/question")
def submit_question_feedback(
    body: QuestionFeedbackReq,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    feedback_id = secrets.token_hex(16)
    question_id = _require_value(body.question_id, "question_id")
    category = _require_value(body.category, "category")
    message = _require_value(body.message, "message")
    source_area = (body.source_area or "").strip() or "question_detail"
    user_identifier = user.get("sub") if user else None

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO feedback (id, feedback_type, question_id, source_area, category, message, user_identifier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (feedback_id, "question", question_id, source_area, category, message, user_identifier),
        )
        db.commit()
    finally:
        db.close()

    return {"ok": True, "id": feedback_id}
