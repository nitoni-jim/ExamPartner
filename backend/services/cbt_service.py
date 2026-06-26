"""
services/cbt_service.py — CBT business logic for ExamPartner.

Extracted from routes/cbt.py. Routes keep only HTTP concerns.
"""
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from config import FOUNDING_CAP, db_conn
from services.access_control import get_free_year_for_subject
from services.question_utils import (
    QUESTION_SELECT_COLS,
    build_passage_lookup,
    row_to_question,
)

CBT_ENGLISH_SUBJECT      = "Use of English"
CBT_ENGLISH_LANGUAGE     = "English Language"
CBT_ENGLISH_CAP          = 60   # JAMB Use of English
CBT_JAMB_CAP             = 40
CBT_WAEC_CAP             = 50
CBT_NECO_CAP             = 60

def get_cbt_cap(subject: str, exam: str, paper: Optional[str] = None) -> int:
    """
    Returns the correct CBT question cap for the given exam and subject.
    - JAMB Use of English: 60
    - WAEC/NECO English Language: actual DB count via high ceiling (no artificial cap)
    - WAEC/NECO English Language, paper="Oral English": same uncapped treatment
      as plain English Language Objective — deliberate, not an oversight.
    - JAMB: 40 per subject
    - WAEC: 50 per subject
    - NECO: 60 per subject
    - Other/unknown: 50 (safe default)
    """
    exam_upper = (exam or "").strip().upper()
    if subject == CBT_ENGLISH_SUBJECT and exam_upper == "JAMB":
        return CBT_ENGLISH_CAP
    if subject == CBT_ENGLISH_LANGUAGE and exam_upper in ("WAEC", "NECO"):
        # Covers both plain English Language Objective and paper="Oral English"
        # sessions — Oral English deliberately inherits the same uncapped
        # treatment rather than getting its own smaller cap.
        return 999  # no artificial cap — return all available after dedup
    if exam_upper == "JAMB":
        return CBT_JAMB_CAP
    if exam_upper == "WAEC":
        return CBT_WAEC_CAP
    if exam_upper == "NECO":
        return CBT_NECO_CAP
    return CBT_WAEC_CAP  # safe default


def get_founding_status() -> Dict[str, Any]:
    """
    Returns cap, current founding count, and whether new founding slots are open.
    """
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


def fetch_cbt_questions(
    subject: str,
    exam: str,
    is_paid: bool,
    paper: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetches, deduplicates, shuffles, and caps CBT questions for one subject.

    - Paid users: all years pooled.
    - Free users: oldest year only (resolved here).
    - Deduplication: first occurrence of each unique question_text wins.
    - Cap: per get_cbt_cap() — JAMB English 60, WAEC/NECO English Language uncapped,
      JAMB subjects 40, WAEC subjects 50, NECO subjects 60.
    - paper: optional discriminator within a subject (e.g. "Oral English" under
      "English Language"). When omitted, no filtering by paper occurs — existing
      callers and existing subjects behave exactly as before.

    Returns a dict ready to be returned directly by the route.
    """
    db = db_conn()

    year_filter: Optional[int] = None
    if not is_paid:
        free_year = get_free_year_for_subject(db, exam, subject)
        if free_year is None:
            db.close()
            raise HTTPException(
                status_code=404,
                detail="No questions found for this subject.",
            )
        year_filter = free_year

    cur = db.cursor()
    try:
        if year_filter is not None:
            if paper:
                cur.execute(
                    f"""
                    SELECT {QUESTION_SELECT_COLS}
                    FROM questions
                    WHERE qtype = ? AND exam = ? AND subject = ? AND year = ? AND paper = ?
                    ORDER BY id
                    """,
                    ("objective", exam, subject, year_filter, paper),
                )
            else:
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
            if paper:
                cur.execute(
                    f"""
                    SELECT {QUESTION_SELECT_COLS}
                    FROM questions
                    WHERE qtype = ? AND exam = ? AND subject = ? AND paper = ?
                    ORDER BY id
                    """,
                    ("objective", exam, subject, paper),
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

    # Deduplicate by exact question_text — keep first occurrence per text
    seen_texts: set = set()
    deduped: List[Any] = []
    for row in rows:
        text = (row["question_text"] or "").strip()
        if text and text in seen_texts:
            continue
        seen_texts.add(text)
        deduped.append(row)

    random.shuffle(deduped)

    cap = get_cbt_cap(subject=subject, exam=exam, paper=paper)
    capped = deduped[:cap]

    return {
        "items": [row_to_question(r, passage_lookup) for r in capped],
        "subject": subject,
        "total_available": total_available,
        "returned": len(capped),
        "free_year": year_filter,
    }
