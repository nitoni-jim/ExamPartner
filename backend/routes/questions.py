"""
routes/questions.py — Study question HTTP routes for ExamPartner.

Business logic lives in services/question_service.py.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from services.access_control import is_admin_user, is_paid_user
from services.auth_utils import get_current_user
from services.question_service import (
    get_available_papers,
    get_filter_options,
    get_objective_questions,
    get_single_question,
    get_study_years,
    get_theory_questions,
    get_topics,
    get_subtopics,
)

router = APIRouter(tags=["questions"])


@router.get("/filters")
def filters(
    qtype: Optional[str] = Query(default=None),
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
):
    return get_filter_options(qtype=qtype, exam=exam, year=year)


@router.get("/study/years")
def study_years(
    exam: Optional[str] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    paid = (is_paid_user(user) or is_admin_user(user)) if user else False
    return get_study_years(exam=exam, subject=subject, is_paid=paid)


@router.get("/study/topics")
def study_topics(
    exam: Optional[str] = Query(default=None),
    subject: Optional[str] = Query(default=None),
):
    return get_topics(exam=exam, subject=subject)


@router.get("/study/subtopics")
def study_subtopics(
    exam: Optional[str] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    topic: Optional[str] = Query(default=None),
):
    return get_subtopics(exam=exam, subject=subject, topic=topic)


@router.get("/study/papers")
def study_papers(
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    subject: Optional[str] = Query(default=None),
):
    """
    Returns the distinct papers available for an exam/year/subject, with a
    display label and question count for each. Drives the Study mode paper
    picker (e.g. Objective / Theory / Oral English under English Language).

    paper may be null in the response for subjects/years where the paper
    column was never backfilled — Android should omit the paper filter on
    the subsequent question fetch in that case, relying on qtype alone.
    """
    return get_available_papers(exam=exam, year=year, subject=subject)


@router.get("/questions/objective")
@router.get("/questions/study")
def list_objective(
    limit: int = 20,
    offset: int = 0,
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    topic: Optional[str] = Query(default=None),
    subtopic: Optional[str] = Query(default=None),
    paper: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    paid = is_paid_user(user) or is_admin_user(user)
    return get_objective_questions(
        limit=limit, offset=offset,
        exam=exam, year=year, subject=subject,
        is_paid=paid, topic=topic, subtopic=subtopic,
        paper=paper,
    )


@router.get("/questions/theory")
def list_theory(
    limit: int = 20,
    offset: int = 0,
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    topic: Optional[str] = Query(default=None),
    subtopic: Optional[str] = Query(default=None),
    paper: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    paid = is_paid_user(user) or is_admin_user(user)
    return get_theory_questions(
        limit=limit, offset=offset,
        exam=exam, year=year, subject=subject,
        is_paid=paid, topic=topic, subtopic=subtopic,
        paper=paper,
    )


@router.get("/question/{qid}")
def get_question(
    qid: str,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    return get_single_question(qid)
