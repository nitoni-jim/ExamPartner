#!/usr/bin/env python3
"""Quick DB smoke check for questions table."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import get_db, init_db  # noqa: E402


def main() -> int:
    init_db()
    db = get_db()
    try:
        cur = db.cursor()

        cur.execute("SELECT COUNT(*) AS total FROM questions")
        total_row = cur.fetchone()
        total = total_row[0] if not isinstance(total_row, dict) else total_row["total"]

        cur.execute(
            """
            SELECT
                COUNT(DISTINCT exam) AS exams,
                COUNT(DISTINCT year) AS years,
                COUNT(DISTINCT subject) AS subjects
            FROM questions
            """
        )
        meta_row = cur.fetchone()
        if isinstance(meta_row, dict):
            exams = meta_row["exams"]
            years = meta_row["years"]
            subjects = meta_row["subjects"]
        else:
            exams, years, subjects = meta_row

        cur.execute("SELECT id FROM questions ORDER BY id LIMIT 3")
        id_rows = cur.fetchall()
        first_ids = [row["id"] if isinstance(row, dict) else row[0] for row in id_rows]

        print(f"total_questions={total}")
        print(f"distinct_exams={exams}, distinct_years={years}, distinct_subjects={subjects}")
        print(f"first_3_ids={first_ids}")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
