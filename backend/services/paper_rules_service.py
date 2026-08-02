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
import json
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

# ---------------------------------------------------------------------------
# rules_json validation — write-time gate
# ---------------------------------------------------------------------------
#
# rules_json is stored as an opaque TEXT blob and was previously never
# inspected on write. That is a silent-failure surface, and an unusually
# costly one here for two reasons:
#
#   1. Section labels must match questions.section VERBATIM. cbt_service's
#      fetch_cbt_theory_paper() builds its output by iterating the authored
#      section list, so a label that doesn't match any real section (a stray
#      trailing space, "A" instead of "Section A") yields a section that is
#      served empty forever, with no error anywhere. The mirror image — a
#      real section with no authored rule — is now caught at read time by the
#      orphaned-section warning in cbt_service.py, but that only logs; this
#      is the half that can be refused outright.
#
#   2. A bad row is not merely a bad response — it is cached. Android's
#      PaperRulesSyncService does a full-table replace into paper_rules_cache
#      on a 24-hour cycle, and the DAO reads durationMinutes / questionCount
#      / totalMarks straight from that cache, so a wrong value survives a
#      server-side fix for up to a day on every device already synced.
#      (Narrower than it first appears for rules_json specifically: the
#      cached rulesJson column is dead weight — theory sections are resolved
#      live by GET /cbt/theory-questions, so a rules_json fix does reach
#      clients immediately. The caching argument is real for the numeric
#      columns, not for rules_json. Recorded precisely because the sloppier
#      version of this claim was made first and was wrong.)
#
# Deliberately NOT validated here: anything requiring simulation of the
# two-stage reserve-and-pool aggregation. That is Sprint E Phase 3's job and
# cannot be done cheaply at write time. This function only checks what is
# structurally decidable from the row plus the questions table.
#
# Ordering note: this runs BEFORE any write, and opens its own connection.
# Callers that already hold one should be refactored to pass a cursor if this
# ever shows up in profiling; at manual-authoring volume it is irrelevant.

