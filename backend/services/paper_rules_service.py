"""
services/paper_rules_service.py — paper_rules business logic for ExamPartner.

Single source of truth for per-paper CBT timing, question counts, and total
marks (Sprint 3). Replaces the hardcoded CBT_PAPER_DURATION_MINUTES /
get_cbt_cap() in cbt_service.py as the PRIMARY source — those functions
remain in cbt_service.py as the final fallback layer when paper_rules has
no row at all for a given (exam, subject, paper), so this migration never
requires every row to be populated before it's useful.

No access-tier gating on the read paths (unlike /cbt/questions, /cbt/papers):
paper_rules is structural exam metadata, not paid content, and the same
duration/count/marks apply to free and paid users alike. The write path
(upsert_paper_rule) is admin-only, enforced at the route layer via
require_admin(), the same pattern routes/admin.py already uses.
"""
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from config import db_conn
from services.cbt_service import get_paper_duration_minutes, get_cbt_cap

RULE_SOURCE_ACTUAL   = "actual_paper"
RULE_SOURCE_SYLLABUS = "syllabus_default"
RULE_SOURCE_LEGACY   = "legacy_placeholder"

# Preference order when more than one year-NULL row exists for the same
# (exam, subject, paper) — real evidence beats syllabus policy beats guess.
_RULE_SOURCE_PREFERENCE = {
    RULE_SOURCE_ACTUAL:   0,
    RULE_SOURCE_SYLLABUS: 1,
    RULE_SOURCE_LEGACY:   2,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> Dict[str, Any]:
    """
    Normalizes a DB row (sqlite3.Row or psycopg2 RealDictRow) into a plain
    dict. Mirrors the row_get()/hasattr(row, "get") pattern already used
    throughout access_control.py and theory_service.py in this codebase.
    """
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


# ---------------------------------------------------------------------------
# Resolution — the actual lookup used by CBT runtime callers
# ---------------------------------------------------------------------------

def resolve_paper_rule(
    exam: str,
    subject: str,
    paper: str,
    year: Optional[int] = None,
    qtype: str = "objective",
) -> Dict[str, Any]:
    """
    Resolves the effective duration/count/marks for one paper, using the
    three-tier order described in db.py's paper_rules comment:

      1. exact match (exam, subject, paper, year)
      2. best year-NULL match for (exam, subject, paper), preferring
         actual_paper > syllabus_default > legacy_placeholder
      3. hardcoded fallback in cbt_service.py (get_paper_duration_minutes,
         get_cbt_cap) — used only when paper_rules has no row at all for
         this (exam, subject, paper) combination, in either form above.

    Returns a dict shaped like a paper_rules row, with an extra
    "resolved_from" field so callers/clients can tell which tier actually
    answered ("exact_year" | "year_null" | "hardcoded_fallback").
    """
    db = db_conn()
    try:
        cur = db.cursor()

        if year is not None:
            cur.execute(
                """
                SELECT * FROM paper_rules
                WHERE exam = ? AND subject = ? AND paper = ? AND year = ?
                """,
                (exam, subject, paper, year),
            )
            exact = cur.fetchone()
            if exact:
                result = _row_to_dict(exact)
                result["resolved_from"] = "exact_year"
                return result

        cur.execute(
            """
            SELECT * FROM paper_rules
            WHERE exam = ? AND subject = ? AND paper = ? AND year IS NULL
            """,
            (exam, subject, paper),
        )
        year_null_rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        db.close()

    if year_null_rows:
        year_null_rows.sort(
            key=lambda r: _RULE_SOURCE_PREFERENCE.get(r.get("rule_source"), 99)
        )
        best = year_null_rows[0]
        best["resolved_from"] = "year_null"
        return best

    # Tier 3 — no paper_rules row exists at all for this (exam, subject, paper).
    # Fall back to the existing hardcoded logic in cbt_service.py, unchanged.
    return {
        "id":               None,
        "exam":             exam,
        "subject":          subject,
        "paper":            paper,
        "year":             None,
        "duration_minutes": get_paper_duration_minutes(paper, qtype),
        "question_count":   get_cbt_cap(subject=subject, exam=exam, paper=paper),
        "total_marks":      None,
        "rule_source":      RULE_SOURCE_LEGACY,
        "rules_json":       None,
        "resolved_from":    "hardcoded_fallback",
    }


# ---------------------------------------------------------------------------
# Listing — drives GET /paper-rules
# ---------------------------------------------------------------------------

def list_paper_rules(
    exam: Optional[str] = None,
    subject: Optional[str] = None,
    paper: Optional[str] = None,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Returns paper_rules rows matching the given filters (all optional).
    Raw listing — no resolution/fallback logic — used by Android's
    PaperRulesSyncService to pull the full table (or a filtered slice) for
    local caching. For runtime single-paper lookups with fallback, use
    resolve_paper_rule() instead.
    """
    where: List[str] = []
    params: List[Any] = []

    if exam:
        where.append("exam = ?")
        params.append(exam)
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if paper:
        where.append("paper = ?")
        params.append(paper)
    if year is not None:
        where.append("year = ?")
        params.append(year)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            f"SELECT * FROM paper_rules {where_sql} ORDER BY exam, subject, paper, year",
            tuple(params) if params else None,
        )
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        db.close()

    return {"ok": True, "rules": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Admin write path — for populating rows from audit data
# ---------------------------------------------------------------------------

def upsert_paper_rule(
    exam: str,
    subject: str,
    paper: str,
    rule_source: str,
    year: Optional[int] = None,
    duration_minutes: Optional[int] = None,
    question_count: Optional[int] = None,
    total_marks: Optional[int] = None,
    rules_json: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Inserts or updates a paper_rules row, keyed by (exam, subject, paper, year)
    per the unique index in db.py. Admin-only — call sites must enforce this
    via require_admin() at the route layer, the same pattern used elsewhere
    in this codebase (see routes/admin.py).

    rule_source must be one of RULE_SOURCE_ACTUAL, RULE_SOURCE_SYLLABUS,
    RULE_SOURCE_LEGACY. legacy_placeholder rows should generally not be
    written via this path — they exist conceptually as "not yet audited,"
    and the cleaner approach is simply leaving no row at all, letting
    resolve_paper_rule() fall through to tier 3. This function still
    accepts the value for completeness (e.g. explicitly migrating an old
    hardcoded constant into the table, if ever desired).

    For year=None upserts (a syllabus_default or legacy_placeholder row),
    uniqueness is checked by (exam, subject, paper, year IS NULL,
    rule_source) rather than just (exam, subject, paper, year) — this lets
    a syllabus_default row and a legacy_placeholder row coexist as two
    distinct year-NULL rows for the same paper (matching the db.py comment
    on the unique index, which notes NULL != NULL for uniqueness purposes),
    while still updating-in-place rather than duplicating if the exact same
    (exam, subject, paper, rule_source) combination is upserted again.
    """
    valid_sources = {RULE_SOURCE_ACTUAL, RULE_SOURCE_SYLLABUS, RULE_SOURCE_LEGACY}
    if rule_source not in valid_sources:
        raise HTTPException(
            status_code=400,
            detail=f"rule_source must be one of {sorted(valid_sources)}",
        )
    if not exam or not subject or not paper:
        raise HTTPException(status_code=400, detail="exam, subject, and paper are required.")

    db = db_conn()
    try:
        cur = db.cursor()

        if year is not None:
            cur.execute(
                "SELECT id FROM paper_rules WHERE exam = ? AND subject = ? AND paper = ? AND year = ?",
                (exam, subject, paper, year),
            )
        else:
            cur.execute(
                "SELECT id FROM paper_rules WHERE exam = ? AND subject = ? AND paper = ? AND year IS NULL AND rule_source = ?",
                (exam, subject, paper, rule_source),
            )
        existing = cur.fetchone()

        now = _now_iso()

        if existing:
            existing_id = _row_to_dict(existing)["id"]
            cur.execute(
                """
                UPDATE paper_rules
                SET duration_minutes = ?, question_count = ?, total_marks = ?,
                    rule_source = ?, rules_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (duration_minutes, question_count, total_marks, rule_source, rules_json, now, existing_id),
            )
            row_id = existing_id
        else:
            row_id = secrets.token_hex(16)
            cur.execute(
                """
                INSERT INTO paper_rules
                  (id, exam, subject, paper, year, duration_minutes, question_count,
                   total_marks, rule_source, rules_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row_id, exam, subject, paper, year, duration_minutes, question_count,
                 total_marks, rule_source, rules_json, now, now),
            )

        db.commit()
    finally:
        db.close()

    return {"ok": True, "id": row_id}
