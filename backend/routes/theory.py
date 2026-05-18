"""
routes/theory.py — AI theory grading route for ExamPartner.

Business logic lives in services/theory_service.py.

Endpoint:
  POST /theory/grade
    Request:  { "question_id": str, "student_answer": str }
    Response: grading result JSON + usage block
    Errors:
      401 — not authenticated
      400 — missing/empty fields, question not gradeable
      404 — question not found
      429 — usage limit reached
      503 — ANTHROPIC_API_KEY not configured
      502 — Claude error or invalid response
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.auth_utils import get_current_user
from services.theory_service import grade_theory

router = APIRouter(tags=["theory"])


class GradeRequest(BaseModel):
    question_id:    str
    student_answer: str


@router.post("/theory/grade")
def grade_theory_route(
    body: GradeRequest,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required for AI theory grading.")

    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Authentication required for AI theory grading.")

    question_id    = (body.question_id or "").strip()
    student_answer = (body.student_answer or "").strip()

    if not question_id:
        raise HTTPException(status_code=400, detail="question_id is required.")
    if not student_answer:
        raise HTTPException(status_code=400, detail="student_answer cannot be empty.")

    return grade_theory(
        identifier=identifier,
        question_id=question_id,
        student_answer=student_answer,
    )
