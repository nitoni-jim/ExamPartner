"""
routes/cbt.py — CBT HTTP routes for ExamPartner.

Business logic lives in services/cbt_service.py.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from services.access_control import is_admin_user, is_paid_user
from services.auth_utils import get_current_user
from services.cbt_service import fetch_cbt_questions, fetch_cbt_theory_paper, get_cbt_papers, get_founding_status

router = APIRouter(tags=["cbt"])


@router.get("/founding/status")
def founding_status():
    """Returns whether Founding (₦1,000) is still open for NEW users."""
    return get_founding_status()


@router.get("/cbt/papers")
def cbt_papers(
    subject: str = Query(...),
    exam: str = Query(default="JAMB"),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Returns the distinct papers available for a subject's CBT pool
    (e.g. Objective / Theory / Oral English under English Language),
    each with a display label, question count, and duration in minutes.

    Year-agnostic by design — CBT never picks a single year the way Study
    mode does, so this is a separate endpoint from /study/papers, not a
    reuse of it. Scoped to exactly the same access tier the question fetch
    (/cbt/questions) draws from:
      - Free users: only the free year's pool. Never reveals paid-only
        papers or counts from locked years.
      - Paid/admin: the full pooled set across all years.
    """
    subject = (subject or "").strip()
    exam = (exam or "JAMB").strip()

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required.")

    if not user:
        raise HTTPException(status_code=401, detail="Authentication required for CBT.")

    paid = is_paid_user(user) or is_admin_user(user)

    return get_cbt_papers(subject=subject, exam=exam, is_paid=paid)


@router.get("/cbt/questions")
def cbt_questions(
    subject: str = Query(...),
    exam: str = Query(default="JAMB"),
    paper: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Returns a shuffled, deduplicated set of objective questions for one subject.
    - Paid users: pools ALL years.
    - Free users: oldest year only.
    - Deduplicates by exact question_text.
    - Caps at 60 for Use of English, 40 for all other subjects.
    - paper: optional discriminator within a subject (e.g. "Oral English"
      under "English Language"). Omitted = no change to existing behaviour.
    """
    subject = (subject or "").strip()
    exam = (exam or "JAMB").strip()
    paper = (paper or "").strip() or None

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required.")

    if not user:
        raise HTTPException(status_code=401, detail="Authentication required for CBT.")

    paid = is_paid_user(user) or is_admin_user(user)

    return fetch_cbt_questions(subject=subject, exam=exam, is_paid=paid, paper=paper)


@router.get("/cbt/theory-questions")
def cbt_theory_questions(
    subject: str = Query(...),
    exam: str = Query(default="WAEC"),
    paper: str = Query(...),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Returns the section-grouped Theory CBT pool for one paper — gradeable
    questions only, grouped by paper_rules.rules_json section structure.

    Distinct from GET /questions/theory (Study mode): that endpoint returns
    every Theory question including ungradeable ones (Study mode is for
    reading/learning, not scored submission) and is year-scoped. This
    endpoint is CBT-only, gradeable-only, and pools across years at the
    section level, not the paper level — each question carries its own
    sub-questions intact, so pooling Q1(2020) with Q3(2019) within the same
    section is coherent (Theory CBT Section-Aware Implementation v2).

    sections: [] in the response means no paper_rules.rules_json exists yet
    for this exam+subject+paper — Android must fall back to the existing
    flat-list Theory session behaviour, not treat this as an error.
    """
    subject = (subject or "").strip()
    exam = (exam or "WAEC").strip()
    paper = (paper or "").strip()

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required.")
    if not paper:
        raise HTTPException(status_code=400, detail="paper is required.")
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required for CBT.")

    paid = is_paid_user(user) or is_admin_user(user)

    return fetch_cbt_theory_paper(exam=exam, subject=subject, paper=paper, is_paid=paid)
