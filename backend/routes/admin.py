"""
routes/admin.py — admin-only routes for ExamPartner.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from config import db_conn
from services.access_control import require_admin
from services.auth_utils import get_current_user
from services.question_utils import (
    QUESTION_SELECT_COLS,
    build_passage_lookup,
    row_get,
    row_to_question,
)

router = APIRouter(tags=["admin"])


@router.get("/admin/questions")
def admin_list_questions(
    limit: int = 100,
    offset: int = 0,
    exam: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    subject: Optional[str] = Query(default=None),
    qtype: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    require_admin(user)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where: List[str] = []
    params: List[Any] = []
    if exam:
        where.append("exam = ?")
        params.append(exam)
    if year is not None:
        where.append("year = ?")
        params.append(year)
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if qtype:
        where.append("qtype = ?")
        params.append(qtype)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM questions {where_sql}",
            tuple(params) if params else None,
        )
        total_row = cur.fetchone()
        total = int(row_get(total_row, "total", 0) if total_row else 0)

        cur.execute(
            f"""
            SELECT {QUESTION_SELECT_COLS}
            FROM questions
            {where_sql}
            ORDER BY year DESC, exam, subject, COALESCE(sort_key, 999999999), id
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        rows = cur.fetchall()
        passage_lookup = build_passage_lookup(db, rows)
    finally:
        db.close()

    return {
        "items": [row_to_question(r, passage_lookup) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/feedback")
def admin_list_feedback(
    limit: int = 100,
    offset: int = 0,
    feedback_type: Optional[str] = Query(default=None),
    source_area: Optional[str] = Query(default=None),
    user: Optional[Dict[str, Any]] = Depends(get_current_user),
):
    require_admin(user)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where: List[str] = []
    params: List[Any] = []
    if feedback_type:
        where.append("feedback_type = ?")
        params.append(feedback_type)
    if source_area:
        where.append("source_area = ?")
        params.append(source_area)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM feedback {where_sql}",
            tuple(params) if params else None,
        )
        total_row = cur.fetchone()
        total = int(row_get(total_row, "total", 0) if total_row else 0)

        cur.execute(
            f"""
            SELECT id, feedback_type, question_id, category, message,
                   source_area, user_identifier, created_at
            FROM feedback
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        rows = cur.fetchall()
    finally:
        db.close()

    return {"items": rows, "total": total, "limit": limit, "offset": offset}
