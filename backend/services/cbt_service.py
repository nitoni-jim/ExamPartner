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

# --- CBT eligibility ---------------------------------------------------------
#
# Every query in this module that selects questions for a CBT session filters
# on (cbt_eligible IS NULL OR cbt_eligible = 1). NULL means eligible, so no
# existing row changed behaviour when the column was added.
#
# The distinction it draws is between per-question content quality, which the
# audit already covers and which applies equally to Study mode, and whether a
# PAPER's structure has been confirmed against a complete source. CBT allocates
# from paper_rules; Study mode does not. A paper whose real section structure
# cannot be derived — because the available copy is missing a section, or
# because paper_rules describes a different year — cannot be allocated from,
# regardless of how sound its individual questions are.
#
# Before this column existed the only lever was ingest-or-don't, which kept
# audited content out of Study as well. The orphaned-section guard in
# fetch_cbt_theory_paper() below was doing the job by accident: a record whose
# section label has no rules_json entry is silently dropped, so mislabelled
# records were excluded for the wrong reason — and correcting the labels, which
# looks like tidying, would have started serving them. (That guard now lives in
# _build_theory_section_plan(), which fetch_cbt_theory_paper() and
# get_cbt_papers() share.)

# Papers already warned about for orphaned sections (see fetch_cbt_theory_paper).
# Deduped per process so a misconfigured paper logs once rather than on every
# fetch; process restarts re-surface it, which is the behaviour we want — the
# condition is persistent, so a periodic reminder beats either spam or silence.
_ORPHAN_SECTION_WARNED: set = set()

# Same once-per-process dedupe, for two more persistent conditions found by
# _build_theory_section_plan(): a Theory paper whose rules_json has no
# presented_count yet, so its sections are still served uncapped (decision
# D3), and a paper holding records whose marks disagree with their section's
# marks_per_question, which are left out of the CBT pool (decision D10).
_LEGACY_UNCAPPED_WARNED: set = set()
_MARKS_MISMATCH_WARNED: set = set()


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
                      AND (cbt_eligible IS NULL OR cbt_eligible = 1)
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
                      AND (cbt_eligible IS NULL OR cbt_eligible = 1)
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
                      AND (cbt_eligible IS NULL OR cbt_eligible = 1)
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
                      AND (cbt_eligible IS NULL OR cbt_eligible = 1)
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

# Same preference order as _get_theory_section_rules() below and
# paper_rules_service.resolve_paper_rule(): real evidence beats syllabus
# policy beats guess.
_RULE_SOURCE_RANK = {"actual_paper": 0, "syllabus_default": 1, "legacy_placeholder": 2}


def _paper_rules_durations(cur, exam: str, subject: str) -> Dict[str, int]:
    """
    Returns {paper: duration_minutes} from paper_rules for one exam+subject.

    Reads the rows CBT uses everywhere else: year NULL (CBT has no year
    picker) and country NULL (no country-specific rows until Sprint B, and
    this endpoint is not told the candidate's country). Where more than one
    rule_source exists for a paper, the best-ranked one wins. Rows with no
    duration, or a non-positive one, are ignored.

    Queried directly rather than through paper_rules_service, which imports
    this module — importing it back would be circular.

    Never raises: on any failure it logs and returns {}, so every paper falls
    back to the CBT_PAPER_DURATION_MINUTES map exactly as before this change.
    """
    try:
        cur.execute(
            "SELECT paper, rule_source, duration_minutes FROM paper_rules "
            "WHERE exam = ? AND subject = ? AND year IS NULL AND country IS NULL "
            "AND duration_minutes IS NOT NULL",
            (exam, subject),
        )
        found = cur.fetchall()
    except Exception:
        logger.warning("Could not read paper_rules durations for %s/%s", exam, subject, exc_info=True)
        return {}

    best: Dict[str, Tuple[int, int]] = {}
    for r in found:
        paper = row_get(r, "paper")
        try:
            minutes = int(row_get(r, "duration_minutes"))
        except (TypeError, ValueError):
            continue
        if not paper or minutes <= 0:
            continue
        rank = _RULE_SOURCE_RANK.get(row_get(r, "rule_source"), 3)
        if paper not in best or rank < best[paper][0]:
            best[paper] = (rank, minutes)
    return {paper: minutes for paper, (_, minutes) in best.items()}


