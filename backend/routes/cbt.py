"""
routes/cbt.py — CBT routes for ExamPartner.
"""
import os
import random
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from config import db_conn, FOUNDING_CAP
from services.access_control import get_free_year_for_subject, is_admin_user, is_paid_user
from services.auth_utils import get_current_user
from services.question_utils import (
    QUESTION_SELECT_COLS,
    build_passage_lookup,
    row_to_question,
)

router = APIRouter(tags=["cbt"])

CBT_ENGLISH_SUBJECT = "Use of English"
CBT_ENGLISH_CAP = 60
CBT_OTHER_CAP = 40


@router.get("/founding/status")
def founding_status():
    """Returns whether Founding (₦1,000) is still open for NEW users."""
    using_pg = bool(os.getenv("DATABASE_URL"))

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_founding = "
            + ("TRUE" if using_pg else "1")
        )
        row = cur.fetchone()
        try:
            count = int(row.get("c") if hasattr(row, "get") else row[0])
        except Exception:
            count = int(row[0])
        return {"cap": FOUNDING_CAP, "count": count, "open": count < FOUNDING_CAP}
    finally:
        db.close()


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

    db = db_conn()

    # Free users: restrict to oldest available year for this subject
    year_filter: Optional[int] = None
    if not paid:
        free_year = get_free_year_for_subject(db, exam, subject)
        if free_year is None:
            db.close()
            raise HTTPException(status_code=404, detail="No questions found for this subject.")
        year_filter = free_year

    cur = db.cursor()
    try:
        if year_filter is not None:
            cur.execute(
                f"""
                SELECT {QUESTION_SELECT_COLS}
                FROM questions
                WHERE qtype = ? AND exam = ? AND subject = ? AND year = ?
                ORDER BY id
                """,
                ("objective", exam, subject, year_filter),
            )
        else:
            cur.execute(
                f"""
                SELECT {QUESTION_SELECT_COLS}
                FROM questions
                WHERE qtype = ? AND exam = ? AND subject = ?
                ORDER BY id
                """,
                ("objective", exam, subject),
            )
        rows = cur.fetchall()
        passage_lookup = build_passage_lookup(db, rows)
    finally:
        db.close()

    total_available = len(rows)

    # Deduplicate by exact question_text — keep first occurrence
    seen_texts: set = set()
    deduped = []
    for row in rows:
        text = (row["question_text"] or "").strip()
        if text and text in seen_texts:
            continue
        seen_texts.add(text)
        deduped.append(row)

    random.shuffle(deduped)

    cap = CBT_ENGLISH_CAP if subject == CBT_ENGLISH_SUBJECT else CBT_OTHER_CAP
    capped = deduped[:cap]

    return {
        "items": [row_to_question(r, passage_lookup) for r in capped],
        "subject": subject,
        "total_available": total_available,
        "returned": len(capped),
        "free_year": year_filter,
    }
