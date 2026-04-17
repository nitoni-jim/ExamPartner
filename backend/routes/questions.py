"""
routes/questions.py — study (objective + theory) question routes for ExamPartner.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from config import db_conn
from services.access_control import (
    get_free_year_for_subject,
    is_admin_user,
    is_paid_user,
)
from services.auth_utils import get_current_user
from services.question_utils import (
    QUESTION_SELECT_COLS,
    build_filters,
    build_passage_lookup,
    row_get,
    row_to_question,
    sort_theory_rows,
)

router = APIRouter(tags=["questions"])


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

@router.get("/filters")
def filters(
    qtype: Optional[str] = Query(default=None),
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
):
    where: List[str] = []
    params: List[Any] = []

    if qtype:
        where.append("qtype = ?")
        params.append(qtype)
    if exam:
        where.append("exam = ?")
        params.append(exam)
    if year is not None:
        where.append("year = ?")
        params.append(year)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    db = db_conn()
    cur = db.cursor()

    cur.execute(
        f"""SELECT DISTINCT exam FROM questions
        {('WHERE qtype = ?' if qtype else '')}
        AND exam IS NOT NULL AND TRIM(exam) <> ''""" if qtype else
        """SELECT DISTINCT exam FROM questions
        WHERE exam IS NOT NULL AND TRIM(exam) <> ''""",
        (qtype,) if qtype else None,
    )
    exams = sorted([r["exam"] for r in cur.fetchall() if r.get("exam")])

    where_y: List[str] = []
    params_y: List[Any] = []
    if qtype:
        where_y.append("qtype = ?")
        params_y.append(qtype)
    if exam:
        where_y.append("exam = ?")
        params_y.append(exam)
    where_y_sql = ("WHERE " + " AND ".join(where_y)) if where_y else ""

    cur.execute(
        f"""SELECT DISTINCT year FROM questions
        {where_y_sql}
        {'AND' if where_y_sql else 'WHERE'} year IS NOT NULL""",
        tuple(params_y) if params_y else None,
    )
    years = sorted(
        [int(r["year"]) for r in cur.fetchall() if r.get("year") is not None],
        reverse=True,
    )

    cur.execute(
        f"""SELECT DISTINCT subject FROM questions
        {where_sql}
        {'AND' if where_sql else 'WHERE'} subject IS NOT NULL AND TRIM(subject) <> ''""",
        tuple(params) if params else None,
    )
    subjects = sorted([r["subject"] for r in cur.fetchall() if r.get("subject")])
    db.close()

    return {"ok": True, "exams": exams, "years": years, "subjects": subjects}


@router.get("/study/years")
def study_years(
    exam: Optional[str] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    """
    Returns all available years for a subject plus the oldest (free) year.
    Frontend uses this to render the year selector with lock icons.
    """
    where: List[str] = ["year IS NOT NULL"]
    params: List[Any] = []

    if exam:
        where.append("exam = ?")
        params.append(exam)
    if subject:
        where.append("subject = ?")
        params.append(subject)

    where_sql = "WHERE " + " AND ".join(where)

    db = db_conn()
    cur = db.cursor()
    cur.execute(
        f"SELECT DISTINCT year FROM questions {where_sql} AND TRIM(CAST(year AS TEXT)) <> ''",
        tuple(params) if params else None,
    )
    all_years = sorted(
        [int(r["year"]) for r in cur.fetchall() if r.get("year") is not None],
        reverse=True,
    )
    free_year = get_free_year_for_subject(db, exam, subject)
    db.close()

    paid = (is_paid_user(user) or is_admin_user(user)) if user else False

    return {"ok": True, "years": all_years, "free_year": free_year, "is_paid": paid}


# ---------------------------------------------------------------------------
# Objective (Study mode)
# ---------------------------------------------------------------------------

@router.get("/questions/objective")
@router.get("/questions/study")
def list_objective(
    limit: int = 20,
    offset: int = 0,
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    paid = is_paid_user(user) or is_admin_user(user)

    db = db_conn()
    if not paid:
        free_year = get_free_year_for_subject(db, exam, subject)
        if free_year is not None:
            if year is not None and year != free_year:
                db.close()
                raise HTTPException(
                    status_code=402,
                    detail=f"Free access is limited to {free_year}. Upgrade to access all years.",
                )
            year = free_year

    where_sql, params = build_filters("objective", exam, year, subject)
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT {QUESTION_SELECT_COLS}
        FROM questions
        WHERE {where_sql}
        ORDER BY COALESCE(sort_key, 999999999), id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    rows = cur.fetchall()
    passage_lookup = build_passage_lookup(db, rows)
    db.close()

    return {
        "items": [row_to_question(r, passage_lookup) for r in rows],
        "limit": limit,
        "offset": offset,
        "free_year": year if not paid else None,
    }


# ---------------------------------------------------------------------------
# Theory (Study mode)
# ---------------------------------------------------------------------------

@router.get("/questions/theory")
def list_theory(
    limit: int = 20,
    offset: int = 0,
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    paid = is_paid_user(user) or is_admin_user(user)

    db = db_conn()
    if not paid:
        free_year = get_free_year_for_subject(db, exam, subject)
        if free_year is not None:
            if year is not None and year != free_year:
                db.close()
                raise HTTPException(
                    status_code=402,
                    detail=f"Free access is limited to {free_year}. Upgrade to access all years.",
                )
            year = free_year

    where_sql, params = build_filters("theory", exam, year, subject)
    cur = db.cursor()
    cur.execute(
        f"""
        SELECT {QUESTION_SELECT_COLS}
        FROM questions
        WHERE {where_sql}
        ORDER BY year, exam, subject, id
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    )
    rows = cur.fetchall()
    rows = sort_theory_rows(rows)
    passage_lookup = build_passage_lookup(db, rows)
    db.close()

    return {
        "items": [row_to_question(r, passage_lookup) for r in rows],
        "limit": limit,
        "offset": offset,
        "free_year": year if not paid else None,
    }


# ---------------------------------------------------------------------------
# Single question
# ---------------------------------------------------------------------------

@router.get("/question/{qid}")
def get_question(
    qid: str,
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    db = db_conn()
    cur = db.cursor()
    cur.execute(
        f"SELECT {QUESTION_SELECT_COLS} FROM questions WHERE id = ?",
        (qid,),
    )
    row = cur.fetchone()

    if not row:
        db.close()
        raise HTTPException(status_code=404, detail="Question not found")

    passage_lookup = build_passage_lookup(db, [row])
    db.close()
    return row_to_question(row, passage_lookup)