def _is_cbt_eligible(value: Any) -> bool:
    """NULL (None) means eligible; 0 / False means authored out of CBT."""
    if value is None:
        return True
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value)


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
    real-exam timings). It comes from the paper's paper_rules row where one
    exists (year NULL, country NULL, best rule_source), and from
    CBT_PAPER_DURATION_MINUTES otherwise. Before this, the map was the only
    source, so a paper_rules duration reached the app's timer only through its
    once-a-day paper_rules_cache sync, and the test-type picker always showed
    the map's figure (e.g. 120 min for a 150-minute NECO Financial Accounting
    Paper II).

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
                SELECT paper, qtype, question_text, cbt_eligible
                FROM questions
                WHERE exam = ? AND subject = ? AND year = ?
                """,
                (exam, subject, year_filter),
            )
        else:
            cur.execute(
                """
                SELECT paper, qtype, question_text, cbt_eligible
                FROM questions
                WHERE exam = ? AND subject = ?
                """,
                (exam, subject),
            )
        rows = cur.fetchall()
        rule_durations = _paper_rules_durations(cur, exam, subject)
    finally:
        db.close()

    # Group by (paper, qtype), deduplicating by question_text within each
    # group — must mirror fetch_cbt_questions()'s dedup exactly, or
    # /cbt/papers.count will not match what /cbt/questions actually returns.
    # Exception: paper="Oral English" skips text dedup entirely, since
    # stress-pattern-style items legitimately share an identical generic
    # stem across multiple rows with different options/answers — text
    # dedup was incorrectly collapsing 60 genuinely distinct rows into 56.
    #
    # Theory is counted differently: one per CBT-eligible record, no text
    # dedup. A theory question is a whole multi-part record, and many share a
    # generic stem ("Answer all parts of this question.") — text dedup was
    # collapsing NECO 2021 Financial Accounting's 9 questions to 6 and NECO
    # Biology's 5 to 1. The theory session itself never dedups by text
    # (fetch_cbt_theory_paper() pools by id), and it serves only CBT-eligible
    # records, so the count follows the same rule. cbt_eligible NULL means
    # eligible, as in every CBT query in this module.
    grouped: Dict[Tuple[Optional[str], str], Dict[str, Any]] = {}
    for r in rows:
        paper = row_get(r, "paper")
        qtype = row_get(r, "qtype")
        text = (row_get(r, "question_text") or "").strip()
        key = (paper, qtype)
        if qtype == "theory" and not _is_cbt_eligible(row_get(r, "cbt_eligible")):
            continue
        group = grouped.setdefault(key, {"raw_count": 0, "seen_texts": set(), "unique_count": 0})
        group["raw_count"] += 1
        if paper == "Oral English" or qtype == "theory":
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
        # deduplicated, never the raw DB row count.
        #
        # Theory used to keep its own uncapped logic here ("Theory's
        # cap/limit is a separate, later concern"), counting every
        # CBT-eligible record. That is no longer true, and even before it
        # stopped being true the count could disagree with the session it
        # started (plan finding A6): the session serves gradeable records
        # only, in declared sections only, and this counted ungradeable and
        # orphaned records as well. A Theory paper with a
        # paper_rules row now takes count — and available /
        # unavailable_reason — from _build_theory_section_plan(), the same
        # helper fetch_cbt_theory_paper() builds the session from, with the
        # same year_filter, so the two cannot disagree. It runs its own
        # queries; the cheap counting query above is not reused for it. A
        # Theory paper with no rules row keeps the record count, because its
        # session is Android's flat fallback, not this one.
        available = True
        unavailable_reason: Optional[str] = None
        if qtype == "theory":
            session_count = total_available
            if paper:
                plan = _build_theory_section_plan(exam, subject, paper, year_filter)
                if plan["rules"]:
                    session_count = plan["served_count"]
                    available = plan["complete"]
                    unavailable_reason = plan["unavailable_reason"]
        else:
            cap = get_cbt_cap(subject=subject, exam=exam, paper=paper)
            session_count = min(total_available, cap)

        papers.append({
            "paper": paper,
            "qtype": qtype,
            "label": label,
            "count": session_count,
            "total_available": total_available,
            # paper_rules first, the hardcoded map as fallback — see the
            # docstring above and _paper_rules_durations().
            "duration_minutes": rule_durations.get(paper) or get_paper_duration_minutes(paper, qtype),
            # Theory requires AI grading (Claude + quota checks) and is never
            # available offline. Objective-side papers (Objective, Oral
            # English) can be prepared offline via Room sync, regardless of
            # how many distinct paper values exist — this is qtype-driven,
            # unlike duration which is paper-driven.
            "requires_online": qtype == "theory",
            # false for a Theory paper the app must not start a session for,
            # exactly when the session would report selection.complete false:
            #   - a section is "insufficient", fewer usable questions than its
            #     required_count (decision D2);
            #   - a pooled paper cannot reach total_required_questions from
            #     its in_pool sections' surplus (D2 at paper level);
            #   - a free user's subject has no free year. That case never
            #     reaches this line: get_cbt_papers() returns papers: [] for
            #     it above, so the paper is not listed at all, while
            #     /cbt/theory-questions answers it with complete false.
            # Always true for objective-side papers.
            "available": available,
            "unavailable_reason": unavailable_reason,
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
#      Two later qualifications. Free users now draw from the free year
#      only (decision D5), the same tier rule fetch_cbt_questions() and
#      get_cbt_papers() already applied — "pooled across all years" is the
#      paid/admin pool. And where rules_json declares presented_count, each
#      section serves that many questions sampled from its pool, not the
#      whole pool (see _build_theory_section_plan()).
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

    Country NULL only, as _paper_rules_durations() reads: this endpoint is
    not told the candidate's country, and the theory policy is year NULL,
    country NULL until Sprint B. With no country filter, the first
    country-specific Theory row authored would compete with the agnostic one
    on rule_source alone and could be served to every candidate — a wrong
    paper structure, with no error. No such row exists yet, so today the
    filter changes nothing.

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
              AND country IS NULL
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


def _section_header(rule: Dict[str, Any]) -> Dict[str, Any]:
    """
    The rules_json fields served with each section, without its questions.

    in_pool, pattern_type and total_required_questions (Sprint B) drive the
    app's pooled scoring for Pattern B papers. They are always present in the
    response, with the defaults a row that omits them means: in_pool false,
    pattern_type "fixed", total_required_questions null. A row without them
    (the 14 NECO rows, WAEC Biology) is therefore served exactly as before
    plus three fields that tell the app "no pooling".
    """
    return {
        "section": rule.get("section"),
        "instruction": rule.get("instruction", ""),
        "required_count": rule.get("required_count", 0),
        "compulsory": bool(rule.get("compulsory", False)),
        "marks_per_question": rule.get("marks_per_question", 0),
        "total_marks": rule.get("total_marks", 0),
        "in_pool": bool(rule.get("in_pool", False)),
        "pattern_type": rule.get("pattern_type") or "fixed",
        "total_required_questions": rule.get("total_required_questions"),
    }


_PLAN_STATUS_OK           = "ok"
_PLAN_STATUS_SHORT        = "short"
_PLAN_STATUS_INSUFFICIENT = "insufficient"

_NO_FREE_YEAR_REASON = "No free-year Theory questions for this subject."


def _whole_count(value: Any) -> Optional[int]:
    """value as an int if it is a whole number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _marks_mismatch(record_marks: Any, rule_marks: Any) -> bool:
    """
    Decision D10: True when a record's marks is set and differs from the
    section's marks_per_question. A rule without marks_per_question, or a
    record with NULL marks, never mismatches. Compared as floats, so a 12.5
    record matches a 12.5 rule and 15 matches 15.0.
    """
    if record_marks is None or rule_marks is None:
        return False
    try:
        return abs(float(record_marks) - float(rule_marks)) > 1e-9
    except (TypeError, ValueError):
        # A marks value that is not a number cannot be shown to match.
        return True


