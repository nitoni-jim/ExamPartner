"""
services/question_utils.py — shared question helpers for ExamPartner.

Used by routes/questions.py, routes/cbt.py, routes/admin.py.
No route logic lives here — pure data transformation and query helpers.
"""
import json
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Generic row helpers
# ---------------------------------------------------------------------------

def row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def jloads(x: Optional[str]) -> Any:
    try:
        return json.loads(x) if x else None
    except Exception:
        return None


def normalize_marks(value: Any) -> Any:
    """
    Whole marks as int, half marks as float, anything else unchanged.

    questions.marks and paper_rules.total_marks are DOUBLE PRECISION so a half
    mark is stored exactly (NECO 2021 Financial Accounting Q1-Q4 are worth
    12.5). That column returns 15.0 for a whole mark, though, and every reader
    printed it as-is: the app would show "15.0 marks" and the grading prompt
    "Total marks available: 15.0". Normalising at the read boundary keeps
    whole marks exactly as they looked when the column was INTEGER, and lets
    only genuine half marks carry a decimal point.

    Decimal is converted as well, because json.dumps cannot encode it.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# ---------------------------------------------------------------------------
# Explanation normalisation (v12.2)
# ---------------------------------------------------------------------------

_THEORY_STRING_EXPLANATION_SUBJECTS = frozenset({
    "English Language",
    "Literature-in-English",
    "Oral English",
})


def normalize_explanation(
    qtype: Optional[str],
    raw_explanation: Optional[str],
    subject: Optional[str] = None,
) -> Any:
    """
    v12.2: Theory explanations are arrays (one item = one marking point),
    EXCEPT for English Language, Literature-in-English, and Oral English
    which retain string format. Objective explanations remain arrays.
    """
    if qtype == "objective":
        return jloads(raw_explanation) if raw_explanation else []
    if qtype == "theory":
        if (subject or "").strip() in _THEORY_STRING_EXPLANATION_SUBJECTS:
            return raw_explanation or ""
        return jloads(raw_explanation) if raw_explanation else []
    return raw_explanation or ""


def normalize_passage_snapshot(raw: Optional[str]) -> Any:
    parsed = jloads(raw)
    return parsed if parsed is not None else (raw or None)


# ---------------------------------------------------------------------------
# Passage lookup (batch — no N+1)
# ---------------------------------------------------------------------------

def build_passage_lookup(db, rows) -> Dict[str, Any]:
    """
    Fetch passage rows for all unique passage_ids in a batch of question rows.
    Returns a dict keyed by passage_id.
    """
    passage_ids = list({
        row_get(r, "passage_id")
        for r in rows
        if row_get(r, "passage_id")
    })
    if not passage_ids:
        return {}

    placeholders = ",".join("?" * len(passage_ids))
    try:
        cur = db.cursor()
        cur.execute(
            f"""
            SELECT id, title, passage_type, passage_text, section, metadata_json
            FROM passages
            WHERE id IN ({placeholders})
            """,
            tuple(passage_ids),
        )
        passage_rows = cur.fetchall()
    except Exception:
        return {}

    lookup: Dict[str, Any] = {}
    for pr in passage_rows:
        pid = row_get(pr, "id")
        if not pid:
            continue
        meta = jloads(row_get(pr, "metadata_json")) or {}
        lookup[pid] = {
            "title": row_get(pr, "title") or "",
            "passage_type": row_get(pr, "passage_type") or "",
            "passage_text": row_get(pr, "passage_text") or "",
            "section": row_get(pr, "section") or "",
            "question_range": meta.get("question_range", ""),
            "instruction": meta.get("instruction", ""),
        }
    return lookup


# ---------------------------------------------------------------------------
# Row → question dict
# ---------------------------------------------------------------------------

# Grading-only keys. They sit inside sub_questions_json (spec v16.4 Rule 16a:
# a multi-part record carries its rubric on the parts), so they would travel
# to every client with the rest of the sub-question unless removed here.
# Grading never reads them from this serializer: theory_service and
# cbt_service._is_gradeable_theory_row() read the columns directly.
# No Android code reads either key; the app's sub-question parser takes only
# label, marks, question_text, answer, explanation and expects_diagram.
_RUBRIC_KEYS = frozenset({"examiner_points", "rubric_groups"})


def strip_rubric(value: Any) -> Any:
    """
    Returns value with every examiner_points / rubric_groups key removed, at
    any depth, so a nested part (a sub-question inside a sub-question) is
    covered too. Lists and dicts are copied; anything else is returned as-is.
    """
    if isinstance(value, dict):
        return {k: strip_rubric(v) for k, v in value.items() if k not in _RUBRIC_KEYS}
    if isinstance(value, list):
        return [strip_rubric(v) for v in value]
    return value


def row_to_question(
    row: Any,
    passage_lookup: Optional[Dict[str, Any]] = None,
    include_rubric: bool = False,
) -> Dict[str, Any]:
    """
    include_rubric: False for every client-facing route (the default), so the
    marking rubric is never served to candidates. Pass True only from an
    admin-only route that needs to show it.
    """
    qtype = row["qtype"]
    passage_id = row_get(row, "passage_id")

    if passage_id and passage_lookup and passage_id in passage_lookup:
        passage_snapshot = passage_lookup[passage_id]
    else:
        passage_snapshot = normalize_passage_snapshot(row_get(row, "passage_snapshot"))

    sub_questions = jloads(row_get(row, "sub_questions_json"))
    if not include_rubric:
        sub_questions = strip_rubric(sub_questions)

    return {
        "id": row["id"],
        "exam": row_get(row, "exam"),
        "year": row_get(row, "year"),
        "subject": row_get(row, "subject"),
        "paper": row_get(row, "paper"),
        "section": row_get(row, "section"),
        "type": qtype,
        "page": row_get(row, "page"),
        "marks": normalize_marks(row_get(row, "marks")),
        "section_instruction": row_get(row, "section_instruction"),
        "question_text": row["question_text"],
        "options": jloads(row_get(row, "options_json")),
        "answer": jloads(row_get(row, "answer")) if isinstance(row_get(row, "answer"), str) and row_get(row, "answer").startswith("[") else row_get(row, "answer"),
        "explanation": normalize_explanation(qtype, row_get(row, "explanation"), row_get(row, "subject")),
        "sub_questions": sub_questions,
        "solution_steps": jloads(row_get(row, "solution_steps_json")),
        "diagrams": jloads(row_get(row, "diagrams_json")) or [],
        "answer_diagrams": jloads(row_get(row, "answer_diagrams_json")) or [],
        "explanation_diagrams": jloads(row_get(row, "explanation_diagrams_json")) or [],
        "tables": jloads(row_get(row, "tables_json")) or {},
        "passage_id": passage_id,
        "passage_snapshot": passage_snapshot,
        "topic": row_get(row, "topic"),
        "subtopic": row_get(row, "subtopic"),
    }


# ---------------------------------------------------------------------------
# Filter builder
# ---------------------------------------------------------------------------

def build_filters(
    qtype: str,
    exam: Optional[str],
    year: Optional[int],
    subject: Optional[str],
    topic: Optional[str] = None,
    subtopic: Optional[str] = None,
    paper: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Builds a WHERE clause + params for question queries.

    paper: optional discriminator within a subject (e.g. "Oral English" under
    "English Language"). When omitted, no filtering by paper occurs — existing
    callers and existing subjects are unaffected.
    """
    where = ["qtype = ?"]
    params: List[Any] = [qtype]

    if exam:
        where.append("exam = ?")
        params.append(exam)
    if year is not None:
        where.append("year = ?")
        params.append(year)
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if topic:
        where.append("topic = ?")
        params.append(topic)
    if subtopic:
        where.append("subtopic = ?")
        params.append(subtopic)
    if paper:
        where.append("paper = ?")
        params.append(paper)

    return " AND ".join(where), params


# ---------------------------------------------------------------------------
# Theory sort (v12.2 — numeric Q-number, not lexicographic)
# ---------------------------------------------------------------------------

def extract_theory_q_number(question_id: str) -> int:
    m = re.search(r"_Q(\d+)$", str(question_id or ""))
    return int(m.group(1)) if m else 10 ** 9


def sort_theory_rows(rows) -> list:
    return sorted(rows, key=lambda r: extract_theory_q_number(row_get(r, "id") or ""))


# ---------------------------------------------------------------------------
# Shared SELECT column list (keeps all route queries consistent)
# ---------------------------------------------------------------------------

QUESTION_SELECT_COLS = """
    id, exam, year, subject, paper, section, qtype, page, marks, question_text,
    options_json, answer, explanation, sub_questions_json,
    solution_steps_json, diagrams_json, answer_diagrams_json, explanation_diagrams_json,
    tables_json, section_instruction, passage_id, passage_snapshot,
    topic, subtopic
""".strip()
