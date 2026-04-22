"""
services/question_utils.py — shared question helpers for ExamPartner.

Used by routes/questions.py, routes/cbt.py, routes/admin.py.
No route logic lives here — pure data transformation and query helpers.
"""
import json
import re
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

def row_to_question(row: Any, passage_lookup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    qtype = row["qtype"]
    passage_id = row_get(row, "passage_id")

    if passage_id and passage_lookup and passage_id in passage_lookup:
        passage_snapshot = passage_lookup[passage_id]
    else:
        passage_snapshot = normalize_passage_snapshot(row_get(row, "passage_snapshot"))

    return {
        "id": row["id"],
        "exam": row_get(row, "exam"),
        "year": row_get(row, "year"),
        "subject": row_get(row, "subject"),
        "paper": row_get(row, "paper"),
        "section": row_get(row, "section"),
        "type": qtype,
        "page": row_get(row, "page"),
        "marks": row_get(row, "marks"),
        "section_instruction": row_get(row, "section_instruction"),
        "question_text": row["question_text"],
        "options": jloads(row_get(row, "options_json")),
        "answer": row_get(row, "answer"),
        "explanation": normalize_explanation(qtype, row_get(row, "explanation"), row_get(row, "subject")),
        "sub_questions": jloads(row_get(row, "sub_questions_json")),
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
) -> Tuple[str, List[Any]]:
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