def _build_theory_section_plan(
    exam: str,
    subject: str,
    paper: str,
    year_filter: Optional[int],
    rng: Any = None,
    no_free_year: bool = False,
) -> Dict[str, Any]:
    """
    Decides, for one Theory paper, which records each section can serve and
    which it will serve. The ONE place that decision is made: both
    fetch_cbt_theory_paper() (the session) and get_cbt_papers() (the count
    and availability on the test-type picker) call it with the same
    year_filter, so the picker can never promise a session the backend will
    not build.

    Returns:
      rules               the section list from rules_json ([] = no rules row,
                          or malformed: callers keep today's sections: [])
      mode                "presented" when every section declares a whole
                          presented_count, else "legacy_uncapped" (D3)
      sections            one entry per rule, in rules_json order:
                            rule, presented_count (None in legacy),
                            available_ids, selected_ids, available_count,
                            shortfall, status
      served_count        total selected ids — what /cbt/papers shows as count
      complete            False iff any section is "insufficient", or a
                          pooled row cannot fill total_required_questions
                          from its in_pool surplus, or there is no free
                          year at all
      unavailable_reason  None, or one sentence naming the first problem

    A section's pool is a record that is CBT-eligible, gradeable, in this
    year scope, carries the section's label verbatim, and does not fail the
    D10 marks check. Status, per decision D2:
      presented mode: pool >= presented_count            -> "ok"
                      required <= pool < presented_count  -> "short"
                      pool < required                     -> "insufficient"
      legacy mode:    pool < required -> "insufficient", else "ok"
    Presented mode serves min(presented_count, pool) records sampled without
    replacement; legacy mode serves the whole pool, as before. Both shuffle.

    presented_count is read, never inferred: a row without it is legacy,
    however its required_count reads. rng is for tests (random.Random(seed));
    production uses the module's random.

    no_free_year: the caller is a free user and the subject has no free year
    at all. Nothing is queried; every section's pool is empty and the paper
    is unavailable, with that as the reason.
    """
    rng = rng or random
    section_rules = _get_theory_section_rules(exam=exam, subject=subject, paper=paper)
    plan: Dict[str, Any] = {
        "rules": section_rules,
        "mode": "legacy_uncapped",
        "sections": [],
        "served_count": 0,
        "complete": True,
        "unavailable_reason": None,
    }
    if not section_rules:
        return plan

    presented_counts = [_whole_count(rule.get("presented_count")) for rule in section_rules]
    presented_mode = all(c is not None and c >= 1 for c in presented_counts)
    plan["mode"] = "presented" if presented_mode else "legacy_uncapped"
    if not presented_mode:
        # Decision D3: served exactly as before this change. Logged because
        # every Theory paper except Literature in English (D7) is meant to
        # get presented_count; one that still has none is unfinished
        # authoring, not a choice. A row with presented_count on some
        # sections only cannot pass Rule 6, but if one exists it lands here
        # too rather than capping half a paper.
        _key = (exam, subject, paper)
        if _key not in _LEGACY_UNCAPPED_WARNED:
            _LEGACY_UNCAPPED_WARNED.add(_key)
            logger.warning(
                "Theory CBT %s/%s/%s: rules_json has %s; serving every gradeable "
                "question per section (legacy_uncapped).",
                exam, subject, paper,
                "no presented_count" if not any(c is not None for c in presented_counts)
                else "presented_count on some sections only, or an invalid one",
            )

    grading_rows: List[Any] = []
    if not no_free_year:
        db = db_conn()
        cur = db.cursor()
        try:
            if year_filter is not None:
                cur.execute(
                    """
                    SELECT id, section, marks, sub_questions_json, examiner_points_json, metadata_json
                    FROM questions
                    WHERE qtype = ? AND exam = ? AND subject = ? AND paper = ? AND year = ?
                      AND (cbt_eligible IS NULL OR cbt_eligible = 1)
                    ORDER BY id
                    """,
                    ("theory", exam, subject, paper, year_filter),
                )
            else:
                cur.execute(
                    """
                    SELECT id, section, marks, sub_questions_json, examiner_points_json, metadata_json
                    FROM questions
                    WHERE qtype = ? AND exam = ? AND subject = ? AND paper = ?
                      AND (cbt_eligible IS NULL OR cbt_eligible = 1)
                    ORDER BY id
                    """,
                    ("theory", exam, subject, paper),
                )
            grading_rows = cur.fetchall()
        finally:
            db.close()

    # section_label must match questions.section verbatim as authored in
    # rules_json (e.g. "Section A", not "A") — see handover doc §6.
    #
    # Ids are de-duplicated per section (a dict keeps first-seen order) so a
    # record can never be sampled twice or counted twice, whatever the query
    # returns. ORDER BY id makes that order — and so a seeded sample —
    # repeatable across engines.
    gradeable_by_section: Dict[str, Dict[str, Any]] = {}
    for row in grading_rows:
        if not _is_gradeable_theory_row(row):
            continue
        section_label = row_get(row, "section")
        qid = row_get(row, "id")
        if not section_label or not qid:
            continue
        gradeable_by_section.setdefault(section_label, {}).setdefault(qid, row_get(row, "marks"))

    # Orphaned-section guard.
    #
    # The plan below is built by iterating section_rules, so a section that
    # holds gradeable questions but has no matching entry in rules_json is
    # dropped entirely — never served, in any mode, with no error raised. That
    # is sometimes correct (WAEC Biology "Section B" is the Ghana-only variant,
    # deliberately omitted from the row because Ghana is out of scope), but it
    # is indistinguishable from an authoring mistake, because section_label
    # must match questions.section verbatim (see the note above) — a stray
    # trailing space in an authored label silently empties that whole section.
    #
    # This does not change behaviour; it just makes the drop visible. Removing
    # the ambiguity properly needs the planned nullable `country` column on
    # paper_rules, so an intentional exclusion can be declared rather than
    # implied. Until then: log it.
    #
    # Orphaned records are never counted either: get_cbt_papers() takes its
    # Theory count from this plan, so the picker no longer counts records
    # the session drops.
    _orphans = sorted(set(gradeable_by_section) - {r.get("section") for r in section_rules})
    if _orphans:
        _key = (exam, subject, paper)
        if _key not in _ORPHAN_SECTION_WARNED:
            _ORPHAN_SECTION_WARNED.add(_key)
            logger.warning(
                "Orphaned section(s) for %s/%s/%s: %s hold gradeable questions "
                "but have no rules_json entry, so they will not be served. "
                "Intentional exclusion, or a section-label mismatch?",
                exam, subject, paper, ", ".join(repr(o) for o in _orphans),
            )

    mismatched: List[str] = []
    for rule, presented in zip(section_rules, presented_counts):
        section_label = rule.get("section")
        candidates = gradeable_by_section.get(section_label, {})

        # Decision D10. A record whose marks disagree with the section's
        # marks_per_question is either mislabelled into the wrong section or
        # carries a wrong marks value; either way the grader would score it
        # out of a total the paper does not award for that section, so it is
        # left out of the CBT pool. Study mode still shows it.
        rule_marks = rule.get("marks_per_question")
        available_ids: List[str] = []
        for qid, marks in candidates.items():
            if _marks_mismatch(marks, rule_marks):
                mismatched.append(f"{qid} ({section_label!r}: marks {marks!r}, rule {rule_marks!r})")
                continue
            available_ids.append(qid)

        try:
            required = int(rule.get("required_count", 0) or 0)
        except (TypeError, ValueError):
            required = 0
        available_count = len(available_ids)

        if presented_mode:
            take = min(presented, available_count)
            selected_ids = rng.sample(available_ids, take)
            shortfall = max(0, presented - available_count)
            if available_count < required:
                status = _PLAN_STATUS_INSUFFICIENT
            elif available_count < presented:
                status = _PLAN_STATUS_SHORT
            else:
                status = _PLAN_STATUS_OK
        else:
            selected_ids = list(available_ids)
            rng.shuffle(selected_ids)
            shortfall = 0
            status = _PLAN_STATUS_INSUFFICIENT if available_count < required else _PLAN_STATUS_OK

        plan["sections"].append({
            "rule": rule,
            "presented_count": presented if presented_mode else None,
            "required_count": required,
            "available_ids": available_ids,
            "selected_ids": selected_ids,
            "available_count": available_count,
            "shortfall": shortfall,
            "status": status,
        })

    if mismatched:
        _key = (exam, subject, paper)
        if _key not in _MARKS_MISMATCH_WARNED:
            _MARKS_MISMATCH_WARNED.add(_key)
            logger.warning(
                "Theory CBT %s/%s/%s: %d record(s) excluded because their marks differ "
                "from the section's marks_per_question: %s",
                exam, subject, paper, len(mismatched), "; ".join(mismatched),
            )

    plan["served_count"] = sum(len(s["selected_ids"]) for s in plan["sections"])

    if no_free_year:
        plan["complete"] = False
        plan["unavailable_reason"] = _NO_FREE_YEAR_REASON
    else:
        for s in plan["sections"]:
            if s["status"] == _PLAN_STATUS_INSUFFICIENT:
                n = s["available_count"]
                plan["complete"] = False
                plan["unavailable_reason"] = (
                    f"{s['rule'].get('section')} has {n} question{'' if n == 1 else 's'} "
                    f"available; the paper requires {s['required_count']}."
                )
                break

        # Pooled papers (Pattern B): D2 at paper level. Every section can
        # meet its own floor and the paper still be unanswerable — WAEC CRS
        # shape, A/B/C each a floor of 1, T = 4, one question served in each:
        # three answers possible, four required. The extra T - F answers can
        # only come from questions served beyond a floor in an in_pool
        # section; surplus in an in_pool false section is capped at its floor
        # by the scoring and never counts. Checked only when no section is
        # already insufficient, so the section-level reason, which names
        # what to fix, wins. Uses what is served (selected_ids), so it holds
        # in presented and legacy mode alike.
        pooled_t = next(
            (_whole_count(s["rule"].get("total_required_questions")) for s in plan["sections"]
             if s["rule"].get("total_required_questions") is not None),
            None,
        )
        if plan["complete"] and pooled_t is not None:
            floors = sum(s["required_count"] for s in plan["sections"])
            surplus = sum(
                max(0, len(s["selected_ids"]) - s["required_count"])
                for s in plan["sections"] if s["rule"].get("in_pool") is True
            )
            if surplus < pooled_t - floors:
                plan["complete"] = False
                plan["unavailable_reason"] = (
                    f"The paper requires {pooled_t} answers, but only {floors + surplus} "
                    "answers can be selected under the paper rules."
                )

    return plan


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

    is_paid is enforced (decision D5): free users draw only from the free
    year (get_free_year_for_subject()), paid/admin users from every year —
    the same tier rule fetch_cbt_questions() and get_cbt_papers() apply.
    It used to be accepted "for parity and future use" but not enforced, on
    the reasoning that Theory CBT access was already gated upstream by the
    paywall on get_cbt_papers(); in fact a free user reaching this endpoint
    was served every year's questions, while the picker had counted only
    the free year's. A free user whose subject has no free year at all gets
    a 200 with the section structure, every pool empty, and complete false
    — not the 404 fetch_cbt_questions() raises, because Android treats any
    non-200 here as "fall back to the flat list", which would bypass the
    structure entirely.

    Pool size per section was uncapped (all gradeable questions, shuffled)
    when this was written — required_count from rules_json was the only
    number the schema defined; there was no "how many options to show"
    field to cap against, and inventing one would have been arbitrary. See
    handover doc §7.2/design note. That field now exists, authored rather
    than invented: presented_count, how many questions the printed paper
    presents in the section. Where every section declares it, each section
    serves min(presented_count, pool) questions sampled from its pool
    (selection.mode "presented"); where none does, the section is served
    uncapped exactly as before (selection.mode "legacy_uncapped", decision
    D3). The decision itself lives in _build_theory_section_plan(), shared
    with get_cbt_papers().

    Two queries, deliberately, not one:
      1. An explicit-column query (id, section, marks, sub_questions_json,
         examiner_points_json, metadata_json), in
         _build_theory_section_plan(), drives the gradeability decision,
         section grouping and the marks check. This does NOT use
         QUESTION_SELECT_COLS — that constant drives the public-facing
         Question/TheoryQuestion JSON shape, and examiner_points_json is a
         grading secret with no equivalent field anywhere in the Android
         model, so whether QUESTION_SELECT_COLS happens to include it is
         unverified and this function must not gamble a silent, un-erroring
         "every question looks ungradeable" failure on that assumption.
         Mirrors theory_service.py's _fetch_question_data() explicit
         column list exactly, for the same reason it exists there.
      2. Once the selected id set is known, a second query using
         QUESTION_SELECT_COLS (id IN (...)) builds the actual client-facing
         rows via the existing row_to_question() — identical shape to every
         other question-serving endpoint, guaranteed compatible with
         Android's TheoryQuestion model. Only the sampled ids are fetched.
    """
    year_filter: Optional[int] = None
    no_free_year = False
    if not is_paid:
        db = db_conn()
        try:
            year_filter = get_free_year_for_subject(db, exam, subject)
        finally:
            db.close()
        no_free_year = year_filter is None

    plan = _build_theory_section_plan(
        exam, subject, paper, year_filter, no_free_year=no_free_year,
    )

    selection = {
        "mode": plan["mode"],
        "complete": plan["complete"],
        "unavailable_reason": plan["unavailable_reason"],
        "year_scope": "all_years" if is_paid else "free_year",
        "free_year": year_filter,
    }

    if not plan["rules"]:
        return {
            "exam": exam,
            "subject": subject,
            "paper": paper,
            "selection": selection,
            "sections": [],
        }

    all_selected_ids = [qid for s in plan["sections"] for qid in s["selected_ids"]]

    # No gradeable questions anywhere in this paper yet (or none in this
    # year scope) — still return the section shells with empty question
    # lists (not an error), so the UI can show "No gradeable questions
    # available in this section yet" per section rather than falling back
    # or breaking.
    questions_by_id: Dict[str, Any] = {}
    passage_lookup: Dict[str, Any] = {}
    if all_selected_ids:
        db = db_conn()
        cur = db.cursor()
        try:
            placeholders = ",".join(["?"] * len(all_selected_ids))
            cur.execute(
                f"""
                SELECT {QUESTION_SELECT_COLS}
                FROM questions
                WHERE id IN ({placeholders})
                """,
                tuple(all_selected_ids),
            )
            rows = cur.fetchall()
            passage_lookup = build_passage_lookup(db, rows)
        finally:
            db.close()
        questions_by_id = {row_get(r, "id"): r for r in rows}

    sections_out: List[Dict[str, Any]] = []
    for s in plan["sections"]:
        section_question_rows = [questions_by_id[qid] for qid in s["selected_ids"] if qid in questions_by_id]
        sections_out.append({
            **_section_header(s["rule"]),
            "presented_count": s["presented_count"],
            "available_count": s["available_count"],
            "shortfall": s["shortfall"],
            "status": s["status"],
            "questions": [row_to_question(r, passage_lookup) for r in section_question_rows],
        })

    return {
        "exam": exam,
        "subject": subject,
        "paper": paper,
        "selection": selection,
        "sections": sections_out,
    }
