"""
services/cbt_service.py — CBT business logic for ExamPartner.

Extracted from routes/cbt.py. Routes keep only HTTP concerns.
"""
import json
import os
import random
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from config import FOUNDING_CAP, db_conn, logger
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


# ---------------------------------------------------------------------------
# Theory CBT — section-aware pool (Theory CBT Section-Aware Implementation v2)
#
# Distinct from get_theory_questions() in question_service.py, which serves
# GET /questions/theory for Study mode. Two deliberate differences, per
# product decision (not a bug, not scope creep):
#
#   1. Gradeability filter — CBT Theory pool only ever contains questions
#      the AI grader can actually score. Study mode intentionally shows
#      ALL Theory questions, including ones missing examiner_points, since
#      Study mode is for reading/learning (and doubles as the place Nitoni
#      spots content that still needs marking rubrics added). Applying this
#      filter to /questions/theory instead would silently hide legitimate
#      study material — see the note under handover doc §7.1.
#
#   2. Section grouping — driven by paper_rules.rules_json for this
#      exam+subject+paper (year=NULL row), NOT by year. Each question is
#      pooled across all years as a self-contained atomic unit (a question's
#      sub-parts always travel with it), so there is no year picker and no
#      year filter here — mirrors the "CBT always queries year=NULL" rule
#      from Sprint 3, applied at the section level instead of the paper
#      level.
#
# If no paper_rules row / rules_json exists for this exam+subject+paper,
# returns sections=[] — the route and Android must both treat that as "fall
# back to the existing flat-list Theory session", never as an error.
# ---------------------------------------------------------------------------

def _is_gradeable_theory_row(row: Any) -> bool:
    """
    Mirrors theory_service.py's _fetch_question_data() gradeability check
    exactly, so a question that enters the CBT pool is guaranteed to succeed
    at POST /theory/grade — never a NotGradeable response through no fault
    of the student.

    English Language's essay/comprehension/summary grading modes are always
    considered gradeable (same as theory_service.py — those modes carry
    their own structured rubric by construction). The examiner_points check
    only applies to grading_mode == "general", and accepts EITHER a
    top-level examiner_points list OR per-sub-question examiner_points on
    at least one sub-question — a question with only a top-level check
    would wrongly exclude every sub-question-style Theory question (the
    majority of the current Biology content).
    """
    metadata_raw = row_get(row, "metadata_json")
    grading_mode = "general"
    if metadata_raw:
        try:
            grading_mode = (json.loads(metadata_raw) or {}).get("grading_mode", "general")
        except Exception:
            grading_mode = "general"

    if grading_mode != "general":
        return True

    examiner_points_raw = row_get(row, "examiner_points_json")
    examiner_points = None
    if examiner_points_raw:
        try:
            examiner_points = json.loads(examiner_points_raw)
        except Exception:
            examiner_points = None

    sub_questions_raw = row_get(row, "sub_questions_json")
    sub_questions: List[Any] = []
    if sub_questions_raw:
        try:
            sub_questions = json.loads(sub_questions_raw)
        except Exception:
            sub_questions = []

    sub_questions_have_rubric = (
        isinstance(sub_questions, list)
        and len(sub_questions) > 0
        and any(sq.get("examiner_points") for sq in sub_questions if isinstance(sq, dict))
    )
    return bool(examiner_points) or sub_questions_have_rubric


