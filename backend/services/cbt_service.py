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
    row_get,
    row_to_question,
)

CBT_ENGLISH_SUBJECT      = "Use of English"
CBT_ENGLISH_LANGUAGE     = "English Language"
CBT_ENGLISH_CAP          = 60   # JAMB Use of English
CBT_JAMB_CAP             = 40
CBT_WAEC_CAP             = 50
CBT_NECO_CAP             = 60

# ---------------------------------------------------------------------------
# TEMPORARY session caps — paper_rules / year-subject-paper specific rules
# are later work. These are intentionally simple, hardcoded fallbacks so CBT
# never starts a session with every matching DB row (which is the actual bug
# this table fixes — English Language was previously uncapped at the
# "no artificial cap" branch below, which silently meant "however many rows
# exist", observed in production as a 180-question session).
#
# Configurable in one place so WAEC English's 80 (current syllabus) can
# later be split per-year (e.g. older years at 100) without an Android
# change — Android only ever reads count/duration_minutes off the response.
# ---------------------------------------------------------------------------
CBT_ORAL_ENGLISH_CAP           = 60
CBT_WAEC_ENGLISH_OBJECTIVE_CAP = 80
CBT_NECO_ENGLISH_OBJECTIVE_CAP = 80

# ---------------------------------------------------------------------------
# Paper duration map — drives the CBT test-type picker timer.
#
# Objective and Oral English are both qtype="objective" but have different
# real-exam durations (WAEC Oral English Paper 3 is 60 items / 45 minutes,
# distinct from the 60-minute general Objective paper) — duration must come
# from paper, never from qtype alone.
# ---------------------------------------------------------------------------
CBT_PAPER_DURATION_MINUTES = {
    "Objective":     60,
    "Oral English":  45,
    "Theory":        120,
}
_DEFAULT_OBJECTIVE_DURATION = 60
_DEFAULT_THEORY_DURATION    = 120


def get_paper_duration_minutes(paper: Optional[str], qtype: str) -> int:
    """
    Returns the CBT duration in minutes for one paper.
    Looks up by paper first (the source of truth); falls back to a qtype-based
    default only when paper is null (not backfilled for this subject yet).
    """
    if paper and paper in CBT_PAPER_DURATION_MINUTES:
        return CBT_PAPER_DURATION_MINUTES[paper]
    return _DEFAULT_THEORY_DURATION if qtype == "theory" else _DEFAULT_OBJECTIVE_DURATION

def get_cbt_cap(subject: str, exam: str, paper: Optional[str] = None) -> int:
    """
    Returns the correct CBT question cap for the given exam/subject/paper.

    TEMPORARY, paper-driven, until paper_rules / year-specific rules exist:
      - Oral English (any exam):                60
      - WAEC English Language Objective:        80
      - NECO English Language Objective:        80
      - JAMB Use of English:                     60
      - JAMB (other subjects):                   40
      - WAEC (other subjects):                   50
      - NECO (other subjects):                   60
      - Other/unknown:                           50 (safe default)

    English Language was previously uncapped here ("no artificial cap" —
    return all DB rows after dedup). That was the root cause of CBT sessions
    starting with 180 questions for English Language Objective. There is no
    longer an uncapped branch for any paper.
    """
    exam_upper = (exam or "").strip().upper()

    if paper == "Oral English":
        return CBT_ORAL_ENGLISH_CAP

    if subject == CBT_ENGLISH_LANGUAGE and exam_upper == "WAEC":
        return CBT_WAEC_ENGLISH_OBJECTIVE_CAP
    if subject == CBT_ENGLISH_LANGUAGE and exam_upper == "NECO":
        return CBT_NECO_ENGLISH_OBJECTIVE_CAP

    if subject == CBT_ENGLISH_SUBJECT and exam_upper == "JAMB":
        return CBT_ENGLISH_CAP
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

    # Deduplicate by exact question_text — keep first occurrence per text.
    # Skipped for paper="Oral English": stress-pattern-style items legitimately
    # share an identical generic stem ("Identify the word with a different
    # stress pattern.") across multiple rows, each with different options and
    # a different answer. Text-based dedup was incorrectly collapsing these
    # into one — observed as WAEC/NECO Oral English returning 56 instead of
    # 60 despite 60 genuinely distinct rows (60 distinct ids, confirmed by
    # direct SQL check). For Oral English, id is the question identity; since
    # rows here come from a single non-joining SELECT, every row is already
    # a distinct id with no further dedup needed.
    if paper == "Oral English":
        deduped: List[Any] = list(rows)
    else:
        seen_texts: set = set()
        deduped = []
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


# ---------------------------------------------------------------------------
# CBT paper discovery — year-agnostic, mirrors the same free/paid pool rules
# as fetch_cbt_questions(). Distinct from /study/papers, which is year-scoped
# for Study mode. CBT never picks a single year, so it needs its own
# discovery query that respects the same access tier the question fetch uses.
# ---------------------------------------------------------------------------

