#!/usr/bin/env python3
"""
tools/check_paper_rules_sprint_b.py — run the Sprint B validator over every
live paper_rules row. Read-only: it never writes to paper_rules or questions.

    python tools/check_paper_rules_sprint_b.py
    python tools/check_paper_rules_sprint_b.py --exam NECO

Run it from the backend folder, against the database the deployed code will
use, BEFORE pushing the Sprint B paper_rules_service.py. It calls the new
validate_rules_json() on each row exactly as upsert_paper_rule() would, with
the row's own rules_json and total_marks, and reports which rows the new
rules would refuse.

Why this matters: the validator only runs on write. A live row it would now
reject keeps serving unchanged, but the next time anyone re-authors it the
write fails. Better to learn that now than mid-authoring.

Exit code 0 when every row passes, 1 when any row fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description="Check live paper_rules rows against the Sprint B validator")
    ap.add_argument("--exam", help="only rows for this exam (e.g. NECO)")
    args = ap.parse_args()

    from fastapi import HTTPException

    from config import db_conn
    from services.paper_rules_service import validate_rules_json
    from services.question_utils import row_get

    db = db_conn()
    try:
        cur = db.cursor()
        # No parameters, the way check_cbt_structure.py reads the table, so
        # the query runs unchanged on SQLite and Postgres.
        cur.execute(
            "SELECT exam, subject, paper, year, country, rule_source, total_marks, rules_json "
            "FROM paper_rules ORDER BY exam, subject, paper"
        )
        rows = cur.fetchall() or []
    finally:
        db.close()

    if args.exam:
        rows = [r for r in rows if row_get(r, "exam") == args.exam]

    passed = failed = skipped = 0
    for r in rows:
        name = f"{row_get(r, 'exam')}/{row_get(r, 'subject')}/{row_get(r, 'paper')}"
        extra = []
        if row_get(r, "year") is not None:
            extra.append(f"year {row_get(r, 'year')}")
        if row_get(r, "country") is not None:
            extra.append(f"country {row_get(r, 'country')}")
        extra.append(row_get(r, "rule_source") or "no rule_source")
        label = f"{name} ({', '.join(extra)})"

        raw = row_get(r, "rules_json")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            print(f"  skip  {label}: no rules_json")
            skipped += 1
            continue
        # A json/jsonb column comes back already parsed; the validator takes text.
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)

        total = row_get(r, "total_marks")
        try:
            validate_rules_json(
                rules_json=raw,
                exam=row_get(r, "exam"),
                subject=row_get(r, "subject"),
                paper=row_get(r, "paper"),
                total_marks=float(total) if total is not None else None,
            )
        except HTTPException as exc:
            print(f"  FAIL  {label}\n        {exc.detail}")
            failed += 1
            continue
        print(f"  ok    {label}")
        passed += 1

    print()
    print(f"{len(rows)} row(s): {passed} ok, {failed} fail, {skipped} skipped (no rules_json)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
