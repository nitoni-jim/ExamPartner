"""
routes/theory.py — AI theory grading route for ExamPartner.

Business logic lives in services/theory_service.py.

Endpoint:
  POST /theory/grade
    Request:  { "question_id": str,
                "student_answer": str | list[dict] }
      student_answer is either a plain string (legacy — English essay/
      comprehension/summary, or any question with no sub_questions) or the
      structured sub_answers payload (locked spec
      ExamPartner_Spec_Editable_Table_Submission.docx §2.2): a list of
      {"label", "type", "answer"|"rows", "table_key"?} objects, one per
      sub-question, built by Android's buildSubAnswersPayload(). Validation
      and normalization of either shape happens inside grade_theory() via
      _normalize_sub_answers()/_sub_answers_are_blank() — this route no
      longer does its own string-only blank check, since that would break
      on the list shape.
    Response: grading result JSON + usage block
    Errors:
      401 — not authenticated
      400 — missing/empty fields, question not gradeable
      404 — question not found
      429 — usage limit reached
      503 — ANTHROPIC_API_KEY not configured
      502 — Claude error or invalid response
"""
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from services.auth_utils import get_current_user
from services.theory_service import grade_theory, get_ai_grading_quota

router = APIRouter(tags=["theory"])


class GradeRequest(BaseModel):
    question_id: str
    # Union order matters for Pydantic: str is tried first, so a JSON array
    # payload (which cannot match str) correctly falls through to the list
    # branch rather than being coerced/rejected against str first.
    student_answer: Union[str, List[Dict[str, Any]]]


@router.get("/ai-grading/quota")
def get_quota_route(
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Returns the current AI theory grading quota for the authenticated user.

    Response:
      {
        "ok": true,
        "monthly_used": 3,
        "monthly_limit": 10,
        "monthly_remaining": 7,
        "extra_credits_remaining": 50,
        "plan": "core"
      }
    """
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    identifier = user.get("sub")
    if not identifier:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return get_ai_grading_quota(identifier=identifier)


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

    question_id = (body.question_id or "").strip()
    if not question_id:
        raise HTTPException(status_code=400, detail="question_id is required.")

    # student_answer is intentionally NOT .strip()'d or blank-checked here —
    # it may be a list, which has no .strip(). grade_theory() normalizes
    # either shape via _normalize_sub_answers() and raises 400 itself via
    # _sub_answers_are_blank() if the (normalized) answer is empty.
    return grade_theory(
        identifier=identifier,
        question_id=question_id,
        student_answer=body.student_answer,
    )