def _get_theory_section_rules(exam: str, subject: str, paper: str) -> List[Dict[str, Any]]:
    """
    Fetches the section structure for one Theory paper from
    paper_rules.rules_json, using the same year-NULL, rule_source-preference
    resolution order as Android's PaperRuleDao.findBestYearNull() (Sprint 3):
    actual_paper > syllabus_default > legacy_placeholder.

    Returns [] if no matching paper_rules row exists, or if rules_json is
    null/empty/malformed — callers must treat that as "no section rules".
    """
    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT rules_json FROM paper_rules
            WHERE exam = ? AND subject = ? AND paper = ? AND year IS NULL
            ORDER BY
                CASE rule_source
                    WHEN 'actual_paper' THEN 0
                    WHEN 'syllabus_default' THEN 1
                    WHEN 'legacy_placeholder' THEN 2
                    ELSE 3
                END
            LIMIT 1
            """,
            (exam, subject, paper),
        )
        row = cur.fetchone()
    finally:
        db.close()

    if not row:
        return []

    raw = row_get(row, "rules_json")
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("Malformed rules_json for %s/%s/%s", exam, subject, paper)
        return []

    return parsed if isinstance(parsed, list) else []


def fetch_cbt_theory_paper(
    exam: str,
    subject: str,
    paper: str,
    is_paid: bool,
) -> Dict[str, Any]:
    """
    Fetches the full section-grouped Theory CBT pool for one paper in a
    single response — one call per paper, not one per section, to avoid
    both extra round-trips and any drift between what Android's cached
    rulesJson says and what the backend actually pools against (this
    function re-resolves rules_json live from paper_rules rather than
    trusting a value Android sends).

    is_paid is accepted for parity with fetch_cbt_questions() and future
    use, but intentionally not yet enforced at the row level — Theory CBT
    access is already gated upstream (requires_online / paywall, via
    get_cbt_papers()), and Theory pools across all years for any
    authenticated user who reaches this screen, same as it does today.

    Pool size per section is uncapped (all gradeable questions, shuffled) —
    required_count from rules_json is the only number the schema actually
    defines; there's no "how many options to show" field to cap against,
    and inventing one would be arbitrary. See handover doc §7.2/design note.

    Two queries, deliberately, not one:
      1. An explicit-column query (id, section, sub_questions_json,
         examiner_points_json, metadata_json) drives the gradeability
         decision and section grouping. This does NOT use
         QUESTION_SELECT_COLS — that constant drives the public-facing
         Question/TheoryQuestion JSON shape, and examiner_points_json is a
         grading secret with no equivalent field anywhere in the Android
         model, so whether QUESTION_SELECT_COLS happens to include it is
         unverified and this function must not gamble a silent, un-erroring
         "every question looks ungradeable" failure on that assumption.
         Mirrors theory_service.py's _fetch_question_data() explicit
         column list exactly, for the same reason it exists there.
      2. Once the gradeable id set is known, a second query using
         QUESTION_SELECT_COLS (id IN (...)) builds the actual client-facing
         rows via the existing row_to_question() — identical shape to every
         other question-serving endpoint, guaranteed compatible with
         Android's TheoryQuestion model.
    """
    section_rules = _get_theory_section_rules(exam=exam, subject=subject, paper=paper)
    if not section_rules:
        return {
            "exam": exam,
            "subject": subject,
            "paper": paper,
            "sections": [],
        }

    db = db_conn()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT id, section, sub_questions_json, examiner_points_json, metadata_json
            FROM questions
            WHERE qtype = ? AND exam = ? AND subject = ? AND paper = ?
            """,
            ("theory", exam, subject, paper),
        )
        grading_rows = cur.fetchall()
    finally:
        db.close()

    # section_label must match questions.section verbatim as authored in
    # rules_json (e.g. "Section A", not "A") — see handover doc §6.
    gradeable_ids_by_section: Dict[str, List[str]] = {}
    for row in grading_rows:
        if not _is_gradeable_theory_row(row):
            continue
        section_label = row_get(row, "section")
        qid = row_get(row, "id")
        if not section_label or not qid:
            continue
        gradeable_ids_by_section.setdefault(section_label, []).append(qid)

    all_gradeable_ids = [qid for ids in gradeable_ids_by_section.values() for qid in ids]

    if not all_gradeable_ids:
        # No gradeable questions anywhere in this paper yet — still return
        # the section shells with empty question lists (not an error), so
        # the UI can show "No gradeable questions available in this section
        # yet" per section rather than falling back or breaking.
        return {
            "exam": exam,
            "subject": subject,
            "paper": paper,
            "sections": [
                {
                    "section": rule.get("section"),
                    "instruction": rule.get("instruction", ""),
                    "required_count": rule.get("required_count", 0),
                    "compulsory": bool(rule.get("compulsory", False)),
                    "marks_per_question": rule.get("marks_per_question", 0),
                    "total_marks": rule.get("total_marks", 0),
                    "questions": [],
                }
                for rule in section_rules
            ],
        }

    db = db_conn()
    cur = db.cursor()
    try:
        placeholders = ",".join(["?"] * len(all_gradeable_ids))
        cur.execute(
            f"""
            SELECT {QUESTION_SELECT_COLS}
            FROM questions
            WHERE id IN ({placeholders})
            """,
            tuple(all_gradeable_ids),
        )
        rows = cur.fetchall()
        passage_lookup = build_passage_lookup(db, rows)
    finally:
        db.close()

    questions_by_id = {row_get(r, "id"): r for r in rows}

    sections_out: List[Dict[str, Any]] = []
    for rule in section_rules:
        section_label = rule.get("section")
        section_ids = list(gradeable_ids_by_section.get(section_label, []))
        random.shuffle(section_ids)
        section_question_rows = [questions_by_id[qid] for qid in section_ids if qid in questions_by_id]
        sections_out.append({
            "section": section_label,
            "instruction": rule.get("instruction", ""),
            "required_count": rule.get("required_count", 0),
            "compulsory": bool(rule.get("compulsory", False)),
            "marks_per_question": rule.get("marks_per_question", 0),
            "total_marks": rule.get("total_marks", 0),
            "questions": [row_to_question(r, passage_lookup) for r in section_question_rows],
        })

    return {
        "exam": exam,
        "subject": subject,
        "paper": paper,
        "sections": sections_out,
    }
