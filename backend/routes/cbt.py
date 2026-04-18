"""
routes/cbt.py — CBT HTTP routes for ExamPartner.

Business logic lives in services/cbt_service.py.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from services.access_control import is_admin_user, is_paid_user
from services.auth_utils import get_current_user
from services.cbt_service import fetch_cbt_questions, get_founding_status

router = APIRouter(tags=["cbt"])


@router.get("/founding/status")
def founding_status():
    """Returns whether Founding (₦1,000) is still open for NEW users."""
    return get_founding_status()


@router.get("/cbt/questions")
def cbt_questions(
    subject: str = Query(...),
    exam: str = Query(default="JAMB"),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Returns a shuffled, deduplicated set of objective questions for one subject.
    - Paid users: pools ALL years.
    - Free users: oldest year only.
    - Deduplicates by exact question_text.
    - Caps at 60 for Use of English, 40 for all other subjects.
    """
    subject = (subject or "").strip()
    exam = (exam or "JAMB").strip()

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required.")

    if not user:
        raise HTTPException(status_code=401, detail="Authentication required for CBT.")

    paid = is_paid_user(user) or is_admin_user(user)

    return fetch_cbt_questions(subject=subject, exam=exam, is_paid=paid)