def validate_rules_json(
    rules_json: Optional[str],
    exam: str,
    subject: str,
    paper: str,
    total_marks: Optional[int] = None,
) -> None:
    """
    Raises HTTPException(400) if rules_json is malformed or inconsistent.
    Returns None on success. A null/empty rules_json is valid — plenty of
    rows carry only duration/count/marks and no section structure at all.
    """
    if rules_json is None or not str(rules_json).strip():
        return

    # --- Rule 1: parse + shape -------------------------------------------
    # _get_theory_section_rules() requires a list of objects and silently
    # returns [] for anything else, so a wrong shape here means the paper
    # falls back to flat mode with no indication that it did.
    try:
        parsed = json.loads(rules_json)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"rules_json is not valid JSON: {exc}")

    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=400,
            detail=(
                "rules_json must be a JSON list of section objects, not "
                f"{type(parsed).__name__}. An object wrapper is silently "
                "ignored by the parser and the paper falls back to flat mode."
            ),
        )
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="rules_json is an empty list. Omit the field entirely instead.",
        )
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400,
                detail=f"rules_json[{i}] is {type(entry).__name__}, expected an object.",
            )
        if not str(entry.get("section") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"rules_json[{i}] has no non-empty 'section' label.",
            )

    labels = [entry["section"] for entry in parsed]
    duplicates = sorted({lbl for lbl in labels if labels.count(lbl) > 1})
    if duplicates:
        # Not merged by the reader — the later entry wins and the earlier
        # one's marks silently vanish from the paper.
        raise HTTPException(
            status_code=400,
            detail=f"rules_json has duplicate section label(s): {duplicates}",
        )

    # --- Rule 2: labels must exist verbatim in questions.section ----------
    db = db_conn()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT DISTINCT section FROM questions WHERE exam = ? AND subject = ? AND paper = ?",
            (exam, subject, paper),
        )
        actual = {
            _row_to_dict(r)["section"]
            for r in cur.fetchall()
            if _row_to_dict(r).get("section")
        }
    finally:
        db.close()

    if actual:
        unknown = sorted(set(labels) - actual)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"rules_json section label(s) {unknown} do not match any "
                    f"section in the questions table for {exam}/{subject}/{paper}. "
                    f"Labels must match verbatim (note whitespace and casing). "
                    f"Known sections: {sorted(actual)}"
                ),
            )
    # If `actual` is empty the paper has no questions ingested yet. Authoring
    # the rule first is a legitimate order of operations, so this is not an
    # error — the read-time orphan warning covers the case where questions
    # arrive later under a label the rule doesn't list.

    # --- Rule 3: the dead-section combination ----------------------------
    # in_pool false-or-absent with required_count 0 caps the section at zero
    # AND excludes it from the shared pool, so it can never contribute to a
    # score under any pattern — while still rendering, still being tappable,
    # and still spending AI-grading credits.
    #
    # SCOPING — do not simplify this condition. A genuine Pattern C section
    # is in_pool TRUE with required_count 0, which is exactly what Chemistry,
    # both Mathematics papers, NECO Biology and both Commerce papers need.
    # Rejecting on required_count == 0 alone would break every one of them.
    for i, entry in enumerate(parsed):
        if not entry.get("in_pool", False) and int(entry.get("required_count", 0) or 0) == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"rules_json[{i}] ('{entry['section']}') has required_count 0 "
                    "with in_pool false or absent, so it can never contribute to a "
                    "score. Set in_pool true for a Pattern C pooled section, or give "
                    "it a non-zero required_count."
                ),
            )

    # --- Rule 4: section marks must reconcile with the row ----------------
    # This is the failure that opened this whole workstream: a hand-authored
    # row whose total_marks did not equal the sum of its sections. It shipped
    # once. With this check it cannot recur across the subjects still to be
    # authored.
    marked = [e for e in parsed if "total_marks" in e]
    if marked and len(marked) != len(parsed):
        # Partial marks authoring silently disabled this check before, which
        # is exactly how the failure it guards against slipped through once.
        missing = sorted(e["section"] for e in parsed if "total_marks" not in e)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Some sections declare total_marks and others do not (missing: "
                f"{missing}). Declare it on every section or none — a partial set "
                "silently skips the marks-reconciliation check."
            ),
        )

    if total_marks is not None and marked:
        section_sum = sum(int(e.get("total_marks") or 0) for e in parsed)
        if section_sum != int(total_marks):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Section total_marks sum to {section_sum} but the row's "
                    f"total_marks is {total_marks}. One of the two is wrong."
                ),
            )


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
    allow_clearing: bool = False,
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

    # Structural gate on rules_json. Lives here rather than in the route so
    # that every caller is covered — notably the bulk-import endpoint the
    # route docstring anticipates, which would bypass a route-level check
    # while being the one path authoring rows in volume.
    validate_rules_json(
        rules_json=rules_json,
        exam=exam,
        subject=subject,
        paper=paper,
        total_marks=total_marks,
    )

    db = db_conn()
    try:
        cur = db.cursor()

        if year is not None:
            cur.execute(
                "SELECT id, duration_minutes, question_count, total_marks, rules_json "
                "FROM paper_rules WHERE exam = ? AND subject = ? AND paper = ? AND year = ?",
                (exam, subject, paper, year),
            )
        else:
            cur.execute(
                "SELECT id, duration_minutes, question_count, total_marks, rules_json "
                "FROM paper_rules WHERE exam = ? AND subject = ? AND paper = ? AND year IS NULL AND rule_source = ?",
                (exam, subject, paper, rule_source),
            )
        existing = cur.fetchone()

        now = _now_iso()

        if existing:
            existing_row = _row_to_dict(existing)
            existing_id = existing_row["id"]

            # Partial-update guard.
            #
            # The UPDATE below sets every column unconditionally, so any field
            # the caller omitted arrives as None and overwrites whatever was
            # there. That is a real data-loss path, and the validator above
            # makes it MORE likely to be hit, not less: the natural way to fix
            # a row the validator rejected is to re-POST it, and a re-POST
            # carrying only the corrected field silently nulls the rest.
            #
            # Rather than making the UPDATE skip None values — which would
            # remove any way to deliberately clear a field — reject the write
            # and say so. Deliberate clearing stays possible via
            # allow_clearing=True.
            if not allow_clearing:
                _incoming = {
                    "duration_minutes": duration_minutes,
                    "question_count":   question_count,
                    "total_marks":      total_marks,
                    "rules_json":       rules_json,
                }
                _would_clear = sorted(
                    field for field, value in _incoming.items()
                    if value is None and existing_row.get(field) is not None
                )
                if _would_clear:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"This update would clear {_would_clear} on an existing row "
                            "because those fields were omitted. Re-send the complete row "
                            "including their current values, or pass allow_clearing=true "
                            "if you really intend to null them."
                        ),
                    )

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