def get_cbt_papers(
    subject: str,
    exam: str,
    is_paid: bool,
) -> Dict[str, Any]:
    """
    Returns the distinct papers available for a subject's CBT pool, scoped to
    exactly the same access tier fetch_cbt_questions() would draw from:
      - Free users:  only the free year's rows (never reveals paid-only papers
                      or counts from locked years)
      - Paid/admin:   all years pooled

    Each paper entry includes a duration_minutes (paper-driven, not qtype-driven
    — Objective and Oral English share qtype="objective" but have different
    real-exam timings).

    access.available_years / access.locked_years / access.all_years are
    explicit year lists (not just counts) so the client can build dynamic
    upsell copy ("Unlock 2010 and 2023") without any hardcoded text on either
    side. Free users never see locked-year question counts here — only which
    years exist and which of those are locked.
    """
    db = db_conn()

    cur = db.cursor()
    try:
        cur.execute(
            "SELECT DISTINCT year FROM questions WHERE exam = ? AND subject = ? AND year IS NOT NULL",
            (exam, subject),
        )
        all_years = sorted([int(row_get(r, "year")) for r in cur.fetchall()], reverse=True)
    except Exception:
        all_years = []

    year_filter: Optional[int] = None
    available_years: List[int] = all_years
    locked_years: List[int] = []

    if not is_paid:
        free_year = get_free_year_for_subject(db, exam, subject)
        if free_year is None:
            db.close()
            return {
                "exam": exam,
                "subject": subject,
                "access": {
                    "is_paid": False,
                    "mode": "free_year_only",
                    "available_years": [],
                    "locked_years": [],
                    "all_years": all_years,
                    "available_year_count": 0,
                    "locked_year_count": len(all_years),
                },
                "papers": [],
            }
        year_filter = free_year
        available_years = [free_year]
        locked_years = [y for y in all_years if y != free_year]

    cur = db.cursor()
    try:
        if year_filter is not None:
            cur.execute(
                """
                SELECT paper, qtype, question_text
                FROM questions
                WHERE exam = ? AND subject = ? AND year = ?
                """,
                (exam, subject, year_filter),
            )
        else:
            cur.execute(
                """
                SELECT paper, qtype, question_text
                FROM questions
                WHERE exam = ? AND subject = ?
                """,
                (exam, subject),
            )
        rows = cur.fetchall()
    finally:
        db.close()

    # Group by (paper, qtype), deduplicating by question_text within each
    # group — must mirror fetch_cbt_questions()'s dedup exactly, or
    # /cbt/papers.count will not match what /cbt/questions actually returns.
    # Exception: paper="Oral English" skips text dedup entirely, since
    # stress-pattern-style items legitimately share an identical generic
    # stem across multiple rows with different options/answers — text
    # dedup was incorrectly collapsing 60 genuinely distinct rows into 56.
    grouped: Dict[Tuple[Optional[str], str], Dict[str, Any]] = {}
    for r in rows:
        paper = row_get(r, "paper")
        qtype = row_get(r, "qtype")
        text = (row_get(r, "question_text") or "").strip()
        key = (paper, qtype)
        group = grouped.setdefault(key, {"raw_count": 0, "seen_texts": set(), "unique_count": 0})
        group["raw_count"] += 1
        if paper == "Oral English":
            group["unique_count"] += 1
            continue
        if text and text in group["seen_texts"]:
            continue
        if text:
            group["seen_texts"].add(text)
        group["unique_count"] += 1

    papers: List[Dict[str, Any]] = []
    for (paper, qtype), group in grouped.items():
        total_available = group["unique_count"]  # post-dedup, matches fetch_cbt_questions()'s pool size
        label = paper if paper else ("Objective" if qtype == "objective" else "Theory" if qtype == "theory" else (qtype or "Unknown"))

        # count is what the CBT session will actually use — capped AND
        # deduplicated, never the raw DB row count. Theory keeps its own
        # existing uncapped backend logic for now (Theory's cap/limit is a
        # separate, later concern — full theory-paper grading isn't
        # implemented yet).
        if qtype == "theory":
            session_count = total_available
        else:
            cap = get_cbt_cap(subject=subject, exam=exam, paper=paper)
            session_count = min(total_available, cap)

        papers.append({
            "paper": paper,
            "qtype": qtype,
            "label": label,
            "count": session_count,
            "total_available": total_available,
            "duration_minutes": get_paper_duration_minutes(paper, qtype),
            # Theory requires AI grading (Claude + quota checks) and is never
            # available offline. Objective-side papers (Objective, Oral
            # English) can be prepared offline via Room sync, regardless of
            # how many distinct paper values exist — this is qtype-driven,
            # unlike duration which is paper-driven.
            "requires_online": qtype == "theory",
        })

    papers.sort(key=lambda p: _cbt_paper_sort_key(p["label"]))

    return {
        "exam": exam,
        "subject": subject,
        "access": {
            "is_paid": is_paid,
            "mode": "free_year_only" if not is_paid else "full_pool",
            "available_years": available_years,
            "locked_years": locked_years,
            "all_years": all_years,
            "available_year_count": len(available_years),
            "locked_year_count": len(locked_years),
        },
        "papers": papers,
    }


_CBT_PAPER_SORT_ORDER = ["Objective", "Theory", "Oral English", "Practical", "Essay"]


def _cbt_paper_sort_key(label: str) -> Tuple[int, str]:
    try:
        return (_CBT_PAPER_SORT_ORDER.index(label), label)
    except ValueError:
        return (len(_CBT_PAPER_SORT_ORDER), label)
